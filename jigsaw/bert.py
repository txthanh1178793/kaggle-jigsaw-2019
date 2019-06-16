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

from apex import amp
import json_log_plots
import numpy as np
import pandas as pd
from pytorch_pretrained_bert import (
    BertTokenizer, BertForSequenceClassification, BertAdam,
)
import torch
from torch.nn import functional as F
from torch import multiprocessing
from torch.utils.data import TensorDataset, DataLoader
from torch.utils.data.sampler import BatchSampler, RandomSampler
import tqdm

from .metrics import compute_bias_metrics_for_model, IDENTITY_COLUMNS
from .utils import DATA_ROOT, ON_KAGGLE


device = torch.device('cuda')


def main():
    parser = argparse.ArgumentParser()
    arg = parser.add_argument
    arg('run_root')
    arg('--train-size', type=int)
    arg('--valid-size', type=int)
    arg('--bert', default='bert-base-uncased')
    arg('--train-seq-length', type=int, default=224)
    arg('--test-seq-length', type=int, default=296)
    arg('--epochs', type=int, default=2)
    arg('--validation', action='store_true')
    arg('--submission', action='store_true')
    arg('--lr', type=float, default=2e-5)
    arg('--checkpoint-interval', type=int)
    arg('--clean', action='store_true')
    arg('--fold', type=int, default=0)
    arg('--bucket', type=int, default=1)
    arg('--load-weights', help='load weights for training')
    args = parser.parse_args()

    run_root = Path(args.run_root)
    if args.clean and run_root.exists():
        if input(f'Clean "{run_root.absolute()}"? ') == 'y':
            shutil.rmtree(run_root)
    do_train = not (args.submission or args.validation)
    if do_train:
        if run_root.exists():
            parser.error(f'{run_root} exists')
        run_root.mkdir(exist_ok=True, parents=True)
        params_str = json.dumps(vars(args), indent=4)
        print(params_str)
        (run_root / 'params.json').write_text(params_str)
        shutil.copy(__file__, run_root)

    print('Loading tokenizer...')
    tokenizer = BertTokenizer.from_pretrained(args.bert)
    print('Loading model...')
    model = BertForSequenceClassification.from_pretrained(
        args.bert, num_labels=7)
    model = model.to(device)

    model_path = run_root / 'model.pt'
    optimizer_path = run_root / 'optimizer.pt'
    best_model_path = run_root / 'model-best.pt'
    valid_predictions_path = run_root / 'valid-predictions.csv'

    if args.submission:
        model.load_state_dict(torch.load(best_model_path))
        make_submission(model=model, tokenizer=tokenizer,
                        run_root=run_root, max_seq_length=args.test_seq_length)
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
        df_valid.pop('comment_text'), args.test_seq_length, tokenizer)
    y_valid, _ = get_target(df_valid)
    y_train, loss_weight = get_target(df_train)
    print(f'X_valid.shape={x_valid.shape} y_valid.shape={y_valid.shape}')

    criterion = partial(get_loss, loss_weight=loss_weight)

    def _run_validation():
        return validation(
            model=model, criterion=criterion,
            x_valid=x_valid, y_valid=y_valid, df_valid=df_valid)

    if args.validation:
        model.load_state_dict(torch.load(best_model_path))
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
        df_train.pop('comment_text'), args.train_seq_length, tokenizer)
    print(f'X_train.shape={x_train.shape} y_train.shape={y_train.shape}')

    best_auc = 0
    step = optimizer = None
    try:
        for model, optimizer, epoch_pbar, loss, step in train(
                model=model, criterion=criterion,
                x_train=x_train, y_train=y_train, epochs=args.epochs,
                yield_steps=args.checkpoint_interval or len(y_valid) // 8,
                bucket=args.bucket, lr=args.lr,
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


def validation(*, model, criterion, x_valid, y_valid, df_valid):
    valid_dataset = TensorDataset(
        torch.tensor(x_valid, dtype=torch.long),
        torch.tensor(y_valid, dtype=torch.float),
    )
    valid_loader = DataLoader(valid_dataset, batch_size=32, shuffle=False)

    valid_preds, losses = [], []
    model.eval()
    for i, (x_batch, y_batch) in enumerate(
            tqdm.tqdm(valid_loader, desc='validation', leave=False)):
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
        batch_size=32, accumulation_steps=2,
        ):
    train_dataset = TensorDataset(
        torch.tensor(x_train, dtype=torch.long),
        torch.tensor(y_train, dtype=torch.float))

    model.zero_grad()
    model = model.to(device)
    param_optimizer = list(model.named_parameters())
    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer
                    if not any(nd in n for nd in no_decay)],
         'weight_decay': 0.01},
        {'params': [p for n, p in param_optimizer
                    if any(nd in n for nd in no_decay)],
         'weight_decay': 0.0},
    ]

    num_train_optimization_steps = int(
        epochs * len(train_dataset) / (batch_size * accumulation_steps))
    print(f'Starting training for '
          f'{num_train_optimization_steps * accumulation_steps:,} steps, '
          f'checkpoint interval {yield_steps:,}')
    optimizer = BertAdam(
        optimizer_grouped_parameters,
        lr=lr,
        warmup=0.05,
        t_total=num_train_optimization_steps)

    model, optimizer = amp.initialize(
        model, optimizer, opt_level='O1', verbosity=0)
    model.train()

    if bucket:
        sampler = RandomSampler(train_dataset)
        batch_sampler = BucketBatchSampler(sampler, batch_size, drop_last=False)
        train_loader = DataLoader(train_dataset, batch_sampler=batch_sampler)
    else:
        train_loader = DataLoader(
            train_dataset, batch_size=batch_size, shuffle=True)

    smoothed_loss = None
    step = 0
    epoch_pbar = tqdm.trange(epochs)

    def _state():
        return model, optimizer, epoch_pbar, smoothed_loss, step * batch_size

    yield _state()

    for _ in epoch_pbar:
        optimizer.zero_grad()
        pbar = tqdm.tqdm(train_loader, leave=False)
        for x_batch, y_batch in pbar:
            step += 1
            if bucket:
                x_batch, y_batch = trim_tensors([x_batch, y_batch])
            x_batch = x_batch.to(device)
            y_batch = y_batch.to(device)
            y_pred = model(x_batch, attention_mask=x_batch > 0, labels=None)
            loss = criterion(y_pred, y_batch)
            with amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
            if step % accumulation_steps == 0:
                optimizer.step()
                optimizer.zero_grad()

            if smoothed_loss is not None:
                smoothed_loss = 0.98 * smoothed_loss + 0.02 * loss.item()
            else:
                smoothed_loss = loss.item()
            pbar.set_postfix(loss=f'{smoothed_loss:.4f}')

            if step % yield_steps == 0:
                yield _state()
        yield _state()


