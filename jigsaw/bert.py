"""
Initial version based on
https://www.kaggle.com/yuval6967/toxic-bert-plain-vanila/
"""
import argparse
from collections import defaultdict
import json
from functools import partial
import shutil
from pathlib import Path
import os

try:
    from apex import amp
except ImportError:
    amp = None
try:
    import json_log_plots
except ImportError:
    pass
import numpy as np
import pandas as pd
from pytorch_pretrained_bert import (
    BertTokenizer, BertForSequenceClassification, BertAdam,
    GPT2Tokenizer, OpenAIAdam, GPT2Model, WEIGHTS_NAME, CONFIG_NAME)
import torch
from torch import nn
from torch.nn import functional as F
from torch import multiprocessing
from torch.utils.data import TensorDataset, DataLoader
from torch.utils.data.sampler import BatchSampler, RandomSampler
import tqdm

if 'KAGGLE_WORKING_DIR' in os.environ:
    ON_KAGGLE = True
    DATA_ROOT = Path(
        '../input/jigsaw-unintended-bias-in-toxicity-classification')
else:
    from .metrics import compute_bias_metrics_for_model, IDENTITY_COLUMNS
    from .utils import DATA_ROOT, ON_KAGGLE


device = torch.device('cuda')
GPT2_PAD = '<pad>'


