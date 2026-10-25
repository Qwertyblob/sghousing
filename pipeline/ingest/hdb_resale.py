"""HDB resale flat prices: every child dataset of data.gov.sg collection 189."""

from __future__ import annotations

import logging

from sqlalchemy import Engine

from pipeline.ingest.datagov import DataGovClient, DataGovError
from pipeline.ingest.load import LoadResult, load_snapshot

logger = logging.getLogger(__name__)

# https://data.gov.sg/collections/189/view
RESALE_COLLECTION_ID = "189"
SOURCE = "hdb_resale"
RAW_TABLE = "hdb_resale"


def ingest_resale(
    client: DataGovClient, engine: Engine, *, force: bool = False
) -> list[LoadResult]:
    """Download and load every resale dataset listed in the collection metadata."""
    collection = client.get_collection_metadata(RESALE_COLLECTION_ID)
    logger.info(
        "Collection %s (%s): %d datasets",
        collection.collection_id,
        collection.name,
        len(collection.child_datasets),
    )
    results = []
    for dataset_id in collection.child_datasets:
        snapshot = client.download_dataset(dataset_id, SOURCE, force=force)
        if snapshot.metadata.format != "CSV":
            raise DataGovError("unexpected-format", f"{dataset_id} is {snapshot.metadata.format}")
        results.append(load_snapshot(engine, snapshot, table=RAW_TABLE))
    return results
