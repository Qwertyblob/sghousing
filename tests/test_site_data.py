"""The exported site data in `site/public/data/` (settings.site_data_dir) passes the same
validation the export applies before publishing: JSON Schemas in `site/src/schemas/`, cross
references and sizes.

Run with `make export` (which exports first) or `uv run pytest -m site_data`.
"""

from __future__ import annotations

import pytest

from config.settings import get_settings
from pipeline.export.site_data import validation_errors

pytestmark = pytest.mark.site_data


def test_exported_site_data_is_valid() -> None:
    settings = get_settings()
    if not (settings.site_data_dir / "meta.json").exists():
        pytest.fail(f"no site data in {settings.site_data_dir} (run `make export`)")
    errors = validation_errors(settings.site_data_dir, settings.site_schema_dir)
    assert not errors, errors[:20]
