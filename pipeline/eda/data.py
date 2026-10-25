"""Queries for the exploratory notebooks.

Aggregation happens in SQL, so each function returns a small DataFrame; only
`feature_sample` returns individual sales. Everything reads `marts.resale_features`, plus the
planning-area boundaries for the map.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

# From 2015 every sale is located and has every feature (see reports/data_quality.md).
RECENT_FROM = date(2015, 1, 1)
# A town or planning area needs this many sales in the latest 12 months to be ranked or
# mapped, and this many in the earlier window for its 2-year change to be shown.
MIN_GROUP_SALES = 50
# A bin (lease year, storey, distance bucket) needs this many sales to be plotted.
MIN_BIN_SALES = 200
MRT_BUCKET_EDGES_M = (0, 200, 400, 600, 800, 1000, 1500, 2000)
FLAT_TYPE_ORDER = (
    "1 ROOM",
    "2 ROOM",
    "3 ROOM",
    "4 ROOM",
    "5 ROOM",
    "EXECUTIVE",
    "MULTI-GENERATION",
)
FEATURE_SAMPLE_SIZE = 50_000
# Numeric features compared with log price; flat type enters as its rank in FLAT_TYPE_ORDER.
ASSOCIATION_FEATURES = (
    "floor_area_sqm",
    "flat_type_rank",
    "storey_mid",
    "rel_storey",
    "remaining_lease_months",
    "flat_age_years",
    "block_max_floor",
    "dist_mrt_m",
    "n_mrt_exits_500m",
    "n_primary_schools_1km",
    "dist_nearest_primary_m",
    "dist_hawker_m",
    "dist_park_m",
    "dist_cbd_m",
    "lat",
    "lon",
    "month_index",
)
# Integer-valued counts and ranks with few distinct values: mutual information treats them as
# discrete (the nearest-neighbour estimator for continuous features is biased by ties).
# month_index and remaining_lease_months take hundreds of values and stay continuous.
DISCRETE_FEATURES = frozenset(
    {"flat_type_rank", "storey_mid", "block_max_floor", "n_mrt_exits_500m", "n_primary_schools_1km"}
)
WINDOW_COLUMNS = ("earlier_median_psm", "latest_median_psm", "earlier_sales", "latest_sales")

# Price per sqm relative to the median of the sale's town, flat type and year. This removes
# most of the effect of location, flat type and the market's level, leaving within-group
# differences such as lease, floor and distance to the MRT.
_RELATIVE_PSM = """
    WITH medians AS (
        SELECT town, flat_type, extract(year FROM month) AS year,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm) AS median_psm
        FROM marts.resale_features
        WHERE month >= :recent_from
        GROUP BY 1, 2, 3
    ),
    relative AS (
        SELECT r.*, (r.price_psm / m.median_psm)::float8 AS rel_psm
        FROM marts.resale_features AS r
        JOIN medians AS m
          ON m.town = r.town
         AND m.flat_type = r.flat_type
         AND m.year = extract(year FROM r.month)
        WHERE r.month >= :recent_from
    )
"""

_QUARTILES = """
    percentile_cont(0.25) WITHIN GROUP (ORDER BY rel_psm) AS q25,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY rel_psm) AS median,
    percentile_cont(0.75) WITHIN GROUP (ORDER BY rel_psm) AS q75,
    count(*) AS sales
