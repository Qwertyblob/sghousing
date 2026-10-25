"""Apply numbered `.sql` files from `pipeline/sql/` in order.

CLI: `python -m pipeline.common.sql_runner PATTERN [PATTERN ...]` runs each glob in turn.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from sqlalchemy import Engine

from config.settings import REPO_ROOT
from pipeline.common.db import get_engine
from pipeline.common.log import setup_logging

logger = logging.getLogger(__name__)

SQL_DIR = REPO_ROOT / "pipeline" / "sql"


def run_sql_files(engine: Engine, pattern: str, sql_dir: Path = SQL_DIR) -> list[Path]:
    """Run every file matching `pattern` (sorted by name), each in its own transaction.

    Files go straight to the psycopg cursor without parameters, so `%` in SQL (LIKE
    patterns, RAISE format strings) is passed through untouched.
    """
    files = sorted(sql_dir.glob(pattern))
    if not files:
        raise FileNotFoundError(f"no SQL files match {pattern!r} in {sql_dir}")
    for path in files:
        logger.info("Applying %s", path.name)
        with engine.begin() as conn, conn.connection.driver_connection.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
    return files


def main(argv: list[str] | None = None) -> None:
    """Run each glob pattern from the command line, in the order given."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("patterns", nargs="+", help="glob(s) relative to pipeline/sql/")
    args = parser.parse_args(argv)
    setup_logging()
    engine = get_engine()
    try:
        for pattern in args.patterns:
            run_sql_files(engine, pattern)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