def main():
    parser = argparse.ArgumentParser()
    arg = parser.add_argument
    arg('run_root')
    arg('--train-size', type=int)
    arg('--valid-size', type=int)
    arg('--test-size', type=int)
    arg('--model', default='bert-base-uncased')
    arg('--train-seq-length', type=int, default=224)
    arg('--test-seq-length', type=int, default=296)
    arg('--epochs', type=int, default=2)
    arg('--validation', action='store_true')
    arg('--submission', action='store_true')
    arg('--lr', type=float, default=2e-5)
    arg('--batch-size', type=int, default=32)
    arg('--accumulation-steps', type=int, default=2)
    arg('--checkpoint-interval', type=int)
    arg('--clean', action='store_true')
    arg('--fold', type=int, default=0)
    arg('--bucket', type=int, default=1)
    arg('--load-weights', help='load weights for training')
    arg('--export', help='export everything for inference')
    args = parser.parse_args()

    run_root = Path(args.run_root)
    do_train = not (args.submission or args.validation or args.export)
    if do_train:
        if args.clean and run_root.exists():
            if input(f'Clean "{run_root.absolute()}"? ') == 'y':
                shutil.rmtree(run_root)
        if run_root.exists():
            parser.error(f'{run_root} exists')
        run_root.mkdir(exist_ok=True, parents=True)
        params_str = json.dumps(vars(args), indent=4)
        print(params_str)
        (run_root / 'params.json').write_text(params_str)
        shutil.copy(__file__, run_root)
    else:
        run_root.mkdir(exist_ok=True, parents=True)

    use_bert = 'bert' in args.model
    use_gpt2 = 'gpt2' in args.model
    if args.export:
        if ((use_bert and 'bert' not in args.export) or
                (use_gpt2 and 'gpt2' not in args.export)):
            parser.error("Can't determine model kind from the --export option")

    print('Loading tokenizer...')
    if use_bert:
        tokenizer = BertTokenizer.from_pretrained(
            args.model, do_lower_case='uncased' in args.model)
        pad_idx = 0
    elif use_gpt2:
        tokenizer = GPT2Tokenizer.from_pretrained(args.model)
        tokenizer.set_special_tokens([GPT2_PAD])
        pad_idx, = tokenizer.convert_tokens_to_ids([GPT2_PAD])
    else:
        raise ValueError(f'Unexpected model {args.model}')

    print('Loading model...')
    model_is_path = Path(args.model).exists()
    num_labels = 7
    if use_bert:
        model = BertForSequenceClassification.from_pretrained(
            args.model, num_labels=num_labels)
    else:
        model = GPT2ClassificationHeadModel(args.model, num_labels=num_labels)
        model.transformer.set_num_special_tokens(1)
        if model_is_path:
            # to also load linear layer weights
            model.load_state_dict(
                torch.load(Path(args.model) / 'pytorch_model.bin'))

    model_path = run_root / 'model.pt'
    optimizer_path = run_root / 'optimizer.pt'
    best_model_path = run_root / 'model-best.pt'
    valid_predictions_path = run_root / 'valid-predictions.csv'

    if args.export:
        model.load_state_dict(torch.load(best_model_path))
        export_path = Path(args.export)
        export_path.mkdir(exist_ok=True, parents=True)
        torch.save(model.state_dict(), export_path / WEIGHTS_NAME)
        model.config.to_json_file(export_path / CONFIG_NAME)
        tokenizer.save_vocabulary(export_path)
        return

    model = model.to(device)

    if args.submission:
        if not model_is_path:
            model.load_state_dict(torch.load(best_model_path))
        if amp is not None:
            model = amp.initialize(model, opt_level='O1', verbosity=0)
        make_submission(model=model, tokenizer=tokenizer,
                        run_root=run_root, max_seq_length=args.test_seq_length,
                        batch_size=args.batch_size,
                        pad_idx=pad_idx,
                        use_bert=use_bert,
                        bucket=args.bucket,
                        test_size=args.test_size)
        return

    train_pkl_path = DATA_ROOT / 'train.pkl'
    if not train_pkl_path.exists():
        pd.read_csv(DATA_ROOT / 'train.csv').to_pickle(train_pkl_path)
    df = pd.read_pickle(train_pkl_path)
    df = preprocess_df(df)

    folds = json.loads((DATA_ROOT / 'folds.json').read_text())
    valid_index = df['id'].isin(folds[args.fold])
    df_train, df_valid = df[~valid_index], df[valid_index]
    if args.train_size and len(df_train) > args.train_size:
        df_train = df_train.sample(n=args.train_size, random_state=42)
    if args.valid_size and len(df_valid) > args.valid_size:
        df_valid = df_valid.sample(n=args.valid_size, random_state=42)

    x_valid = tokenize_lines(
        df_valid.pop('comment_text'), args.test_seq_length, tokenizer,
        use_bert=use_bert, pad_idx=pad_idx)
    if args.bucket:
        indices, x_valid = sorted_by_length(x_valid, pad_idx)
        # TODO recover original order before saving
        df_valid = df_valid.iloc[indices]
    y_valid, _ = get_target(df_valid)
    y_train, loss_weight = get_target(df_train)
    print(f'X_valid.shape={x_valid.shape} y_valid.shape={y_valid.shape}')

    criterion = partial(get_loss, loss_weight=loss_weight)

    def _run_validation():
        return validation(
            model=model, criterion=criterion,
            x_valid=x_valid, y_valid=y_valid, df_valid=df_valid,
            batch_size=args.batch_size,
            pad_idx=pad_idx, bucket=args.bucket)

    if args.validation:
        if not model_is_path:
            model.load_state_dict(torch.load(best_model_path))
        if amp is not None:
            model = amp.initialize(model, opt_level='O1', verbosity=0)
        metrics, valid_predictions = _run_validation()
        for k, v in metrics.items():
            if isinstance(v, float):
                print(f'{v:.4f}  {k}')
        valid_predictions.to_csv(valid_predictions_path, index=None)
        print(f'Saved validation predictions to {valid_predictions_path}')
        return

    def _save(step, model, optimizer):
        torch.save(model.state_dict(), model_path)
        torch.save({'optimizer': optimizer.state_dict(), 'step': step},
                   optimizer_path)

    if args.load_weights:
        print(f'Loading weights from {args.load_weights}')
        load_info = model.load_state_dict(
            torch.load(args.load_weights), strict=False)
        if load_info:
            print(load_info)

    x_train = tokenize_lines(
        df_train.pop('comment_text'), args.train_seq_length, tokenizer,
        use_bert=use_bert, pad_idx=pad_idx)
    print(f'X_train.shape={x_train.shape} y_train.shape={y_train.shape}')

    best_auc = 0
    step = optimizer = None
    try:
        for model, optimizer, epoch_pbar, loss, step in train(
                model=model, criterion=criterion,
                x_train=x_train, y_train=y_train, epochs=args.epochs,
                yield_steps=args.checkpoint_interval or len(y_valid) // 8,
                bucket=args.bucket,
                lr=args.lr,
                batch_size=args.batch_size,
                accumulation_steps=args.accumulation_steps,
                pad_idx=pad_idx,
                ):
            if step == 0:
                continue  # step 0 allows saving on Ctrl+C from the start
            _save(step, model, optimizer)
            metrics, valid_predictions = _run_validation()
            metrics['loss'] = loss
            if metrics['auc'] > best_auc:
                best_auc = metrics['auc']
                shutil.copy(model_path, best_model_path)
                valid_predictions.to_csv(valid_predictions_path, index=None)
            epoch_pbar.set_postfix(valid_loss=f'{metrics["valid_loss"]:.4f}',
                                   auc=f'{metrics["auc"]:.4f}')
            json_log_plots.write_event(run_root, step=step, **metrics)
    except KeyboardInterrupt:
        if step is not None and optimizer is not None:
            print('Ctrl+C pressed, saving checkpoint')
            _save(step, model, optimizer)
        raise


