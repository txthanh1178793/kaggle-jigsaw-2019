import argparse
import json
from pathlib import Path
import statistics
import shutil

import json_log_plots
import pandas as pd
from sklearn.model_selection import KFold
import torch
import torch.cuda
from torch import nn, optim
from torch.utils.data import DataLoader, Dataset
import tqdm

from .dataset import encode_comment, load_sp_model, SP_MODEL, DATA_ROOT
from .metrics import compute_bias_metrics_for_model, MAIN_METRICS
from . import models


class JigsawDataset(Dataset):
    def __init__(self, df, sp_model, max_len: int):
        super().__init__()
        self.df = df
        self.sp_model = sp_model
        self.max_len = max_len

    def __len__(self):
        return len(self.df)

    def __getitem__(self, idx):
        item = self.df.iloc[idx]
        comment = encode_comment(self.sp_model, item.comment_text,
                                 max_len=self.max_len)
        comment = torch.tensor(comment, dtype=torch.int64)
        target = torch.tensor([float(item.target >= 0.5)])
        return comment, target


def main():
    parser = argparse.ArgumentParser()
    arg = parser.add_argument
    arg('run_path')
    arg('--model', default='SimpleLSTM')
    arg('--sp-model', default=SP_MODEL)
    arg('--max-len', type=int, default=250)
    arg('--batch-size', type=int, default=512)
    arg('--lr', type=float, default=1e-4)
    arg('--epochs', type=int, default=10)
    arg('--workers', type=int, default=4)
    arg('--validate-every', type=int, default=1000)
    arg('--clean', action='store_true')
    arg('--validate', action='store_true')
    args = parser.parse_args()

    run_path = Path(args.run_path)
    params_path = run_path / 'params.json'
    save_path = run_path / 'net.pt'
    run_validation = args.validate
    if run_validation:
        params = json.loads(params_path.read_text())
        # args are ignored
    else:
        params = vars(args)
        params_string = json.dumps(params, indent=4, sort_keys=True)
        print(params_string)
        if args.clean and run_path.exists():
            shutil.rmtree(run_path)
        run_path.mkdir(exist_ok=True, parents=True)
        params_path.write_text(params_string)
    del args

    sp_model = load_sp_model(params['sp_model'])
    df = pd.read_pickle(DATA_ROOT / 'train.pkl')
    kfold = KFold(n_splits=10, shuffle=True, random_state=42)
    train_ids, valid_ids = next(kfold.split(df))
    train_df, valid_df = df.iloc[train_ids], df.iloc[valid_ids]

    train_dataset = JigsawDataset(train_df, sp_model, params['max_len'])
    train_loader = DataLoader(
        train_dataset,
        batch_size=params['batch_size'],
        shuffle=True,
        num_workers=params['workers'],
    )
    valid_dataset = JigsawDataset(valid_df, sp_model, params['max_len'])
    valid_loader = DataLoader(
        valid_dataset,
        batch_size=params['batch_size'],
        shuffle=False,
        num_workers=params['workers'],
    )
    print(f'train size: {len(train_dataset):,} '
          f'valid size: {len(valid_dataset):,}')

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    model_cls = getattr(models, params['model'])
    model: nn.Module = model_cls(n_vocab=len(sp_model))
    model.to(device)
    optimizer = optim.Adam(model.parameters(), lr=params['lr'])
    criterion = nn.BCEWithLogitsLoss()
    step = 0

    def save():
        print('saving...')
        torch.save({
            'state_dict': model.state_dict(),
            'params': params,
        }, save_path)

    def train_step(xs, ys):
        optimizer.zero_grad()
        xs, ys = xs.to(device), ys.to(device)
        ys_pred = model(xs)
        loss = criterion(ys_pred, ys)
        loss.backward()
        optimizer.step()
        return loss.item()

    def get_validation_metrics():
        losses = []
        predictions = []
        model.eval()
        for xs, ys in tqdm.tqdm(valid_loader, desc='validate',
                                dynamic_ncols=True, leave=False):
            xs, ys = xs.to(device), ys.to(device)
            ys_pred = model(xs)
            predictions.extend(
                map(float, torch.sigmoid(ys_pred[:, 0]).data.cpu()))
            loss = criterion(ys_pred, ys)
            losses.append(loss.item())
        model.train()
        valid_loss_value = statistics.mean(losses)
        pred_df = valid_loader.dataset.df.copy()
        pred_df['pred'] = predictions
        metrics = compute_bias_metrics_for_model(pred_df, 'pred')
        metrics['valid_loss'] = valid_loss_value
        return metrics

    def validate():
        json_log_plots.write_event(
            run_path, step * params['batch_size'], **get_validation_metrics())

    def train():
        nonlocal step
        try:
            for _ in tqdm.trange(params['epochs'], desc='epoch',
                                 dynamic_ncols=True):
                pbar = tqdm.tqdm(train_loader, desc='train', dynamic_ncols=True)
                for batch in pbar:
                    loss_value = train_step(*batch)
                    step += 1
                    pbar.set_postfix(loss=f'{loss_value:.2f}')
                    json_log_plots.write_event(
                        run_path, step * params['batch_size'], loss=loss_value)
                    if (params['validate_every'] and
                            step % params['validate_every'] == 0):
                        validate()
                save()
                validate()
        except KeyboardInterrupt:
            save()
            exit(1)

    if run_validation:
        model.load_state_dict(
            torch.load(save_path, map_location=device)['state_dict'])
        valid_metrics = get_validation_metrics()
        for k in MAIN_METRICS:
            print(f'{k:<20} {valid_metrics[k]:.4f}')
    else:
        train()


if __name__ == '__main__':
    main()