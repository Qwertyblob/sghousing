"""Leak tests for marts.rolling_features (pipeline/sql/205_marts_rolling_features.sql).

Run with `make test-db` after `make features`.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, text

from config.settings import get_settings
from pipeline.common.db import get_engine

pytestmark = pytest.mark.db

SQL_PATH = Path(__file__).parent.parent / "pipeline" / "sql" / "205_marts_rolling_features.sql"
CUTOFF = date(2020, 6, 1)
NEXT_MONTH = date(2020, 7, 1)
FEATURES = "town_psm_3m, town_psm_6m, town_psm_12m, block_psm_24m, block_sales_24m"


@pytest.fixture(scope="module")
def live() -> Iterator[Engine]:
    get_settings.cache_clear()
    engine = get_engine()
    with engine.connect() as conn:
        built = conn.execute(
            text("SELECT to_regclass('marts.rolling_features') IS NOT NULL")
        ).scalar_one()
    if not built:
        engine.dispose()
        pytest.skip("rolling features not built yet (run `make features`)")
    yield engine
    engine.dispose()


def features_between(conn: Connection, start: date, end: date) -> dict[str, tuple[Any, ...]]:
    rows = conn.execute(
        text(
            f"SELECT f.txn_id, {FEATURES} FROM marts.rolling_features AS f "
            "JOIN staging.hdb_resale AS r USING (txn_id) "
            "WHERE r.month BETWEEN :start AND :end"
        ),
        {"start": start, "end": end},
    ).all()
    return {row[0]: tuple(row[1:]) for row in rows}


def test_later_prices_do_not_change_earlier_features(live: Engine) -> None:
    """Multiply every price from CUTOFF on by 10 and rebuild with the real SQL file: features
    of sales up to and including CUTOFF's month must not move, while the next month's must."""
    with live.connect() as conn:
        before = features_between(conn, date(2019, 6, 1), CUTOFF)
        later_before = features_between(conn, NEXT_MONTH, NEXT_MONTH)
        # Everything below runs in the connection's open transaction and is rolled back.
        try:
            conn.execute(
                text(
                    "UPDATE staging.hdb_resale SET price_psm = price_psm * 10, "
                    "resale_price = resale_price * 10 WHERE month >= :cutoff"
                ),
                {"cutoff": CUTOFF},
            )
            conn.exec_driver_sql(SQL_PATH.read_text(encoding="utf-8"))
            after = features_between(conn, date(2019, 6, 1), CUTOFF)
            later_after = features_between(conn, NEXT_MONTH, NEXT_MONTH)
        finally:
            conn.rollback()
    assert len(before) > 10_000
    assert after == before
    # The perturbation does reach the following month, so the test can detect a leak.
    changed = sum(later_after[k][0] != later_before[k][0] for k in later_before)
    assert changed > 0.9 * len(later_before)


def test_town_window_is_the_pooled_median_of_earlier_months(live: Engine) -> None:
    with live.connect() as conn:
        txn_id, town, flat_type, month, stored = conn.execute(
            text(
                "SELECT f.txn_id, r.town, r.flat_type, r.month, f.town_psm_3m "
                "FROM marts.rolling_features AS f JOIN staging.hdb_resale AS r USING (txn_id) "
                "WHERE r.month = DATE '2023-05-01' AND r.town = 'TAMPINES' "
                "AND r.flat_type = '4 ROOM' ORDER BY f.txn_id LIMIT 1"
            )
        ).one()
        prices = conn.execute(
            text(
                "SELECT price_psm FROM staging.hdb_resale WHERE town = :town "
                "AND flat_type = :ft AND month >= :month - interval '3 months' "
                "AND month < :month"
            ),
            {"town": town, "ft": flat_type, "month": month},
        ).scalars()
        expected = statistics.median(float(p) for p in prices)
    assert stored == pytest.approx(expected), txn_id