def get_loss(pred, targets, loss_weight):
    bce_loss_1 = F.binary_cross_entropy_with_logits(
        pred[:, :1], targets[:, :1], weight=targets[:, 1:2])
    bce_loss_2 = F.binary_cross_entropy_with_logits(pred[:, 1:], targets[:, 2:])
    return (bce_loss_1 * loss_weight) + bce_loss_2


def validation(*, model, criterion, x_valid, y_valid, df_valid,
               batch_size: int, bucket: bool, pad_idx: int):
    valid_dataset = TensorDataset(
        torch.tensor(x_valid, dtype=torch.long),
        torch.tensor(y_valid, dtype=torch.float),
    )
    valid_loader = DataLoader(
        valid_dataset, batch_size=batch_size, shuffle=False)

    valid_preds, losses = [], []
    model.eval()
    for i, (x_batch, y_batch) in enumerate(
            tqdm.tqdm(valid_loader, desc='validation', leave=False,
                      disable=ON_KAGGLE)):
        if bucket:
            x_batch, y_batch = trim_tensors([x_batch, y_batch], pad_idx)
        x_batch = x_batch.to(device)
        y_batch = y_batch.to(device)
        with torch.no_grad():
            y_pred = model(x_batch, attention_mask=x_batch > 0, labels=None)
            loss = criterion(y_pred, y_batch)
        losses.append(float(loss.item()))
        valid_preds.extend(y_pred[:, 0].cpu().squeeze().numpy())
    model.train()

    df_valid = df_valid.copy()
    df_valid['prediction'] = torch.sigmoid(torch.tensor(valid_preds)).numpy()

    metrics = compute_bias_metrics_for_model(df_valid, 'prediction')
    metrics['valid_loss'] = np.mean(losses)
    return metrics, df_valid


def train(
        *, model, criterion, x_train, y_train, epochs, yield_steps, bucket, lr,
        batch_size: int, accumulation_steps: int, pad_idx: int,
        ):
    train_dataset = TensorDataset(
        torch.tensor(x_train, dtype=torch.long),
        torch.tensor(y_train, dtype=torch.float))

    model.zero_grad()
    model = model.to(device)
    param_optimizer = list(model.named_parameters())

    num_train_optimization_steps = int(
        epochs * len(train_dataset) / (batch_size * accumulation_steps))
    if isinstance(model, BertForSequenceClassification):
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer
                        if not any(nd in n for nd in no_decay)],
             'weight_decay': 0.01},
            {'params': [p for n, p in param_optimizer
                        if any(nd in n for nd in no_decay)],
             'weight_decay': 0.0},
        ]
        optimizer = BertAdam(
            optimizer_grouped_parameters,
            lr=lr,
            warmup=0.05,
            t_total=num_train_optimization_steps)
    elif isinstance(model, GPT2ClassificationHeadModel):
        optimizer = OpenAIAdam(
            [p for _, p in param_optimizer],
            lr=lr,
            warmup=0.1,
            t_total=num_train_optimization_steps)
    else:
        raise ValueError

    model, optimizer = amp.initialize(
        model, optimizer, opt_level='O1', verbosity=0)
    model.train()

    if bucket:
        sampler = RandomSampler(train_dataset)
        batch_sampler = BucketBatchSampler(
            sampler, batch_size, drop_last=False, pad_idx=pad_idx)
        train_loader = DataLoader(train_dataset, batch_sampler=batch_sampler)
    else:
        train_loader = DataLoader(
            train_dataset, batch_size=batch_size, shuffle=True)

    smoothed_loss = None
    step = 0
    epoch_pbar = tqdm.trange(epochs)

    def _state():
        return model, optimizer, epoch_pbar, smoothed_loss, step * batch_size

    print(f'Starting training for '
          f'{num_train_optimization_steps * accumulation_steps:,} steps, '
          f'checkpoint interval {yield_steps:,}')

    yield _state()

    torch.cuda.empty_cache()
    for _ in epoch_pbar:
        optimizer.zero_grad()
        pbar = tqdm.tqdm(train_loader, leave=False)
        for x_batch, y_batch in pbar:
            step += 1
            if bucket:
                x_batch, y_batch = trim_tensors([x_batch, y_batch], pad_idx)
            x_batch = x_batch.to(device)
            y_batch = y_batch.to(device)
            try:
                y_pred = model(x_batch, attention_mask=x_batch > 0, labels=None)
                loss = criterion(y_pred, y_batch)
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
                if step % accumulation_steps == 0:
                    optimizer.step()
                    optimizer.zero_grad()
            except RuntimeError as e:
                if 'CUDA out of memory' in str(e):
                    print('ignoring', e)
                    torch.cuda.empty_cache()
                    continue
                raise

            if smoothed_loss is not None:
                smoothed_loss = 0.98 * smoothed_loss + 0.02 * loss.item()
            else:
                smoothed_loss = loss.item()
            pbar.set_postfix(loss=f'{smoothed_loss:.4f}')

            if step % yield_steps == 0:
                yield _state()

        yield _state()
        torch.cuda.empty_cache()


