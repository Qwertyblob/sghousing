"""Price one flat with the exported ONNX models (`python -m pipeline.cli predict`).

The flat is described by its address, flat type, floor area and storey. Everything else the
model needs is looked up the way the training rows were built:

- town and lease start year: the latest resale at the address
- remaining lease: HDB's reported figure at that latest resale (counted from the actual lease
  start month), less the months since; the staging formula when none was reported
- flat model: the most common one for that flat type at the address, else in the town
- location and block features: `marts.block_features` and the block's building generation
- storey_mid: the middle of the 3-storey range the storey falls in (e.g. 10 -> "10 TO 12"
  -> 11), as every sale since 2015 is recorded in such ranges
- the sale is priced in the month after the latest month of data; lease, age, month_index
  and the rolling medians are computed for that month, from sales strictly before it
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Connection, text

from pipeline.train import export_onnx
from pipeline.train.dataset import CATEGORICAL, FEATURES, encode
from pipeline.train.train_lgbm import ordered_quantiles

logger = logging.getLogger(__name__)

LEASE_YEARS = 99
STOREY_RANGE = 3
MIN_BLOCK_SALES = 3
BLOCK_COLUMNS = (
    "lat",
    "lon",
    "planning_area",
    "region",
    "dist_mrt_m",
    "n_mrt_exits_500m",
    "n_primary_schools_1km",
    "dist_nearest_primary_m",
    "dist_hawker_m",
    "dist_park_m",
    "dist_cbd_m",
)


class UnknownAddressError(LookupError):
    """No resale is recorded at the requested address."""


@dataclass(frozen=True)
class Flat:
    block: str
    street: str
    flat_type: str
    floor_area_sqm: float
    storey: int

    @property
    def address_key(self) -> str:
        return f"{normalise(self.block)} {normalise(self.street)}"


def normalise(value: str) -> str:
    """Upper case with single spaces, as staging.norm_text and upper() store addresses."""
    return " ".join(value.split()).upper()


def sql_round(numerator: float, denominator: float, places: int) -> float:
    """numerator / denominator rounded like Postgres `round(numeric, places)`: exact decimal
    division, halves away from zero (Python's round() on floats rounds 0.3125 to 0.312)."""
    exact = Decimal(str(numerator)) / Decimal(str(denominator))
    return float(exact.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def storey_mid(storey: int) -> int:
    """Middle of the 3-storey range ("01 TO 03", "04 TO 06", ...) containing `storey`."""
    if storey < 1:
        raise ValueError(f"storey must be at least 1, got {storey}")
    low = (storey - 1) // STOREY_RANGE * STOREY_RANGE + 1
    return low + STOREY_RANGE // 2


def next_month(month: date) -> date:
    return date(month.year + month.month // 12, month.month % 12 + 1, 1)


def month_index(month: date) -> int:
    """Months since January 1990 (as in marts.resale_features)."""
    return (month.year - 1990) * 12 + month.month - 1


def months_between(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month


def lease_features(
    lease_commence_year: int, month: date, reported: tuple[int, date] | None = None
) -> dict[str, float]:
    """Remaining lease and flat age at `month`, as staging computes them.

    `reported` is a remaining lease HDB reported for this building and the month of that
    sale; staging prefers reported figures, so it is carried forward to `month` when given.
    """
    months = max(0, (month.year - lease_commence_year) * 12 + month.month - 1)
    remaining = LEASE_YEARS * 12 - months
    if reported is not None:
        reported_months, reported_month = reported
        remaining = reported_months - months_between(reported_month, month)
    return {
        "remaining_lease_months": float(min(max(remaining, 0), LEASE_YEARS * 12)),
        "flat_age_years": sql_round(months, 12, 2),
    }


def relative_storey(mid: int, max_floor: int) -> float:
    """storey_mid capped at the block's top floor, as a share of it (as in 210)."""
    return sql_round(min(mid, max_floor), max_floor, 3)


def _one(conn: Connection, sql: str, **params: Any) -> dict[str, Any] | None:
    row = conn.execute(text(sql), params).mappings().first()
    return dict(row) if row else None


def _street_blocks(conn: Connection, street: str) -> list[str]:
    rows = conn.execute(
        text("SELECT DISTINCT block FROM staging.hdb_resale WHERE street_name = :street"),
        {"street": street},
    )
    return sorted((r[0] for r in rows), key=lambda b: (len(b), b))


def build_features(
    conn: Connection, flat: Flat, month: date | None = None
) -> tuple[dict[str, Any], date]:
    """The model's raw feature values for `flat`, and the month it is priced in (by default
    the month after the latest data; tests pass a past month to rebuild recorded sales)."""
    address = flat.address_key
    flat_type = normalise(flat.flat_type)
    latest = _one(
        conn,
        """SELECT town, lease_commence_year, month, remaining_lease_months,
                  remaining_lease_basis
           FROM staging.hdb_resale
           WHERE address_key = :address ORDER BY month DESC, txn_id LIMIT 1""",
        address=address,
    )
    if latest is None:
        blocks = _street_blocks(conn, normalise(flat.street))
        hint = (
            f"blocks on {normalise(flat.street)} with sales: {', '.join(blocks)}"
            if blocks
            else "no sales on that street either; check the street name"
        )
        raise UnknownAddressError(f"no resale recorded at {address!r} ({hint})")
    if month is None:
        last = conn.execute(text("SELECT max(month) FROM staging.hdb_resale")).scalar_one()
        month = next_month(last)

    model = _one(
        conn,
        """SELECT coalesce(
               (SELECT mode() WITHIN GROUP (ORDER BY flat_model) FROM staging.hdb_resale
                WHERE address_key = :address AND flat_type = :flat_type),
               (SELECT mode() WITHIN GROUP (ORDER BY flat_model) FROM staging.hdb_resale
                WHERE town = :town AND flat_type = :flat_type)) AS flat_model""",
        address=address,
        flat_type=flat_type,
        town=latest["town"],
    )
    block = _one(
        conn,
        f"SELECT {', '.join(BLOCK_COLUMNS)} FROM marts.block_features WHERE address_key = :a",
        a=address,
    ) or dict.fromkeys(BLOCK_COLUMNS)
    building = _one(
        conn,
        """SELECT block_max_floor, block_year_completed FROM marts.block_generations
           WHERE address_key = :address AND :year BETWEEN lease_year_from AND lease_year_to""",
        address=address,
        year=latest["lease_commence_year"],
    )
    if building is None:
        raise UnknownAddressError(f"no building generation for {address!r}")
    rolling = _one(
        conn,
        """SELECT
               percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm)
                   FILTER (WHERE town = :town AND flat_type = :flat_type
                           AND month >= :month - interval '3 months') AS town_psm_3m,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm)
                   FILTER (WHERE town = :town AND flat_type = :flat_type
                           AND month >= :month - interval '6 months') AS town_psm_6m,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm)
                   FILTER (WHERE town = :town AND flat_type = :flat_type
                           AND month >= :month - interval '12 months') AS town_psm_12m,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm)
                   FILTER (WHERE address_key = :address
                           AND month >= :month - interval '24 months') AS block_psm_24m,
               count(*) FILTER (WHERE address_key = :address
                                AND month >= :month - interval '24 months') AS block_sales_24m
           FROM staging.hdb_resale
           WHERE month >= :month - interval '24 months' AND month < :month""",
        town=latest["town"],
        flat_type=flat_type,
        address=address,
        month=month,
    )
    assert rolling is not None
    if rolling["block_sales_24m"] < MIN_BLOCK_SALES:
        rolling["block_psm_24m"] = None

    reported = None
    if latest["remaining_lease_basis"] == "reported":
        reported = (latest["remaining_lease_months"], latest["month"])
    mid = storey_mid(flat.storey)
    features: dict[str, Any] = {
        "town": latest["town"],
        "flat_type": flat_type,
        "flat_model": model["flat_model"] if model else None,
        "floor_area_sqm": flat.floor_area_sqm,
        "storey_mid": mid,
        "rel_storey": relative_storey(mid, building["block_max_floor"]),
        "month_index": month_index(month),
        "lease_commence_year": latest["lease_commence_year"],
        **lease_features(latest["lease_commence_year"], month, reported),
        **block,
        **building,
        **rolling,
    }
    return features, month


def encode_one(features: dict[str, Any], maps: dict[str, dict[str, int]]) -> pd.DataFrame:
    """One model input row; logs categories the model never saw (they become missing)."""
    for col in CATEGORICAL:
        value = features.get(col)
        if value is not None and value not in maps[col]:
            logger.warning("%s %r was not in the training data; treated as missing", col, value)
    row = pd.DataFrame([{name: features.get(name) for name in FEATURES}])
    for col in FEATURES:
        if col not in CATEGORICAL:
            row[col] = pd.to_numeric(row[col], errors="coerce")
    return encode(row, maps)


def predict(directory: Path, x: pd.DataFrame) -> dict[str, float]:
    """Price quantiles in dollars for one row, sorted so p10 <= p50 <= p90."""
    spec = json.loads((directory / "features.json").read_text(encoding="utf-8"))
    raw = {
        name: np.exp(export_onnx.run_onnx(directory / file, x))
        for name, file in spec["outputs"].items()
    }
    if not all(np.isfinite(v).all() for v in raw.values()):
        raise ValueError(f"model returned a non-finite price: {raw}")
    return {name: float(v[0]) for name, v in ordered_quantiles(raw).items()}
