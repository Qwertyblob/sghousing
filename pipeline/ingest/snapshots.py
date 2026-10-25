"""Immutable on-disk snapshots of downloaded datasets.

Layout per dataset directory `data/raw/{source}/{dataset_id}/`:

    {YYYY-MM-DD}_{sha256}.{ext}        data file, never overwritten
    {YYYY-MM-DD}_{sha256}.meta.json    sidecar: hash, size, download time, dataset metadata
    current.json                       which snapshot is current, and when it was retrieved

A snapshot is complete only when both files exist, the sidecar parses, its hash matches the
filename and its size matches the data file. Publication writes the sidecar before the data
file and updates `current.json` last, so an interrupted run never selects a partial snapshot.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pipeline.ingest.models import DatasetMetadata

logger = logging.getLogger(__name__)

SGT = ZoneInfo("Asia/Singapore")
CURRENT_FILE = "current.json"
_DATA_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})_([0-9a-f]{64})\.([a-z]+)$")


@dataclass(frozen=True)
class Snapshot:
    """A complete, verified snapshot on disk."""

    dataset_id: str
    source: str
    path: Path
    sha256: str
    bytes: int
    downloaded_at: datetime
    metadata: DatasetMetadata


def sha256_file(path: Path) -> str:
    """Full SHA-256 of a file, streamed."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_file_synced(path: Path, payload: bytes) -> None:
    with path.open("wb") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())


def _write_json_atomic(path: Path, obj: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        _write_file_synced(tmp, json.dumps(obj, indent=2, sort_keys=True).encode())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(path.parent)


def meta_path_for(data_path: Path) -> Path:
    """Sidecar path for a data file."""
    return data_path.with_name(data_path.name.rsplit(".", 1)[0] + ".meta.json")


def read_complete(data_path: Path, *, verify_checksum: bool) -> Snapshot | None:
    """Return the snapshot at `data_path` if it is complete (and its checksum verifies)."""
    match = _DATA_NAME.match(data_path.name)
    meta_path = meta_path_for(data_path)
    if not match or not data_path.is_file() or not meta_path.is_file():
        if data_path.exists() or meta_path.exists():
            logger.warning("Ignoring incomplete snapshot %s", data_path.name)
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        snapshot = Snapshot(
            dataset_id=meta["dataset_id"],
            source=meta["source"],
            path=data_path,
            sha256=meta["sha256"],
            bytes=int(meta["bytes"]),
            downloaded_at=datetime.fromisoformat(meta["downloaded_at"]),
            metadata=DatasetMetadata.model_validate(meta["metadata"]),
        )
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("Ignoring snapshot %s with invalid sidecar: %s", data_path.name, exc)
        return None
    if snapshot.sha256 != match.group(2) or data_path.stat().st_size != snapshot.bytes:
        logger.warning("Ignoring snapshot %s: sidecar does not match file", data_path.name)
        return None
    if verify_checksum and sha256_file(data_path) != snapshot.sha256:
        logger.warning("Ignoring snapshot %s: checksum mismatch", data_path.name)
        return None
    return snapshot


def publish(
    directory: Path,
    part: Path,
    *,
    dataset_id: str,
    source: str,
    sha256: str,
    nbytes: int,
    downloaded_at: datetime,
    metadata: DatasetMetadata,
    ext: str,
) -> Snapshot:
    """Move a fully written, fsynced `.part` file into place as a complete snapshot.

    If a complete snapshot with identical content (full checksum) already exists at the
    target path it is reused and `part` is discarded. An incomplete one is repaired.
    """
    stamp = downloaded_at.astimezone(SGT).date().isoformat()
    data_path = directory / f"{stamp}_{sha256}.{ext}"
    existing = read_complete(data_path, verify_checksum=True)
    if existing is not None:
        part.unlink(missing_ok=True)
        logger.info("Content unchanged; reusing %s", data_path.name)
        return existing

    meta_path = meta_path_for(data_path)
    sidecar = {
        "dataset_id": dataset_id,
        "source": source,
        "sha256": sha256,
        "bytes": nbytes,
        "downloaded_at": downloaded_at.isoformat(),
        "metadata": metadata.model_dump(mode="json"),
    }
    meta_part = directory / f".{meta_path.name}.{uuid.uuid4().hex}.part"
    try:
        _write_file_synced(meta_part, json.dumps(sidecar, indent=2, sort_keys=True).encode())
        os.replace(meta_part, meta_path)
        os.replace(part, data_path)
    finally:
        meta_part.unlink(missing_ok=True)
    _fsync_dir(directory)
    snapshot = read_complete(data_path, verify_checksum=False)
    if snapshot is None:  # pragma: no cover - would indicate a bug in this module
        raise RuntimeError(f"published snapshot {data_path} is not complete")
    return snapshot


def write_current(directory: Path, snapshot: Snapshot, *, retrieved_at: datetime) -> None:
    """Point `current.json` at `snapshot`. Called after every successful retrieval."""
    _write_json_atomic(
        directory / CURRENT_FILE,
        {
            "file": snapshot.path.name,
            "sha256": snapshot.sha256,
            "downloaded_at": snapshot.downloaded_at.isoformat(),
            "retrieved_at": retrieved_at.isoformat(),
        },
    )


def read_current(
    directory: Path, *, verify_checksum: bool = True
) -> tuple[Snapshot, datetime] | None:
    """Return the current snapshot and its retrieval time, or None if absent or invalid."""
    current_path = directory / CURRENT_FILE
    if not current_path.is_file():
        return None
    try:
        current = json.loads(current_path.read_text(encoding="utf-8"))
        retrieved_at = datetime.fromisoformat(current["retrieved_at"])
        data_path = directory / current["file"]
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("Ignoring invalid %s: %s", current_path, exc)
        return None
    snapshot = read_complete(data_path, verify_checksum=verify_checksum)
    if snapshot is None or snapshot.sha256 != current.get("sha256"):
        return None
    return snapshot, retrieved_at


def cached_for(directory: Path, day: date) -> Snapshot | None:
    """The current snapshot if it was retrieved on `day` (SGT) and verifies."""
    found = read_current(directory, verify_checksum=True)
    if found is None:
        return None
    snapshot, retrieved_at = found
    if retrieved_at.astimezone(SGT).date() != day:
        return None
    return snapshot
