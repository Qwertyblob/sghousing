"""Geocoding run: pick uncached addresses, query OneMap, cache results, apply overrides.

Two kinds of address are geocoded into `staging.geocode_cache`:

- HDB blocks from `staging.hdb_resale`, keyed by `address_key` (`"{block} {street_name}"`).
- School postal codes from `staging.schools`, keyed `"POSTAL {postal_code}"`.

Statuses: `ok` (exact query matched), `fallback` (query with abbreviations expanded matched),
`override` (hand-maintained), `approx` (no match; placed at the centroid of the located blocks
on the same street, mostly demolished blocks) and `failed` (no match and no located block on
the street).
"""

from __future__ import annotations

import csv
import json
import logging
import math
from collections import Counter
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Connection, Engine, text
from tqdm import tqdm

from pipeline.geocode.onemap import (
    SG_LAT_RANGE,
    SG_LON_RANGE,
    Match,
    expand_abbreviations,
    match_block,
    match_postal,
)

logger = logging.getLogger(__name__)

EXACT_STATUSES = ("ok", "fallback", "override")
DONE_STATUSES = (*EXACT_STATUSES, "approx")
ALL_STATUSES = (*DONE_STATUSES, "failed")
POSTAL_KEY_PREFIX = "POSTAL "
OVERRIDE_COLUMNS = ["address_key", "lat", "lon", "postal", "note"]
FAILURE_COLUMNS = [
    "address_key",
    "status",
    "query_used",
    "found",
    "top_result",
    "lat",
    "lon",
    "geocoded_at",
]


class Searcher(Protocol):
    """What the run needs from a OneMap client."""

    request_counts: Counter[str]

    def search(self, query: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Address:
    """One address to geocode: an HDB block (block + street) or a postal code."""

    key: str
    block: str | None = None
    street: str | None = None
    postal: str | None = None


@dataclass(frozen=True)
class CacheRow:
    """One `geocode_cache` row."""

    address_key: str
    status: str
    query_used: str | None
    postal: str | None = None
    lat: float | None = None
    lon: float | None = None
    raw_response: dict[str, Any] | None = None


@dataclass(frozen=True)
class Override:
    """A hand-maintained location from `data/manual/geocode_overrides.csv`."""

    address_key: str
    lat: float
    lon: float
    postal: str | None
    note: str


@dataclass
class RunSummary:
    """What one run did."""

    pending: int = 0
    statuses: Counter[str] = field(default_factory=Counter)
    requests: Counter[str] = field(default_factory=Counter)
    overrides: int = 0
    approximated: int = 0
    failures_reported: int = 0


def _schema(conn: Connection, schema: str) -> str:
    return conn.dialect.identifier_preparer.quote_schema(schema)


def normalise_key(value: str) -> str:
    """Uppercase and collapse whitespace, as `staging.hdb_resale.address_key` does."""
    return " ".join(value.upper().split())


def ensure_cache_table(conn: Connection, schema: str = "staging") -> None:
    """Create the geocode cache if missing. Never dropped: it is the record of API calls."""
    s = _schema(conn, schema)
    conn.execute(
        text(f"""
        CREATE TABLE IF NOT EXISTS {s}.geocode_cache (
            address_key text PRIMARY KEY,
            query_used text,
            status text NOT NULL,
            postal text,
            lat double precision,
            lon double precision,
            geom geometry(Point, 3414),
            raw_response jsonb,
            geocoded_at timestamptz NOT NULL DEFAULT now(),
            CHECK ((status = 'failed') = (geom IS NULL))
        )""")
    )
    # Re-created on every run so caches made before a status was added accept it.
    conn.execute(
        text(f"ALTER TABLE {s}.geocode_cache DROP CONSTRAINT IF EXISTS geocode_cache_status_check")
    )
    allowed = ", ".join(f"'{status}'" for status in ALL_STATUSES)
    conn.execute(
        text(
            f"ALTER TABLE {s}.geocode_cache ADD CONSTRAINT geocode_cache_status_check "
            f"CHECK (status IN ({allowed}))"
        )
    )
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS geocode_cache_geom_idx "
            f"ON {s}.geocode_cache USING gist (geom)"
        )
    )


def _wanted_cte(s: str) -> str:
    """CTE `wanted(key, block, street_name, postal)`: every address the data needs located.

    Bind `:prefix` to POSTAL_KEY_PREFIX.
    """
    return f"""
        wanted AS (
            SELECT DISTINCT address_key AS key, block, street_name, NULL::text AS postal
            FROM {s}.hdb_resale
            UNION
            SELECT DISTINCT :prefix || postal_code, NULL, NULL, postal_code
            FROM {s}.schools
            WHERE postal_code IS NOT NULL
        )"""


