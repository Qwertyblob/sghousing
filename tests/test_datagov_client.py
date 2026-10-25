"""data.gov.sg client behaviour against a scripted mock server (no network)."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from pipeline.common.http import (
    API_HOSTS,
    API_KEY_HEADER,
    USER_AGENT,
    DeadlineExceededError,
    RetryableHTTPError,
    RetryBudgetExhaustedError,
    UnsafeRequestError,
    make_api_client,
)
from pipeline.ingest import snapshots
from pipeline.ingest.datagov import DataGovClient, DataGovError
from pipeline.ingest.snapshots import SGT

DATASET = "d_test0000000000000000000000000001"
API_KEY = "secret-key-123"
STORAGE_URL = "https://blobs.storage.test/file?X-Amz-Signature=sig"
BODY_A = b"a,b\n1,2\n"
BODY_B = b"a,b\n3,4\n5,6\n"
DAY1 = datetime(2026, 10, 25, 9, 0, tzinfo=SGT)


def api_ok(data: dict | None, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"code": 0, "data": data, "errorMsg": ""})


class FailingStream(httpx.SyncByteStream):
    """Body that breaks after the first chunk, like a dropped connection."""

    def __iter__(self) -> Iterator[bytes]:
        yield b"a,b\n1,"
        raise httpx.ReadError("connection reset by peer")


class FakeServer:
    """Routes API and storage requests; scripted responses first, then defaults."""

    def __init__(self, fmt: str = "CSV", bodies: tuple[bytes, ...] = (BODY_A,)) -> None:
        self.fmt = fmt
        self.bodies = list(bodies)
        self.storage_served = 0
        self.scripts: dict[str, list[httpx.Response]] = {}
        self.log: list[tuple[str, httpx.Request]] = []

    @staticmethod
    def kind(request: httpx.Request) -> str:
        if request.url.host not in API_HOSTS:
            return "storage"
        path = request.url.path
        for suffix, kind in (
            ("/initiate-download", "initiate"),
            ("/poll-download", "poll"),
            ("/metadata", "metadata"),
        ):
            if path.endswith(suffix):
                return kind
        raise AssertionError(f"unexpected API path {path}")

    def script(self, kind: str, *responses: httpx.Response) -> None:
        self.scripts.setdefault(kind, []).extend(responses)

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.log]

    def handle(self, request: httpx.Request) -> httpx.Response:
        kind = self.kind(request)
        self.log.append((kind, request))
        queue = self.scripts.get(kind)
        if queue:
            return queue.pop(0)
        if kind == "metadata":
            column_meta = (
                {"order": ["c1", "c2"], "map": {"c1": "a", "c2": "b"}}
                if self.fmt == "CSV"
                else None
            )
            return api_ok(
                {
                    "datasetId": DATASET,
                    "name": "Test",
                    "format": self.fmt,
                    "columnMetadata": column_meta,
                }
            )
        if kind == "initiate":
            return api_ok({"message": "Download initiated"}, 201)
        if kind == "poll":
            return api_ok({"status": "DOWNLOAD_SUCCESS", "url": STORAGE_URL}, 201)
        body = self.bodies[min(self.storage_served, len(self.bodies) - 1)]
        self.storage_served += 1
        return httpx.Response(200, content=body)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        server: FakeServer,
        *,
        api_key: str | None = API_KEY,
        timeout_s: float = 600.0,
        max_attempts: int = 10,
    ) -> None:
        self.server = server
        self.fake_clock = FakeClock()
        self.now = DAY1
        transport = httpx.MockTransport(server.handle)
        self.client = DataGovClient(
            raw_dir=tmp_path / "raw",
            api_key=api_key,
            max_attempts=max_attempts,
            timeout_s=timeout_s,
            api_transport=transport,
            download_transport=transport,
            sleep=self.fake_clock.sleep,
            clock=self.fake_clock.clock,
            now=lambda: self.now,
        )
        self.dir = tmp_path / "raw" / "src" / DATASET

    def download(self, *, force: bool = False) -> snapshots.Snapshot:
        return self.client.download_dataset(DATASET, "src", force=force)

    def current(self) -> dict:
        return json.loads((self.dir / snapshots.CURRENT_FILE).read_text())

    def data_files(self) -> list[Path]:
        return sorted(p for p in self.dir.iterdir() if p.suffix == ".csv")


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    h = Harness(tmp_path, FakeServer())
    yield h
    h.client.close()


# --- retry policy ------------------------------------------------------------------------


def test_retry_after_is_honoured(harness: Harness) -> None:
    harness.server.script("metadata", httpx.Response(429, headers={"Retry-After": "7"}))
    harness.client.get_dataset_metadata(DATASET)
    assert harness.fake_clock.sleeps == [7.0]


def test_backoff_grows_without_retry_after(harness: Harness) -> None:
    harness.server.script("metadata", *[httpx.Response(503)] * 3)
    harness.client.get_dataset_metadata(DATASET)
    sleeps = harness.fake_clock.sleeps
    assert len(sleeps) == 3
    assert sleeps[0] >= 2
    assert sleeps[0] < sleeps[1] < sleeps[2]


def test_gives_up_after_max_attempts(harness: Harness) -> None:
    harness.server.script("metadata", *[httpx.Response(503)] * 10)
    with pytest.raises(RetryableHTTPError):
        harness.client.get_dataset_metadata(DATASET)
    assert harness.server.kinds().count("metadata") == harness.client.max_attempts


def test_deadline_includes_retry_waits(tmp_path: Path) -> None:
    server = FakeServer()
    server.script("metadata", *[httpx.Response(429, headers={"Retry-After": "8"})] * 10)
    h = Harness(tmp_path, server, timeout_s=10, max_attempts=50)
    with pytest.raises(DeadlineExceededError):
        h.client.get_dataset_metadata(DATASET)
    assert h.fake_clock.t <= 10
    assert h.fake_clock.sleeps == [8.0, 2.0]  # second wait clipped to the deadline
    assert h.server.kinds() == ["metadata", "metadata"]  # no request after the deadline


@pytest.mark.parametrize("kind", ["metadata", "initiate", "poll"])
def test_nonzero_code_raises(harness: Harness, kind: str) -> None:
    harness.server.script(
        kind, httpx.Response(200, json={"code": 2, "errorMsg": "bad dataset", "data": None})
    )
    with pytest.raises(DataGovError, match="bad dataset"):
        harness.download()


# --- download sequence -------------------------------------------------------------------


def test_csv_initiates_before_polling(harness: Harness) -> None:
    harness.download()
    assert harness.server.kinds() == ["metadata", "initiate", "poll", "storage"]


def test_geojson_only_polls(tmp_path: Path) -> None:
    h = Harness(tmp_path, FakeServer(fmt="GEOJSON", bodies=(b'{"type":"FeatureCollection"}',)))
    snap = h.client.download_dataset(DATASET, "src")
    assert h.server.kinds() == ["metadata", "poll", "storage"]
    assert snap.path.suffix == ".geojson"
    assert snap.metadata.columns is None


def test_poll_waits_until_url_is_ready(harness: Harness) -> None:
    harness.server.script("poll", api_ok({"status": "IN_PROGRESS"}, 201))
    harness.download()
    assert harness.server.kinds() == ["metadata", "initiate", "poll", "poll", "storage"]


# --- credentials -------------------------------------------------------------------------


def test_key_sent_to_api_hosts_only(harness: Harness) -> None:
    harness.download()
    for kind, request in harness.server.log:
        assert request.headers["User-Agent"] == USER_AGENT
        if kind == "storage":
            assert API_KEY_HEADER not in request.headers
        else:
            assert request.headers[API_KEY_HEADER] == API_KEY


def test_storage_cross_host_redirect_gets_no_key(harness: Harness) -> None:
    harness.server.script(
        "storage", httpx.Response(302, headers={"Location": "https://other-storage.test/x"})
    )
    harness.download()
    storage = [r for kind, r in harness.server.log if kind == "storage"]
    assert [r.url.host for r in storage] == ["blobs.storage.test", "other-storage.test"]
    assert all(API_KEY_HEADER not in r.headers for r in storage)


def test_api_client_refuses_redirects(harness: Harness) -> None:
    harness.server.script(
        "metadata", httpx.Response(302, headers={"Location": "https://evil.test/"})
    )
    with pytest.raises(UnsafeRequestError):
        harness.client.get_dataset_metadata(DATASET)
    assert all(r.url.host != "evil.test" for _, r in harness.server.log)


def test_api_client_refuses_non_api_hosts() -> None:
    seen: list[httpx.Request] = []
    client = make_api_client(API_KEY, transport=httpx.MockTransport(lambda r: seen.append(r)))
    with pytest.raises(UnsafeRequestError):
        client.get("https://evil.test/steal")
    assert seen == []


def test_no_key_header_when_unset(tmp_path: Path) -> None:
    h = Harness(tmp_path, FakeServer(), api_key=None)
    h.download()
    assert all(API_KEY_HEADER not in r.headers for _, r in h.server.log)


# --- transfer and snapshots --------------------------------------------------------------


def test_midstream_failure_restarts_whole_transfer(harness: Harness) -> None:
    first = harness.download()
    harness.now = DAY1 + timedelta(days=1)
    harness.server.bodies = [BODY_B]
    harness.server.storage_served = 0
    harness.server.log.clear()
    harness.server.script("storage", httpx.Response(200, stream=FailingStream()))

    snap = harness.download(force=True)

    assert snap.sha256 == hashlib.sha256(BODY_B).hexdigest()
    assert snap.path.read_bytes() == BODY_B
    assert harness.server.kinds() == [
        "metadata", "initiate", "poll", "storage", "initiate", "poll", "storage",
    ]  # fmt: skip
    assert not list(harness.dir.glob(".*.part"))
    assert snapshots.read_complete(first.path, verify_checksum=True) == first


def test_two_downloads_same_date_are_distinct(harness: Harness) -> None:
    harness.server.bodies = [BODY_A, BODY_B]
    a = harness.download(force=True)
    b = harness.download(force=True)
    assert a.path != b.path
    assert harness.data_files() == sorted([a.path, b.path])
    assert all(snapshots.meta_path_for(p).is_file() for p in (a.path, b.path))
    assert harness.current()["file"] == b.path.name


def test_identical_content_reuses_existing_file(harness: Harness) -> None:
    a1 = harness.download(force=True)
    harness.now = DAY1 + timedelta(hours=3)
    a2 = harness.download(force=True)
    assert a1.path == a2.path
    assert len(harness.data_files()) == 1
    assert datetime.fromisoformat(harness.current()["retrieved_at"]) == harness.now


def test_unchanged_content_on_a_later_day_keeps_one_copy(harness: Harness) -> None:
    a1 = harness.download()
    harness.now = DAY1 + timedelta(days=1)
    a2 = harness.download()
    assert harness.server.kinds().count("storage") == 2
    assert a2.path == a1.path
    assert len(harness.data_files()) == 1


def test_a_b_a_sequence_ends_on_a(harness: Harness) -> None:
    harness.server.bodies = [BODY_A, BODY_B, BODY_A]
    a = harness.download(force=True)
    harness.download(force=True)
    harness.download(force=True)
    assert harness.current()["file"] == a.path.name
    harness.server.log.clear()
    assert harness.download().path == a.path
    assert harness.server.kinds() == []


def test_cached_run_makes_no_requests(harness: Harness) -> None:
    first = harness.download()
    harness.server.log.clear()
    assert harness.download() == first
    assert harness.server.kinds() == []


def test_interrupted_between_meta_and_data_rename(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = harness.download()
    harness.server.bodies = [BODY_B]
    harness.server.storage_served = 0
    real_replace = os.replace

    def crash_on_data(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if str(dst).endswith(".csv"):
            raise OSError("simulated crash")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", crash_on_data)
    with pytest.raises(OSError, match="simulated crash"):
        harness.download(force=True)
    monkeypatch.setattr(os, "replace", real_replace)

    assert harness.current()["file"] == a.path.name
    assert harness.download() == a  # partial B is never selected
    assert not list(harness.dir.glob(".*.part"))

    b = harness.download(force=True)  # same content again repairs it
    assert snapshots.read_complete(b.path, verify_checksum=True) == b
    assert harness.current()["file"] == b.path.name


def test_interrupted_before_current_update(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = harness.download()
    harness.server.bodies = [BODY_B]
    harness.server.storage_served = 0

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(snapshots, "write_current", boom)
    with pytest.raises(OSError):
        harness.download(force=True)
    monkeypatch.undo()
    assert harness.current()["file"] == a.path.name
    assert harness.download() == a


@pytest.mark.parametrize("damage", ["meta_missing", "meta_corrupt", "bit_flip", "truncated"])
def test_damaged_cache_is_not_reused(harness: Harness, damage: str) -> None:
    a = harness.download()
    meta = snapshots.meta_path_for(a.path)
    if damage == "meta_missing":
        meta.unlink()
    elif damage == "meta_corrupt":
        meta.write_text("{not json")
    elif damage == "bit_flip":
        data = bytearray(a.path.read_bytes())
        data[0] ^= 0x01
        a.path.write_bytes(bytes(data))
    else:
        a.path.write_bytes(a.path.read_bytes()[:-1])

    harness.server.log.clear()
    repaired = harness.download()
    assert "storage" in harness.server.kinds()
    assert repaired.sha256 == hashlib.sha256(BODY_A).hexdigest()
    assert snapshots.read_complete(repaired.path, verify_checksum=True) == repaired


# --- deadline during requests, and one budget across nested retries -----------------------


class SlowStream(httpx.SyncByteStream):
    """Body whose chunks each take `seconds` of (fake) time to arrive."""

    def __init__(self, clock: FakeClock, chunks: int, seconds: float) -> None:
        self.clock, self.chunks, self.seconds = clock, chunks, seconds

    def __iter__(self) -> Iterator[bytes]:
        for _ in range(self.chunks):
            self.clock.t += self.seconds
            yield b"x" * 16


def test_request_timeout_is_capped_by_deadline(tmp_path: Path) -> None:
    server = FakeServer()
    server.script("metadata", httpx.Response(429, headers={"Retry-After": "8"}))
    h = Harness(tmp_path, server, timeout_s=10, max_attempts=5)
    h.client.get_dataset_metadata(DATASET)
    assert [r.extensions["timeout"]["read"] for _, r in server.log] == [10.0, 2.0]


def test_slow_stream_is_stopped_at_deadline(tmp_path: Path) -> None:
    server = FakeServer()
    h = Harness(tmp_path, server, timeout_s=100, max_attempts=5)
    server.script("storage", httpx.Response(200, stream=SlowStream(h.fake_clock, 10, 30)))
    with pytest.raises(DeadlineExceededError):
        h.download()
    assert h.fake_clock.t <= 100 + 30  # stopped at the first chunk past the deadline
    assert server.kinds().count("storage") == 1  # deadline errors are not retried
    assert not list(h.dir.glob(".*.part"))
    assert not (h.dir / snapshots.CURRENT_FILE).exists()


def test_nested_retries_share_one_budget(tmp_path: Path) -> None:
    server = FakeServer()
    server.script("poll", *[httpx.Response(429)] * 100)
    h = Harness(tmp_path, server, max_attempts=5)
    with pytest.raises(RetryableHTTPError):
        h.download()
    # metadata has its own budget; initiate + polls share the download's 5 attempts
    assert [k for k in server.kinds() if k != "metadata"] == ["initiate"] + ["poll"] * 4


def test_repeated_stream_failures_share_one_budget(tmp_path: Path) -> None:
    server = FakeServer()
    server.script("storage", *[httpx.Response(200, stream=FailingStream()) for _ in range(50)])
    h = Harness(tmp_path, server, max_attempts=7)
    with pytest.raises((httpx.ReadError, RetryBudgetExhaustedError)):
        h.download()
    assert len([k for k in server.kinds() if k != "metadata"]) <= 7
    assert not list(h.dir.glob(".*.part"))
