"""OneMap client and address matching, against a mock transport (no network)."""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from config.settings import Settings
from pipeline.common.http import DeadlineExceededError
from pipeline.geocode.onemap import (
    NEAREST_MRT_PATH,
    SEARCH_DEADLINE_S,
    SEARCH_PATH,
    TOKEN_PATH,
    OneMapAuthError,
    OneMapClient,
    OneMapError,
    RateLimiter,
    expand_abbreviations,
    match_block,
    match_postal,
)
from pipeline.geocode.run import Address, geocode_one

PASSWORD = "s3cret-pw"


def result(
    blk: str, road: str, *, lat: str = "1.362", lon: str = "103.849", postal: str = "560406"
) -> dict[str, str]:
    return {
        "SEARCHVAL": f"{blk} {road} SINGAPORE {postal}",
        "BLK_NO": blk,
        "ROAD_NAME": road,
        "BUILDING": "NIL",
        "POSTAL": postal,
        "LATITUDE": lat,
        "LONGITUDE": lon,
    }


def body(*results: dict[str, str]) -> dict[str, Any]:
    return {"found": len(results), "totalNumPages": 1, "pageNum": 1, "results": list(results)}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Harness:
    """A OneMapClient wired to scripted responses."""

    def __init__(self, *, token_expiry: float = 10_000.0) -> None:
        self.clock = FakeClock()
        self.wall = 1_000.0
        self.token_responses: list[httpx.Response] = []
        self.search_responses: list[httpx.Response] = []
        self.requests: list[httpx.Request] = []
        self.tokens_issued = 0
        self.token_expiry = token_expiry
        self.client = OneMapClient(
            "me@example.com",
            SecretStr(PASSWORD),
            requests_per_second=1000.0,
            transport=httpx.MockTransport(self.handle),
            sleep=self.clock.sleep,
            clock=self.clock,
            wall_clock=lambda: self.wall,
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == TOKEN_PATH:
            if self.token_responses:
                return self.token_responses.pop(0)
            self.tokens_issued += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"tok-{self.tokens_issued}",
                    "expiry_timestamp": str(self.token_expiry),
                },
            )
        assert request.url.path in (SEARCH_PATH, NEAREST_MRT_PATH)
        return self.search_responses.pop(0)

    def paths(self) -> Counter[str]:
        return Counter(r.url.path for r in self.requests)


@pytest.fixture
def h() -> Harness:
    return Harness()


# --- matching -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("street", "expanded"),
    [
        ("ANG MO KIO AVE 10", "ANG MO KIO AVENUE 10"),
        ("BT BATOK WEST AVE 2", "BUKIT BATOK WEST AVENUE 2"),
        ("C'WEALTH CRES", "COMMONWEALTH CRESCENT"),
        ("ST. GEORGE'S LANE", "SAINT GEORGE'S LANE"),
        ("  jln  tenteram ", "JALAN TENTERAM"),
        ("ANCHORVALE ST", "ANCHORVALE STREET"),
    ],
)
def test_expand_abbreviations(street: str, expanded: str) -> None:
    assert expand_abbreviations(street) == expanded


def test_match_block_compares_expanded_road_names() -> None:
    results = [
        result("406A", "ANG MO KIO AVENUE 10"),
        result("406", "ANG MO KIO AVENUE 1"),
        result("406", "ANG MO KIO AVENUE 10", lat="1.3620"),
    ]
    m = match_block(results, "406", "ANG MO KIO AVE 10")
    assert m is not None and m.lat == 1.362 and m.postal == "560406"


def test_match_block_rejects_points_outside_singapore() -> None:
    assert match_block([result("1", "BEACH ROAD", lat="0.0", lon="0.0")], "1", "BEACH RD") is None


def test_match_block_none_when_nothing_matches() -> None:
    assert match_block([result("12", "BEACH ROAD")], "1", "BEACH RD") is None
    assert match_block([], "1", "BEACH RD") is None


def test_match_postal() -> None:
    results = [result("1", "X ROAD", postal="111111"), result("2", "Y ROAD", postal="529366")]
    m = match_postal(results, "529366")
    assert m is not None and m.result["BLK_NO"] == "2"
    assert match_postal([result("1", "X ROAD", postal="NIL")], "NIL") is None