def tokenize_lines(texts, max_seq_length, tokenizer, use_bert: bool, pad_idx):
    all_tokens = []
    worker = partial(
        tokenize, max_seq_length=max_seq_length, tokenizer=tokenizer,
        use_bert=use_bert, pad_idx=pad_idx)
    with multiprocessing.Pool(processes=4 if ON_KAGGLE else 16) as pool:
        for tokens in tqdm.tqdm(pool.imap(worker, texts, chunksize=100),
                                disable=ON_KAGGLE,
                                total=len(texts), desc='tokenizing'):
            all_tokens.append(tokens)
    n_max_len = sum(t[-1] != 0 for t in all_tokens)
    print(f'{n_max_len / len(texts):.1%} texts are '
          f'at least {max_seq_length} tokens long')
    return np.array(all_tokens)


def tokenize(text, max_seq_length, tokenizer, use_bert: bool, pad_idx: int):
    trim_seq_length = max_seq_length
    if use_bert:
        trim_seq_length = max_seq_length - 2  # cls and sep
    tokens_a = tokenizer.tokenize(text)
    if len(tokens_a) > trim_seq_length:
        tokens_a = tokens_a[:trim_seq_length]
    if use_bert:
        tokens_a = ['[CLS]'] + tokens_a + ['[SEP]']
    return (tokenizer.convert_tokens_to_ids(tokens_a) +
            [pad_idx] * (max_seq_length - len(tokens_a)))


def preprocess_df(df: pd.DataFrame) -> pd.DataFrame:
    df['comment_text'] = df['comment_text'].astype(str).fillna('DUMMY_VALUE')
    return df


def get_target(df_train):
    y_aux_train = df_train[['target', 'severe_toxicity', 'obscene',
                            'identity_attack', 'insult', 'threat']]
    # Overall
    weights = np.ones((len(df_train),)) / 4

    # Subgroup
    weights += ((df_train[IDENTITY_COLUMNS].fillna(0).values >= 0.5)
                .sum(axis=1).astype(bool).astype(np.int) / 4)

    # Background Positive, Subgroup Negative
    weights += (
        ((df_train['target'].values >= 0.5).astype(bool).astype(np.int) +
         (df_train[IDENTITY_COLUMNS].fillna(0).values < 0.5)
         .sum(axis=1).astype(bool).astype(np.int))
        > 1).astype(bool).astype(np.int) / 4

    # Background Negative, Subgroup Positive
    weights += (
        ((df_train['target'].values < 0.5).astype(bool).astype(np.int) +
         (df_train[IDENTITY_COLUMNS].fillna(0).values >= 0.5)
         .sum(axis=1).astype(bool).astype(np.int))
        > 1 ).astype(bool).astype(np.int) / 4

    loss_weight = 1.0 / weights.mean()
    y_train = np.vstack(
        [(df_train['target'].values >= 0.5).astype(np.int), weights]).T
    return np.hstack([y_train, y_aux_train]), loss_weight


