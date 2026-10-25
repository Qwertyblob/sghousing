"""Load snapshots into `raw` tables, idempotently.

Each load runs on one connection in one transaction: column DDL, DELETE of the dataset's
previous rows, COPY of the new rows and the `ingestion_log` insert either all commit or all
roll back. A snapshot whose SHA-256 equals the dataset's last logged load is skipped.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import psycopg
from psycopg import sql
from sqlalchemy import Engine

from config.settings import REPO_ROOT
from pipeline.ingest.parsers import (
    IDENTIFIER,
    ParseError,
    check_header,
    open_csv,
    read_geojson_features,
)
from pipeline.ingest.snapshots import Snapshot

logger = logging.getLogger(__name__)

CSV_META_COLUMNS = ("source_dataset_id", "source_row_number", "ingested_at")
GEOJSON_COLUMNS = (
    "source_dataset_id",
    "feature_index",
    "properties",
    "geometry_geojson",
    "ingested_at",
)


@dataclass(frozen=True)
class LoadResult:
    """Outcome of loading one snapshot."""

    dataset_id: str
    table: str
    status: Literal["loaded", "skipped"]
    row_count: int


def _ident(name: str) -> sql.Identifier:
    if not IDENTIFIER.match(name):
        raise ValueError(f"unsafe SQL identifier {name!r}")
    return sql.Identifier(name)


def ensure_log_table(cur: psycopg.Cursor, schema: str) -> None:
    """Create `{schema}.ingestion_log` if missing."""
    cur.execute(
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {schema}.ingestion_log (
                id bigserial PRIMARY KEY,
                source text NOT NULL,
                dataset_id text NOT NULL,
                file_path text NOT NULL,
                sha256 text NOT NULL,
                row_count integer NOT NULL,
                downloaded_at timestamptz NOT NULL,
                loaded_at timestamptz NOT NULL DEFAULT now()
            )
            """
        ).format(schema=_ident(schema))
    )
    cur.execute(
        sql.SQL(
            "CREATE INDEX IF NOT EXISTS ingestion_log_dataset_idx "
            "ON {schema}.ingestion_log (dataset_id, loaded_at DESC, id DESC)"
        ).format(schema=_ident(schema))
    )


def last_loaded_sha256(cur: psycopg.Cursor, schema: str, dataset_id: str) -> str | None:
    """SHA-256 of the most recent load of `dataset_id`, if any."""
    cur.execute(
        sql.SQL(
            "SELECT sha256 FROM {schema}.ingestion_log WHERE dataset_id = %s "
            "ORDER BY loaded_at DESC, id DESC LIMIT 1"
        ).format(schema=_ident(schema)),
        (dataset_id,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _ensure_csv_table(cur: psycopg.Cursor, schema: str, table: str, columns: list[str]) -> None:
    target = sql.SQL("{}.{}").format(_ident(schema), _ident(table))
    cur.execute(
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {target} (
                source_dataset_id text NOT NULL,
                source_row_number integer NOT NULL,
                ingested_at timestamptz NOT NULL,
                UNIQUE (source_dataset_id, source_row_number)
            )
            """
        ).format(target=target)
    )
    for column in columns:
        cur.execute(
            sql.SQL("ALTER TABLE {target} ADD COLUMN IF NOT EXISTS {col} text").format(
                target=target, col=_ident(column)
            )
        )


def _ensure_geojson_table(cur: psycopg.Cursor, schema: str, table: str) -> None:
    cur.execute(
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {schema}.{table} (
                source_dataset_id text NOT NULL,
                feature_index integer NOT NULL,
                properties jsonb NOT NULL,
                geometry_geojson text,
                ingested_at timestamptz NOT NULL,
                UNIQUE (source_dataset_id, feature_index)
            )
            """
        ).format(schema=_ident(schema), table=_ident(table))
    )


def _delete_dataset(cur: psycopg.Cursor, schema: str, table: str, dataset_id: str) -> None:
    cur.execute(
        sql.SQL("DELETE FROM {}.{} WHERE source_dataset_id = %s").format(
            _ident(schema), _ident(table)
        ),
        (dataset_id,),
    )


def _copy_statement(schema: str, table: str, columns: list[str]) -> sql.Composed:
    return sql.SQL("COPY {}.{} ({}) FROM STDIN").format(
        _ident(schema), _ident(table), sql.SQL(", ").join(_ident(c) for c in columns)
    )


def _load_csv(
    cur: psycopg.Cursor, schema: str, table: str, snap: Snapshot, ingested_at: datetime
) -> int:
    with open_csv(snap.path) as (header, records):
        check_header(header, snap.metadata.columns)
        clash = set(header) & set(CSV_META_COLUMNS)
        if clash:
            raise ParseError(f"source columns collide with metadata columns: {sorted(clash)}")
        _ensure_csv_table(cur, schema, table, header)
        _delete_dataset(cur, schema, table, snap.dataset_id)
        count = 0
        with cur.copy(_copy_statement(schema, table, [*CSV_META_COLUMNS, *header])) as copy:
            for record_number, values in records:
                copy.write_row((snap.dataset_id, record_number, ingested_at, *values))
                count += 1
    return count


def _load_geojson(
    cur: psycopg.Cursor, schema: str, table: str, snap: Snapshot, ingested_at: datetime
) -> int:
    _ensure_geojson_table(cur, schema, table)
    _delete_dataset(cur, schema, table, snap.dataset_id)
    count = 0
    with cur.copy(_copy_statement(schema, table, list(GEOJSON_COLUMNS))) as copy:
        for index, properties, geometry in read_geojson_features(snap.path):
            copy.write_row((snap.dataset_id, index, json.dumps(properties), geometry, ingested_at))
            count += 1
    return count


def _display_path(snap: Snapshot) -> str:
    try:
        return str(snap.path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(snap.path)


def load_snapshot(engine: Engine, snap: Snapshot, *, table: str, schema: str = "raw") -> LoadResult:
    """Replace `snap.dataset_id`'s rows in `{schema}.{table}` with the snapshot's contents.

    Skips (and changes nothing) when the snapshot hash equals the last logged load.
    """
    with engine.begin() as conn:
        pg: psycopg.Connection = conn.connection.driver_connection  # same transaction
        with pg.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{schema}.{table}",))
            ensure_log_table(cur, schema)
            if last_loaded_sha256(cur, schema, snap.dataset_id) == snap.sha256:
                logger.info("%s: unchanged, skipped", snap.dataset_id)
                return LoadResult(snap.dataset_id, table, "skipped", 0)

            ingested_at = datetime.now(UTC)
            if snap.metadata.format == "CSV":
                count = _load_csv(cur, schema, table, snap, ingested_at)
            elif snap.metadata.format == "GEOJSON":
                count = _load_geojson(cur, schema, table, snap, ingested_at)
            else:
                raise ValueError(f"unsupported format {snap.metadata.format}")

            cur.execute(
                sql.SQL(
                    "INSERT INTO {schema}.ingestion_log "
                    "(source, dataset_id, file_path, sha256, row_count, downloaded_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s)"
                ).format(schema=_ident(schema)),
                (
                    snap.source,
                    snap.dataset_id,
                    _display_path(snap),
                    snap.sha256,
                    count,
                    snap.downloaded_at,
                ),
            )
    logger.info("%s: loaded %d rows into %s.%s", snap.dataset_id, count, schema, table)
    return LoadResult(snap.dataset_id, table, "loaded", count)
