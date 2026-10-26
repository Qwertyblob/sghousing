"""Data snapshots (`pipeline.snapshot`): Parquet types, reproducible archives, checksum
checks, and a database round trip of every supported column type, index and key.

The round trip runs with `make test-db`; it uses a scratch schema it drops afterwards.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest
from sqlalchemy import Engine, text

from config.settings import get_settings
from pipeline.common.db import get_engine
from pipeline.snapshot import (
    FORMAT_VERSION,
    MANIFEST,
    SnapshotError,
    arrow_type,
    export_table,
    pack_model,
    read_manifest,
    restore_table,
    sha256,
    verify,
)

SCRATCH = "snapshot_test"


def test_arrow_types() -> None:
    assert arrow_type("integer") == pa.int32()
    assert arrow_type("double precision") == pa.float64()
    assert arrow_type("numeric", 4) == pa.decimal128(38, 4)
    assert arrow_type("geometry(Point,3414)") == pa.binary()
    assert arrow_type("timestamp with time zone") == pa.timestamp("us", tz="UTC")
    with pytest.raises(SnapshotError, match="no Parquet mapping"):
        arrow_type("jsonb")


def test_pack_model_is_reproducible(tmp_path: Path) -> None:
    model = tmp_path / "2026-10"
    model.mkdir()
    (model / "b.json").write_text("{}")
    (model / "a.onnx").write_bytes(b"\x00\x01")
    first = pack_model(model, tmp_path / "one.tar.gz")
    (model / "a.onnx").touch()  # a new mtime must not change the archive
    second = pack_model(model, tmp_path / "two.tar.gz")
    assert first.read_bytes() == second.read_bytes()


def test_verify_reports_missing_and_changed_files(tmp_path: Path) -> None:
    (tmp_path / "t.parquet").write_bytes(b"data")
    manifest = {
        "format": FORMAT_VERSION,
        "tables": [{"file": "t.parquet", "sha256": sha256(tmp_path / "t.parquet")}],
        "model": {"file": "model.tar.gz", "sha256": "0" * 64},
    }
    with pytest.raises(SnapshotError, match="model.tar.gz: missing"):
        verify(tmp_path, manifest)
    (tmp_path / "model.tar.gz").write_bytes(b"x")
    with pytest.raises(SnapshotError, match="checksum mismatch"):
        verify(tmp_path, manifest)


def test_read_manifest_requires_a_snapshot(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError, match="gh release download"):
        read_manifest(tmp_path)
    (tmp_path / MANIFEST).write_text(json.dumps({"format": FORMAT_VERSION + 1}))
    with pytest.raises(SnapshotError, match="format"):
        read_manifest(tmp_path)


@pytest.fixture
def engine() -> Iterator[Engine]:
    get_settings.cache_clear()
    engine = get_engine()
    yield engine
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCRATCH} CASCADE"))
    engine.dispose()


@pytest.mark.db
def test_table_round_trip(engine: Engine, tmp_path: Path) -> None:
    table = f"{SCRATCH}.sample"
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCRATCH} CASCADE; CREATE SCHEMA {SCRATCH}"))
        conn.execute(
            text(f"""
            CREATE TABLE {table} (
                id integer, name text, price numeric, ratio double precision,
                month date, at timestamptz, ok boolean, n bigint,
                geom geometry(Point, 3414)
            );
            INSERT INTO {table} VALUES
                (1, 'Blk 1', 6268.66, 0.1, '2026-10-01', '2026-10-25 13:33:57.223189+00',
                 true, 9007199254740993, ST_SetSRID(ST_MakePoint(28000.5, 38000.25), 3414)),
                (2, NULL, 12.3456, NULL, NULL, NULL, NULL, NULL, NULL);
            ALTER TABLE {table} ADD PRIMARY KEY (id);
            CREATE INDEX sample_month_idx ON {table} (month);
            """)
        )
        before = conn.execute(text(f"SELECT *, geom::text FROM {table} ORDER BY id")).all()
        entry = export_table(conn, table, tmp_path / f"{table}.parquet")
    assert entry["rows"] == 2
    assert {i["name"] for i in entry["indexes"]} == {"sample_pkey", "sample_month_idx"}

    with engine.begin() as conn:
        raw = conn.connection.driver_connection
        with raw.cursor() as cur:
            restore_table(cur, tmp_path, entry)
    with engine.connect() as conn:
        after = conn.execute(text(f"SELECT *, geom::text FROM {table} ORDER BY id")).all()
        primary = (
            conn.execute(
                text("SELECT conname FROM pg_constraint WHERE conrelid = CAST(:t AS regclass)"),
                {"t": table},
            )
            .scalars()
            .all()
        )
        types = (
            conn.execute(
                text("""SELECT format_type(atttypid, atttypmod) FROM pg_attribute
                    WHERE attrelid = CAST(:t AS regclass) AND attnum > 0 ORDER BY attnum"""),
                {"t": table},
            )
            .scalars()
            .all()
        )
    assert after == before
    assert after[0].price == Decimal("6268.66")
    assert after[0].at == datetime(2026, 10, 25, 13, 33, 57, 223189, tzinfo=UTC)
    assert after[0].month == date(2026, 10, 1)
    assert primary == ["sample_pkey"]
    assert types[-1] == "geometry(Point,3414)"
