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


def test_window_medians_keep_current_groups_without_history() -> None:
    rows = pd.DataFrame(
        [
            ("OLD", "latest", 6000.0, 80),
            ("OLD", "earlier", 5000.0, 90),
            ("NEW", "latest", 7000.0, 60),  # no earlier sales at all
            ("THIN", "latest", 6500.0, 70),
            ("THIN", "earlier", 6000.0, 10),  # too few earlier sales for a change
            ("RARE", "latest", 9000.0, 5),  # too few recent sales: dropped
            ("RARE", "earlier", 8000.0, 500),
        ],
        columns=["grp", "win", "median_psm", "sales"],
    )
    result = data.window_medians(rows, "town").set_index("town")
    assert sorted(result.index) == ["NEW", "OLD", "THIN"]
    assert result.loc["OLD", "change"] == pytest.approx(0.2)
    assert pd.isna(result.loc["NEW", "change"])
    assert pd.isna(result.loc["THIN", "change"])
    assert result.loc["NEW", "earlier_sales"] == 0


def test_feature_associations_treat_counts_as_discrete() -> None:
    from sklearn.feature_selection import mutual_info_regression

    rng = np.random.default_rng(1)
    n = 2000
    sample = pd.DataFrame({"n_mrt_exits_500m": rng.integers(0, 4, n).astype(float)})
    sample["log_price"] = sample["n_mrt_exits_500m"] * 0.1 + rng.normal(0, 0.05, n)
    result = data.feature_associations(sample)
    expected = mutual_info_regression(
        sample[["n_mrt_exits_500m"]], sample["log_price"], discrete_features=True, random_state=0
    )[0]
    assert result.loc[0, "mutual_info"] == pytest.approx(expected)


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
