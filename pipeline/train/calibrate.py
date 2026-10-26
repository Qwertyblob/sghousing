"""Conformal calibration of the p10/p90 models (conformalized quantile regression).

Quantile models fit in-sample, but out of sample their intervals drift with the market, and
the direction changes between regimes: fit to 2023, the raw p10-p90 range held 68% of 2024
prices with most misses above p90 (boom); an offset learned from that year alone pushed both
bounds up and missed 2025-26 on the low side (cooling). So the width is calibrated on
several years with different markets and widened symmetrically (Romano, Patterson and
Candès, 2019):

    score  e = max(p10 - y, y - p90)          p10' = p10 - Q,  p90' = p90 + Q

with Q the ceil((n + 1)(1 - alpha)) / n empirical quantile of the scores pooled over the
calibration years, alpha = 0.2. Each year's scores come from models fit only on earlier
years (`train_lgbm.backtest`). Q is added to the leaves of the p90 model's first tree and
subtracted from the p10 model's, so the saved LightGBM and ONNX models output calibrated
quantiles directly.
"""

from __future__ import annotations

import math
import re

import numpy as np

ALPHA = 0.2


def conformal_quantile(scores: np.ndarray, alpha: float) -> float:
    """The finite-sample-corrected (1 - alpha) quantile of conformity scores."""
    n = len(scores)
    level = min(1.0, math.ceil((n + 1) * (1 - alpha)) / n)
    return float(np.quantile(scores, level, method="higher"))


def interval_scores(y: np.ndarray, p10: np.ndarray, p90: np.ndarray) -> np.ndarray:
    """How far each log price falls outside [p10, p90] (negative inside)."""
    return np.maximum(p10 - y, y - p90)


def symmetric_offsets(scores: np.ndarray, alpha: float = ALPHA) -> dict[str, float]:
    """Log-price offsets for the p10 and p90 models from held-out interval scores."""
    q = conformal_quantile(scores, alpha)
    return {"p10": -q, "p90": q}


def shift_model(model_text: str, offset: float) -> str:
    """A LightGBM model (text format) whose every prediction is `offset` higher.

    Each prediction sums one leaf per tree, so adding the offset to every leaf of the first
    tree shifts all predictions by exactly that amount. The `tree_sizes` header no longer
    matches and is dropped (LightGBM then parses trees sequentially).
    """
    text = re.sub(r"^tree_sizes=.*\n", "", model_text, flags=re.MULTILINE)
    start = text.index("Tree=0\n")
    line = re.compile(r"^leaf_value=(.*)$", flags=re.MULTILINE).search(text, start)
    if line is None:
        raise ValueError("model has no leaf values for its first tree")
    values = " ".join(repr(float(v) + offset) for v in line.group(1).split(" "))
    return text[: line.start()] + f"leaf_value={values}" + text[line.end() :]
