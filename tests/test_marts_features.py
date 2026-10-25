"""Phase 4 acceptance on live data: marts.block_features and marts.resale_features.

Run with `make test-db` after `make features`; skipped when the marts have not been built.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text

from config.settings import get_settings
from pipeline.common.db import get_engine

pytestmark = pytest.mark.db

# Real rural blocks farther than the spec's 5 km sanity limit from any MRT exit (Lim Chu
# Kang, sold 1990-1998). The limit exists to catch coordinate-system mistakes, which would
# move every block, not two.
KNOWN_REMOTE_BLOCKS = {"3 LIM CHU KANG RD", "4 LIM CHU KANG RD"}


@pytest.fixture(scope="module")
def live() -> Iterator[Engine]:
    get_settings.cache_clear()
    engine = get_engine()
    with engine.connect() as conn:
        built = conn.execute(
            text("SELECT to_regclass('marts.resale_features') IS NOT NULL")
        ).scalar_one()
    if not built:
        engine.dispose()
        pytest.skip("marts not built yet (run `make features`)")
    yield engine
    engine.dispose()


def scalar(engine: Engine, sql: str, **params: Any) -> Any:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def test_one_row_per_located_block(live: Engine) -> None:
    located = scalar(
        live,
        """
        SELECT count(*) FROM staging.geocode_cache AS c
        WHERE c.status IN ('ok', 'fallback', 'override', 'approx')
          AND EXISTS (SELECT 1 FROM staging.hdb_resale AS r WHERE r.address_key = c.address_key)
        """,
    )
    assert located > 0
    assert scalar(live, "SELECT count(*) FROM marts.block_features") == located


def test_every_block_feature_is_filled(live: Engine) -> None:
    with live.connect() as conn:
        columns = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'marts' AND table_name = 'block_features'"
            )
        ).scalars()
        nulls = {
            c: conn.execute(
                text(f'SELECT count(*) FROM marts.block_features WHERE "{c}" IS NULL')
            ).scalar_one()
            for c in columns
        }
    assert {c: n for c, n in nulls.items() if n} == {}


def test_mrt_distance_sanity(live: Engine) -> None:
    median = scalar(
        live,
        "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY dist_mrt_m) FROM marts.block_features",
    )
    assert median < 1500
    with live.connect() as conn:
        far = set(
            conn.execute(
                text("SELECT address_key FROM marts.block_features WHERE dist_mrt_m >= 5000")
            ).scalars()
        )
    assert far <= KNOWN_REMOTE_BLOCKS


def test_counts_and_distances_are_plausible(live: Engine) -> None:
    bad = scalar(
        live,
        """
        SELECT count(*) FROM marts.block_features
        WHERE n_mrt_exits_500m < 0 OR n_primary_schools_1km < 0
           OR dist_park_m < 0 OR dist_hawker_m < 0 OR dist_nearest_primary_m < 0
           OR dist_cbd_m > 30000 OR block_max_floor < 1
           OR block_year_completed NOT BETWEEN 1930 AND extract(year FROM now())
           OR (n_mrt_exits_500m > 0) <> (dist_mrt_m <= 500)
           OR (n_primary_schools_1km > 0) <> (dist_nearest_primary_m <= 1000)
        """,
    )
    assert bad == 0


def test_interchange_lrt_exits_serve_mrt(live: Engine) -> None:
    orphans = scalar(
        live,
        """
        SELECT count(*) FROM staging.mrt_exits AS e
        WHERE e.serves_mrt AND e.rail_type = 'LRT' AND NOT EXISTS (
            SELECT 1 FROM staging.mrt_exits AS m
            WHERE m.station_base = e.station_base AND m.rail_type = 'MRT'
        )
        """,
    )
    assert orphans == 0
    assert (
        scalar(
            live, "SELECT count(*) FROM staging.mrt_exits WHERE serves_mrt AND rail_type = 'LRT'"
        )
        > 0
    )


def test_resale_features_one_row_per_transaction(live: Engine) -> None:
    assert scalar(live, "SELECT count(*) FROM marts.resale_features") == scalar(
        live, "SELECT count(*) FROM staging.hdb_resale"
    )


def test_only_unlocatable_transactions_lack_features(live: Engine) -> None:
    bad = scalar(
        live,
        """
        SELECT count(*) FROM marts.resale_features AS r
        LEFT JOIN staging.geocode_cache AS c USING (address_key)
        WHERE (r.geocode_status IS NULL)
              <> coalesce(c.status NOT IN ('ok', 'fallback', 'override', 'approx'), true)
        """,
    )
    assert bad == 0
    assert (
        scalar(
            live,
            "SELECT count(*) FROM marts.resale_features WHERE geocode_status IS NULL "
            "AND month >= DATE '2015-01-01'",
        )
        == 0
    )


def test_month_index_and_rel_storey(live: Engine) -> None:
    bad = scalar(
        live,
        """
        SELECT count(*) FROM marts.resale_features
        WHERE month_index <> (extract(year FROM month)::int - 1990) * 12
                             + extract(month FROM month)::int - 1
           OR month_index < 0
           OR rel_storey IS NULL OR rel_storey <= 0 OR rel_storey > 1
           OR storey_lo > block_max_floor
        """,
    )
    assert bad == 0


def test_property_info_only_describes_current_generation(live: Engine) -> None:
    assert (
        scalar(
            live,
            "SELECT count(*) FROM marts.block_generations "
            "WHERE block_info_source = 'hdb_property_info' AND NOT is_current",
        )
        == 0
    )
    # every sale belongs to exactly one generation of its address
    assert (
        scalar(
            live,
            """
            SELECT count(*) FROM staging.hdb_resale AS r
            WHERE (
                SELECT count(*) FROM marts.block_generations AS g
                WHERE g.address_key = r.address_key
                  AND r.lease_commence_year BETWEEN g.lease_year_from AND g.lease_year_to
            ) <> 1
            """,
        )
        == 0
    )


def test_rebuilt_block_keeps_generations_apart(live: Engine) -> None:
    """21 TEBAN GDNS RD: a 1978 block (15 storeys, sold 1990-1999) was replaced by a
    40-storey block completed in 2012 (lease 2013) under the same number."""
    with live.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT lease_commence_year, block_max_floor, block_year_completed "
                "FROM marts.resale_features WHERE address_key = '21 TEBAN GDNS RD' ORDER BY 1"
            )
        ).all()
    if not rows:
        pytest.skip("21 TEBAN GDNS RD not in the data")
    assert [tuple(r) for r in rows] == [(1978, 15, 1978), (2013, 40, 2012)]


def test_new_generation_only_after_old_sales_end(live: Engine) -> None:
    """A later generation is a replacement building: no earlier generation's sale may fall
    in or after its lease start year."""
    overlapping = scalar(
        live,
        """
        SELECT count(*) FROM marts.block_generations AS g
        JOIN staging.hdb_resale AS r
          ON r.address_key = g.address_key AND r.lease_commence_year < g.lease_year_from
        WHERE g.generation > 0 AND extract(year FROM r.month) >= g.lease_year_from
        """,
    )
    assert overlapping == 0


def test_concurrent_lease_years_stay_one_building(live: Engine) -> None:
    """37 TEBAN GDNS RD: 1966-lease sales run 2004-2026 and one 1981-lease sale is in 2025,
    so both lease years are the same standing building."""
    with live.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT block_generation, block_max_floor, block_year_completed "
                "FROM marts.resale_features WHERE address_key = '37 TEBAN GDNS RD'"
            )
        ).all()
    if not rows:
        pytest.skip("37 TEBAN GDNS RD not in the data")
    assert [tuple(r) for r in rows] == [(0, 5, 1976)]


@pytest.mark.parametrize(
    ("table", "column", "method"),
    [
        ("block_features", "geom", "gist"),
        ("block_generations", "address_key, generation", "btree"),
        ("resale_features", "month", "btree"),
        ("resale_features", "town", "btree"),
        ("resale_features", "address_key", "btree"),
    ],
)
def test_indexes_exist(live: Engine, table: str, column: str, method: str) -> None:
    count = scalar(
        live,
        """
        SELECT count(*) FROM pg_indexes
        WHERE schemaname = 'marts' AND tablename = :t
          AND indexdef ILIKE '%USING ' || :m || ' (' || :c || ')%'
        """,
        t=table,
        c=column,
        m=method,
    )
    assert count == 1
