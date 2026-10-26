"""Baselines the LightGBM model has to beat.

1. Town median: the town and flat type's median price per sqm over the 12 months before the
   sale (the leak-free `town_psm_12m` feature) times floor area. Where that window is empty,
   the flat type's median price per sqm in the training split is used instead.
2. Ridge regression on log price, with one-hot categoricals and standardised numerics
   (missing numerics imputed with the training median).
"""

from __future__ import annotations

import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline, make_pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from pipeline.train.dataset import CATEGORICAL, NUMERIC, Split

RIDGE_ALPHA = 1.0


class TownMedianBaseline:
    """Price = prior-12-month town and flat type median price per sqm x floor area."""

    def fit(self, train: Split) -> TownMedianBaseline:
        rows = train.rows
        psm = rows["resale_price"] / rows["floor_area_sqm"]
        self.fallback_psm_ = psm.groupby(rows["flat_type"]).median().to_dict()
        self.overall_psm_ = float(psm.median())
        return self

    def predict_price(self, split: Split) -> np.ndarray:
        rows = split.rows
        fallback = rows["flat_type"].map(self.fallback_psm_).fillna(self.overall_psm_)
        psm = rows["town_psm_12m"].fillna(fallback)
        return (psm * rows["floor_area_sqm"]).to_numpy()


def ridge_pipeline() -> Pipeline:
    """One-hot categoricals, imputed and standardised numerics, then Ridge."""
    columns = ColumnTransformer(
        [
            ("categorical", OneHotEncoder(handle_unknown="ignore"), list(CATEGORICAL)),
            (
                "numeric",
                make_pipeline(SimpleImputer(strategy="median"), StandardScaler()),
                list(NUMERIC),
            ),
        ]
    )
    return make_pipeline(columns, Ridge(alpha=RIDGE_ALPHA))


class RidgeBaseline:
    """Ridge regression on log price over the raw (unencoded) feature columns."""

    def fit(self, train: Split) -> RidgeBaseline:
        self.model_ = ridge_pipeline().fit(train.rows[list(CATEGORICAL + NUMERIC)], train.y)
        return self

    def predict_price(self, split: Split) -> np.ndarray:
        return np.exp(self.model_.predict(split.rows[list(CATEGORICAL + NUMERIC)]))