def pending_addresses(
    conn: Connection, schema: str = "staging", *, retry_failed: bool = False
) -> list[Address]:
    """Addresses not yet cached; with `retry_failed`, also `failed` and `approx` ones."""
    s = _schema(conn, schema)
    rows = conn.execute(
        text(f"""
        WITH {_wanted_cte(s)}
        SELECT w.key, w.block, w.street_name, w.postal
        FROM wanted AS w
        LEFT JOIN {s}.geocode_cache AS c ON c.address_key = w.key
        WHERE c.address_key IS NULL OR (c.status IN ('failed', 'approx') AND :retry_failed)
        ORDER BY w.key"""),
        {"prefix": POSTAL_KEY_PREFIX, "retry_failed": retry_failed},
    )
    return [Address(key, block, street, postal) for key, block, street, postal in rows]


def _row(addr: Address, status: str, query: str, body: dict[str, Any], m: Match | None) -> CacheRow:
    if m is None:
        return CacheRow(addr.key, status, query, raw_response=body)
    return CacheRow(addr.key, status, query, m.postal, m.lat, m.lon, body)


def geocode_one(client: Searcher, addr: Address) -> CacheRow:
    """Geocode one address: exact query, then abbreviations expanded; else `failed`."""
    if addr.postal is not None:
        body = client.search(addr.postal)
        m = match_postal(body["results"], addr.postal)
        return _row(addr, "ok" if m else "failed", addr.postal, body, m)

    if addr.block is None or addr.street is None:
        raise ValueError(f"address {addr.key!r} has neither a postal code nor block + street")
    query = f"{addr.block} {addr.street}"
    body = client.search(query)
    m = match_block(body["results"], addr.block, addr.street)
    if m:
        return _row(addr, "ok", query, body, m)

    expanded = f"{addr.block} {expand_abbreviations(addr.street)}"
    if expanded != query:
        query, body = expanded, client.search(expanded)
        m = match_block(body["results"], addr.block, addr.street)
        if m:
            return _row(addr, "fallback", query, body, m)
    return _row(addr, "failed", query, body, None)


def upsert_rows(conn: Connection, rows: Iterable[CacheRow], schema: str = "staging") -> None:
    """Insert or replace cache rows; geom is derived from lat/lon in SVY21."""
    params = [
        {
            "key": r.address_key,
            "query": r.query_used,
            "status": r.status,
            "postal": r.postal,
            "lat": r.lat,
            "lon": r.lon,
            "raw": json.dumps(r.raw_response) if r.raw_response is not None else None,
        }
        for r in rows
    ]
    if not params:
        return
    s = _schema(conn, schema)
    conn.execute(
        text(f"""
        INSERT INTO {s}.geocode_cache
            (address_key, query_used, status, postal, lat, lon, geom, raw_response, geocoded_at)
        VALUES (
            :key, :query, :status, :postal, :lat, :lon,
            ST_Transform(ST_SetSRID(ST_MakePoint(
                CAST(:lon AS double precision), CAST(:lat AS double precision)), 4326), 3414),
            CAST(:raw AS jsonb), now()
        )
        ON CONFLICT (address_key) DO UPDATE SET
            query_used = EXCLUDED.query_used,
            status = EXCLUDED.status,
            postal = EXCLUDED.postal,
            lat = EXCLUDED.lat,
            lon = EXCLUDED.lon,
            geom = EXCLUDED.geom,
            raw_response = EXCLUDED.raw_response,
            geocoded_at = EXCLUDED.geocoded_at"""),
        params,
    )