"""


def _read(
    engine: Engine, sql: str, parse_dates: list[str] | None = None, **params: Any
) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params, parse_dates=parse_dates)


def last_month(engine: Engine) -> date:
    """The latest month with sales."""
    with engine.connect() as conn:
        return conn.execute(text("SELECT max(month) FROM marts.resale_features")).scalar_one()


def median_psm_by_month(engine: Engine) -> pd.DataFrame:
    """Median price per sqm and sales by month and flat type, 1990 onwards."""
    return _read(
        engine,
        """
        SELECT month, flat_type,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm) AS median_psm,
               count(*) AS sales
        FROM marts.resale_features
        GROUP BY month, flat_type
        ORDER BY month, flat_type
        """,
        parse_dates=["month"],
    )


def monthly_volume(engine: Engine) -> pd.DataFrame:
    """Sales per month."""
    return _read(
        engine,
        "SELECT month, count(*) AS sales FROM marts.resale_features GROUP BY month ORDER BY month",
        parse_dates=["month"],
    )


def window_medians(rows: pd.DataFrame, group: str) -> pd.DataFrame:
    """Turn (grp, win, median_psm, sales) rows into one row per group.

    A group is kept when it has MIN_GROUP_SALES in the latest window, whatever its history.
    Its 2-year change is NaN when the earlier window has fewer sales than that.
    """
    wide = rows.pivot(index="grp", columns="win", values=["median_psm", "sales"])
    wide.columns = [f"{win}_{measure}" for measure, win in wide.columns]
    wide = wide.reindex(columns=list(WINDOW_COLUMNS))
    wide[["earlier_sales", "latest_sales"]] = wide[["earlier_sales", "latest_sales"]].fillna(0)
    wide = wide[wide["latest_sales"] >= MIN_GROUP_SALES].copy()
    change = wide["latest_median_psm"] / wide["earlier_median_psm"] - 1
    wide["change"] = change.where(wide["earlier_sales"] >= MIN_GROUP_SALES)
    return wide.rename_axis(group).reset_index()


def _latest_and_earlier(engine: Engine, group: str) -> pd.DataFrame:
    """Median price per sqm by `group` in the latest 12 months and the same 12 months two
    years earlier, with the 2-year change (see `window_medians`)."""
    rows = _read(
        engine,
        f"""
        WITH bounds AS (SELECT max(month) AS last FROM marts.resale_features),
        windows AS (
            SELECT r.{group} AS grp,
                   CASE WHEN r.month > b.last - interval '12 months' THEN 'latest'
                        WHEN r.month <= b.last - interval '24 months' THEN 'earlier'
                   END AS win,
                   r.price_psm
            FROM marts.resale_features AS r
            CROSS JOIN bounds AS b
            WHERE r.month > b.last - interval '36 months' AND r.{group} IS NOT NULL
        )
        SELECT grp, win,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm) AS median_psm,
               count(*) AS sales
        FROM windows
        WHERE win IS NOT NULL
        GROUP BY grp, win
        """,
    )
    return window_medians(rows, group)


def town_ranking(engine: Engine) -> pd.DataFrame:
    """Towns by median price per sqm over the latest 12 months, with the 2-year change."""
    return (
        _latest_and_earlier(engine, "town")
        .sort_values("latest_median_psm", ascending=False)
        .reset_index(drop=True)
    )


def planning_area_prices(engine: Engine) -> gpd.GeoDataFrame:
    """Planning-area boundaries with the latest 12 months' median price per sqm (NaN where
    an area has too few HDB resales in that window; earlier years do not matter)."""
    with engine.connect() as conn:
        areas = gpd.read_postgis(
            text("SELECT planning_area, region, geom FROM staging.planning_areas"),
            conn,
            geom_col="geom",
        )
    prices = _latest_and_earlier(engine, "planning_area")
    return areas.merge(prices, on="planning_area", how="left").sort_values("planning_area")


def lease_decay(engine: Engine) -> pd.DataFrame:
    """Relative price per sqm by whole years of remaining lease, sales since RECENT_FROM."""
    df = _read(
        engine,
        _RELATIVE_PSM
        + f"""
        SELECT (remaining_lease_months / 12) AS lease_years, {_QUARTILES}
        FROM relative
        GROUP BY 1
        ORDER BY 1
        """,
        recent_from=RECENT_FROM,
    )
    return df[df["sales"] >= MIN_BIN_SALES].reset_index(drop=True)


def floor_premium(engine: Engine) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Relative price per sqm by storey (middle of the storey range) and by relative storey
    (storey over the block's top floor, in tenths), sales since RECENT_FROM."""
    by_storey = _read(
        engine,
        _RELATIVE_PSM + f"SELECT storey_mid, {_QUARTILES} FROM relative GROUP BY 1 ORDER BY 1",
        recent_from=RECENT_FROM,
    )
    by_rel = _read(
        engine,
        _RELATIVE_PSM
        + f"""
        SELECT (least(width_bucket(rel_storey, 0, 1, 10), 10) - 0.5) / 10.0 AS rel_storey,
               {_QUARTILES}
        FROM relative
        GROUP BY 1
        ORDER BY 1
        """,
        recent_from=RECENT_FROM,
    )
    return (
        by_storey[by_storey["sales"] >= MIN_BIN_SALES].reset_index(drop=True),
        by_rel[by_rel["sales"] >= MIN_BIN_SALES].reset_index(drop=True),
    )


