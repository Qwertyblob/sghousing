"""Shared test helpers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from pipeline.ingest.models import DatasetMetadata
from pipeline.ingest.parsers import open_csv
from pipeline.ingest.snapshots import SGT, Snapshot, sha256_file

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def make_snapshot() -> Callable[..., Snapshot]:
    """Build a Snapshot for a local file, as if it had been downloaded."""

    def build(path: Path, dataset_id: str, *, fmt: str = "CSV", source: str = "test") -> Snapshot:
        columns = None
        if fmt == "CSV":
            with open_csv(path) as (header, _records):
                columns = header
        return Snapshot(
            dataset_id=dataset_id,
            source=source,
            path=path,
            sha256=sha256_file(path),
            bytes=path.stat().st_size,
            downloaded_at=datetime(2026, 10, 25, 12, 0, tzinfo=SGT),
            metadata=DatasetMetadata(
                dataset_id=dataset_id, name=f"test {dataset_id}", format=fmt, columns=columns
            ),
        )

    return build
