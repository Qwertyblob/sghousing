"""Site data export (`pipeline.export.site_data`): JSON helpers, the schemas themselves, and
blocks.json agreeing with the CLI's feature builder.

The database test runs with `make test-db` after `make features`; the exported files are
checked against the schemas by tests/test_site_data.py (`make export`).
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import Engine, text

from config.settings import get_settings
from pipeline.common.db import get_engine
from pipeline.export.site_data import blocks, display_label, dumps, last_month, town_slug
from pipeline.train.predict import Flat, build_features, next_month

SCHEMAS = Path(__file__).parents[1] / "site" / "src" / "schemas"
N_BLOCKS = 25
# Columns compared with the CLI's features, and the decimal places the export rounds to.
BLOCK_PLACES = {
    "lat": 6,
    "lon": 6,
    "planning_area": 0,
    "region": 0,
    "lease_commence_year": 0,
    "remaining_lease_months": 0,
    "block_max_floor": 0,
    "block_year_completed": 0,
    "dist_mrt_m": 6,
    "n_mrt_exits_500m": 0,
    "n_primary_schools_1km": 0,
    "dist_nearest_primary_m": 6,
    "dist_hawker_m": 6,
    "dist_park_m": 6,
    "dist_cbd_m": 6,
    "block_psm_24m": 2,
    "block_sales_24m": 0,
}


def schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


def test_dumps_is_minified_iso_and_rejects_nan() -> None:
    payload = {"a": [1, Decimal("2.50"), Decimal("3")], "m": date(2026, 10, 1)}
    assert dumps(payload) == '{"a":[1,2.5,3],"m":"2026-10-01"}'
    assert dumps(datetime(2026, 10, 25, 12, 0)) == '"2026-10-25T12:00:00"'
    with pytest.raises(ValueError):
        dumps(math.nan)
    with pytest.raises(TypeError):
        dumps(object())


@pytest.mark.parametrize(
    ("town", "slug"),
    [
        ("ANG MO KIO", "ang-mo-kio"),
        ("KALLANG/WHAMPOA", "kallang-whampoa"),
        ("CENTRAL AREA", "central-area"),
    ],
)
def test_town_slug(town: str, slug: str) -> None:
    assert town_slug(town) == slug


def test_display_label_keeps_numbers_and_block_suffix() -> None:
    assert display_label("406A", "ANG MO KIO AVE 10") == "Blk 406A Ang Mo Kio Ave 10"
    assert display_label("1", "ST. GEORGE'S RD") == "Blk 1 St. George's Rd"


@pytest.mark.parametrize("path", sorted(SCHEMAS.glob("*.schema.json")), ids=lambda p: p.stem)
def test_schemas_are_valid_draft_2020_12(path: Path) -> None:
    Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_columnar_schema_rejects_short_rows_and_renamed_columns() -> None:
    validator = Draft202012Validator(schema("town_month"))
    good = {
        "from": "2000-01",
        "columns": ["town", "flat_type", "month", "sales", "median_psm", "median_price"],
        "rows": [["ANG MO KIO", "ALL", "2000-01", 120, 2100.5, 180000]],
    }
    assert validator.is_valid(good)
    assert not validator.is_valid({**good, "rows": [["ANG MO KIO", "ALL", "2000-01", 120]]})
    assert not validator.is_valid({**good, "rows": [["ANG MO KIO", "ALL", "2000-1", 1, 1, 1]]})
    assert not validator.is_valid({**good, "columns": [*good["columns"][:-1], "price"]})


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    get_settings.cache_clear()
    engine = get_engine()
    with engine.connect() as conn:
        built = conn.execute(
            text("SELECT to_regclass('marts.block_features') IS NOT NULL")
        ).scalar_one()
    if not built:
        engine.dispose()
        pytest.skip("features not built yet (run `make features`)")
    yield engine
    engine.dispose()


def _close(actual: Any, expected: Any, places: int) -> bool:
    if actual is None or expected is None:
        return actual is None and expected is None
    if isinstance(expected, str):
        return actual == expected
    return abs(float(actual) - float(expected)) <= 0.6 * 10**-places


@pytest.mark.db
def test_blocks_match_cli_features(engine: Engine) -> None:
    """Each sampled block's row, and its town's rolling medians, are what the CLI feeds the
    model for that block, up to the rounding the export applies."""
    with engine.connect() as conn:
        as_of = next_month(last_month(conn))
        data = blocks(conn, as_of)
        rows = [dict(zip(data["columns"], row, strict=True)) for row in data["rows"]]
        for row in rows[:: max(1, len(rows) // N_BLOCKS)][:N_BLOCKS]:
            flat_type = sorted(row["flat_models"])[0]
            flat = Flat(row["block"], row["street_name"], flat_type, 90.0, 5)
            features, month = build_features(conn, flat)
            where = row["address_key"]
            assert month == as_of
            assert row["town"] == features["town"], where
            assert row["flat_models"][flat_type] == features["flat_model"], where
            for col, places in BLOCK_PLACES.items():
                assert _close(row[col], features[col], places), (where, col, row[col])
            rolling = data["town_rolling"].get(row["town"], {}).get(flat_type, [None] * 3)
            for value, col in zip(rolling, data["town_rolling_columns"], strict=True):
                assert _close(value, features[col], 2), (where, col, value, features[col])
