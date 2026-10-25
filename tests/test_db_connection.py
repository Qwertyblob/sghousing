"""End-to-end check: Python settings -> engine -> host port -> Postgres/PostGIS.

Run with `make test-db`. Fails (does not skip) if the database is unreachable.
"""

from __future__ import annotations

import pytest

from config.settings import get_settings
from pipeline.common.db import PROJECT_SCHEMAS, check_connection, get_engine

pytestmark = pytest.mark.db


def test_python_can_reach_postgis_and_schemas() -> None:
    get_settings.cache_clear()
    postgis, schemas = check_connection(get_engine())
    assert postgis.startswith("3.4"), postgis
    assert schemas == sorted(PROJECT_SCHEMAS)
