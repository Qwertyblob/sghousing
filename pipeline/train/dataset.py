"""Modelling dataset: features, the time split and categorical encoding.

Rows come from `marts.resale_features` from TRAIN_FROM on. The split is by month, never
random: train up to TRAIN_END, validation up to VALID_END, test after. Categories are mapped
to fixed integer codes learnt from the training split; LightGBM treats those columns as
categorical, and a value unseen in training becomes NaN (LightGBM's missing value).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from config.settings import Settings

TRAIN_FROM = date(2015, 1, 1)
CATEGORICAL = ("town", "flat_type", "flat_model", "planning_area")
NUMERIC = (
    "floor_area_sqm",
    "storey_mid",
    "rel_storey",
    "remaining_lease_months",
    "flat_age_years",
    "month_index",
    "lat",
    "lon",
    "dist_mrt_m",
    "n_mrt_exits_500m",
    "n_primary_schools_1km",
    "dist_nearest_primary_m",
    "dist_hawker_m",
    "dist_park_m",
    "dist_cbd_m",
    "block_max_floor",
    "block_year_completed",
    "town_psm_3m",
    "town_psm_6m",
    "town_psm_12m",
    "block_psm_24m",
    "block_sales_24m",
)
# Model input order (ONNX input columns, features.json).
FEATURES = CATEGORICAL + NUMERIC
SPLITS = ("train", "valid", "test")

CategoryMaps = dict[str, dict[str, int]]


@dataclass(frozen=True)
class Split:
    """One part of the time split: model inputs, log-price target and row context."""

    name: str
    x: pd.DataFrame
    y: np.ndarray
    rows: pd.DataFrame

    @property
    def price(self) -> np.ndarray:
        return np.exp(self.y)


def load_frame(engine: Engine) -> pd.DataFrame:
    """Sales from TRAIN_FROM on with every model feature plus context columns."""
    numeric = ", ".join(f"{c}::float8 AS {c}" for c in NUMERIC)
    sql = f"""
        SELECT txn_id, month, region, address_key, resale_price::float8 AS resale_price,
               {", ".join(CATEGORICAL)}, {numeric}
        FROM marts.resale_features
        WHERE month >= :start
        ORDER BY month, txn_id
    """
    with engine.connect() as conn:
        df = pd.read_sql(text(sql), conn, params={"start": TRAIN_FROM}, parse_dates=["month"])
    df["log_price"] = np.log(df["resale_price"])
    return df


def assign_split(months: pd.Series, train_end: date, valid_end: date) -> pd.Series:
    """'train' up to `train_end`, 'valid' up to `valid_end`, 'test' after (by month)."""
    return pd.Series(
        np.select(
            [months <= pd.Timestamp(train_end), months <= pd.Timestamp(valid_end)],
            ["train", "valid"],
            "test",
        ),
        index=months.index,
    )


def category_maps(train: pd.DataFrame) -> CategoryMaps:
    """Fixed integer codes per categorical column, from the sorted training values."""
    return {
        col: {value: code for code, value in enumerate(sorted(train[col].dropna().unique()))}
        for col in CATEGORICAL
    }


def encode(df: pd.DataFrame, maps: CategoryMaps) -> pd.DataFrame:
    """Model inputs in FEATURES order as float32; unseen categories become NaN."""
    out = pd.DataFrame(index=df.index)
    for col in CATEGORICAL:
        out[col] = df[col].map(maps[col]).astype("float32")
    for col in NUMERIC:
        out[col] = df[col].astype("float32")
    return out[list(FEATURES)]


def make_splits(df: pd.DataFrame, settings: Settings) -> tuple[dict[str, Split], CategoryMaps]:
    """Split by month and encode each part with the training split's category codes."""
    part = assign_split(df["month"], settings.train_end, settings.valid_end)
    maps = category_maps(df[part == "train"])
    splits = {}
    for name in SPLITS:
        rows = df[part == name].reset_index(drop=True)
        splits[name] = Split(name, encode(rows, maps), rows["log_price"].to_numpy(), rows)
    return splits, maps


def combine(name: str, *parts: Split) -> Split:
    """Several splits as one (e.g. train and validation for the final fit)."""
    return Split(
        name,
        pd.concat([p.x for p in parts], ignore_index=True),
        np.concatenate([p.y for p in parts]),
        pd.concat([p.rows for p in parts], ignore_index=True),
    )


def model_version(df: pd.DataFrame) -> str:
    """The version names the last month of data, e.g. "2026-10"."""
    return f"{df['month'].max():%Y-%m}"
