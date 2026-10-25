"""`python -m pipeline.geocode`: geocode HDB blocks and school postal codes via OneMap."""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import Engine, text

from config.settings import REPO_ROOT, get_settings
from pipeline.common.db import get_engine
from pipeline.common.log import setup_logging
from pipeline.common.sql_runner import run_sql_files
from pipeline.geocode.onemap import OneMapAuthError, OneMapClient
from pipeline.geocode.run import DONE_STATUSES, EXACT_STATUSES, run

logger = logging.getLogger("pipeline.geocode")

OVERRIDES_PATH = REPO_ROOT / "data" / "manual" / "geocode_overrides.csv"
COVERAGE_TARGET = 0.995


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(prog="python -m pipeline.geocode", description=__doc__)
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="query OneMap again for addresses that failed on an earlier run",
    )
    return parser.parse_args(argv)


def coverage(engine: Engine) -> tuple[int, int, int]:
    """(exactly located, located incl. street-centroid approximations, total) distinct HDB
    addresses in staging.hdb_resale."""
    with engine.connect() as conn:
        exact, done, total = conn.execute(
            text("""
            SELECT count(*) FILTER (WHERE c.status = ANY(CAST(:exact AS text[]))),
                   count(*) FILTER (WHERE c.status = ANY(CAST(:done AS text[]))),
                   count(*)
            FROM (SELECT DISTINCT address_key FROM staging.hdb_resale) AS a
            LEFT JOIN staging.geocode_cache AS c USING (address_key)"""),
            {"exact": list(EXACT_STATUSES), "done": list(DONE_STATUSES)},
        ).one()
    return exact, done, total


def main(argv: list[str] | None = None) -> int:
    """Run geocoding and log a summary."""
    args = parse_args(argv)
    setup_logging()
    settings = get_settings()
    engine = get_engine()
    run_sql_files(engine, "0*.sql")

    try:
        summary = run(
            engine,
            lambda: OneMapClient.from_settings(settings),
            overrides_path=OVERRIDES_PATH,
            failures_path=settings.reports_dir / "geocode_failures.csv",
            retry_failed=args.retry_failed,
        )
    except OneMapAuthError as exc:
        logger.error("%s", exc)
        return 1
    run_sql_files(engine, "130_*.sql")  # copy school locations into staging.schools

    exact, done, total = coverage(engine)
    share = done / total if total else 0.0
    logger.info(
        "Geocoded %d address(es) this run (%s). Requests: token=%d search=%d. "
        "Overrides applied: %d. Street-centroid approximations: %d. Failures reported: %d.",
        summary.pending,
        ", ".join(f"{k}={v}" for k, v in sorted(summary.statuses.items())) or "none",
        summary.requests["token"],
        summary.requests["search"],
        summary.overrides,
        summary.approximated,
        summary.failures_reported,
    )
    log = logger.info if share >= COVERAGE_TARGET else logger.warning
    log(
        "HDB address coverage: %d / %d (%.2f%%; target %.1f%%), of which %d exact and %d "
        "street-centroid approximations.",
        done,
        total,
        100 * share,
        100 * COVERAGE_TARGET,
        exact,
        done - exact,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
