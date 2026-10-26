"""Data snapshots: `python -m pipeline.snapshot export` (`make snapshot`) and
`python -m pipeline.snapshot restore` (`make restore-snapshot`).

A snapshot lets someone rebuild the site without ingesting or geocoding: `make
restore-snapshot && make export && make site-build`. It holds, in settings.snapshot_dir:

- `{schema}.{table}.parquet` for every `marts` table, and for the three other tables the
  site export reads (SNAPSHOT_EXTRA_TABLES), with typed columns: numeric as exact decimals,
  geometry as EWKB
- `model-{version}.tar.gz`: the latest model directory (ONNX, LightGBM text models, metrics
  and model card), so the export can run without training
- `manifest.json`: every table's column types, indexes, row count and SHA-256, the model's
  checksum, and the source datasets' names and dates (meta.json credits them, and their raw
  downloads are not part of a snapshot)

Restoring checks every checksum, recreates each table with its original types and indexes,
loads it, and makes the snapshot's model the latest one. Both steps are idempotent: the
same database gives byte-identical files, and restoring twice gives the same database.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import shutil
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
from psycopg import sql
from sqlalchemy import Connection, text

from config.settings import Settings, get_settings
from pipeline.common.db import get_engine
from pipeline.common.log import setup_logging
from pipeline.export.site_data import sources
from pipeline.ingest.load import ensure_log_table
from pipeline.train.train_lgbm import LATEST_FILE, latest_model_dir, publish

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
MANIFEST = "manifest.json"
# Tables outside `marts` that pipeline.export.site_data reads.
SNAPSHOT_EXTRA_TABLES = ("staging.hdb_resale", "staging.planning_areas", "raw.ingestion_log")
# raw.ingestion_log is recreated by the ingest code (its id sequence and index), not from
# the manifest, so a later `make ingest` keeps appending to it.
LOG_TABLE = "raw.ingestion_log"
BATCH_ROWS = 50_000
# Exact decimals for unconstrained numeric columns, at the largest scale in the column.
DECIMAL_PRECISION = 38

_ARROW_TYPES: dict[str, pa.DataType] = {
    "text": pa.string(),
    "character varying": pa.string(),
    "smallint": pa.int16(),
    "integer": pa.int32(),
    "bigint": pa.int64(),
    "real": pa.float32(),
    "double precision": pa.float64(),
    "boolean": pa.bool_(),
    "date": pa.date32(),
    "timestamp with time zone": pa.timestamp("us", tz="UTC"),
    "timestamp without time zone": pa.timestamp("us"),
}


class SnapshotError(RuntimeError):
    """A snapshot is missing, incomplete or does not match its manifest."""


def arrow_type(pg_type: str, scale: int | None = None) -> pa.DataType:
    """The Parquet column type for a Postgres column (`format_type` spelling)."""
    if pg_type.startswith("geometry"):
        return pa.binary()
    if pg_type == "numeric":
        return pa.decimal128(DECIMAL_PRECISION, scale or 0)
    if pg_type in _ARROW_TYPES:
        return _ARROW_TYPES[pg_type]
    raise SnapshotError(f"no Parquet mapping for Postgres type {pg_type!r}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _split(table: str) -> tuple[str, str]:
    schema, name = table.split(".")
    return schema, name


def snapshot_tables(conn: Connection) -> list[str]:
    """Every base table in `marts`, then SNAPSHOT_EXTRA_TABLES."""
    marts = conn.execute(
        text("""SELECT table_schema || '.' || table_name FROM information_schema.tables
                WHERE table_schema = 'marts' AND table_type = 'BASE TABLE'
                ORDER BY table_name""")
    ).scalars()
    return [*marts, *SNAPSHOT_EXTRA_TABLES]


def table_columns(conn: Connection, table: str) -> list[dict[str, Any]]:
    """Name, type and (numeric) largest scale of each column, in table order."""
    rows = conn.execute(
        text("""SELECT a.attname, format_type(a.atttypid, a.atttypmod)
                FROM pg_attribute AS a
                WHERE a.attrelid = CAST(:table AS regclass) AND a.attnum > 0
                  AND NOT a.attisdropped
                ORDER BY a.attnum"""),
        {"table": table},
    ).all()
    columns = []
    for name, pg_type in rows:
        column: dict[str, Any] = {"name": name, "type": pg_type}
        if pg_type == "numeric":
            column["scale"] = conn.execute(
                text(f"SELECT coalesce(max(scale({_ident(name)})), 0) FROM {_table_sql(table)}")
            ).scalar_one()
        columns.append(column)
    return columns


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_sql(table: str) -> str:
    return ".".join(_ident(part) for part in _split(table))


def table_indexes(conn: Connection, table: str) -> list[dict[str, Any]]:
    """Index definitions, and the primary key or unique constraint each one backs."""
    rows = conn.execute(
        text("""SELECT ic.relname, pg_get_indexdef(i.indexrelid), con.conname, con.contype
                FROM pg_index AS i
                JOIN pg_class AS ic ON ic.oid = i.indexrelid
                LEFT JOIN pg_constraint AS con
                  ON con.conindid = i.indexrelid AND con.contype IN ('p', 'u')
                WHERE i.indrelid = CAST(:table AS regclass)
                ORDER BY ic.relname"""),
        {"table": table},
    ).all()
    return [
        {
            "name": name,
            "definition": definition,
            "constraint": (
                {"name": constraint, "type": "PRIMARY KEY" if kind == "p" else "UNIQUE"}
                if constraint
                else None
            ),
        }
        for name, definition, constraint, kind in rows
    ]


def _order_by(conn: Connection, table: str, columns: list[dict[str, Any]]) -> str:
    """The primary key, or every non-geometry column: a stable row order makes the Parquet
    file byte-identical for the same data."""
    key = conn.execute(
        text("""SELECT array_agg(a.attname ORDER BY k.ord)
                FROM pg_index AS i
                CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
                JOIN pg_attribute AS a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
                WHERE i.indrelid = CAST(:table AS regclass) AND i.indisprimary"""),
        {"table": table},
    ).scalar_one()
    names = key or [c["name"] for c in columns if not c["type"].startswith("geometry")]
    return ", ".join(_ident(n) for n in names)


def export_table(conn: Connection, table: str, path: Path) -> dict[str, Any]:
    """Write `table` to Parquet at `path`; return its manifest entry."""
    columns = table_columns(conn, table)
    schema = pa.schema(
        [pa.field(c["name"], arrow_type(c["type"], c.get("scale"))) for c in columns]
    )
    select = ", ".join(
        f"ST_AsEWKB({_ident(c['name'])}) AS {_ident(c['name'])}"
        if c["type"].startswith("geometry")
        else _ident(c["name"])
        for c in columns
    )
    query = f"SELECT {select} FROM {_table_sql(table)} ORDER BY {_order_by(conn, table, columns)}"
    rows = 0
    with pq.ParquetWriter(path, schema, compression="zstd") as writer:
        result = conn.execution_options(yield_per=BATCH_ROWS).execute(text(query))
        for batch in result.partitions():
            values = list(zip(*batch, strict=True)) if batch else [[] for _ in columns]
            arrays = [
                pa.array([bytes(v) if isinstance(v, memoryview) else v for v in column], type=t)
                for column, t in zip(values, schema.types, strict=True)
            ]
            writer.write_batch(pa.record_batch(arrays, schema=schema))
            rows += len(batch)
    return {
        "table": table,
        "file": path.name,
        "rows": rows,
        "sha256": sha256(path),
        "columns": columns,
        "indexes": table_indexes(conn, table),
    }


def pack_model(directory: Path, path: Path) -> Path:
    """`directory` as a gzipped tar with fixed timestamps and owners, so the same model gives
    the same archive."""
    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped,
        tarfile.open(fileobj=zipped, mode="w", format=tarfile.GNU_FORMAT) as tar,
    ):
        for file in sorted(p for p in directory.iterdir() if p.is_file()):
            info = tarfile.TarInfo(f"{directory.name}/{file.name}")
            info.size = file.stat().st_size
            info.mtime = 0
            info.mode = 0o644
            with file.open("rb") as f:
                tar.addfile(info, f)
    return path


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def export(settings: Settings) -> Path:
    """Write a complete snapshot next to settings.snapshot_dir and swap it in."""
    final = settings.snapshot_dir
    final.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".snapshot.", dir=final.parent))
    model_dir = latest_model_dir(settings)
    engine = get_engine()
    try:
        with engine.connect() as conn:
            tables = []
            for table in snapshot_tables(conn):
                entry = export_table(conn, table, staging / f"{table}.parquet")
                logger.info("%s: %s rows", table, f"{entry['rows']:,}")
                tables.append(entry)
            data_through = conn.execute(
                text("SELECT to_char(max(month), 'YYYY-MM') FROM staging.hdb_resale")
            ).scalar_one()
            credits = sources(conn, settings)
        model_file = pack_model(model_dir, staging / f"model-{model_dir.name}.tar.gz")
        manifest = {
            "format": FORMAT_VERSION,
            "data_through": data_through,
            "model": {
                "version": model_dir.name,
                "file": model_file.name,
                "sha256": sha256(model_file),
            },
            "tables": tables,
            "sources": credits,
        }
        (staging / MANIFEST).write_text(
            json.dumps(manifest, indent=2, default=_json_default) + "\n", encoding="utf-8"
        )
        publish(staging, final)
    finally:
        engine.dispose()
        shutil.rmtree(staging, ignore_errors=True)
    return final


def release_commands(settings: Settings, manifest: dict[str, Any]) -> list[str]:
    """The commands to publish this snapshot as a GitHub release, and to download it."""
    tag = f"snapshot-{manifest['data_through']}"
    directory = settings.snapshot_dir.relative_to(settings.snapshot_dir.parents[1])
    return [
        f"gh release create {tag} {directory}/* --repo {settings.github_repo} "
        f'--title "Data snapshot {manifest["data_through"]}" '
        f'--notes "Parquet snapshot of the marts tables, the tables the site export reads, '
        f'and model {manifest["model"]["version"]}. Restore with: make restore-snapshot"',
        f"gh release download {tag} --repo {settings.github_repo} --dir {directory}",
    ]


def read_manifest(directory: Path) -> dict[str, Any]:
    path = directory / MANIFEST
    if not path.exists():
        raise SnapshotError(
            f"no {path} (download a snapshot first, e.g. `gh release download "
            f"snapshot-YYYY-MM --dir {directory}`)"
        )
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT_VERSION:
        raise SnapshotError(f"{path}: format {manifest.get('format')}, expected {FORMAT_VERSION}")
    return manifest


def verify(directory: Path, manifest: dict[str, Any]) -> None:
    """Raise unless every file in the manifest is present with its checksum."""
    expected = {t["file"]: t["sha256"] for t in manifest["tables"]}
    expected[manifest["model"]["file"]] = manifest["model"]["sha256"]
    problems = []
    for name, digest in expected.items():
        path = directory / name
        if not path.exists():
            problems.append(f"{name}: missing")
        elif sha256(path) != digest:
            problems.append(f"{name}: checksum mismatch")
    if problems:
        raise SnapshotError("snapshot incomplete: " + "; ".join(problems))


def _rows(path: Path, columns: list[dict[str, Any]]) -> Iterator[tuple[Any, ...]]:
    """Rows of a Parquet file ready for COPY: geometry as hex EWKB, which PostGIS parses."""
    geometry = [c["type"].startswith("geometry") for c in columns]
    for batch in pq.ParquetFile(path).iter_batches(batch_size=BATCH_ROWS):
        values = [column.to_pylist() for column in batch.columns]
        for row in zip(*values, strict=True):
            yield tuple(
                v.hex() if is_geometry and v is not None else v
                for v, is_geometry in zip(row, geometry, strict=True)
            )


def restore_table(cur: psycopg.Cursor, directory: Path, entry: dict[str, Any]) -> None:
    """Recreate one table from its manifest entry and load its Parquet file."""
    table = entry["table"]
    schema, name = _split(table)
    target = sql.Identifier(schema, name)
    cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(target))
    if table == LOG_TABLE:
        ensure_log_table(cur, schema)
    else:
        cur.execute(
            sql.SQL("CREATE TABLE {} ({})").format(
                target,
                sql.SQL(", ").join(
                    sql.SQL("{} {}").format(sql.Identifier(c["name"]), sql.SQL(c["type"]))
                    for c in entry["columns"]
                ),
            )
        )
    names = sql.SQL(", ").join(sql.Identifier(c["name"]) for c in entry["columns"])
    with cur.copy(sql.SQL("COPY {} ({}) FROM STDIN").format(target, names)) as copy:
        for row in _rows(directory / entry["file"], entry["columns"]):
            copy.write_row(row)
    if table == LOG_TABLE:
        cur.execute(
            "SELECT setval(pg_get_serial_sequence('raw.ingestion_log', 'id'), "
            "coalesce(max(id), 0) + 1, false) FROM raw.ingestion_log"
        )
    else:
        for index in entry["indexes"]:
            cur.execute(index["definition"])
            if index["constraint"]:
                cur.execute(
                    sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} {} USING INDEX {}").format(
                        target,
                        sql.Identifier(index["constraint"]["name"]),
                        sql.SQL(index["constraint"]["type"]),
                        sql.Identifier(index["name"]),
                    )
                )
    cur.execute(sql.SQL("ANALYZE {}").format(target))


def restore_model(settings: Settings, directory: Path, model: dict[str, Any]) -> Path:
    """Unpack the snapshot's model into settings.models_dir and make it the latest."""
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".restore.", dir=settings.models_dir))
    try:
        with tarfile.open(directory / model["file"], mode="r:gz") as tar:
            tar.extractall(staging, filter="data")
        final = publish(staging / model["version"], settings.models_dir / model["version"])
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    (settings.models_dir / LATEST_FILE).write_text(model["version"] + "\n", encoding="utf-8")
    return final


