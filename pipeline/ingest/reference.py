"""Reference datasets used for location features.

Every dataset ID below was checked against the data.gov.sg metadata endpoint. Each loads into
`raw.ref_{key}`; GeoJSON as (properties jsonb, geometry_geojson text), CSV as text columns.

Supermarkets are intentionally absent: a full catalog scan (Oct 2026) found no current
supermarket location dataset. The only candidate, "List of Supermarket Licences"
(d_11edd0117280c5776651d7891114c88c), is a June 2016 snapshot without coordinates.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import Engine

from pipeline.ingest.datagov import DataGovClient
from pipeline.ingest.load import LoadResult, load_snapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReferenceDataset:
    """A reference dataset and the raw table it loads into."""

    key: str
    dataset_id: str
    description: str

    @property
    def raw_table(self) -> str:
        return f"ref_{self.key}"


REFERENCE_DATASETS: tuple[ReferenceDataset, ...] = (
    # https://data.gov.sg/datasets/d_b39d3a0871985372d7e1637193335da5/view
    ReferenceDataset(
        "mrt_exits", "d_b39d3a0871985372d7e1637193335da5", "LTA MRT/LRT station exits"
    ),
    # https://data.gov.sg/datasets/d_4765db0e87b9c86336792efe8a1f7a66/view
    ReferenceDataset(
        "planning_areas",
        "d_4765db0e87b9c86336792efe8a1f7a66",
        "URA Master Plan 2019 planning area boundaries (no sea)",
    ),
    # https://data.gov.sg/datasets/d_17f5382f26140b1fdae0ba2ef6239d2f/view
    ReferenceDataset(
        "hdb_block_info",
        "d_17f5382f26140b1fdae0ba2ef6239d2f",
        "HDB property information (max floor, year completed per block)",
    ),
    # https://data.gov.sg/datasets/d_688b934f82c1059ed0a6993d2a829089/view
    ReferenceDataset(
        "schools",
        "d_688b934f82c1059ed0a6993d2a829089",
        "MOE general information of schools (no coordinates; geocoded in Phase 3)",
    ),
    # https://data.gov.sg/datasets/d_4a086da0a5553be1d89383cd90d07ecd/view
    ReferenceDataset("hawker_centres", "d_4a086da0a5553be1d89383cd90d07ecd", "NEA hawker centres"),
    # https://data.gov.sg/datasets/d_77d7ec97be83d44f61b85454f844382f/view
    ReferenceDataset(
        "parks",
        "d_77d7ec97be83d44f61b85454f844382f",
        "NParks parks and nature reserves (polygons)",
    ),
    # https://data.gov.sg/datasets/d_14f63e595975691e7c24a27ae4c07c79/view
    ReferenceDataset(
        "resale_price_index",
        "d_14f63e595975691e7c24a27ae4c07c79",
        "HDB resale price index (1Q2009 = 100), quarterly",
    ),
)

REFERENCE_KEYS = tuple(ds.key for ds in REFERENCE_DATASETS)


def ingest_reference(
    client: DataGovClient,
    engine: Engine,
    *,
    keys: Iterable[str] | None = None,
    force: bool = False,
) -> list[LoadResult]:
    """Download and load reference datasets (all, or only `keys`)."""
    wanted = set(keys) if keys is not None else set(REFERENCE_KEYS)
    unknown = wanted - set(REFERENCE_KEYS)
    if unknown:
        raise ValueError(f"unknown reference datasets: {sorted(unknown)}")
    results = []
    for dataset in REFERENCE_DATASETS:
        if dataset.key not in wanted:
            continue
        snapshot = client.download_dataset(dataset.dataset_id, dataset.key, force=force)
        results.append(load_snapshot(engine, snapshot, table=dataset.raw_table))
    return results
