"""`python -m pipeline.train.train_lgbm` (`make train`): baselines, tuning, quantile models.

1. Load sales from 2015 on and split by month (see `dataset`).
2. Fit both baselines on the training split and score them on validation.
3. Random search (N_TRIALS) over LightGBM parameters for the median (alpha 0.5 quantile)
   model on log price, scored by validation MAE in dollars, with early stopping on validation.
4. Train the 0.1, 0.5 and 0.9 quantile models on the training split with the best
   parameters, early stopping on validation, and score them on validation.
5. Backtest the p10/p90 models year by year (each of CALIBRATION_YEARS predicted by models
   fit on all earlier years; the validation year by the models of step 4) and size a
   symmetric conformal widening of the interval from the pooled scores (see `calibrate`).
6. Refit the three models on training + validation with the same parameters and rounds,
   and widen p10 and p90 by the conformal offset.
7. Save the models (`models/{version}/model_p*.txt`, thresholds rounded to float32), export
   them to ONNX with `features.json`, and record everything in `training.json`, including
   the validation metrics of step 4 (the final models have seen the validation months).

The test split is not looked at here; `evaluate` uses it.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from config.settings import Settings, get_settings
from pipeline.common.db import get_engine
from pipeline.common.log import setup_logging
from pipeline.train import calibrate, export_onnx
from pipeline.train.baselines import RidgeBaseline, TownMedianBaseline
from pipeline.train.dataset import (
    CATEGORICAL,
    TRAIN_FROM,
    Split,
    combine,
    load_frame,
    make_splits,
    model_version,
)
from pipeline.train.metrics import interval_coverage, regression_metrics

logger = logging.getLogger("pipeline.train")

QUANTILES = {"p10": 0.1, "p50": 0.5, "p90": 0.9}
N_TRIALS = 30
SEED = 7
MAX_ROUNDS = 5000
EARLY_STOPPING_ROUNDS = 100
BASE_PARAMS: dict[str, Any] = {
    "objective": "quantile",
    "metric": "quantile",
    "verbose": -1,
    "seed": SEED,
    "deterministic": True,
    "force_row_wise": True,
    "bagging_freq": 1,
}
LATEST_FILE = "LATEST"
# Years whose sales size the interval widening, each predicted by models fit only on the
# years before it (from TRAIN_FROM): a flat market, COVID and the 2021-24 boom.
CALIBRATION_FROM_YEAR = 2019


def ordered_quantiles(predictions: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Predictions sorted across quantiles so p10 <= p50 <= p90 for every row.

    The quantile models are trained separately and can cross; sorting (monotone
    rearrangement) never makes a quantile estimate worse and keeps intervals valid.
    """
    stacked = np.sort(np.vstack([predictions[name] for name in QUANTILES]), axis=0)
    return dict(zip(QUANTILES, stacked, strict=True))


def sample_params(rng: np.random.Generator) -> dict[str, Any]:
    """One random point in the search space."""
    return {
        "learning_rate": float(np.exp(rng.uniform(np.log(0.02), np.log(0.15)))),
        "num_leaves": int(rng.integers(31, 256)),
        "min_data_in_leaf": int(rng.integers(20, 201)),
        "feature_fraction": float(rng.uniform(0.6, 1.0)),
        "bagging_fraction": float(rng.uniform(0.6, 1.0)),
        "lambda_l2": float(rng.uniform(0.0, 10.0)),
        "cat_smooth": float(rng.uniform(1.0, 50.0)),
    }


def _dataset(split: Split, reference: lgb.Dataset | None = None) -> lgb.Dataset:
    return lgb.Dataset(
        split.x,
        split.y,
        categorical_feature=list(CATEGORICAL),
        reference=reference,
        free_raw_data=False,
    )


def train_quantile(params: dict[str, Any], alpha: float, train: Split, valid: Split) -> lgb.Booster:
    """Train one quantile model with early stopping on the validation split."""
    dtrain = _dataset(train)
    return lgb.train(
        {**BASE_PARAMS, **params, "alpha": alpha},
        dtrain,
        num_boost_round=MAX_ROUNDS,
        valid_sets=[_dataset(valid, dtrain)],
        callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)],
    )


def validation_mae(booster: lgb.Booster, valid: Split) -> float:
    return regression_metrics(valid.price, np.exp(booster.predict(valid.x)))["mae"]


