"""Regression tests for the data quality checks on small fixture copies of the live tables.

Each test copies January-March 1990 into a throwaway schema, injects one fault, and runs the
checks against the copy via `Sources`. Run with `make test-db` after `make features`.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import Engine, text

from config.settings import get_settings
from pipeline.common.db import get_engine
from pipeline.quality.checks import (
    MissingInputError,
    Sources,
    check_feature_coverage,
    check_known_values,
    check_price_outliers,
    check_ranges,
    check_required_nulls,
    check_year_counts,
    run_checks,
)

pytestmark = pytest.mark.db

FIXTURE_END = "1990-04-01"


@pytest.fixture
def fixture() -> Iterator[tuple[Engine, Sources]]:
    get_settings.cache_clear()
    engine = get_engine()
    schema = f"qa_fixture_{uuid4().hex[:8]}"
    with engine.connect() as conn:
        built = conn.execute(
            text("SELECT to_regclass('marts.resale_features') IS NOT NULL")
        ).scalar_one()
    if not built:
        engine.dispose()
        pytest.skip("marts not built yet (run `make features`)")
    src = Sources(f"{schema}.hdb_resale", f"{schema}.geocode_cache", f"{schema}.resale_features")
    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(
            text(
                f"CREATE TABLE {src.resale} AS SELECT * FROM staging.hdb_resale "
                f"WHERE month < DATE '{FIXTURE_END}'"
            )
        )
        conn.execute(
            text(
                f"CREATE TABLE {src.features} AS SELECT * FROM marts.resale_features "
                f"WHERE month < DATE '{FIXTURE_END}'"
            )
        )
        conn.execute(
            text(
                f"CREATE TABLE {src.geocode_cache} AS SELECT c.* FROM staging.geocode_cache AS c "
                f"WHERE EXISTS (SELECT 1 FROM {src.resale} AS r "
                "WHERE r.address_key = c.address_key)"
            )
        )
    try:
        yield engine, src
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        engine.dispose()


def run_sql(engine: Engine, *statements: str) -> None:
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def test_unmodified_fixture_passes(fixture: tuple[Engine, Sources]) -> None:
    engine, src = fixture
    with engine.connect() as conn:
        for check in (
            check_year_counts,
            check_required_nulls,
            check_ranges,
            check_known_values,
            check_feature_coverage,
        ):
            result = check(conn, src)
            assert result.status == "pass", (result.name, result.summary)


def test_missing_month_fails_before_scaling(fixture: tuple[Engine, Sources]) -> None:
    # Scaling January and March to 12 months would hide that February is gone.
    engine, src = fixture
    run_sql(engine, f"DELETE FROM {src.resale} WHERE month = DATE '1990-02-01'")
    with engine.connect() as conn:
        result = check_year_counts(conn, src)
    assert result.status == "fail"
    assert "1990-02" in result.summary


def test_feature_coverage_is_checked_per_transaction(fixture: tuple[Engine, Sources]) -> None:
    """Faults that cancel out in column totals: one sale swapped for a foreign txn_id, one
    located sale losing `lat`, one located sale losing its geocode_status but keeping `lat`."""
    engine, src = fixture
    f = src.features
    run_sql(
        engine,
        f"CREATE TABLE {f}_swap AS SELECT * FROM {f} WHERE txn_id = (SELECT max(txn_id) FROM {f})",
        f"UPDATE {f}_swap SET txn_id = 'not-a-staged-sale'",
        f"DELETE FROM {f} WHERE txn_id = (SELECT min(txn_id) FROM {f})",
        f"INSERT INTO {f} SELECT * FROM {f}_swap",
        f"""UPDATE {f} SET lat = NULL WHERE txn_id = (
            SELECT min(txn_id) FROM {f} WHERE geocode_status IS NOT NULL)""",
        f"""UPDATE {f} SET geocode_status = NULL WHERE txn_id = (
            SELECT max(txn_id) FROM {f} WHERE geocode_status IS NOT NULL
              AND txn_id <> 'not-a-staged-sale')""",
    )
    with engine.connect() as conn:
        result = check_feature_coverage(conn, src)
    assert result.status == "fail"
    assert "1 staged sales missing from the mart and 1 mart rows not in staging" in result.summary
    assert "geocode_status disagrees" in result.summary
    assert "`lat` missing for 1 located sales" in result.summary
    assert "`lat` set on 1 unlocated sales" in result.summary


def test_geocode_status_must_match_the_cache(fixture: tuple[Engine, Sources]) -> None:
    # Still located, so a located/unlocated comparison would miss it.
    engine, src = fixture
    f = src.features
    run_sql(
        engine,
        f"""UPDATE {f}
            SET geocode_status = CASE geocode_status WHEN 'ok' THEN 'approx' ELSE 'ok' END
            WHERE txn_id = (SELECT min(txn_id) FROM {f} WHERE geocode_status IS NOT NULL)""",
    )
    with engine.connect() as conn:
        result = check_feature_coverage(conn, src)
    assert result.status == "fail"
    assert "1 sales whose geocode_status disagrees with the cache" in result.summary


@pytest.mark.parametrize(
    ("town", "area", "status"),
    [
        ("KALLANG/WHAMPOA", 350, "pass"),
        ("KALLANG/WHAMPOA", 5000, "fail"),
        ("KALLANG/WHAMPOA", 10, "fail"),
        ("QUEENSTOWN", 350, "fail"),
    ],
)
def test_terrace_exception_is_bounded(
    fixture: tuple[Engine, Sources], town: str, area: int, status: str
) -> None:
    engine, src = fixture
    run_sql(
        engine,
        f"""UPDATE {src.resale}
            SET flat_model = 'Terrace', town = '{town}', floor_area_sqm = {area}
            WHERE txn_id = (SELECT min(txn_id) FROM {src.resale})""",
    )
    with engine.connect() as conn:
        result = check_ranges(conn, src)
    assert result.status == status, result.summary


def test_missing_or_empty_inputs_raise(fixture: tuple[Engine, Sources]) -> None:
    engine, src = fixture
    absent = Sources(src.resale, src.geocode_cache, src.features + "_absent")
    with pytest.raises(MissingInputError, match="missing tables"):
        run_checks(engine, absent)
    run_sql(engine, f"DELETE FROM {src.resale}")
    with pytest.raises(MissingInputError, match="no sales"):
        run_checks(engine, src)


def test_no_price_outliers_is_valid(fixture: tuple[Engine, Sources]) -> None:
    # Spread prices evenly around each group's centre so no sale is 4 MADs out.
    engine, src = fixture
    run_sql(engine, f"UPDATE {src.resale} SET price_psm = 1000 + (source_row_number % 3)")
    with engine.connect() as conn:
        result = check_price_outliers(conn, src)
    assert result.status == "pass", result.summary
    assert result.summary.startswith("0 sales")