def test_rate_limiter_spaces_calls() -> None:
    clock = FakeClock()
    limiter = RateLimiter(2.0, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        limiter.wait()
    assert clock.sleeps == [0.5, 0.5]


# --- client -------------------------------------------------------------------------------


def test_token_is_fetched_once_and_sent_as_bare_authorization(h: Harness) -> None:
    h.search_responses += [httpx.Response(200, json=body()), httpx.Response(200, json=body())]
    h.client.search("1 BEACH RD")
    h.client.search("2 BEACH RD")

    assert h.paths() == {TOKEN_PATH: 1, SEARCH_PATH: 2}
    token_req = h.requests[0]
    assert token_req.method == "POST"
    assert json.loads(token_req.content) == {"email": "me@example.com", "password": PASSWORD}
    search_req = h.requests[1]
    assert search_req.headers["Authorization"] == "tok-1"
    assert dict(search_req.url.params) == {
        "searchVal": "1 BEACH RD",
        "returnGeom": "Y",
        "getAddrDetails": "Y",
        "pageNum": "1",
    }
    assert h.client.request_counts == {"token": 1, "search": 2}


def test_token_refreshed_before_expiry() -> None:
    h = Harness(token_expiry=1_200.0)
    h.search_responses += [httpx.Response(200, json=body()), httpx.Response(200, json=body())]
    h.client.search("a")
    h.wall = 1_000.0  # 200 s before expiry: inside the refresh margin
    h.client.search("b")
    assert h.paths()[TOKEN_PATH] == 2


def test_rejected_token_is_refreshed_once(h: Harness) -> None:
    h.search_responses += [httpx.Response(401), httpx.Response(200, json=body())]
    h.client.search("a")
    assert [r.headers.get("Authorization") for r in h.requests if r.url.path == SEARCH_PATH] == [
        "tok-1",
        "tok-2",
    ]


def test_token_rejected_twice_fails(h: Harness) -> None:
    h.search_responses += [httpx.Response(401), httpx.Response(403)]
    with pytest.raises(OneMapAuthError):
        h.client.search("a")


def test_bad_credentials_fail_without_leaking_password(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    h.token_responses.append(httpx.Response(401, json={"error": "Authentication failed"}))
    with caplog.at_level(logging.DEBUG), pytest.raises(OneMapAuthError) as exc:
        h.client.search("a")
    assert PASSWORD not in str(exc.value)
    assert PASSWORD not in caplog.text
    assert h.paths()[SEARCH_PATH] == 0


def test_429_waits_for_retry_after(h: Harness) -> None:
    h.search_responses += [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, json=body(result("1", "BEACH ROAD"))),
    ]
    assert h.client.search("a")["found"] == 1
    assert 7.0 in h.clock.sleeps


def test_non_auth_error_field_in_response_raises(h: Harness) -> None:
    h.search_responses.append(httpx.Response(200, json={**body(), "error": "Invalid searchVal"}))
    with pytest.raises(OneMapError, match="Invalid searchVal"):
        h.client.search("a")
    assert h.paths()[TOKEN_PATH] == 1


AUTH_ERROR = (
    "Authentication token missing. Please create an account and generate or renew your API Token."
)


def test_auth_error_with_http_200_refreshes_token(h: Harness) -> None:
    h.search_responses += [
        httpx.Response(200, json={**body(), "error": AUTH_ERROR}),
        httpx.Response(200, json=body(result("1", "BEACH ROAD"))),
    ]
    assert h.client.search("a")["found"] == 1
    assert [r.headers.get("Authorization") for r in h.requests if r.url.path == SEARCH_PATH] == [
        "tok-1",
        "tok-2",
    ]


def test_auth_error_with_http_200_twice_fails(h: Harness) -> None:
    h.search_responses += [
        httpx.Response(200, json={**body(), "error": AUTH_ERROR}),
        httpx.Response(200, json={**body(), "error": AUTH_ERROR}),
    ]
    with pytest.raises(OneMapAuthError, match="Authentication token missing"):
        h.client.search("a")


class SlowStream(httpx.SyncByteStream):
    """Body whose chunks each take `seconds` of fake time to arrive."""

    def __init__(self, clock: FakeClock, chunks: int, seconds: float) -> None:
        self.clock, self.chunks, self.seconds = clock, chunks, seconds

    def __iter__(self) -> Iterator[bytes]:
        for _ in range(self.chunks):
            self.clock.now += self.seconds
            yield b" "


def test_slow_body_is_cut_off_at_the_deadline(h: Harness) -> None:
    # Each chunk arrives well within the per-read timeout, but together they outlast the
    # search deadline.
    h.search_responses.append(
        httpx.Response(200, stream=SlowStream(h.clock, chunks=100, seconds=SEARCH_DEADLINE_S / 10))
    )
    with pytest.raises(DeadlineExceededError):
        h.client.search("a")
    assert h.clock.now < SEARCH_DEADLINE_S * 1.2


def test_redirects_are_not_followed(h: Harness) -> None:
    h.search_responses.append(
        httpx.Response(302, headers={"Location": "https://evil.example/steal"})
    )
    with pytest.raises(OneMapError, match="HTTP 302"):
        h.client.search("a")
    assert {r.url.host for r in h.requests} == {"www.onemap.gov.sg"}


class StallsBeforeEnd(httpx.SyncByteStream):
    """Whole body in one prompt chunk, then the end of the response arrives late."""

    def __init__(self, clock: FakeClock, payload: bytes, stall: float) -> None:
        self.clock, self.payload, self.stall = clock, payload, stall

    def __iter__(self) -> Iterator[bytes]:
        yield self.payload
        self.clock.now += self.stall


def test_deadline_checked_after_the_body_ends(h: Harness) -> None:
    h.search_responses.append(
        httpx.Response(
            200,
            stream=StallsBeforeEnd(h.clock, json.dumps(body()).encode(), SEARCH_DEADLINE_S + 1),
        )
    )
    with pytest.raises(DeadlineExceededError):
        h.client.search("a")


def test_from_settings_requires_credentials() -> None:
    settings = Settings(_env_file=None, onemap_email=None, onemap_password=None)  # type: ignore[call-arg]
    with pytest.raises(OneMapAuthError, match="ONEMAP_EMAIL"):
        OneMapClient.from_settings(settings)


# --- geocode_one --------------------------------------------------------------------------


class FakeSearcher:
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.queries: list[str] = []
        self.request_counts: Counter[str] = Counter()

    def search(self, query: str) -> dict[str, Any]:
        self.queries.append(query)
        self.request_counts["search"] += 1
        return self.responses.get(query, body())


def test_geocode_one_exact_match_is_ok() -> None:
    s = FakeSearcher({"406 ANG MO KIO AVE 10": body(result("406", "ANG MO KIO AVENUE 10"))})
    row = geocode_one(s, Address("406 ANG MO KIO AVE 10", "406", "ANG MO KIO AVE 10"))
    assert (row.status, row.query_used, row.postal) == ("ok", "406 ANG MO KIO AVE 10", "560406")
    assert row.raw_response == body(result("406", "ANG MO KIO AVENUE 10"))
    assert s.queries == ["406 ANG MO KIO AVE 10"]


def test_geocode_one_falls_back_to_expanded_query() -> None:
    s = FakeSearcher({"1 BUKIT BATOK CENTRAL": body(result("1", "BUKIT BATOK CENTRAL"))})
    row = geocode_one(s, Address("1 BT BATOK CTRL", "1", "BT BATOK CTRL"))
    assert (row.status, row.query_used) == ("fallback", "1 BUKIT BATOK CENTRAL")
    assert s.queries == ["1 BT BATOK CTRL", "1 BUKIT BATOK CENTRAL"]


def test_geocode_one_failure_keeps_last_response() -> None:
    s = FakeSearcher({"9 BUKIT BATOK CENTRAL": body(result("8", "BUKIT BATOK CENTRAL"))})
    row = geocode_one(s, Address("9 BT BATOK CTRL", "9", "BT BATOK CTRL"))
    assert row.status == "failed" and row.lat is None
    assert row.query_used == "9 BUKIT BATOK CENTRAL"
    assert row.raw_response is not None and row.raw_response["found"] == 1


def test_geocode_one_skips_fallback_when_nothing_to_expand() -> None:
    s = FakeSearcher({})
    assert geocode_one(s, Address("1 YISHUN RING", "1", "YISHUN RING")).status == "failed"
    assert s.queries == ["1 YISHUN RING"]


def test_geocode_one_postal() -> None:
    s = FakeSearcher({"529366": body(result("1", "X ROAD", postal="529366"))})
    row = geocode_one(s, Address("POSTAL 529366", postal="529366"))
    assert (row.status, row.postal, row.query_used) == ("ok", "529366", "529366")


# --- nearest MRT stations -----------------------------------------------------------------


def test_nearest_mrt_stations_request_and_parse(h: Harness) -> None:
    stations = [{"id": "NE17", "name": "PUNGGOL MRT STATION", "lat": 1.40, "lon": 103.90}]
    h.search_responses.append(httpx.Response(200, json=stations))
    assert h.client.nearest_mrt_stations(1.4044693, 103.9008362, 1000) == stations
    req = h.requests[-1]
    assert req.url.path == NEAREST_MRT_PATH
    assert req.headers["Authorization"] == "tok-1"
    assert dict(req.url.params) == {
        "latitude": "1.40446930",
        "longitude": "103.90083620",
        "radius_in_meters": "1000",
    }
    assert h.client.request_counts["nearest_mrt"] == 1


@pytest.mark.parametrize("payload", [{"found": 0}, [{"name": "X"}], ["not a station"]])
def test_nearest_mrt_stations_rejects_unexpected_shapes(h: Harness, payload: object) -> None:
    h.search_responses.append(httpx.Response(200, json=payload))
    with pytest.raises(OneMapError, match="expected a list of stations"):
        h.client.nearest_mrt_stations(1.4, 103.9, 500)


def test_nearest_mrt_stations_refreshes_token_on_in_body_auth_error(h: Harness) -> None:
    h.search_responses += [
        httpx.Response(200, json={"error": AUTH_ERROR}),
        httpx.Response(200, json=[]),
    ]
    assert h.client.nearest_mrt_stations(1.4, 103.9, 500) == []
    assert h.paths()[TOKEN_PATH] == 2
