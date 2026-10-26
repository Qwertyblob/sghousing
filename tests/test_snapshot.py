"""Data snapshots (`pipeline.snapshot`): Parquet types, reproducible archives, checksum
checks, a database round trip of every supported column type, constraint and index, and
what a restore changes, or leaves alone when it fails.

The database tests run with `make test-db`; they use a scratch schema they drop afterwards.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest
from sqlalchemy import Connection, Engine, text

from config.settings import Settings, get_settings
from pipeline import snapshot
from pipeline.common.db import get_engine
from pipeline.export import site_data
from pipeline.snapshot import (
    FORMAT_VERSION,
    MANIFEST,
    MODEL_FILES,
    SnapshotError,
    arrow_type,
    export_table,
    pack_model,
    read_manifest,
    restore,
    restore_table,
    sha256,
    unpack_model,
    verify,
)
from pipeline.train.train_lgbm import LATEST_FILE

SCRATCH = "snapshot_test"
SAMPLE = f"{SCRATCH}.sample"
CREDIT = {
    "source": "hdb_resale",
    "dataset_id": "d_test",
    "name": "Test dataset",
    "url": "https://data.gov.sg/datasets/d_test/view",
    "last_updated": "2026-10-25T10:06:44+08:00",
    "retrieved": "2026-10-25T13:31:48.374191+00:00",
}


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


def _model(directory: Path, files: tuple[str, ...] = MODEL_FILES) -> Path:
    model = directory / "2026-10"
    model.mkdir(parents=True)
    for name in files:
        (model / name).write_text("{}")
    return model


def test_unpack_model_requires_the_files_the_export_reads(tmp_path: Path) -> None:
    entry = {"file": "model.tar.gz", "version": "2026-10"}
    pack_model(_model(tmp_path / "partial", ("features.json",)), tmp_path / entry["file"])
    with pytest.raises(SnapshotError, match="model_p10.onnx"):
        unpack_model(tmp_path, entry, tmp_path / "one")
    pack_model(_model(tmp_path / "complete"), tmp_path / entry["file"])
    unpacked = unpack_model(tmp_path, entry, tmp_path / "two")
    assert sorted(p.name for p in unpacked.iterdir()) == sorted(MODEL_FILES)


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
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCRATCH} CASCADE; CREATE SCHEMA {SCRATCH}"))
    yield engine
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCRATCH} CASCADE"))
    engine.dispose()


def _definition(entry: dict[str, Any]) -> dict[str, Any]:
    return {k: entry[k] for k in ("rows", "sha256", "columns", "indexes", "checks")}


@pytest.mark.db
def test_table_round_trip(engine: Engine, tmp_path: Path) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(f"""
            CREATE TABLE {SAMPLE} (
                id integer, name text, price numeric NOT NULL CHECK (price > 0),
                ratio double precision, month date, at timestamptz, ok boolean DEFAULT true,
                n bigint, geom geometry(Point, 3414)
            );
            INSERT INTO {SAMPLE} VALUES
                (1, 'Blk 1', 6268.66, 0.1, '2026-10-01', '2026-10-25 13:33:57.223189+00',
                 true, 9007199254740993, ST_SetSRID(ST_MakePoint(28000.5, 38000.25), 3414)),
                (2, NULL, 12.3456, NULL, NULL, NULL, NULL, NULL, NULL);
            ALTER TABLE {SAMPLE} ADD PRIMARY KEY (id);
            CREATE INDEX sample_month_idx ON {SAMPLE} (month);
            """)
        )
        before = conn.execute(text(f"SELECT *, geom::text FROM {SAMPLE} ORDER BY id")).all()
        entry = export_table(conn, SAMPLE, tmp_path / f"{SAMPLE}.parquet")
    assert entry["rows"] == 2
    assert {i["name"] for i in entry["indexes"]} == {"sample_pkey", "sample_month_idx"}
    assert [c["name"] for c in entry["checks"]] == ["sample_price_check"]
    columns = {c["name"]: c for c in entry["columns"]}
    assert columns["price"]["not_null"] and columns["id"]["not_null"]
    assert columns["ok"]["default"] == "true"

    with engine.begin() as conn:
        raw = conn.connection.driver_connection
        with raw.cursor() as cur:
            restore_table(cur, tmp_path, entry)
    with engine.connect() as conn:
        after = conn.execute(text(f"SELECT *, geom::text FROM {SAMPLE} ORDER BY id")).all()
        again = export_table(conn, SAMPLE, tmp_path / "again.parquet")
    assert after == before
    assert after[0].price == Decimal("6268.66")
    assert after[0].at == datetime(2026, 10, 25, 13, 33, 57, 223189, tzinfo=UTC)
    assert after[0].month == date(2026, 10, 1)
    assert after[0].geom is not None
    # Same types, NOT NULLs, defaults, keys, indexes, CHECKs and bytes as the original.
    assert _definition(again) == _definition(entry)


@pytest.mark.db
def test_export_rejects_constraints_it_cannot_restore(engine: Engine, tmp_path: Path) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(f"""CREATE TABLE {SCRATCH}.parent (id integer PRIMARY KEY);
                     CREATE TABLE {SCRATCH}.child (parent integer REFERENCES {SCRATCH}.parent)""")
        )
        with pytest.raises(SnapshotError, match="cannot be snapshotted"):
            export_table(conn, f"{SCRATCH}.child", tmp_path / "child.parquet")


@pytest.fixture
def restoring(engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """A snapshot of SAMPLE (values 'a', 'b') and a stub model; SAMPLE then holds 'changed',
    the model in use is 2026-09, and restored credits go to the scratch schema."""
    directory = tmp_path / "snapshot"
    directory.mkdir()
    with engine.begin() as conn:
        conn.execute(
            text(f"""CREATE TABLE {SAMPLE} (id integer PRIMARY KEY, v text NOT NULL);
                     INSERT INTO {SAMPLE} VALUES (1, 'a'), (2, 'b')""")
        )
        entry = export_table(conn, SAMPLE, directory / f"{SAMPLE}.parquet")
        conn.execute(text(f"UPDATE {SAMPLE} SET v = 'changed'"))
    archive = pack_model(_model(tmp_path / "trained"), directory / "model-2026-10.tar.gz")
    manifest = {
        "format": FORMAT_VERSION,
        "data_through": "2026-10",
        "model": {"version": "2026-10", "file": archive.name, "sha256": sha256(archive)},
        "tables": [entry],
        "sources": [CREDIT],
    }
    (directory / MANIFEST).write_text(json.dumps(manifest))
    models = tmp_path / "models"
    models.mkdir()
    (models / LATEST_FILE).write_text("2026-09\n")
    for module in (snapshot, site_data):
        monkeypatch.setattr(module, "RESTORED_SOURCES", f"{SCRATCH}.restored_sources")
    return get_settings().model_copy(update={"snapshot_dir": directory, "models_dir": models})


def _values(conn: Connection) -> list[str]:
    return list(conn.execute(text(f"SELECT v FROM {SAMPLE} ORDER BY id")).scalars())


def _ingestion_log(conn: Connection) -> Any:
    if conn.execute(text("SELECT to_regclass('raw.ingestion_log')")).scalar_one() is None:
        return None
    return conn.execute(text("SELECT count(*), max(id) FROM raw.ingestion_log")).one()


@pytest.mark.db
def test_restore_loads_tables_credits_and_model(engine: Engine, restoring: Settings) -> None:
    with engine.connect() as conn:
        log = _ingestion_log(conn)
    restore(restoring)
    with engine.connect() as conn:
        assert _values(conn) == ["a", "b"]
        credits = site_data.sources(conn, restoring)
        # What this machine ingested is untouched, so `make ingest` reloads every raw table.
        assert _ingestion_log(conn) == log
    retrieved = datetime(2026, 10, 25, 13, 31, 48, 374191, tzinfo=UTC)
    assert credits == [{**CREDIT, "retrieved": retrieved}]
    assert (restoring.models_dir / LATEST_FILE).read_text() == "2026-10\n"
    assert sorted(p.name for p in (restoring.models_dir / "2026-10").iterdir()) == sorted(
        MODEL_FILES
    )


@pytest.mark.db
def test_restore_changes_nothing_when_installing_the_model_fails(
    engine: Engine, restoring: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(snapshot, "install_model", fail)
    with pytest.raises(OSError, match="disk full"):
        restore(restoring)
    with engine.connect() as conn:
        assert _values(conn) == ["changed", "changed"]
        restored = conn.execute(text(f"SELECT to_regclass('{SCRATCH}.restored_sources')"))
        assert restored.scalar_one() is None
    assert (restoring.models_dir / LATEST_FILE).read_text() == "2026-09\n"


@pytest.mark.db
def test_restore_points_latest_back_when_the_commit_fails(
    engine: Engine, restoring: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def begin_then_fail_commit() -> Iterator[Connection]:
        with engine.connect() as conn:
            transaction = conn.begin()
            yield conn
            transaction.rollback()
            raise RuntimeError("commit failed")

    failing = SimpleNamespace(begin=begin_then_fail_commit, dispose=lambda: None)
    monkeypatch.setattr(snapshot, "get_engine", lambda: failing)
    with pytest.raises(RuntimeError, match="commit failed"):
        restore(restoring)
    with engine.connect() as conn:
        assert _values(conn) == ["changed", "changed"]
    assert (restoring.models_dir / LATEST_FILE).read_text() == "2026-09\n"
