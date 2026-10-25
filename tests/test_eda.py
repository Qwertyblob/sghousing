"""Unit tests for the EDA helpers and the committed notebooks (no database)."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pytest

from pipeline.eda import data, figures

NOTEBOOKS = sorted((Path(__file__).parent.parent / "notebooks").glob("*.ipynb"))


def test_bucket_labels_match_width_bucket_indexes() -> None:
    assert data.bucket_labels((0, 200, 400, 1000)) == ["0–200", "200–400", "400–1,000", "1,000+"]
    assert len(data.bucket_labels(data.MRT_BUCKET_EDGES_M)) == len(data.MRT_BUCKET_EDGES_M)


def test_feature_associations_rank_an_informative_feature_first() -> None:
    rng = np.random.default_rng(0)
    n = 2000
    sample = pd.DataFrame({"floor_area_sqm": rng.uniform(40, 150, n), "lat": rng.normal(size=n)})
    sample["log_price"] = np.log(sample["floor_area_sqm"] * 5000)
    result = data.feature_associations(sample)
    assert list(result["feature"]) == ["floor_area_sqm", "lat"]
    assert result.loc[0, "spearman"] == pytest.approx(1.0)
    assert abs(result.loc[1, "spearman"]) < 0.1


def test_save_figure_writes_identical_png_and_svg_each_time(tmp_path: Path) -> None:
    figures.apply_style()
    fig, ax = plt.subplots()
    ax.plot([1, 2, 3], [3, 1, 2])
    png, svg = figures.save_figure(fig, "check", tmp_path)
    first = (png.read_bytes(), svg.read_bytes())
    figures.save_figure(fig, "check", tmp_path)
    plt.close(fig)
    assert (png.read_bytes(), svg.read_bytes()) == first
    assert b"<dc:date>" not in first[1]


def test_notebooks_exist() -> None:
    assert [p.name for p in NOTEBOOKS] == [
        "01_prices_and_volume.ipynb",
        "02_towns_and_map.ipynb",
        "03_price_drivers.ipynb",
        "04_feature_associations.ipynb",
    ]


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_notebook_outputs_are_cleared(path: Path) -> None:
    notebook = json.loads(path.read_text(encoding="utf-8"))
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            assert cell["outputs"] == [], f"{path.name}: clear outputs before committing"
            assert cell["execution_count"] is None
