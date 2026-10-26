"""The CLI's feature builder (`pipeline.train.predict.build_features`) reproduces the training
features: recorded sales rebuilt from address, flat type, area and storey match their rows
in marts.resale_features.

Run with `make test-db` after `make features`.
"""

from __future__ import annotations

import math
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text

from config.settings import get_settings
from pipeline.common.db import get_engine
from pipeline.train.dataset import CATEGORICAL, NUMERIC
from pipeline.train.predict import Flat, build_features

pytestmark = pytest.mark.db

N_SALES = 25
# flat_model is chosen as the block's most common model, not the sale's own.
COMPARED = [c for c in (*CATEGORICAL, *NUMERIC) if c != "flat_model"]
# HDB's reported remaining lease is rounded per sale: two sales in one building a month
# apart can differ by 0 or 2 months, so a figure carried forward is within a month.
TOLERANCE = {"remaining_lease_months": 1.0}


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    get_settings.cache_clear()
    engine = get_engine()
    with engine.connect() as conn:
        built = conn.execute(
            text("SELECT to_regclass('marts.resale_features') IS NOT NULL")
        ).scalar_one()
    if not built:
        engine.dispose()
        pytest.skip("features not built yet (run `make features`)")
    yield engine
    engine.dispose()


def test_rebuilt_features_match_marts(engine: Engine) -> None:
    # Recent sales whose lease start year is the address's latest one (the CLI prices the
    # current building), sampled deterministically.
    sql = f"""
        WITH latest AS (
            SELECT DISTINCT ON (address_key) address_key, lease_commence_year
            FROM staging.hdb_resale ORDER BY address_key, month DESC, txn_id
        )
        SELECT f.block, f.street_name, f.flat_type, f.floor_area_sqm::float8 AS area,
               f.month, {", ".join(f"f.{c}" for c in COMPARED)}
        FROM marts.resale_features AS f
        JOIN latest AS l USING (address_key, lease_commence_year)
        WHERE f.month >= (SELECT max(month) FROM staging.hdb_resale) - interval '6 months'
        ORDER BY md5(f.txn_id)
        LIMIT {N_SALES}
    """
    with engine.connect() as conn:
        sales = conn.execute(text(sql)).mappings().all()
        assert len(sales) == N_SALES
        for sale in sales:
            # storey_mid is the middle of the sale's range, so pricing that storey reproduces it.
            flat = Flat(
                sale["block"], sale["street_name"], sale["flat_type"], sale["area"],
                int(sale["storey_mid"]),
            )  # fmt: skip
            features, month = build_features(conn, flat, month=sale["month"])
            assert month == sale["month"]
            for col in COMPARED:
                want, got = sale[col], features[col]
                if col in CATEGORICAL:
                    assert got == want, (flat.address_key, col)
                elif want is None:
                    assert got is None, (flat.address_key, col, got)
                else:
                    assert got is not None, (flat.address_key, col)
                    tol = TOLERANCE.get(col, 1e-9)
                    assert math.isclose(float(got), float(want), rel_tol=1e-9, abs_tol=tol), (
                        flat.address_key,
                        col,
                        got,
                        want,
                    )