def restore(settings: Settings) -> dict[str, Any]:
    """Load the snapshot in settings.snapshot_dir into the database (one transaction) and
    restore its model."""
    directory = settings.snapshot_dir
    manifest = read_manifest(directory)
    verify(directory, manifest)
    engine = get_engine()
    try:
        with engine.begin() as conn:
            raw = conn.connection.driver_connection
            assert isinstance(raw, psycopg.Connection)
            with raw.cursor() as cur:
                for entry in manifest["tables"]:
                    restore_table(cur, directory, entry)
                    logger.info("%s: %s rows", entry["table"], f"{entry['rows']:,}")
    finally:
        engine.dispose()
    model = restore_model(settings, directory, manifest["model"])
    logger.info("Model %s restored to %s", manifest["model"]["version"], model)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["export", "restore"])
    args = parser.parse_args(argv)
    setup_logging()
    settings = get_settings()
    if args.action == "export":
        directory = export(settings)
        manifest = read_manifest(directory)
        size = sum(p.stat().st_size for p in directory.iterdir())
        logger.info("Snapshot written to %s (%.1f MB)", directory, size / 1e6)
        create, download = release_commands(settings, manifest)
        logger.info("To publish it as a GitHub release, run by hand:\n\n  %s\n", create)
        logger.info("Others then download it with:\n\n  %s\n", download)
    else:
        manifest = restore(settings)
        logger.info(
            "Restored %d tables with data to %s; next: make export && make site-build",
            len(manifest["tables"]),
            manifest["data_through"],
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
