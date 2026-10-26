"""Data snapshots: `python -m pipeline.snapshot export` (`make snapshot`) and
`python -m pipeline.snapshot restore` (`make restore-snapshot`).

A snapshot lets someone rebuild the site without ingesting or geocoding: `make
restore-snapshot && make export && make site-build`. It holds, in settings.snapshot_dir:

- `{schema}.{table}.parquet` for every `marts` table, and for the two other tables the site
  export reads (SNAPSHOT_EXTRA_TABLES), with typed columns: numeric as exact decimals,
  geometry as EWKB
- `model-{version}.tar.gz`: the latest model directory (ONNX, LightGBM text models, metrics
  and model card), so the export can run without training
- `manifest.json`: every table's columns (type, NOT NULL, default), indexes, keys and CHECK
  constraints, row count and SHA-256, the model's checksum, and the source datasets' names
  and dates (meta.json credits them, and their raw downloads are not part of a snapshot)

Restoring checks every checksum and unpacks the model before changing anything. Then, in one
transaction, it recreates each table with its types, constraints and indexes, loads it,
records the snapshot's dataset credits in RESTORED_SOURCES, and, as the last step before
committing, makes the snapshot's model the latest one. Both steps are idempotent: the same
database gives byte-identical files, and restoring twice gives the same database.
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
from contextlib import ExitStack, contextmanager
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
from pipeline.export.site_data import RESTORED_SOURCES, sources
from pipeline.train.train_lgbm import (
    LATEST_FILE,
    QUANTILES,
    latest_model_dir,
    publish,
    write_latest,
)

logger = logging.getLogger(__name__)

FORMAT_VERSION = 2
MANIFEST = "manifest.json"
# Tables outside `marts` that pipeline.export.site_data reads. Its third input,
# raw.ingestion_log, records what this machine ingested and stays out of snapshots: a
# restore loads no raw tables, so `make ingest` must not see the snapshot's datasets as
# loaded. The snapshot's dataset credits go to RESTORED_SOURCES instead.
SNAPSHOT_EXTRA_TABLES = ("staging.hdb_resale", "staging.planning_areas")
# What the export needs from a model directory.
MODEL_FILES = ("features.json", *(f"model_{name}.onnx" for name in QUANTILES))
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
    """Name, type, NOT NULL, default and (numeric) largest scale of each column, in table
    order."""
    rows = conn.execute(
        text("""SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
                       pg_get_expr(d.adbin, d.adrelid), a.attgenerated <> '' OR a.attidentity <> ''
                FROM pg_attribute AS a
                LEFT JOIN pg_attrdef AS d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                WHERE a.attrelid = CAST(:table AS regclass) AND a.attnum > 0
                  AND NOT a.attisdropped
                ORDER BY a.attnum"""),
        {"table": table},
    ).all()
    columns = []
    for name, pg_type, not_null, default, generated in rows:
        if generated:
            raise SnapshotError(f"{table}.{name}: generated and identity columns are not supported")
        column: dict[str, Any] = {
            "name": name,
            "type": pg_type,
            "not_null": not_null,
            "default": default,
        }
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


def table_checks(conn: Connection, table: str) -> list[dict[str, str]]:
    """CHECK constraint definitions. Raise on any other constraint (other than keys and NOT
    NULL, which the indexes and columns carry), since a restore would silently drop it."""
    rows = conn.execute(
        text("""SELECT conname, contype, pg_get_constraintdef(oid) FROM pg_constraint
                WHERE conrelid = CAST(:table AS regclass) AND contype NOT IN ('p', 'u', 'n')
                ORDER BY conname"""),
        {"table": table},
    ).all()
    unsupported = [name for name, kind, _ in rows if kind != "c"]
    if unsupported:
        raise SnapshotError(f"{table}: constraints {unsupported} cannot be snapshotted")
    return [{"name": name, "definition": definition} for name, _, definition in rows]


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
        # On the statement: Connection.execution_options would stream every later query too.
        result = conn.execute(text(query).execution_options(yield_per=BATCH_ROWS))
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
        "checks": table_checks(conn, table),
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


def _column_sql(column: dict[str, Any]) -> sql.Composable:
    parts = [sql.Identifier(column["name"]), sql.SQL(column["type"])]
    if column["default"] is not None:
        parts.append(sql.SQL("DEFAULT " + column["default"]))
    if column["not_null"]:
        parts.append(sql.SQL("NOT NULL"))
    return sql.SQL(" ").join(parts)


def restore_table(cur: psycopg.Cursor, directory: Path, entry: dict[str, Any]) -> None:
    """Recreate one table from its manifest entry and load its Parquet file."""
    target = sql.Identifier(*_split(entry["table"]))
    cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(target))
    cur.execute(
        sql.SQL("CREATE TABLE {} ({})").format(
            target, sql.SQL(", ").join(_column_sql(c) for c in entry["columns"])
        )
    )
    names = sql.SQL(", ").join(sql.Identifier(c["name"]) for c in entry["columns"])
    with cur.copy(sql.SQL("COPY {} ({}) FROM STDIN").format(target, names)) as copy:
        for row in _rows(directory / entry["file"], entry["columns"]):
            copy.write_row(row)
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
    for check in entry["checks"]:  # added after the load, so the rows are checked once
        cur.execute(
            sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} {}").format(
                target, sql.Identifier(check["name"]), sql.SQL(check["definition"])
            )
        )
    cur.execute(sql.SQL("ANALYZE {}").format(target))


def restore_sources(cur: psycopg.Cursor, credits: list[dict[str, Any]]) -> None:
    """Replace RESTORED_SOURCES with the snapshot's dataset credits."""
    target = sql.Identifier(*_split(RESTORED_SOURCES))
    cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(target))
    # last_updated stays the text data.gov.sg published (ISO 8601 with its UTC offset), so
    # the export credits it exactly as an export from the raw downloads would.
    cur.execute(
        sql.SQL("""CREATE TABLE {} (
                       source text NOT NULL,
                       dataset_id text NOT NULL,
                       name text NOT NULL,
                       last_updated text,
                       retrieved timestamptz NOT NULL,
                       PRIMARY KEY (source, dataset_id)
                   )""").format(target)
    )
    cur.executemany(
        sql.SQL("INSERT INTO {} VALUES (%s, %s, %s, %s, %s)").format(target),
        [
            (c["source"], c["dataset_id"], c["name"], c["last_updated"], c["retrieved"])
            for c in credits
        ],
    )


