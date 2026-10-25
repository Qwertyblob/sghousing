"""`python -m pipeline.ingest`: download and load resale and reference datasets."""

from __future__ import annotations

import argparse
import logging
import sys

from config.settings import get_settings
from pipeline.common.db import get_engine
from pipeline.common.log import setup_logging
from pipeline.common.sql_runner import run_sql_files
from pipeline.ingest.datagov import DataGovClient
from pipeline.ingest.hdb_resale import ingest_resale
from pipeline.ingest.load import LoadResult
from pipeline.ingest.reference import REFERENCE_KEYS, ingest_reference

logger = logging.getLogger("pipeline.ingest")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(prog="python -m pipeline.ingest", description=__doc__)
    parser.add_argument(
        "--only",
        choices=["resale", "reference", *REFERENCE_KEYS],
        help="ingest only the resale collection, all reference data, or one reference dataset",
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="download again even if today's snapshot exists",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run ingestion and log a summary."""
    args = parse_args(argv)
    setup_logging()
    settings = get_settings()
    engine = get_engine()
    run_sql_files(engine, "0*.sql")

    results: list[LoadResult] = []
    with DataGovClient.from_settings(settings) as client:
        if args.only in (None, "resale"):
            results += ingest_resale(client, engine, force=args.force_download)
        if args.only is None or args.only == "reference":
            results += ingest_reference(client, engine, force=args.force_download)
        elif args.only in REFERENCE_KEYS:
            results += ingest_reference(client, engine, keys=[args.only], force=args.force_download)
        counts = client.request_counts

    for r in results:
        logger.info("%-38s %-24s %-8s %8d rows", r.dataset_id, r.table, r.status, r.row_count)
    loaded = sum(r.status == "loaded" for r in results)
    logger.info(
        "%d datasets: %d loaded, %d unchanged. "
        "Requests: metadata=%d initiate=%d poll=%d storage=%d",
        len(results),
        loaded,
        len(results) - loaded,
        counts["metadata"],
        counts["initiate"],
        counts["poll"],
        counts["storage"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
