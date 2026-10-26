"""Loading snapshots into Postgres, in a throwaway schema. Run with `make test-db`."""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from config.settings import get_settings
from pipeline.common.db import get_engine
from pipeline.ingest.load import load_snapshot
from pipeline.ingest.parsers import ParseError
from pipeline.ingest.snapshots import Snapshot
from tests.conftest import FIXTURES

pytestmark = pytest.mark.db

MakeSnapshot = Callable[..., Snapshot]


@pytest.fixture
def engine() -> Iterator[Engine]:
    get_settings.cache_clear()
    eng = get_engine()
    yield eng
    eng.dispose()


@pytest.fixture
def schema(engine: Engine) -> Iterator[str]:
    name = f"test_ingest_{uuid.uuid4().hex[:10]}"
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{name}"'))
    yield name
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{name}" CASCADE'))


def fetch(engine: Engine, sql: str) -> list[tuple]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql))]


def columns(engine: Engine, schema: str, table: str) -> list[str]:
    return [
        r[0]
        for r in fetch(
            engine,
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_schema = '{schema}' AND table_name = '{table}' ORDER BY ordinal_position",
        )
    ]


def test_reload_is_idempotent(engine: Engine, schema: str, make_snapshot: MakeSnapshot) -> None:
    snap = make_snapshot(FIXTURES / "resale_2017.csv", "d_a")
    first = load_snapshot(engine, snap, table="hdb_resale", schema=schema)
    second = load_snapshot(engine, snap, table="hdb_resale", schema=schema)
    assert (first.status, first.row_count) == ("loaded", 2)
    assert second.status == "skipped"
    assert fetch(engine, f'SELECT count(*) FROM "{schema}".hdb_resale') == [(2,)]
    assert fetch(engine, f'SELECT dataset_id, row_count FROM "{schema}".ingestion_log') == [
        ("d_a", 2)
    ]


def test_changed_content_replaces_rows(
    engine: Engine, schema: str, make_snapshot: MakeSnapshot, tmp_path: Path
) -> None:
    v1 = tmp_path / "v1.csv"
    shutil.copy(FIXTURES / "resale_1990.csv", v1)
    v2 = tmp_path / "v2.csv"
    v2.write_text("".join(v1.read_text().splitlines(keepends=True)[:2]))  # header + 1 row
    load_snapshot(engine, make_snapshot(v1, "d_a"), table="t", schema=schema)
    result = load_snapshot(engine, make_snapshot(v2, "d_a"), table="t", schema=schema)
    assert result.row_count == 1
    assert fetch(engine, f'SELECT count(*) FROM "{schema}".t') == [(1,)]
    assert fetch(engine, f'SELECT row_count FROM "{schema}".ingestion_log ORDER BY id') == [
        (3,),
        (1,),
    ]


@pytest.mark.parametrize(
    ("name", "content", "fmt"),
    [
        ("header_only.csv", None, "CSV"),
        ("empty.geojson", '{"type": "FeatureCollection", "features": []}', "GEOJSON"),
    ],
)
def test_empty_refresh_keeps_existing_rows(
    engine: Engine,
    schema: str,
    make_snapshot: MakeSnapshot,
    tmp_path: Path,
    name: str,
    content: str | None,
    fmt: str,
) -> None:
    if fmt == "CSV":
        v1 = tmp_path / "v1.csv"
        shutil.copy(FIXTURES / "resale_1990.csv", v1)
        content = v1.read_text().splitlines(keepends=True)[0]  # the header line only
    else:
        v1 = tmp_path / "v1.geojson"
        v1.write_text(
            '{"type": "FeatureCollection", "features": [{"type": "Feature", '
            '"properties": {"name": "a"}, "geometry": {"type": "Point", "coordinates": [0, 0]}}]}'
        )
    empty = tmp_path / name
    empty.write_text(content)
    load_snapshot(engine, make_snapshot(v1, "d_a", fmt=fmt), table="t", schema=schema)
    with pytest.raises(ParseError, match="no data rows"):
        load_snapshot(engine, make_snapshot(empty, "d_a", fmt=fmt), table="t", schema=schema)
    expected = 3 if fmt == "CSV" else 1
    assert fetch(engine, f'SELECT count(*) FROM "{schema}".t') == [(expected,)]
    assert fetch(engine, f'SELECT row_count FROM "{schema}".ingestion_log') == [(expected,)]


