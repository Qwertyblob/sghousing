"""Price metrics shared by training (validation) and evaluation (test)."""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

MIN_GROUP_ROWS = 30


def regression_metrics(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float]:
    """MAE, MAPE, RMSE and R² of prices in dollars."""
    return {
        "n": int(len(actual)),
        "mae": float(mean_absolute_error(actual, predicted)),
        "mape": float(np.mean(np.abs(predicted - actual) / actual)),
        "rmse": float(np.sqrt(mean_squared_error(actual, predicted))),
        "r2": float(r2_score(actual, predicted)),
    }


def interval_coverage(actual: np.ndarray, low: np.ndarray, high: np.ndarray) -> float:
    """Share of prices inside [low, high]."""
    return float(np.mean((actual >= low) & (actual <= high)))


def grouped_metrics(
    keys: pd.Series, actual: np.ndarray, predicted: np.ndarray
) -> dict[str, dict[str, float]]:
    """Metrics per group, for groups with at least MIN_GROUP_ROWS rows."""
    out = {}
    for key in sorted(keys.dropna().unique()):
        mask = (keys == key).to_numpy()
        if mask.sum() >= MIN_GROUP_ROWS:
            out[str(key)] = regression_metrics(actual[mask], predicted[mask])
    return out