def bucket_labels(edges: tuple[int, ...]) -> list[str]:
    """Labels for `width_bucket(x, edges)` buckets 1..len(edges): "0–200", ..., "2,000+"."""
    pairs = [f"{lo:,}–{hi:,}" for lo, hi in zip(edges, edges[1:], strict=False)]
    return [*pairs, f"{edges[-1]:,}+"]


def mrt_distance(engine: Engine) -> pd.DataFrame:
    """By distance to the nearest MRT exit: median price per sqm over the latest 12 months,
    and relative price per sqm (within town, flat type and year) since RECENT_FROM."""
    edges = "ARRAY[" + ",".join(str(e) for e in MRT_BUCKET_EDGES_M) + "]::float8[]"
    relative = _read(
        engine,
        _RELATIVE_PSM
        + f"""
        SELECT width_bucket(dist_mrt_m, {edges}) AS bucket, {_QUARTILES}
        FROM relative
        WHERE dist_mrt_m IS NOT NULL
        GROUP BY 1
        ORDER BY 1
        """,
        recent_from=RECENT_FROM,
    )
    latest = _read(
        engine,
        f"""
        SELECT width_bucket(dist_mrt_m, {edges}) AS bucket,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm) AS latest_median_psm,
               count(*) AS latest_sales
        FROM marts.resale_features
        WHERE month > (SELECT max(month) FROM marts.resale_features) - interval '12 months'
          AND dist_mrt_m IS NOT NULL
        GROUP BY 1
        ORDER BY 1
        """,
    )
    df = relative.merge(latest, on="bucket", how="left")
    labels = bucket_labels(MRT_BUCKET_EDGES_M)
    df["distance"] = [labels[b - 1] for b in df["bucket"]]
    return df


def feature_sample(engine: Engine, size: int = FEATURE_SAMPLE_SIZE) -> pd.DataFrame:
    """A fixed pseudo-random sample of sales since RECENT_FROM (ordered by a hash of txn_id,
    so the same data gives the same sample) with numeric features and log price."""
    numeric = [f for f in ASSOCIATION_FEATURES if f != "flat_type_rank"]
    columns = ", ".join(f"{c}::float8 AS {c}" for c in numeric)
    df = _read(
        engine,
        f"""
        SELECT flat_type, {columns}, ln(resale_price)::float8 AS log_price
        FROM marts.resale_features
        WHERE month >= :recent_from
        ORDER BY md5(txn_id)
        LIMIT :size
        """,
        recent_from=RECENT_FROM,
        size=size,
    )
    df["flat_type_rank"] = df["flat_type"].map({t: i for i, t in enumerate(FLAT_TYPE_ORDER)})
    return df


def feature_associations(sample: pd.DataFrame, seed: int = 0) -> pd.DataFrame:
    """Spearman correlation and mutual information (nats) of each feature with log price,
    sorted by mutual information."""
    from sklearn.feature_selection import mutual_info_regression

    features = [f for f in ASSOCIATION_FEATURES if f in sample.columns]
    x = sample[features].astype(float)
    y = sample["log_price"].astype(float)
    spearman = x.corrwith(y, method="spearman")
    discrete = [f in DISCRETE_FEATURES for f in features]
    mi = mutual_info_regression(
        x.to_numpy(), y.to_numpy(), discrete_features=discrete, random_state=seed
    )
    out = pd.DataFrame(
        {"feature": features, "spearman": spearman.to_numpy(), "mutual_info": np.asarray(mi)}
    )
    return out.sort_values("mutual_info", ascending=False).reset_index(drop=True)
