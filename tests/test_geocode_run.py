"""Geocoding runs against Postgres, in a throwaway schema. Run with `make test-db`."""

from __future__ import annotations

import csv
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from config.settings import get_settings
from pipeline.common.db import get_engine
from pipeline.common.sql_runner import SQL_DIR
from pipeline.geocode.run import (
    CacheRow,
    RunSummary,
    approximate_from_street,
    ensure_cache_table,
    run,
    upsert_rows,
)

pytestmark = pytest.mark.db

LONE = ("1", "SHORT ST")  # demolished street: nothing on it is ever located
BLOCKS = [(f"{n}", "BT BATOK CTRL") for n in range(1, 251)] + [LONE]  # 251 addresses
FAIL_BLOCK = "13"  # demolished block on a street with located neighbours
N_ADDRESSES = 252  # 251 distinct blocks + 1 school postal code
SCHOOL_POSTAL = "659163"


class FakeSearcher:
    """Answers every block except FAIL_BLOCK and SHORT ST; block 7 only via the expanded
    query."""

    def __init__(self, stop_after: int | None = None) -> None:
        self.request_counts: Counter[str] = Counter()
        self.stop_after = stop_after

    def search(self, query: str) -> dict[str, Any]:
        self.request_counts["search"] += 1
        if self.stop_after is not None and self.request_counts["search"] > self.stop_after:
            raise KeyboardInterrupt
        if query == SCHOOL_POSTAL:
            return _body({"BLK_NO": "1", "ROAD_NAME": "X ROAD", "POSTAL": SCHOOL_POSTAL})
        block, _, road = query.partition(" ")
        if block == FAIL_BLOCK or road.startswith("SHORT"):
            return _body()
        if block == "7" and road == "BT BATOK CTRL":
            return _body()
        return _body({"BLK_NO": block, "ROAD_NAME": "BUKIT BATOK CENTRAL", "POSTAL": "650000"})


def _body(*results: dict[str, str]) -> dict[str, Any]:
    full = [{**r, "LATITUDE": "1.349", "LONGITUDE": "103.749"} for r in results]
    return {"found": len(full), "totalNumPages": 1, "pageNum": 1, "results": full}


@pytest.fixture
def engine() -> Iterator[Engine]:
    get_settings.cache_clear()
    eng = get_engine()
    yield eng
    eng.dispose()


@pytest.fixture
def schema(engine: Engine) -> Iterator[str]:
    name = f"test_geocode_{uuid.uuid4().hex[:10]}"
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{name}"'))
        conn.execute(
            text(
                f'CREATE TABLE "{name}".hdb_resale (address_key text, block text, street_name text)'
            )
        )
        conn.execute(text(f'CREATE TABLE "{name}".schools (postal_code text)'))
        conn.execute(
            text(f'INSERT INTO "{name}".hdb_resale VALUES (:k, :b, :s)'),
            [{"k": f"{b} {s}", "b": b, "s": s} for b, s in BLOCKS + BLOCKS[:5]],  # duplicates
        )
        conn.execute(text(f"INSERT INTO \"{name}\".schools VALUES ('{SCHOOL_POSTAL}'), (NULL)"))
    yield name
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{name}" CASCADE'))


@pytest.fixture
def paths(tmp_path: Path) -> dict[str, Path]:
    overrides = tmp_path / "overrides.csv"
    overrides.write_text("address_key,lat,lon,postal,note\n", encoding="utf-8")
    return {"overrides_path": overrides, "failures_path": tmp_path / "reports" / "failures.csv"}


Run = Callable[..., RunSummary]


@pytest.fixture
def do_run(engine: Engine, schema: str, paths: dict[str, Path]) -> Run:
    def go(searcher: FakeSearcher | None = None, **kwargs: Any) -> RunSummary:
        def make_client() -> nullcontext[FakeSearcher]:
            if searcher is None:
                raise AssertionError("client created although nothing should be looked up")
            return nullcontext(searcher)

        return run(engine, make_client, schema=schema, **paths, **kwargs)

    return go


def statuses(engine: Engine, schema: str) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(text(f'SELECT address_key, status FROM "{schema}".geocode_cache'))
        return {key: status for key, status in rows}


