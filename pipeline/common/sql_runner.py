"""Apply numbered `.sql` files from `pipeline/sql/` in order."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import Engine

from config.settings import REPO_ROOT

logger = logging.getLogger(__name__)

SQL_DIR = REPO_ROOT / "pipeline" / "sql"


def run_sql_files(engine: Engine, pattern: str, sql_dir: Path = SQL_DIR) -> list[Path]:
    """Run every file matching `pattern` (sorted by name), each in its own transaction."""
    files = sorted(sql_dir.glob(pattern))
    if not files:
        raise FileNotFoundError(f"no SQL files match {pattern!r} in {sql_dir}")
    for path in files:
        logger.info("Applying %s", path.name)
        with engine.begin() as conn:
            conn.exec_driver_sql(path.read_text(encoding="utf-8"))
    return files
