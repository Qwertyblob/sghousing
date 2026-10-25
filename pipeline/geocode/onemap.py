"""OneMap search client and address matching.

Endpoints, from the official docs at https://www.onemap.gov.sg/apidocs/:

- `POST /api/auth/post/getToken` with JSON `{"email", "password"}` returns
  `{"access_token", "expiry_timestamp"}` (Unix seconds, as a string).
- `GET /api/common/elastic/search?searchVal=...&returnGeom=Y&getAddrDetails=Y&pageNum=1`
  with header `Authorization: <access_token>` (no scheme prefix). Results carry `BLK_NO`,
  `ROAD_NAME`, `POSTAL`, `LATITUDE` and `LONGITUDE` as strings.
- `GET /api/public/nearbysvc/getNearestMrtStops?latitude=..&longitude=..&radius_in_meters=..`
  returns a JSON list of stations with `id`, `name`, `lat`, `lon` and `road`.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Self

import httpx
from pydantic import SecretStr

from config.settings import Settings
from pipeline.common.http import USER_AGENT, RetryPolicy, raise_for_retryable, retrying

logger = logging.getLogger(__name__)

ONEMAP_HOST = "www.onemap.gov.sg"
BASE_URL = f"https://{ONEMAP_HOST}"
TOKEN_PATH = "/api/auth/post/getToken"
SEARCH_PATH = "/api/common/elastic/search"
NEAREST_MRT_PATH = "/api/public/nearbysvc/getNearestMrtStops"
REGISTER_URL = f"{BASE_URL}/apidocs/register"

REQUEST_TIMEOUT_S = 30.0
SEARCH_MAX_ATTEMPTS = 6  # HTTP requests per search, token refreshes included
SEARCH_DEADLINE_S = 180.0
TOKEN_REFRESH_MARGIN_S = 300.0  # fetch a new token this long before the old one expires

# Generous WGS84 bounding box around Singapore; matches outside it are rejected.
SG_LAT_RANGE = (1.15, 1.48)
SG_LON_RANGE = (103.59, 104.10)

# Street-name abbreviations used by HDB. Expansion is applied to both the HDB street and
# OneMap's ROAD_NAME before comparing, so an entry can only make matching more lenient.
ABBREVIATIONS: Mapping[str, str] = {
    "AVE": "AVENUE",
    "ST": "STREET",
    "RD": "ROAD",
    "DR": "DRIVE",
    "CRES": "CRESCENT",
    "CL": "CLOSE",
    "PK": "PARK",
    "CTRL": "CENTRAL",
    "NTH": "NORTH",
    "STH": "SOUTH",
    "UPP": "UPPER",
    "C'WEALTH": "COMMONWEALTH",
    "KG": "KAMPONG",
    "JLN": "JALAN",
    "BT": "BUKIT",
    "TG": "TANJONG",
    "LOR": "LORONG",
    "TER": "TERRACE",
    "HTS": "HEIGHTS",
    "MKT": "MARKET",
    "GDNS": "GARDENS",
    # Not in the original list; seen in HDB street names.
    "GDN": "GARDEN",
    "PL": "PLACE",
    "CTR": "CENTRE",
    "IND": "INDUSTRIAL",
    "SQ": "SQUARE",
    "ST.": "SAINT",
}


class OneMapError(RuntimeError):
    """OneMap returned something we cannot use."""


class OneMapAuthError(OneMapError):
    """Missing or rejected OneMap credentials. Not retried."""


def expand_abbreviations(street: str) -> str:
    """Uppercase, collapse whitespace and expand abbreviations word by word."""
    return " ".join(ABBREVIATIONS.get(word, word) for word in street.upper().split())


@dataclass(frozen=True)
class Match:
    """A search result accepted as the location of an address."""

    lat: float
    lon: float
    postal: str | None
    result: Mapping[str, Any]


def _coordinates(result: Mapping[str, Any]) -> tuple[float, float] | None:
    try:
        lat, lon = float(result["LATITUDE"]), float(result["LONGITUDE"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (
        SG_LAT_RANGE[0] <= lat <= SG_LAT_RANGE[1] and SG_LON_RANGE[0] <= lon <= SG_LON_RANGE[1]
    ):
        return None
    return lat, lon


def _postal(result: Mapping[str, Any]) -> str | None:
    value = str(result.get("POSTAL") or "").strip()
    return value if value and value.upper() != "NIL" else None


def _first_match(
    results: list[Mapping[str, Any]], accept: Callable[[Mapping[str, Any]], bool]
) -> Match | None:
    for result in results:
        if not accept(result):
            continue
        coords = _coordinates(result)
        if coords is not None:
            return Match(coords[0], coords[1], _postal(result), result)
    return None


def match_block(results: list[Mapping[str, Any]], block: str, street: str) -> Match | None:
    """First result whose BLK_NO equals `block` and whose road name matches `street`."""
    want_block = block.strip().upper()
    want_road = expand_abbreviations(street)
    return _first_match(
        results,
        lambda r: (
            str(r.get("BLK_NO") or "").strip().upper() == want_block
            and expand_abbreviations(str(r.get("ROAD_NAME") or "")) == want_road
        ),
    )


def match_postal(results: list[Mapping[str, Any]], postal: str) -> Match | None:
    """First result whose POSTAL equals `postal`."""
    return _first_match(results, lambda r: _postal(r) == postal.strip())


class RateLimiter:
    """Spaces calls to `wait()` at least `1 / rate` seconds apart."""

    def __init__(
        self,
        rate: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._next = float("-inf")

    def wait(self) -> None:
        now = self._clock()
        if now < self._next:
            self._sleep(self._next - now)
            now = self._next
        self._next = now + self._interval


def _require_onemap_host(request: httpx.Request) -> None:
    if request.url.host != ONEMAP_HOST:
        raise OneMapError(f"OneMap client refused non-OneMap host {request.url.host!r}")


def _is_auth_error(error: object) -> bool:
    """True for OneMap's in-body token errors, which arrive with HTTP 200, e.g.
    "Authentication token missing. Please create an account and generate or renew your API
    Token."
    """
    return isinstance(error, str) and ("token" in error.lower() or "authenticat" in error.lower())


@dataclass(frozen=True)
class _Reply:
    """Status and fully read body of one response."""

    status_code: int
    content: bytes


def _json(reply: _Reply, what: str) -> Any:
    try:
        return json.loads(reply.content)
    except ValueError as exc:
        raise OneMapError(f"{what}: response is not JSON") from exc


def _json_object(reply: _Reply, what: str) -> dict[str, Any]:
    body = _json(reply, what)
    if not isinstance(body, dict):
        raise OneMapError(f"{what}: expected a JSON object")
    return body


class OneMapClient:
    """Authenticated, rate-limited OneMap search. Use as a context manager."""

    def __init__(
        self,
        email: str,
        password: SecretStr,
        *,
        requests_per_second: float,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._email = email
        self._password = password
        self._sleep = sleep
        self._clock = clock
        self._wall_clock = wall_clock
        self._limiter = RateLimiter(requests_per_second, clock=clock, sleep=sleep)
        self._token: str | None = None
        self._token_expires_at = 0.0
        self.request_counts: Counter[str] = Counter()
        self._http = httpx.Client(
            base_url=BASE_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=10.0),
            follow_redirects=False,
            event_hooks={"request": [_require_onemap_host]},
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> OneMapClient:
        """Build a client from settings; fails clearly if credentials are not set."""
        if not settings.onemap_email or settings.onemap_password is None:
            raise OneMapAuthError(
                "ONEMAP_EMAIL and ONEMAP_PASSWORD must be set in .env to geocode "
                f"(register at {REGISTER_URL})"
            )
        return cls(
            settings.onemap_email,
            settings.onemap_password,
            requests_per_second=settings.geocode_requests_per_second,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _send(
        self, kind: str, method: str, path: str, policy: RetryPolicy, **kwargs: Any
    ) -> _Reply:
        policy.charge()
        self._limiter.wait()
        self.request_counts[kind] += 1
        timeout = policy.timeout(REQUEST_TIMEOUT_S)
        with self._http.stream(method, path, timeout=timeout, **kwargs) as response:
            raise_for_retryable(response)
            # Read chunk by chunk: httpx's read timeout is per read, so a slowly trickling
            # body could otherwise outlive the search deadline.
            content = bytearray()
            for chunk in response.iter_bytes():
                policy.check_deadline()
                content += chunk
            policy.check_deadline()  # the end of the body may arrive after the last chunk
        return _Reply(response.status_code, bytes(content))

    def _access_token(self, policy: RetryPolicy) -> str:
        if self._token and self._wall_clock() < self._token_expires_at - TOKEN_REFRESH_MARGIN_S:
            return self._token
        response = self._send(
            "token",
            "POST",
            TOKEN_PATH,
            policy,
            json={"email": self._email, "password": self._password.get_secret_value()},
        )
        if response.status_code != 200:
            raise OneMapAuthError(
                f"OneMap token request failed with HTTP {response.status_code}; "
                "check ONEMAP_EMAIL and ONEMAP_PASSWORD"
            )
        body = _json_object(response, "token")
        token, expiry = body.get("access_token"), body.get("expiry_timestamp")
        try:
            expires_at = float(expiry)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            expires_at = None
        if not isinstance(token, str) or not token or expires_at is None:
            raise OneMapError("token: response lacks access_token or expiry_timestamp")
        self._token, self._token_expires_at = token, expires_at
        logger.info(
            "Obtained OneMap token (expires %s)",
            time.strftime("%Y-%m-%d %H:%M", time.localtime(expires_at)),
        )
        return token

    def _authed_get(self, kind: str, path: str, params: dict[str, str], what: str) -> Any:
        """GET with the access token; a rejected token (HTTP 401/403, or an in-body token
        error with HTTP 200) is replaced once. Returns the parsed JSON body."""
        policy = RetryPolicy.starting_now(
            SEARCH_MAX_ATTEMPTS, SEARCH_DEADLINE_S, self._sleep, self._clock
        )

        def attempt() -> Any:
            for refreshed in (False, True):
                token = self._access_token(policy)
                reply = self._send(
                    kind, "GET", path, policy, params=params, headers={"Authorization": token}
                )
                if reply.status_code in (401, 403):
                    problem = f"HTTP {reply.status_code}"
                elif reply.status_code != 200:
                    raise OneMapError(f"{what}: unexpected HTTP {reply.status_code}")
                else:
                    body = _json(reply, what)
                    if not (isinstance(body, dict) and _is_auth_error(body.get("error"))):
                        return body
                    problem = repr(body["error"])
                if refreshed:
                    raise OneMapAuthError(f"OneMap rejected a fresh token ({problem})")
                self._token = None  # rejected: fetch a fresh token once

            raise AssertionError("unreachable")

        return retrying(attempt, policy, what=f"OneMap {what}")

    def search(self, query: str) -> dict[str, Any]:
        """Return the first page of OneMap search results for `query`."""
        what = f"search {query!r}"
        params = {"searchVal": query, "returnGeom": "Y", "getAddrDetails": "Y", "pageNum": "1"}
        body = self._authed_get("search", SEARCH_PATH, params, what)
        if not isinstance(body, dict):
            raise OneMapError(f"{what}: expected a JSON object")
        if "error" in body:
            raise OneMapError(f"{what}: OneMap returned error {body['error']!r}")
        if not isinstance(body.get("found"), int) or not isinstance(body.get("results"), list):
            raise OneMapError(f"{what}: response lacks 'found' or 'results'")
        return body

    def nearest_mrt_stations(self, lat: float, lon: float, radius_m: int) -> list[dict[str, Any]]:
        """MRT and LRT stations within `radius_m` of a point, as OneMap lists them."""
        what = f"nearest MRT stops at ({lat:.6f}, {lon:.6f})"
        params = {
            "latitude": f"{lat:.8f}",
            "longitude": f"{lon:.8f}",
            "radius_in_meters": str(radius_m),
        }
        body = self._authed_get("nearest_mrt", NEAREST_MRT_PATH, params, what)
        if not isinstance(body, list) or not all(
            isinstance(s, dict) and {"name", "lat", "lon"} <= s.keys() for s in body
        ):
            raise OneMapError(f"{what}: expected a list of stations with name, lat and lon")
        return body