def test_first_run_caches_everything_and_second_run_makes_no_calls(
    engine: Engine, schema: str, paths: dict[str, Path], do_run: Run
) -> None:
    summary = do_run(FakeSearcher())

    cached = statuses(engine, schema)
    assert len(cached) == N_ADDRESSES
    assert cached["1 BT BATOK CTRL"] == "ok"
    assert cached["7 BT BATOK CTRL"] == "fallback"
    assert cached[f"{FAIL_BLOCK} BT BATOK CTRL"] == "approx"
    assert cached["1 SHORT ST"] == "failed"
    assert cached[f"POSTAL {SCHOOL_POSTAL}"] == "ok"
    assert summary.statuses == {"ok": 249, "fallback": 1, "failed": 2}  # API outcomes
    assert summary.approximated == 1

    with engine.connect() as conn:
        srid, n_geom = conn.execute(
            text(f'SELECT max(ST_SRID(geom)), count(geom) FROM "{schema}".geocode_cache')
        ).one()
    assert (srid, n_geom) == (3414, N_ADDRESSES - 1)

    with paths["failures_path"].open(newline="") as f:
        failures = {r["address_key"]: r for r in csv.DictReader(f)}
    assert set(failures) == {f"{FAIL_BLOCK} BT BATOK CTRL", "1 SHORT ST"}
    approx = failures[f"{FAIL_BLOCK} BT BATOK CTRL"]
    assert approx["status"] == "approx"
    assert (float(approx["lat"]), float(approx["lon"])) == pytest.approx((1.349, 103.749))
    assert failures["1 SHORT ST"]["status"] == "failed" and failures["1 SHORT ST"]["lat"] == ""

    with engine.begin() as conn:  # a cached failure whose address has left the data
        upsert_rows(conn, [CacheRow("9 GONE RD", "failed", "9 GONE RD")], schema)
    again = do_run(None)  # make_client must not be called
    with paths["failures_path"].open(newline="") as f:
        assert {r["address_key"] for r in csv.DictReader(f)} == set(failures)
    assert again.pending == 0 and again.requests == Counter()
    assert statuses(engine, schema) == {**cached, "9 GONE RD": "failed"}


def test_interrupted_run_keeps_committed_batches_and_resumes(
    engine: Engine, schema: str, do_run: Run
) -> None:
    with pytest.raises(KeyboardInterrupt):
        do_run(FakeSearcher(stop_after=150), batch_size=100)
    saved = len(statuses(engine, schema))
    assert 100 <= saved < N_ADDRESSES

    summary = do_run(FakeSearcher())
    assert summary.pending == N_ADDRESSES - saved
    assert len(statuses(engine, schema)) == N_ADDRESSES


def test_retry_failed_queries_failures_again(engine: Engine, schema: str, do_run: Run) -> None:
    do_run(FakeSearcher())
    searcher = FakeSearcher()
    summary = do_run(searcher, retry_failed=True)
    assert summary.pending == 2  # the approx and the failed address
    assert searcher.request_counts["search"] == 4  # exact + expanded query for each


def test_overrides_win_and_are_removed_with_the_file_entry(
    engine: Engine, schema: str, paths: dict[str, Path], do_run: Run
) -> None:
    key = f"{FAIL_BLOCK} BT BATOK CTRL"
    paths["overrides_path"].write_text(
        f"address_key,lat,lon,postal,note\n{key.lower()},1.3500,103.7500,650013,checked on map\n",
        encoding="utf-8",
    )
    searcher = FakeSearcher()
    do_run(searcher)
    assert statuses(engine, schema)[key] == "override"
    # 251 addresses queried (13 skipped); block 7 and SHORT ST need two queries each
    assert searcher.request_counts["search"] == 253

    paths["overrides_path"].write_text("address_key,lat,lon,postal,note\n", encoding="utf-8")
    do_run(None)
    assert key not in statuses(engine, schema)  # so the next run geocodes it again


@pytest.mark.parametrize("row", ["x,abc,103.8,,", "x,0.0,0.0,,", ",1.35,103.8,,"])
def test_bad_override_rows_are_rejected(paths: dict[str, Path], do_run: Run, row: str) -> None:
    paths["overrides_path"].write_text(f"address_key,lat,lon,postal,note\n{row}\n")
    with pytest.raises(ValueError, match=":2:"):
        do_run(None)


