"""data.gov.sg client: metadata, and initiate/poll/download into immutable snapshots."""

from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx

from config.settings import Settings
from pipeline.common.http import (
    NotReadyError,
    RetryPolicy,
    UnsafeRequestError,
    make_api_client,
    make_download_client,
    raise_for_retryable,
    retrying,
)
from pipeline.ingest import snapshots
from pipeline.ingest.models import CollectionMetadata, DatasetMetadata
from pipeline.ingest.snapshots import SGT, Snapshot

logger = logging.getLogger(__name__)

METADATA_BASE = "https://api-production.data.gov.sg/v2/public/api"
DOWNLOAD_BASE = "https://api-open.data.gov.sg/v1/public/api"
FORMAT_EXTENSIONS = {"CSV": "csv", "GEOJSON": "geojson"}
API_TIMEOUT_S = 30.0  # per request, further capped by the operation deadline
DOWNLOAD_TIMEOUT_S = 60.0


class DataGovError(RuntimeError):
    """The API answered with a non-zero application `code`."""

    def __init__(self, code: object, message: str) -> None:
        super().__init__(f"data.gov.sg error code={code}: {message}")
        self.code = code


class DataGovClient:
    """Client for data.gov.sg. Use as a context manager.

    `request_counts` tallies requests by kind (metadata, initiate, poll, storage) so callers
    can show that a cached run made no download requests.
    """

    def __init__(
        self,
        *,
        raw_dir: Path,
        api_key: str | None = None,
        max_attempts: int = 20,
        timeout_s: float = 600.0,
        api_transport: httpx.BaseTransport | None = None,
        download_transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(SGT),
    ) -> None:
        self.raw_dir = raw_dir
        self.max_attempts = max_attempts
        self.timeout_s = timeout_s
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._api = make_api_client(api_key, transport=api_transport)
        self._download = make_download_client(transport=download_transport)
        self.request_counts: Counter[str] = Counter()

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> DataGovClient:
        """Build a client from project settings."""
        key = settings.datagov_api_key.get_secret_value() if settings.datagov_api_key else None
        return cls(
            raw_dir=settings.data_dir / "raw",
            api_key=key,
            max_attempts=settings.datagov_max_attempts,
            timeout_s=settings.datagov_download_timeout_s,
            **kwargs,
        )

    def __enter__(self) -> DataGovClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close both HTTP clients."""
        self._api.close()
        self._download.close()

    def _policy(self) -> RetryPolicy:
        return RetryPolicy.starting_now(self.max_attempts, self.timeout_s, self._sleep, self._clock)

    def _api_once(self, url: str, kind: str, policy: RetryPolicy) -> dict[str, Any]:
        policy.charge()
        self.request_counts[kind] += 1
        response = self._api.get(url, timeout=policy.timeout(API_TIMEOUT_S))
        if response.is_redirect or 300 <= response.status_code < 400:
            raise UnsafeRequestError(f"API redirect refused ({response.status_code}) for {kind}")
        raise_for_retryable(response)
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise DataGovError(body.get("code"), body.get("errorMsg") or body.get("name") or "")
        return body.get("data") or {}

    def _api_data(self, url: str, kind: str, policy: RetryPolicy | None = None) -> dict[str, Any]:
        policy = policy or self._policy()
        return retrying(lambda: self._api_once(url, kind, policy), policy, what=kind)

    def get_collection_metadata(self, collection_id: str) -> CollectionMetadata:
        """Metadata for a collection, including its child dataset IDs."""
        data = self._api_data(f"{METADATA_BASE}/collections/{collection_id}/metadata", "metadata")
        return CollectionMetadata.from_api(data)

    def get_dataset_metadata(self, dataset_id: str) -> DatasetMetadata:
        """Metadata for one dataset (name, format, column order for CSVs)."""
        data = self._api_data(f"{METADATA_BASE}/datasets/{dataset_id}/metadata", "metadata")
        return DatasetMetadata.from_api(data)

    def _poll_once(self, dataset_id: str, policy: RetryPolicy) -> str:
        data = self._api_once(
            f"{DOWNLOAD_BASE}/datasets/{dataset_id}/poll-download", "poll", policy
        )
        url = data.get("url")
        if not url:
            raise NotReadyError(f"download not ready (status={data.get('status')!r})")
        return url

    def _stream_to_part(
        self, url: str, directory: Path, policy: RetryPolicy
    ) -> tuple[Path, str, int]:
        directory.mkdir(parents=True, exist_ok=True)
        part = directory / f".{uuid.uuid4().hex}.part"
        digest = hashlib.sha256()
        nbytes = 0
        policy.charge()
        self.request_counts["storage"] += 1
        try:
            timeout = policy.timeout(DOWNLOAD_TIMEOUT_S)
            with self._download.stream("GET", url, timeout=timeout) as response:
                raise_for_retryable(response)
                response.raise_for_status()
                with part.open("wb") as f:
                    # No chunk_size: buffering to a fixed size would delay the deadline check.
                    for chunk in response.iter_bytes():
                        policy.check_deadline()
                        f.write(chunk)
                        digest.update(chunk)
                        nbytes += len(chunk)
                    f.flush()
                    os.fsync(f.fileno())
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        return part, digest.hexdigest(), nbytes

    def _transfer(
        self, meta: DatasetMetadata, directory: Path, policy: RetryPolicy
    ) -> tuple[Path, str, int]:
        """One full attempt: (initiate for CSV) -> poll until a URL -> stream to a fresh file."""
        dataset_id = meta.dataset_id
        if meta.format == "CSV":
            self._api_data(
                f"{DOWNLOAD_BASE}/datasets/{dataset_id}/initiate-download", "initiate", policy
            )
        url = retrying(lambda: self._poll_once(dataset_id, policy), policy, what="poll")
        logger.info("Downloading %s from %s", dataset_id, httpx.URL(url).host)
        return self._stream_to_part(url, directory, policy)

    def download_dataset(self, dataset_id: str, source: str, *, force: bool = False) -> Snapshot:
        """Return the current snapshot of a dataset, downloading it if needed.

        Without `force`, a snapshot already retrieved today (SGT) is reused with no download
        requests. The returned `Snapshot.path` is the immutable data file.
        """
        directory = self.raw_dir / source / dataset_id
        if not force:
            cached = snapshots.cached_for(directory, self._now().date())
            if cached is not None:
                logger.info("%s: using today's snapshot %s", dataset_id, cached.path.name)
                return cached

        meta = self.get_dataset_metadata(dataset_id)
        ext = FORMAT_EXTENSIONS.get(meta.format)
        if ext is None:
            raise DataGovError("unsupported-format", f"{dataset_id} has format {meta.format}")
        policy = self._policy()
        part, sha256, nbytes = retrying(
            lambda: self._transfer(meta, directory, policy), policy, what=f"download {dataset_id}"
        )
        try:
            current = snapshots.read_current(directory, verify_checksum=True)
            if current is not None and current[0].sha256 == sha256:
                # Same content as the current snapshot (e.g. on a later day): keep one copy.
                snapshot = current[0]
                logger.info("%s: content unchanged since %s", dataset_id, snapshot.path.name)
            else:
                snapshot = snapshots.publish(
                    directory,
                    part,
                    dataset_id=dataset_id,
                    source=source,
                    sha256=sha256,
                    nbytes=nbytes,
                    downloaded_at=self._now(),
                    metadata=meta,
                    ext=ext,
                )
        finally:
            part.unlink(missing_ok=True)
        snapshots.write_current(directory, snapshot, retrieved_at=self._now())
        logger.info("%s: snapshot %s (%d bytes)", dataset_id, snapshot.path.name, snapshot.bytes)
        return snapshot