def unpack_model(directory: Path, model: dict[str, Any], into: Path) -> Path:
    """Extract the snapshot's model archive into `into`; return the model directory."""
    with tarfile.open(directory / model["file"], mode="r:gz") as tar:
        tar.extractall(into, filter="data")
    unpacked = into / model["version"]
    missing = [name for name in MODEL_FILES if not (unpacked / name).is_file()]
    if missing:
        raise SnapshotError(f"{model['file']}: no {', '.join(missing)} in {model['version']}/")
    return unpacked


def _replaced(final: Path) -> Path:
    """Where installing_model keeps the directory it replaces until the install is final."""
    return final.with_name(f".{final.name}.replaced")


def recover_install(settings: Settings, version: str) -> None:
    """Undo what an interrupted installing_model left: a model directory moved aside but
    not put back. The directory is only ever missing between installing_model's renames."""
    final = settings.models_dir / version
    replaced = _replaced(final)
    if replaced.exists():
        if final.exists():
            shutil.rmtree(replaced)
        else:
            replaced.rename(final)
            logger.warning("Put back %s from an interrupted restore", final)


@contextmanager
def installing_model(settings: Settings, unpacked: Path, version: str) -> Iterator[Path]:
    """Install an unpacked model and make it the latest for the duration of the block; if
    anything fails, here or in the block, put back the directory it replaced and the
    previous models/LATEST."""
    final = settings.models_dir / version
    replaced = _replaced(final)
    latest = settings.models_dir / LATEST_FILE
    previous = latest.read_text(encoding="utf-8").strip() if latest.exists() else None
    moved_aside = moved_in = False
    try:
        if final.exists():
            final.rename(replaced)
            moved_aside = True
        unpacked.rename(final)
        moved_in = True
        write_latest(settings.models_dir, version)
        yield final
    except BaseException:
        if moved_in:
            shutil.rmtree(final)
        if moved_aside:
            replaced.rename(final)
        write_latest(settings.models_dir, previous)
        raise
    shutil.rmtree(replaced, ignore_errors=True)


def restore(settings: Settings) -> dict[str, Any]:
    """Load the snapshot in settings.snapshot_dir into the database and make its model the
    latest.

    Nothing changes unless every checksum matches and the model unpacks. The tables load in
    one transaction whose last step installs the model, and the install is undone (the
    replaced model directory and models/LATEST put back) unless the commit succeeds, so a
    failure at any step leaves the database and the models as they were.
    """
    directory = settings.snapshot_dir
    manifest = read_manifest(directory)
    verify(directory, manifest)
    version = manifest["model"]["version"]
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    recover_install(settings, version)
    scratch = Path(tempfile.mkdtemp(prefix=".restore.", dir=settings.models_dir))
    engine = get_engine()
    try:
        unpacked = unpack_model(directory, manifest["model"], scratch)
        # Contexts exit in reverse: engine.begin() commits first, then the stack finishes
        # the install, or undoes it if the commit (or anything before it) failed.
        with ExitStack() as install, engine.begin() as conn:
            raw = conn.connection.driver_connection
            assert isinstance(raw, psycopg.Connection)
            with raw.cursor() as cur:
                for entry in manifest["tables"]:
                    restore_table(cur, directory, entry)
                    logger.info("%s: %s rows", entry["table"], f"{entry['rows']:,}")
                restore_sources(cur, manifest["sources"])
            model = install.enter_context(installing_model(settings, unpacked, version))
    finally:
        engine.dispose()
        shutil.rmtree(scratch, ignore_errors=True)
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
