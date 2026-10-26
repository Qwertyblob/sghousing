"""The website's feature builder (site/src/predict/features.ts, run by Node) gives the same
float32 model input as the CLI (`pipeline.train.predict.build_features` + `encode_one`) for a
sample of blocks, flat types, floor areas and storeys.

Run with `make test-db` after `make features` and `make export`; needs Node 22.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import Engine, text

from config.settings import get_settings
from pipeline.common.db import get_engine
from pipeline.train.dataset import FEATURES
from pipeline.train.predict import Flat, build_features, encode_one

pytestmark = pytest.mark.db

SITE = Path(__file__).parents[1] / "site"
N_CASES = 60


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    get_settings.cache_clear()
    engine = get_engine()
    with engine.connect() as conn:
        built = conn.execute(
            text("SELECT to_regclass('marts.block_features') IS NOT NULL")
        ).scalar_one()
    if not built:
        engine.dispose()
        pytest.skip("features not built yet (run `make features`)")
    yield engine
    engine.dispose()


def test_site_features_match_cli(engine: Engine, tmp_path: Path) -> None:
    data = get_settings().site_data_dir
    if not (data / "blocks.json").exists():
        pytest.skip("no site data (run `make export`)")
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    blocks = json.loads((data / "blocks.json").read_text(encoding="utf-8"))
    spec = json.loads((data / "model" / "features.json").read_text(encoding="utf-8"))
    columns = blocks["columns"]
    rows = [dict(zip(columns, row, strict=True)) for row in blocks["rows"]]
    rng = np.random.default_rng(9)
    types = sorted(spec["categories"]["flat_type"])
    cases = []
    for i in rng.choice(len(rows), N_CASES, replace=False):
        row = rows[int(i)]
        # Mostly a flat type sold at the block, sometimes one that is not (town fallback).
        own = sorted(row["flat_models"])
        flat_type = str(rng.choice(own if rng.random() < 0.8 else types))
        cases.append(
            {
                "address_key": row["address_key"],
                "block": row["block"],
                "street_name": row["street_name"],
                "flat_type": flat_type,
                "floor_area_sqm": float(rng.choice([45.0, 67.5, 92.0, 104.0, 121.0])),
                "storey": int(rng.integers(1, row["block_max_floor"] + 3)),
            }
        )
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps(cases), encoding="utf-8")
    # Priced at as_of with no months elapsed, as the CLI prices.
    today = f"{blocks['as_of']}-01T12:00:00+08:00"
    result = subprocess.run(
        [
            "node",
            "--experimental-strip-types",
            "--no-warnings=ExperimentalWarning",
            "scripts/vectors.ts",
            str(data / "blocks.json"),
            str(data / "model" / "features.json"),
            str(cases_path),
            today,
        ],
        cwd=SITE,
        capture_output=True,
        text=True,
        check=True,
    )
    site = np.array(json.loads(result.stdout), dtype=np.float64)

    with engine.connect() as conn:
        for case, vector in zip(cases, site, strict=True):
            flat = Flat(
                case["block"],
                case["street_name"],
                case["flat_type"],
                case["floor_area_sqm"],
                case["storey"],
            )
            features, _ = build_features(conn, flat)
            expected = encode_one(features, spec["categories"]).to_numpy(np.float64)[0]
            mismatched = [
                f"{name}: site {a!r}, cli {b!r}"
                for name, a, b in zip(FEATURES, vector, expected, strict=True)
                if not (a == b or (np.isnan(a) and np.isnan(b)))
            ]
            assert not mismatched, (case, mismatched)
