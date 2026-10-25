"""Staging transforms: SQL parsing functions, and Phase 2 acceptance checks on live data.

Run with `make test-db`. The live-data checks skip when nothing has been ingested yet and
fail if raw data exists but `make transform` has not been run.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from config.settings import get_settings
from pipeline.common.db import get_engine
from pipeline.common.sql_runner import run_sql_files

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    get_settings.cache_clear()
    eng = get_engine()
    run_sql_files(eng, "0*.sql")
    run_sql_files(eng, "100_*.sql")  # functions only: idempotent CREATE OR REPLACE
    yield eng
    eng.dispose()


def scalar(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


# --- parsing functions -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "months"),
    [
        ("61 years 04 months", 736),
        ("61 years 01 month", 733),
        ("66 years 0 months", 792),
        ("61 years", 732),
        ("1 year", 12),
        ("70", 840),
        ("  61   years 04 months ", 736),
        (None, None),
        ("", None),
    ],
)
def test_parse_remaining_lease(engine: Engine, value: str | None, months: int | None) -> None:
    assert scalar(engine, "SELECT staging.parse_remaining_lease_months(:v)", v=value) == months


@pytest.mark.parametrize("value", ["61 yrs", "about 61 years", "61.5", "61 years 4"])
def test_parse_remaining_lease_rejects_unknown_shapes(engine: Engine, value: str) -> None:
    with pytest.raises(DBAPIError, match="unrecognised remaining_lease"):
        scalar(engine, "SELECT staging.parse_remaining_lease_months(:v)", v=value)


def test_parse_storey_range(engine: Engine) -> None:
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM staging.parse_storey_range('10 TO 12')")).one()
    assert tuple(row) == (10, 12)


@pytest.mark.parametrize("value", ["10-12", "12 TO 10", "", None])
def test_parse_storey_range_rejects_bad_values(engine: Engine, value: str | None) -> None:
    with pytest.raises(DBAPIError, match="unrecognised storey_range"):
        scalar(engine, "SELECT lo FROM staging.parse_storey_range(:v)", v=value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2017-01", "2017-01-01"), (" 1990-12 ", "1990-12-01")],
)
def test_parse_month(engine: Engine, value: str, expected: str) -> None:
    assert str(scalar(engine, "SELECT staging.parse_month(:v)", v=value)) == expected


@pytest.mark.parametrize("value", ["2017-1", "2017-13", "Jan 2017"])
def test_parse_month_rejects_bad_values(engine: Engine, value: str) -> None:
    with pytest.raises(DBAPIError):
        scalar(engine, "SELECT staging.parse_month(:v)", v=value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("  ANG  MO KIO\tAVE 3 ", "ANG MO KIO AVE 3"), ("   ", None), (None, None)],
)
def test_norm_text(engine: Engine, value: str | None, expected: str | None) -> None:
    assert scalar(engine, "SELECT staging.norm_text(:v)", v=value) == expected


# --- Phase 2 acceptance on live data ------------------------------------------------------


@pytest.fixture(scope="module")
def live(engine: Engine) -> Engine:
    has_raw = scalar(engine, "SELECT to_regclass('raw.hdb_resale') IS NOT NULL")
    if not has_raw or not scalar(engine, "SELECT EXISTS (SELECT 1 FROM raw.hdb_resale)"):
        pytest.skip("no raw data ingested yet (run `make ingest`)")
    assert scalar(engine, "SELECT to_regclass('staging.hdb_resale') IS NOT NULL"), (
        "raw data exists but staging is missing: run `make transform`"
    )
    return engine


def test_row_count_matches_raw(live: Engine) -> None:
    raw = scalar(live, "SELECT count(*) FROM raw.hdb_resale")
    assert scalar(live, "SELECT count(*) FROM staging.hdb_resale") == raw


@pytest.mark.parametrize(
    "column",
    ["month", "town", "flat_type", "floor_area_sqm", "resale_price", "remaining_lease_months"],
)
def test_required_columns_not_null(live: Engine, column: str) -> None:
    assert scalar(live, f"SELECT count(*) FROM staging.hdb_resale WHERE {column} IS NULL") == 0


def test_remaining_lease_in_range(live: Engine) -> None:
    assert (
        scalar(
            live,
            "SELECT count(*) FROM staging.hdb_resale "
            "WHERE remaining_lease_months NOT BETWEEN 0 AND 1188",
        )
        == 0
    )


def test_flat_type_spellings_unified(live: Engine) -> None:
    assert (
        scalar(live, "SELECT count(*) FROM staging.hdb_resale WHERE flat_type = 'MULTI GENERATION'")
        == 0
    )


def test_one_spelling_per_flat_model(live: Engine) -> None:
    assert (
        scalar(
            live,
            "SELECT count(*) FROM (SELECT upper(flat_model) FROM staging.hdb_resale "
            "GROUP BY 1 HAVING count(DISTINCT flat_model) > 1) AS t",
        )
        == 0
    )
    assert scalar(live, "SELECT count(*) FROM staging.hdb_resale WHERE flat_model = 'DBSS'") > 0


def test_date_basis_switches_in_march_2012(live: Engine) -> None:
    rows = scalar(
        live,
        "SELECT count(*) FROM staging.hdb_resale WHERE "
        "(month < DATE '2012-03-01') <> (date_basis = 'approval')",
    )
    assert rows == 0


def test_address_key_is_normalised(live: Engine) -> None:
    assert (
        scalar(
            live,
            "SELECT count(*) FROM staging.hdb_resale "
            "WHERE address_key <> upper(address_key) OR address_key ~ '\\s\\s|^\\s|\\s$'",
        )
        == 0
    )


@pytest.mark.parametrize(
    ("staging_table", "raw_table"),
    [
        ("mrt_exits", "ref_mrt_exits"),
        ("planning_areas", "ref_planning_areas"),
        ("hawker_centres", "ref_hawker_centres"),
        ("parks", "ref_parks"),
        ("schools", "ref_schools"),
        ("hdb_block_info", "ref_hdb_block_info"),
        ("resale_price_index", "ref_resale_price_index"),
    ],
)
def test_reference_row_counts(live: Engine, staging_table: str, raw_table: str) -> None:
    raw = scalar(live, f"SELECT count(*) FROM raw.{raw_table}")
    assert scalar(live, f"SELECT count(*) FROM staging.{staging_table}") == raw


@pytest.mark.parametrize("table", ["mrt_exits", "planning_areas", "hawker_centres", "parks"])
def test_reference_geometry_valid_svy21_in_singapore(live: Engine, table: str) -> None:
    # SVY21 extent of Singapore, generous margin (metres).
    bad = scalar(
        live,
        f"SELECT count(*) FROM staging.{table} WHERE ST_SRID(geom) <> 3414 OR NOT ST_IsValid(geom) "
        "OR NOT ST_Within(geom, ST_MakeEnvelope(0, 15000, 60000, 55000, 3414))",
    )
    assert bad == 0


def test_school_postal_codes_have_six_digits(live: Engine) -> None:
    assert scalar(live, "SELECT count(*) FROM staging.schools WHERE postal_code !~ '^\\d{6}$'") == 0
