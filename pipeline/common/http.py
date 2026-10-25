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


class DeadlineExceededError(TimeoutError):
    """The operation's overall deadline passed. Not retried."""


class RetryBudgetExhaustedError(RuntimeError):
    """The operation's shared attempt budget is used up. Not retried."""


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
    """One operation's request budget and absolute deadline (on `clock`).

    Shared by nested retries: every HTTP request in the operation, at any nesting level,
    charges one attempt, so the total never exceeds `max_attempts`. Request timeouts are
    capped at the time left, so no single request or stream outlives the deadline by more
    than one read.
    """

    max_attempts: int
    deadline: float
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    attempts: int = 0
    _backoff: Any = field(
        default_factory=lambda: tenacity.wait_exponential_jitter(multiplier=2, max=60, jitter=1)
    )

    def charge(self) -> None:
        """Record one request attempt; refuse if the budget or deadline is spent."""
        if self.attempts >= self.max_attempts:
            raise RetryBudgetExhaustedError(f"used all {self.max_attempts} attempts")
        self.check_deadline()
        self.attempts += 1

    def check_deadline(self) -> None:
        """Raise DeadlineExceededError once the deadline has passed."""
        if self.remaining() <= 0:
            raise DeadlineExceededError("operation deadline exceeded")

    def timeout(self, cap: float) -> httpx.Timeout:
        """A request timeout of `cap` seconds, shortened to the time left."""
        self.check_deadline()
        return httpx.Timeout(min(cap, self.remaining()))

    def exhausted(self) -> bool:
        return self.attempts >= self.max_attempts or self.remaining() <= 0

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
    """Call `fn`, retrying retryable errors until the policy's budget or deadline runs out.

    `fn` must charge the policy for each request it makes (see `RetryPolicy.charge`), so
    nested calls draw from one budget. Waits honour Retry-After when present, otherwise
    exponential backoff with jitter, clipped to the remaining deadline. The last exception
    is re-raised on exhaustion.
    """

    def stop(state: tenacity.RetryCallState) -> bool:
        return policy.exhausted()

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
            "%s failed (%d/%d attempts used): %s; retrying in %.1fs",
            what,
            policy.attempts,
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