def make_submission(*, model, tokenizer, run_root: Path, max_seq_length: int,
                    batch_size: int, pad_idx, use_bert, bucket, test_size):
    df = pd.read_csv(DATA_ROOT / 'test.csv')
    if test_size and len(df) > test_size:
        df = df.sample(n=test_size, random_state=42)
    df = preprocess_df(df)
    x_test = tokenize_lines(df.pop('comment_text'), max_seq_length, tokenizer,
                            pad_idx=pad_idx, use_bert=use_bert)
    if bucket:
        indices, x_test = sorted_by_length(x_test, pad_idx)
        df = df.iloc[indices]

    test_dataset = TensorDataset(torch.tensor(x_test, dtype=torch.long))
    test_loader = DataLoader(test_dataset, batch_size=batch_size, shuffle=False)

    test_preds = []
    model.eval()
    for i, (x_batch, ) in enumerate(
            tqdm.tqdm(test_loader, desc='submission', leave=False,
                      disable=ON_KAGGLE)):
        if bucket:
            x_batch, = trim_tensors([x_batch], pad_idx)
        x_batch = x_batch.to(device)
        with torch.no_grad():
            y_pred = model(x_batch, attention_mask=x_batch > 0, labels=None)
        test_preds.extend(y_pred[:, 0].cpu().squeeze().numpy())
    model.train()

    df['prediction'] = torch.sigmoid(torch.tensor(test_preds)).numpy()
    path = run_root / 'submission.csv'
    df.sort_values('id', inplace=True)
    df.to_csv(path, index=None)
    print(f'Saved submission to {path}')


class BucketBatchSampler(BatchSampler):
    def __init__(self, *args, pad_idx=None, **kwargs):
        assert pad_idx is not None
        super().__init__(*args, **kwargs)
        self.pad_idx = pad_idx

    def __iter__(self):
        k = 8
        buckets = defaultdict(list)
        lengths = (self.sampler.data_source.tensors[0] != self.pad_idx
                   ).sum(dim=1).numpy()
        for idx in self.sampler:
            buckets[binned_length(lengths[idx], k)].append(idx)

        rng = np.random.RandomState()
        for i in range(len(self)):
            batch = []
            while len(batch) < self.batch_size:
                if not batch:
                    candidates = list(buckets)
                p = np.array([len(buckets[x]) for x in candidates])
                if p.sum() == 0:
                    if len(candidates) == len(buckets):
                        assert i == len(self) - 1
                        break
                    else:
                        candidates = list(buckets)
                        continue
                length = rng.choice(candidates, p=p / p.sum())
                idx = buckets[length].pop()
                if not batch:
                    # control efficiency vs. randomness
                    if rng.rand() > 0.2:
                        candidates = [x for x in buckets
                                      if abs(x - length) <= 2 * k]
                batch.append(idx)

            yield batch


def binned_length(length: int, k=8) -> int:
    length = int(length)
    binned = max(k, k * (length // k + (length % k > 0)))
    assert binned % k == 0 and binned >= length and binned > 0
    return binned


def trim_tensors(tsrs, pad_idx):
    max_len = binned_length(int(torch.max(torch.sum(tsrs[0] != pad_idx, 1))))
    tsrs = [tsr[:, :max_len] for tsr in tsrs]
    return tsrs


def sorted_by_length(tokens, pad_idx):
    assert len(tokens.shape) == 2
    lengths = np.sum(tokens != pad_idx, axis=1)
    indexed = sorted(enumerate(tokens), key=lambda x: lengths[x[0]])
    indices = np.array([i for i, _ in indexed])
    tokens = np.array([t for _, t in indexed])
    return indices, tokens


class GPT2ClassificationHeadModel(nn.Module):
    def __init__(self, model_name, num_labels: int, clf_dropout=0.2):
        super().__init__()
        self.transformer = GPT2Model.from_pretrained(model_name)
        self.dropout = nn.Dropout(clf_dropout)
        self.linear = nn.Linear(self.transformer.config.n_embd * 2, num_labels)
        nn.init.normal_(self.linear.weight, std=0.02)
        nn.init.normal_(self.linear.bias, 0)

    @property
    def config(self):
        return self.transformer.config

    def forward(self, input_ids, attention_mask=None,
                position_ids=None, token_type_ids=None,
                labels=None, past=None):
        hidden_states, _ = self.transformer(
            input_ids, position_ids, token_type_ids, past)
        last_hidden = hidden_states[-1]
        avg_pool = torch.mean(last_hidden, 1)
        max_pool, _ = torch.max(last_hidden, 1)
        h_conc = torch.cat((avg_pool, max_pool), 1)
        logits = self.linear(self.dropout(h_conc))
        return logits


if __name__ == '__main__':
    main()
