"""Project settings, read from environment variables and `.env`.

Every key in `.env.example` is declared here. Unknown keys are rejected so typos fail loudly.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

REPO_ROOT = Path(__file__).resolve().parent.parent

_MONTH_RE = re.compile(r"[0-9]{4}-[0-9]{2}")
_LOCAL_HOSTS = {"localhost", "127.0.0.1"}


class Settings(BaseSettings):
    """Typed configuration for the pipeline, model and export steps."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="forbid",
        # Validation errors must never echo DATABASE_URL, API keys or passwords.
        hide_input_in_errors=True,
    )

    database_url: str = "postgresql+psycopg://sg:sg@localhost:5432/sg_housing"
    # Host port Compose publishes Postgres on; must match the port in DATABASE_URL.
    postgres_port: int = Field(default=5432, ge=1, le=65535)

    datagov_api_key: SecretStr | None = None
    onemap_email: str | None = None
    onemap_password: SecretStr | None = None
    geocode_requests_per_second: float = 3.0
    # Total HTTP requests allowed per operation (all retries of initiate, poll and download).
    datagov_max_attempts: int = Field(default=20, ge=1)
    datagov_download_timeout_s: float = Field(default=600.0, gt=0)

    train_end: date = date(2023, 12, 1)
    valid_end: date = date(2024, 12, 1)

    data_dir: Path = REPO_ROOT / "data"
    reports_dir: Path = REPO_ROOT / "reports"
    models_dir: Path = REPO_ROOT / "models"
    site_data_dir: Path = REPO_ROOT / "site" / "public" / "data"
    site_schema_dir: Path = REPO_ROOT / "site" / "src" / "schemas"
    snapshot_dir: Path = REPO_ROOT / "data" / "snapshot"
    # owner/name on GitHub: snapshot release commands and the Pages site.
    github_repo: str = "qwertyblob/sghousing"

    @field_validator("datagov_api_key", "onemap_email", "onemap_password", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("train_end", "valid_end", mode="before")
    @classmethod
    def _parse_month(cls, value: Any) -> Any:
        if isinstance(value, date):
            return value.replace(day=1)
        if not isinstance(value, str) or not _MONTH_RE.fullmatch(value):
            raise ValueError(f"expected YYYY-MM, got {value!r}")
        try:
            return datetime.strptime(value, "%Y-%m").date()
        except ValueError as exc:
            raise ValueError(f"not a calendar month: {value!r}") from exc

    @field_validator("geocode_requests_per_second")
    @classmethod
    def _positive_finite_rate(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"must be a finite number > 0, got {value!r}")
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if self.train_end >= self.valid_end:
            raise ValueError(
                f"TRAIN_END ({self.train_end:%Y-%m}) must be before "
                f"VALID_END ({self.valid_end:%Y-%m})"
            )
        url = make_url(self.database_url)
        url_port = url.port or 5432
        if url.host in _LOCAL_HOSTS and url_port != self.postgres_port:
            raise ValueError(
                f"DATABASE_URL port ({url_port}) does not match POSTGRES_PORT "
                f"({self.postgres_port}); update one so they agree"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached)."""
    return Settings()
