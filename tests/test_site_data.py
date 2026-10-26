"""The exported site data in `site/public/data/` (settings.site_data_dir): every file
validates against its JSON Schema in `site/src/schemas/`, and the sizes stay within budget.

Run with `make export` (which exports first) or `uv run pytest -m site_data`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from config.settings import get_settings
from pipeline.export.site_data import MAX_GEOJSON_BYTES, MAX_TOTAL_BYTES, directory_bytes

pytestmark = pytest.mark.site_data

SCHEMAS = Path(__file__).parents[1] / "site" / "src" / "schemas"
DATA = get_settings().site_data_dir
# Schema name for each fixed file; recent/*.json all use "recent".
FILES = {
    "meta.json": "meta",
    "town_month.json": "town_month",
    "planning_areas.geojson": "planning_areas",
    "lease_decay.json": "lease_decay",
    "blocks.json": "blocks",
    "model/features.json": "features",
}


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def data_files() -> list[tuple[str, str]]:
    recent = sorted(p.relative_to(DATA).as_posix() for p in (DATA / "recent").glob("*.json"))
    return [*FILES.items(), *((name, "recent") for name in recent)]


@pytest.fixture(scope="module", autouse=True)
def exported() -> None:
    if not (DATA / "meta.json").exists():
        pytest.fail(f"no site data in {DATA} (run `make export`)")


@pytest.mark.parametrize(("name", "schema_name"), data_files())
def test_file_matches_schema(name: str, schema_name: str) -> None:
    validator = Draft202012Validator(
        load(SCHEMAS / f"{schema_name}.schema.json"), format_checker=FormatChecker()
    )
    errors = [
        f"{'/'.join(map(str, e.absolute_path))}: {e.message[:200]}"
        for e in validator.iter_errors(load(DATA / name))
    ]
    assert not errors, errors[:5]


def test_meta_lists_every_town_file_and_model() -> None:
    meta = load(DATA / "meta.json")
    listed = {t["file"] for t in meta["towns"]}
    on_disk = {p.relative_to(DATA).as_posix() for p in (DATA / "recent").glob("*.json")}
    assert listed == on_disk
    features = load(DATA / "model" / "features.json")
    assert meta["model"]["version"] == features["model_version"]
    for file in features["outputs"].values():
        assert (DATA / "model" / file).stat().st_size > 0


def test_sizes_within_budget() -> None:
    assert (DATA / "planning_areas.geojson").stat().st_size < MAX_GEOJSON_BYTES
    assert directory_bytes(DATA) < MAX_TOTAL_BYTES