def tokenize_lines(texts, max_seq_length, tokenizer):
    all_tokens = []
    worker = partial(
        tokenize, max_seq_length=max_seq_length, tokenizer=tokenizer)
    with multiprocessing.Pool(processes=4 if ON_KAGGLE else 16) as pool:
        for tokens in tqdm.tqdm(pool.imap(worker, texts, chunksize=100),
                                total=len(texts), desc='tokenizing'):
            all_tokens.append(tokens)
    n_max_len = sum(t[-1] != 0 for t in all_tokens)
    print(f'{n_max_len / len(texts):.1%} texts are '
          f'at least {max_seq_length} tokens long')
    return np.array(all_tokens)


def tokenize(text, max_seq_length, tokenizer):
    max_seq_length -= 2  # cls and sep
    tokens_a = tokenizer.tokenize(text)
    if len(tokens_a) > max_seq_length:
        tokens_a = tokens_a[:max_seq_length]
    return (tokenizer.convert_tokens_to_ids(['[CLS]'] + tokens_a + ['[SEP]']) +
            [0] * (max_seq_length - len(tokens_a)))


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


def make_submission(*, model, tokenizer, run_root: Path, max_seq_length: int):
    df = pd.read_csv(DATA_ROOT / 'test.csv')
    df = preprocess_df(df)
    x_test = tokenize_lines(df.pop('comment_text'), max_seq_length, tokenizer)
    test_dataset = TensorDataset(torch.tensor(x_test, dtype=torch.long))
    test_loader = DataLoader(test_dataset, batch_size=32, shuffle=False)

    test_preds = []
    model.eval()
    for i, (x_batch, ) in enumerate(
            tqdm.tqdm(test_loader, desc='submission', leave=False)):
        x_batch = x_batch.to(device)
        with torch.no_grad():
            y_pred = model(x_batch, attention_mask=x_batch > 0, labels=None)
        test_preds.extend(y_pred[:, 0].cpu().squeeze().numpy())
    model.train()

    df['prediction'] = torch.sigmoid(torch.tensor(test_preds)).numpy()
    root = Path('.') if ON_KAGGLE else run_root
    path = root / 'submission.csv'
    df.to_csv(path, index=None)
    print(f'Saved submission to {path}')


class BucketBatchSampler(BatchSampler):
    def __iter__(self):
        k = 8
        buckets = defaultdict(list)
        lengths = (self.sampler.data_source.tensors[0] == 0).sum(dim=1).numpy()
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


def trim_tensors(tsrs):
    max_len = binned_length(int(torch.max(torch.sum(tsrs[0] != 0, 1))))
    tsrs = [tsr[:, :max_len] for tsr in tsrs]
    return tsrs


if __name__ == '__main__':
    main()