def read_overrides(path: Path) -> dict[str, Override]:
    """Parse the overrides CSV, rejecting bad rows with their line number."""
    if not path.exists():
        return {}
    overrides: dict[str, Override] = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != OVERRIDE_COLUMNS:
            raise ValueError(f"{path}: header must be {','.join(OVERRIDE_COLUMNS)}")
        for line, rec in enumerate(reader, start=2):
            key = normalise_key(rec["address_key"] or "")
            try:
                lat, lon = float(rec["lat"]), float(rec["lon"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{path}:{line}: lat/lon must be numbers") from exc
            if not key or not (
                math.isfinite(lat)
                and SG_LAT_RANGE[0] <= lat <= SG_LAT_RANGE[1]
                and SG_LON_RANGE[0] <= lon <= SG_LON_RANGE[1]
            ):
                raise ValueError(f"{path}:{line}: need an address_key and a lat/lon in Singapore")
            if key in overrides:
                raise ValueError(f"{path}:{line}: duplicate address_key {key!r}")
            postal = (rec["postal"] or "").strip() or None
            overrides[key] = Override(key, lat, lon, postal, (rec["note"] or "").strip())
    return overrides


def apply_overrides(
    conn: Connection, overrides: dict[str, Override], schema: str = "staging"
) -> int:
    """Write overrides as status `override`.

    Overrides removed from the file are dropped from the cache, so the address is geocoded
    again on the next run.
    """
    upsert_rows(
        conn,
        (
            CacheRow(o.address_key, "override", None, o.postal, o.lat, o.lon, {"note": o.note})
            for o in overrides.values()
        ),
        schema,
    )
    s = _schema(conn, schema)
    removed = conn.execute(
        text(
            f"DELETE FROM {s}.geocode_cache "
            "WHERE status = 'override' AND NOT (address_key = ANY(CAST(:keys AS text[])))"
        ),
        {"keys": list(overrides)},
    ).rowcount
    if removed:
        logger.info("Removed %d override(s) no longer in the overrides file", removed)
    return len(overrides)


def approximate_from_street(conn: Connection, schema: str = "staging") -> Counter[str]:
    """Place unmatched HDB blocks at the centroid of their street's located blocks.

    Applies to `failed` and `approx` rows and is recomputed on every run from the current
    cache, so centroids improve as more blocks are located and overrides take effect. Rows
    whose street has no located block become (or stay) `failed`. Returns the new statuses.
    """
    s = _schema(conn, schema)
    rows = conn.execute(
        text(f"""
        WITH located AS (
            SELECT DISTINCT ON (r.address_key) r.street_name, c.geom
            FROM {s}.hdb_resale AS r
            JOIN {s}.geocode_cache AS c USING (address_key)
            WHERE c.status = ANY(CAST(:exact AS text[]))
        ),
        centroids AS (
            SELECT street_name, ST_Centroid(ST_Collect(geom)) AS geom
            FROM located
            GROUP BY street_name
        ),
        targets AS (
            SELECT DISTINCT ON (r.address_key) r.address_key, ct.geom
            FROM {s}.hdb_resale AS r
            JOIN {s}.geocode_cache AS c USING (address_key)
            LEFT JOIN centroids AS ct USING (street_name)
            WHERE c.status IN ('failed', 'approx')
        )
        UPDATE {s}.geocode_cache AS c
        SET status = CASE WHEN t.geom IS NULL THEN 'failed' ELSE 'approx' END,
            postal = NULL,
            geom = t.geom,
            lat = ST_Y(ST_Transform(t.geom, 4326)),
            lon = ST_X(ST_Transform(t.geom, 4326))
        FROM targets AS t
        WHERE c.address_key = t.address_key
        RETURNING c.status"""),
        {"exact": list(EXACT_STATUSES)},
    )
    return Counter(status for (status,) in rows)


def write_failures(conn: Connection, path: Path, schema: str = "staging") -> int:
    """Rewrite the failures CSV with every address OneMap could not match.

    That is `failed` rows and `approx` rows (which carry their street-centroid location), for
    addresses still in the data; cache rows for keys that have left the data are not listed.
    """
    s = _schema(conn, schema)
    rows = conn.execute(
        text(f"""
        WITH {_wanted_cte(s)}
        SELECT c.address_key, c.status, c.query_used, c.raw_response ->> 'found',
               c.raw_response -> 'results' -> 0 ->> 'SEARCHVAL', c.lat, c.lon, c.geocoded_at
        FROM {s}.geocode_cache AS c
        JOIN wanted AS w ON w.key = c.address_key
        WHERE c.status IN ('failed', 'approx')
        ORDER BY c.address_key"""),
        {"prefix": POSTAL_KEY_PREFIX},
    ).all()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(FAILURE_COLUMNS)
        for key, status, query, found, top, lat, lon, at in rows:
            writer.writerow(
                [
                    key,
                    status,
                    query,
                    found,
                    top or "",
                    "" if lat is None else f"{lat:.6f}",
                    "" if lon is None else f"{lon:.6f}",
                    at.isoformat(),
                ]
            )
    return len(rows)


def run(
    engine: Engine,
    make_client: Callable[[], AbstractContextManager[Searcher]],
    *,
    overrides_path: Path,
    failures_path: Path,
    schema: str = "staging",
    retry_failed: bool = False,
    batch_size: int = 100,
) -> RunSummary:
    """Geocode pending addresses, committing every `batch_size`; then apply overrides and
    place unmatched blocks at their street centroid.

    The client is only created when there is something to look up, so a fully cached run
    makes no API calls and needs no credentials. Stopping mid-run keeps every committed batch
    (and the partial batch, on Ctrl-C); the next run picks up where this one stopped.
    """
    summary = RunSummary()
    overrides = read_overrides(overrides_path)
    with engine.begin() as conn:
        ensure_cache_table(conn, schema)
        pending = [
            a
            for a in pending_addresses(conn, schema, retry_failed=retry_failed)
            if a.key not in overrides
        ]
    summary.pending = len(pending)
    logger.info("%d address(es) to geocode", len(pending))

    if pending:
        with (
            make_client() as client,
            tqdm(total=len(pending), unit="addr", desc="geocode") as bar,
        ):
            batch: list[CacheRow] = []
            try:
                for addr in pending:
                    row = geocode_one(client, addr)
                    batch.append(row)
                    summary.statuses[row.status] += 1
                    bar.update()
                    if len(batch) >= batch_size:
                        with engine.begin() as conn:
                            upsert_rows(conn, batch, schema)
                        batch.clear()
            finally:
                if batch:
                    with engine.begin() as conn:
                        upsert_rows(conn, batch, schema)
                summary.requests = Counter(client.request_counts)

    with engine.begin() as conn:
        summary.overrides = apply_overrides(conn, overrides, schema)
        summary.approximated = approximate_from_street(conn, schema)["approx"]
    with engine.connect() as conn:
        summary.failures_reported = write_failures(conn, failures_path, schema)
    return summary