def test_column_sets_merge_across_datasets(
    engine: Engine, schema: str, make_snapshot: MakeSnapshot
) -> None:
    load_snapshot(
        engine, make_snapshot(FIXTURES / "resale_1990.csv", "d_old"), table="t", schema=schema
    )
    load_snapshot(
        engine, make_snapshot(FIXTURES / "resale_2017.csv", "d_new"), table="t", schema=schema
    )
    assert "remaining_lease" in columns(engine, schema, "t")
    rows = fetch(
        engine,
        f'SELECT source_dataset_id, count(*), count(remaining_lease) FROM "{schema}".t '
        "GROUP BY 1 ORDER BY 1",
    )
    assert rows == [("d_new", 2, 2), ("d_old", 3, 0)]  # duplicates kept; old rows NULL


def test_failed_replacement_rolls_back_everything(
    engine: Engine, schema: str, make_snapshot: MakeSnapshot, tmp_path: Path
) -> None:
    v1 = tmp_path / "v1.csv"
    v1.write_text("a,b\n1,2\n3,4\n")
    load_snapshot(engine, make_snapshot(v1, "d_a"), table="t", schema=schema)
    rows_before = fetch(engine, f'SELECT * FROM "{schema}".t ORDER BY source_row_number')
    log_before = fetch(engine, f'SELECT * FROM "{schema}".ingestion_log ORDER BY id')
    columns_before = columns(engine, schema, "t")

    # New column (DDL) + DELETE + first COPY row all happen before the ragged record fails.
    v2 = tmp_path / "v2.csv"
    v2.write_text("a,b,extra\n5,6,7\n8,9\n10,11,12\n")
    with pytest.raises(ParseError, match="record 2"):
        load_snapshot(engine, make_snapshot(v2, "d_a"), table="t", schema=schema)

    assert fetch(engine, f'SELECT * FROM "{schema}".t ORDER BY source_row_number') == rows_before
    assert fetch(engine, f'SELECT * FROM "{schema}".ingestion_log ORDER BY id') == log_before
    assert columns(engine, schema, "t") == columns_before


def test_values_stored_exactly(
    engine: Engine, schema: str, make_snapshot: MakeSnapshot, tmp_path: Path
) -> None:
    full = tmp_path / "full.csv"
    full.write_text('postal_code,name,note,blank\n018935,"Shop, A","line1\nline2",\n', newline="")
    partial = tmp_path / "partial.csv"
    partial.write_text("postal_code,name\n000123,B\n")
    load_snapshot(engine, make_snapshot(full, "d_full"), table="t", schema=schema)
    load_snapshot(engine, make_snapshot(partial, "d_partial"), table="t", schema=schema)
    rows = fetch(
        engine,
        f'SELECT source_dataset_id, postal_code, name, note, blank FROM "{schema}".t ORDER BY 1',
    )
    assert rows == [
        ("d_full", "018935", "Shop, A", "line1\nline2", ""),
        ("d_partial", "000123", "B", None, None),
    ]


def test_geojson_load(engine: Engine, schema: str, make_snapshot: MakeSnapshot) -> None:
    snap = make_snapshot(FIXTURES / "two_features.geojson", "d_geo", fmt="GEOJSON")
    assert load_snapshot(engine, snap, table="ref_parks", schema=schema).row_count == 2
    assert load_snapshot(engine, snap, table="ref_parks", schema=schema).status == "skipped"
    rows = fetch(
        engine,
        f"SELECT feature_index, properties->>'NAME', geometry_geojson::json->>'type' "
        f'FROM "{schema}".ref_parks ORDER BY 1',
    )
    assert rows == [(0, "Alpha Park", "Point"), (1, "Beta Park", "Polygon")]


def test_malformed_csv_does_not_replace_valid_rows(
    engine: Engine, schema: str, make_snapshot: MakeSnapshot, tmp_path: Path
) -> None:
    v1 = tmp_path / "v1.csv"
    v1.write_text("a,b\n1,2\n3,4\n")
    load_snapshot(engine, make_snapshot(v1, "d_a"), table="t", schema=schema)
    rows_before = fetch(engine, f'SELECT * FROM "{schema}".t ORDER BY source_row_number')
    log_before = fetch(engine, f'SELECT * FROM "{schema}".ingestion_log ORDER BY id')

    v2 = tmp_path / "v2.csv"
    v2.write_text('a,b\n5,"unterminated\n6,7\n', newline="")
    with pytest.raises(ParseError, match="malformed CSV"):
        load_snapshot(engine, make_snapshot(v2, "d_a"), table="t", schema=schema)

    assert fetch(engine, f'SELECT * FROM "{schema}".t ORDER BY source_row_number') == rows_before
    assert fetch(engine, f'SELECT * FROM "{schema}".ingestion_log ORDER BY id') == log_before