def test_street_centroid_is_recomputed_from_located_blocks(engine: Engine, schema: str) -> None:
    rows = [("1", "X ST"), ("2", "X ST"), ("3", "X ST"), ("1", "Y ST")]
    with engine.begin() as conn:
        conn.execute(
            text(f'INSERT INTO "{schema}".hdb_resale VALUES (:k, :b, :s)'),
            [{"k": f"{b} {s}", "b": b, "s": s} for b, s in rows],
        )
        ensure_cache_table(conn, schema)
        upsert_rows(
            conn,
            [
                CacheRow("1 X ST", "ok", "q", None, 1.30, 103.80),
                CacheRow("2 X ST", "override", None, None, 1.32, 103.82),
                CacheRow("3 X ST", "failed", "q"),
                CacheRow("1 Y ST", "failed", "q"),
            ],
            schema,
        )
        assert approximate_from_street(conn, schema) == {"approx": 1, "failed": 1}

    def location(key: str) -> tuple[str, float | None, float | None]:
        with engine.connect() as conn:
            return conn.execute(
                text(
                    f'SELECT status, lat, lon FROM "{schema}".geocode_cache WHERE address_key = :k'
                ),
                {"k": key},
            ).one()

    status, lat, lon = location("3 X ST")
    assert status == "approx" and (lat, lon) == pytest.approx((1.31, 103.81), abs=1e-4)
    assert location("1 Y ST") == ("failed", None, None)

    with engine.begin() as conn:  # a neighbour is lost: the centroid moves to the one left
        upsert_rows(conn, [CacheRow("2 X ST", "failed", "q")], schema)
        approximate_from_street(conn, schema)
    status, lat, lon = location("3 X ST")
    assert status == "approx" and (lat, lon) == pytest.approx((1.30, 103.80), abs=1e-6)
    # "2 X ST" itself is now approximated from "1 X ST" too
    assert location("2 X ST")[0] == "approx"


def test_school_geom_follows_the_cache(engine: Engine, schema: str) -> None:
    """130_staging_school_geom.sql, run against the throwaway schema."""
    sql = (SQL_DIR / "130_staging_school_geom.sql").read_text().replace("staging.", f'"{schema}".')
    key = f"POSTAL {SCHOOL_POSTAL}"
    with engine.begin() as conn:
        conn.execute(text(f'ALTER TABLE "{schema}".schools ADD COLUMN geom geometry(Point, 3414)'))
        ensure_cache_table(conn, schema)
        upsert_rows(conn, [CacheRow(key, "ok", SCHOOL_POSTAL, SCHOOL_POSTAL, 1.35, 103.75)], schema)
        conn.exec_driver_sql(sql)

    def located() -> int:
        with engine.connect() as conn:
            return conn.execute(text(f'SELECT count(geom) FROM "{schema}".schools')).scalar_one()

    assert located() == 1
    with engine.begin() as conn:
        upsert_rows(conn, [CacheRow(key, "failed", SCHOOL_POSTAL)], schema)
        conn.exec_driver_sql(sql)
    assert located() == 0  # a location that left the cache is cleared


# --- Phase 3 acceptance on live data ------------------------------------------------------


@pytest.fixture
def live(engine: Engine) -> Engine:
    with engine.connect() as conn:
        exists = conn.execute(text("SELECT to_regclass('staging.geocode_cache') IS NOT NULL"))
        if (
            not exists.scalar_one()
            or not conn.execute(
                text("SELECT EXISTS (SELECT 1 FROM staging.geocode_cache)")
            ).scalar_one()
        ):
            pytest.skip("nothing geocoded yet (run `make geocode`)")
    return engine


def test_live_coverage_at_least_99_5_percent(live: Engine) -> None:
    with live.connect() as conn:
        share = conn.execute(
            text("""
            -- Uncached addresses (c.status NULL) count as failures, not as missing data.
            SELECT avg(coalesce(c.status IN ('ok', 'fallback', 'override', 'approx'), false)::int)
            FROM (SELECT DISTINCT address_key FROM staging.hdb_resale) AS a
            LEFT JOIN staging.geocode_cache AS c USING (address_key)""")
        ).scalar_one()
    assert share >= 0.995


def test_live_points_inside_singapore_and_a_planning_area(live: Engine) -> None:
    with live.connect() as conn:
        outside = conn.execute(
            text("""
            SELECT count(*) FROM staging.geocode_cache AS c
            WHERE c.geom IS NOT NULL AND (
                c.lat NOT BETWEEN 1.15 AND 1.48 OR c.lon NOT BETWEEN 103.59 AND 104.10
                OR NOT EXISTS (
                    SELECT 1 FROM staging.planning_areas AS p WHERE ST_Intersects(p.geom, c.geom)
                )
            )""")
        ).scalar_one()
    assert outside == 0
