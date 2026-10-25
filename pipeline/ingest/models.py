"""Typed views of data.gov.sg metadata responses."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class CollectionMetadata(BaseModel):
    """Subset of `/v2/public/api/collections/{id}/metadata`."""

    collection_id: str
    name: str
    child_datasets: list[str]

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> CollectionMetadata:
        meta = data["collectionMetadata"]
        return cls(
            collection_id=str(meta["collectionId"]),
            name=meta["name"],
            child_datasets=list(meta["childDatasets"]),
        )


class DatasetMetadata(BaseModel):
    """Subset of `/v2/public/api/datasets/{id}/metadata`.

    `columns` is the CSV column order; it is None for non-tabular formats such as GeoJSON,
    where the API returns `columnMetadata: null`.
    """

    dataset_id: str
    name: str
    format: str
    last_updated_at: str | None = None
    coverage_start: str | None = None
    coverage_end: str | None = None
    columns: list[str] | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> DatasetMetadata:
        column_meta = data.get("columnMetadata") or None
        columns = None
        if column_meta:
            mapping = column_meta["map"]
            columns = [mapping[key] for key in column_meta["order"]]
        return cls(
            dataset_id=data["datasetId"],
            name=data["name"],
            format=str(data["format"]).upper(),
            last_updated_at=data.get("lastUpdatedAt"),
            coverage_start=data.get("coverageStart"),
            coverage_end=data.get("coverageEnd"),
            columns=columns,
        )
