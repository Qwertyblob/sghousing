"""HTTP clients and retry policy.

Two clients on purpose: the API client carries the data.gov.sg key and only talks to the API
hosts; the download client carries no credentials and fetches pre-signed storage URLs.
"""

from __future__ import annotations

import email.utils
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import tenacity

logger = logging.getLogger(__name__)

API_HOSTS = frozenset({"api-production.data.gov.sg", "api-open.data.gov.sg"})
USER_AGENT = "sghousing/0.1 (+https://github.com/qwertyblob/sghousing)"
API_KEY_HEADER = "x-api-key"


class UnsafeRequestError(RuntimeError):
    """Raised when the API client would send credentials somewhere it must not."""


class RetryableHTTPError(RuntimeError):
    """A 429 or 5xx response; `retry_after` is in seconds when the server sent one."""

    def __init__(self, status_code: int, retry_after: float | None) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.retry_after = retry_after


class NotReadyError(RuntimeError):
    """A polled resource is not ready yet; retried with backoff."""


RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    RetryableHTTPError,
    NotReadyError,
    httpx.TransportError,  # includes connection resets and errors while reading a body
)


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date) into seconds."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def raise_for_retryable(response: httpx.Response) -> None:
    """Raise RetryableHTTPError for 429/5xx responses."""
    if response.status_code == 429 or response.status_code >= 500:
        raise RetryableHTTPError(
            response.status_code, parse_retry_after(response.headers.get("Retry-After"))
        )


def _require_api_host(request: httpx.Request) -> None:
    if request.url.host not in API_HOSTS:
        raise UnsafeRequestError(f"API client refused non-API host {request.url.host!r}")


def make_api_client(
    api_key: str | None, *, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """Client for data.gov.sg API hosts only. Never follows redirects."""
    headers = {"User-Agent": USER_AGENT}
    if api_key:
        headers[API_KEY_HEADER] = api_key
    return httpx.Client(
        headers=headers,
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=False,
        event_hooks={"request": [_require_api_host]},
        transport=transport,
    )


def make_download_client(*, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """Credential-free client for pre-signed storage URLs. Follows redirects."""
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(60.0, connect=10.0),
        follow_redirects=True,
        transport=transport,
    )


@dataclass
class RetryPolicy:
    """Attempt limit plus an absolute deadline on `clock`, shared by nested retries."""

    max_attempts: int
    deadline: float
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    _backoff: Any = field(
        default_factory=lambda: tenacity.wait_exponential_jitter(multiplier=2, max=60, jitter=1)
    )

    @classmethod
    def starting_now(
        cls,
        max_attempts: int,
        timeout_s: float,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> RetryPolicy:
        return cls(max_attempts, clock() + timeout_s, sleep, clock)

    def remaining(self) -> float:
        return self.deadline - self.clock()


def retrying[T](fn: Callable[[], T], policy: RetryPolicy, *, what: str = "request") -> T:
    """Call `fn`, retrying retryable errors until attempts or the deadline run out.

    Waits honour Retry-After when present, otherwise exponential backoff with jitter. Waits
    are clipped to the remaining deadline. The last exception is re-raised on exhaustion.
    """

    def stop(state: tenacity.RetryCallState) -> bool:
        return state.attempt_number >= policy.max_attempts or policy.remaining() <= 0

    def wait(state: tenacity.RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, RetryableHTTPError) and exc.retry_after is not None:
            seconds = exc.retry_after
        else:
            seconds = policy._backoff(state)
        return max(0.0, min(seconds, policy.remaining()))

    def before_sleep(state: tenacity.RetryCallState) -> None:
        exc = state.outcome.exception() if state.outcome else None
        logger.warning(
            "%s failed (attempt %d/%d): %s; retrying in %.1fs",
            what,
            state.attempt_number,
            policy.max_attempts,
            exc,
            state.upcoming_sleep,
        )

    retryer = tenacity.Retrying(
        stop=stop,
        wait=wait,
        retry=tenacity.retry_if_exception_type(RETRYABLE_EXCEPTIONS),
        sleep=policy.sleep,
        before_sleep=before_sleep,
        reraise=True,
    )
    return retryer(fn)