def search(train: Split, valid: Split) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Random search for the median model; returns the best parameters and every trial."""
    rng = np.random.default_rng(SEED)
    trials = []
    for i in range(N_TRIALS):
        params = sample_params(rng)
        booster = train_quantile(params, QUANTILES["p50"], train, valid)
        mae = validation_mae(booster, valid)
        trials.append({"trial": i, "params": params, "rounds": booster.best_iteration, "mae": mae})
        logger.info("trial %2d: validation MAE S$%.0f (%d rounds)", i, mae, booster.best_iteration)
    best = min(trials, key=lambda t: t["mae"])
    return best["params"], trials


def publish(staging: Path, final: Path) -> Path:
    """Replace `final` with the finished `staging` directory (two renames, same filesystem)."""
    old = final.with_name(f".{final.name}.old")
    shutil.rmtree(old, ignore_errors=True)
    if final.exists():
        final.rename(old)
    try:
        staging.rename(final)
    except BaseException:
        # Put the active model back so models/LATEST still points at a complete model.
        if old.exists() and not final.exists():
            old.rename(final)
        raise
    shutil.rmtree(old, ignore_errors=True)
    return final


def run(settings: Settings) -> Path:
    """Train everything and return the model directory.

    Everything is written to a temporary directory next to `models/{version}` and swapped in
    only once training, export and the ONNX check have all succeeded, so a failed or
    interrupted retrain leaves the current model (and `models/LATEST`) untouched.
    """
    engine = get_engine()
    df = load_frame(engine)
    engine.dispose()
    version = model_version(df)
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{version}.", dir=settings.models_dir))
    try:
        train_into(staging, df, version, settings)
        directory = publish(staging, settings.models_dir / version)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    (settings.models_dir / LATEST_FILE).write_text(version + "\n", encoding="utf-8")
    logger.info("Saved models to %s", directory)
    return directory


def _subset(split: Split, mask: np.ndarray, name: str) -> Split:
    return Split(name, split.x[mask], split.y[mask], split.rows[mask])


def backtest(
    params: dict[str, Any],
    rounds: dict[str, int],
    train: Split,
    valid: Split,
    valid_raw: dict[str, np.ndarray],
) -> tuple[np.ndarray, dict[str, dict[str, float]]]:
    """Out-of-sample interval scores for each calibration year, and per-year coverage.

    Years inside the training split are predicted by p10/p90 models fit on the years before
    them with the tuned parameters and rounds; the validation year uses `valid_raw`, the
    predictions of the models fit on the whole training split.
    """
    years = train.rows["month"].dt.year.to_numpy()
    folds: list[tuple[str, np.ndarray, dict[str, np.ndarray]]] = []
    for year in range(CALIBRATION_FROM_YEAR, int(years.max()) + 1):
        fit = _subset(train, years < year, f"before_{year}")
        held = _subset(train, years == year, str(year))
        raw = {}
        for name in ("p10", "p90"):
            booster = lgb.train(
                {**BASE_PARAMS, **params, "alpha": QUANTILES[name]},
                _dataset(fit),
                num_boost_round=rounds[name],
            )
            raw[name] = booster.predict(held.x)
        folds.append((str(year), held.y, raw))
    folds.append(("validation", valid.y, valid_raw))

    scores = []
    report: dict[str, dict[str, float]] = {}
    for label, y, raw in folds:
        low, high = np.minimum(raw["p10"], raw["p90"]), np.maximum(raw["p10"], raw["p90"])
        scores.append(calibrate.interval_scores(y, low, high))
        report[label] = {
            "n": int(len(y)),
            "coverage": interval_coverage(y, low, high),
            "below_p10": float(np.mean(y < low)),
            "above_p90": float(np.mean(y > high)),
        }
        logger.info(
            "calibration %s: raw 80%% coverage %.1f%% (%.1f%% below p10, %.1f%% above p90)",
            label,
            100 * report[label]["coverage"],
            100 * report[label]["below_p10"],
            100 * report[label]["above_p90"],
        )
    return np.concatenate(scores), report


def save_model(booster: lgb.Booster, path: Path, offset: float = 0.0) -> lgb.Booster:
    """Save with float32 thresholds (so ONNX takes the same branches as LightGBM) and an
    optional constant log-price offset; return the model reloaded from the file."""
    text = export_onnx.float32_thresholds(booster.model_to_string())
    if offset:
        text = calibrate.shift_model(text, offset)
    path.write_text(text, encoding="utf-8")
    return lgb.Booster(model_file=str(path))


def train_into(directory: Path, df: pd.DataFrame, version: str, settings: Settings) -> None:
    """Baselines, search, calibration, final models, ONNX export and training.json."""
    splits, maps = make_splits(df, settings)
    train, valid = splits["train"], splits["valid"]
    logger.info(
        "Model %s: %s train, %s validation, %s test sales",
        version,
        *(f"{len(splits[s].y):,}" for s in ("train", "valid", "test")),
    )

    baselines = {
        "town_median": TownMedianBaseline().fit(train),
        "ridge": RidgeBaseline().fit(train),
    }
    validation: dict[str, Any] = {
        name: regression_metrics(valid.price, model.predict_price(valid))
        for name, model in baselines.items()
    }
    for name, scores in validation.items():
        logger.info("baseline %s: validation MAE S$%.0f", name, scores["mae"])

    best, trials = search(train, valid)
    # Models on the training split only: rounds, validation metrics and calibration.
    rounds: dict[str, int] = {}
    raw: dict[str, np.ndarray] = {}
    for name, alpha in QUANTILES.items():
        booster = train_quantile(best, alpha, train, valid)
        rounds[name] = booster.best_iteration
        raw[name] = booster.predict(valid.x, num_iteration=booster.best_iteration)
    ordered = ordered_quantiles(raw)
    validation["lightgbm_p50"] = regression_metrics(valid.price, np.exp(ordered["p50"]))
    lgbm_mae = validation["lightgbm_p50"]["mae"]
    logger.info("LightGBM p50: validation MAE S$%.0f", lgbm_mae)
    if lgbm_mae >= min(validation[b]["mae"] for b in baselines):
        logger.warning("LightGBM does not beat the best baseline on validation MAE")

    scores, folds = backtest(best, rounds, train, valid, raw)
    offsets = calibrate.symmetric_offsets(scores)
    calibrated = ordered_quantiles({name: raw[name] + offsets.get(name, 0.0) for name in QUANTILES})
    validation["interval_80_coverage"] = {
        "uncalibrated": interval_coverage(valid.y, ordered["p10"], ordered["p90"]),
        # In sample: the validation year is one of the calibration years.
        "calibrated": interval_coverage(valid.y, calibrated["p10"], calibrated["p90"]),
    }
    logger.info(
        "Interval widened by %.4f in log price (%.1f%%) from %s backtest scores",
        offsets["p90"],
        100 * np.expm1(offsets["p90"]),
        f"{len(scores):,}",
    )

    # Final models: training + validation, same parameters and rounds, calibrated.
    full = combine("train_valid", train, valid)
    boosters: dict[str, lgb.Booster] = {}
    for name, alpha in QUANTILES.items():
        booster = lgb.train(
            {**BASE_PARAMS, **best, "alpha": alpha},
            _dataset(full),
            num_boost_round=rounds[name],
        )
        path = directory / f"model_{name}.txt"
        boosters[name] = save_model(booster, path, offsets.get(name, 0.0))
    logger.info("Refit on %s sales up to %s", f"{len(full.y):,}", f"{settings.valid_end:%Y-%m}")

    trained_on = date.today()
    window = {
        "train_from": TRAIN_FROM.isoformat(),
        "train_end": f"{settings.train_end:%Y-%m}",
        "valid_end": f"{settings.valid_end:%Y-%m}",
        "fit_end": f"{settings.valid_end:%Y-%m}",
        "last_month": version,
    }
    export_onnx.export_models(boosters, directory, maps, version, window, valid.x, trained_on)
    record = {
        "model_version": version,
        "trained_on": trained_on.isoformat(),
        "lightgbm_version": lgb.__version__,
        "data_window": window,
        "rows": {name: len(split.y) for name, split in splits.items()},
        "validation": validation,
        "calibration": {
            "method": "symmetric conformalized quantile regression, yearly backtest",
            "alpha": calibrate.ALPHA,
            "offsets_log_price": offsets,
            "folds": folds,
        },
        "best_params": {**BASE_PARAMS, **best},
        "rounds": rounds,
        "trials": trials,
    }
    (directory / "training.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def latest_model_dir(settings: Settings) -> Path:
    """The directory of the most recently trained model."""
    marker = settings.models_dir / LATEST_FILE
    if not marker.exists():
        raise FileNotFoundError(f"{marker} not found (run `make train`)")
    return settings.models_dir / marker.read_text(encoding="utf-8").strip()


def main() -> int:
    setup_logging()
    run(get_settings())
    return 0


if __name__ == "__main__":
    sys.exit(main())
