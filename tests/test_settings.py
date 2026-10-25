"""Unit tests for config.settings validation. No database needed."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from config.settings import Settings, get_settings

_SETTINGS_ENV_VARS = (
    "DATABASE_URL",
    "POSTGRES_PORT",
    "DATAGOV_API_KEY",
    "ONEMAP_EMAIL",
    "ONEMAP_PASSWORD",
    "GEOCODE_REQUESTS_PER_SECOND",
    "DATAGOV_MAX_ATTEMPTS",
    "DATAGOV_DOWNLOAD_TIMEOUT_S",
    "TRAIN_END",
    "VALID_END",
    "DATA_DIR",
    "REPORTS_DIR",
    "MODELS_DIR",
    "SITE_DATA_DIR",
)

BASE = {
    "database_url": "postgresql+psycopg://sg:sg@localhost:5432/sg_housing",
    "postgres_port": 5432,
    "train_end": "2023-12",
    "valid_end": "2024-12",
}


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep exported shell variables and the settings cache out of every test."""
    for name in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def make(**overrides: object) -> Settings:
    return Settings(_env_file=None, **{**BASE, **overrides})


def test_defaults_parse() -> None:
    s = make()
    assert s.train_end == date(2023, 12, 1)
    assert s.valid_end == date(2024, 12, 1)
    assert s.geocode_requests_per_second == 3.0
    assert s.datagov_api_key is None


@pytest.mark.parametrize("field", ["datagov_api_key", "onemap_email", "onemap_password"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_credentials_become_none(field: str, blank: str) -> None:
    assert getattr(make(**{field: blank}), field) is None


def test_api_key_kept_secret() -> None:
    s = make(datagov_api_key="abc123")
    assert s.datagov_api_key is not None
    assert s.datagov_api_key.get_secret_value() == "abc123"
    assert "abc123" not in repr(s)


@pytest.mark.parametrize("bad", ["2023-1", "2023-13", "23-12", "2023/12", "2023-12-01", ""])
def test_month_must_be_strict_calendar_month(bad: str) -> None:
    with pytest.raises(ValidationError):
        make(train_end=bad, valid_end="2099-01")


@pytest.mark.parametrize(("train", "valid"), [("2024-12", "2024-12"), ("2025-01", "2024-12")])
def test_train_end_before_valid_end(train: str, valid: str) -> None:
    with pytest.raises(ValidationError, match="must be before"):
        make(train_end=train, valid_end=valid)


@pytest.mark.parametrize("rate", [0, -1, float("inf"), float("nan")])
def test_rate_must_be_finite_positive(rate: float) -> None:
    with pytest.raises(ValidationError):
        make(geocode_requests_per_second=rate)


def test_port_mismatch_rejected() -> None:
    with pytest.raises(ValidationError, match="does not match POSTGRES_PORT"):
        make(postgres_port=5433)


def test_validation_errors_hide_secrets() -> None:
    with pytest.raises(ValidationError) as exc_info:
        make(
            database_url="postgresql+psycopg://sg:dbpass-xyz@localhost:5432/sg_housing",
            datagov_api_key="key-abc-123",
            onemap_password="pw-def-456",
            postgres_port=5433,
        )
    message = str(exc_info.value)
    for secret in ("dbpass-xyz", "key-abc-123", "pw-def-456"):
        assert secret not in message


def test_port_match_on_nondefault_port() -> None:
    s = make(
        database_url="postgresql+psycopg://sg:sg@localhost:55432/sg_housing",
        postgres_port=55432,
    )
    assert s.postgres_port == 55432


def test_remote_host_port_not_tied_to_postgres_port() -> None:
    make(database_url="postgresql+psycopg://sg:sg@db.example:6543/sg_housing")


def test_unknown_key_in_env_file_rejected(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("TRAIN_END=2023-12\nTRAIN_ENDD=2023-12\n")
    with pytest.raises(ValidationError, match="train_endd"):
        Settings(_env_file=env)


def test_env_vars_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRAIN_END", "2022-06")
    monkeypatch.setenv("GEOCODE_REQUESTS_PER_SECOND", "1.5")
    s = Settings(_env_file=None)
    assert s.train_end == date(2022, 6, 1)
    assert s.geocode_requests_per_second == 1.5
