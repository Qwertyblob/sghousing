"""Database engine construction and a connectivity check (`python -m pipeline.common.db`)."""

from __future__ import annotations

import logging
import sys

from sqlalchemy import Engine, create_engine, text

from config.settings import get_settings
from pipeline.common.log import setup_logging

logger = logging.getLogger(__name__)

PROJECT_SCHEMAS = ("raw", "staging", "marts")


def get_engine() -> Engine:
    """Build a SQLAlchemy engine for `DATABASE_URL`."""
    return create_engine(get_settings().database_url, pool_pre_ping=True)


def check_connection(engine: Engine) -> tuple[str, list[str]]:
    """Return the PostGIS version and which project schemas exist.

    Raises if the database is unreachable or PostGIS is not installed.
    """
    with engine.connect() as conn:
        postgis = conn.execute(text("SELECT postgis_version()")).scalar_one()
        schemas = (
            conn.execute(
                text(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name = ANY(:names) ORDER BY schema_name"
                ),
                {"names": list(PROJECT_SCHEMAS)},
            )
            .scalars()
            .all()
        )
    return postgis, list(schemas)


def main() -> int:
    """Log connection details; exit non-zero if PostGIS or any project schema is missing."""
    setup_logging()
    engine = get_engine()
    logger.info("Connecting to %s", engine.url.render_as_string(hide_password=True))
    postgis, schemas = check_connection(engine)
    logger.info("PostGIS version: %s", postgis)
    missing = sorted(set(PROJECT_SCHEMAS) - set(schemas))
    if missing:
        logger.error("Missing schemas: %s (run `make db-up`)", ", ".join(missing))
        return 1
    logger.info("Schemas present: %s", ", ".join(schemas))
    return 0


if __name__ == "__main__":
    sys.exit(main())
