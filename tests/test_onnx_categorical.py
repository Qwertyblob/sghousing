"""The ONNX converter supports what the models use: LightGBM native categorical splits,
the quantile objective and missing values (NaN), including category codes unseen in training.

The training pipeline relies on this; `export_onnx.check_onnx` repeats the comparison on
the real models at export time.
"""

from __future__ import annotations

from pathlib import Path

import lightgbm as lgb
import numpy as np
import onnx
import pandas as pd
import pytest

from pipeline.train import calibrate, export_onnx
from pipeline.train.dataset import CATEGORICAL, FEATURES

N_ROWS = 3000


def synthetic(rng: np.random.Generator, n: int) -> tuple[pd.DataFrame, np.ndarray]:
    """Rows in FEATURES order; log price depends on category codes and two numerics."""
    x = pd.DataFrame(
        {
            col: rng.integers(0, 12, n).astype("float32")
            if col in CATEGORICAL
            else rng.normal(size=n).astype("float32")
            for col in FEATURES
        }
    )[list(FEATURES)]
    town_effect = np.where(np.isin(x["town"], [1, 4, 7, 9]), 0.3, -0.2)
    y = 13 + town_effect + 0.1 * x["flat_type"] + 0.2 * x["floor_area_sqm"]
    y += 0.05 * rng.normal(size=n)
    return x, y.to_numpy()


@pytest.fixture(scope="module")
def models(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, lgb.Booster], Path]:
    rng = np.random.default_rng(0)
    x, y = synthetic(rng, N_ROWS)
    x.loc[rng.random(N_ROWS) < 0.05, "floor_area_sqm"] = np.nan
    directory = tmp_path_factory.mktemp("onnx")
    boosters = {}
    for name, alpha in {"p10": 0.1, "p50": 0.5, "p90": 0.9}.items():
        data = lgb.Dataset(x, y, categorical_feature=list(CATEGORICAL))
        params = {
            "objective": "quantile",
            "alpha": alpha,
            "num_leaves": 15,
            "min_data_per_group": 5,
            "cat_smooth": 1.0,
            "max_cat_to_onehot": 2,
            "verbose": -1,
            "seed": 0,
        }
        boosters[name] = lgb.train(params, data, num_boost_round=60)
        export_onnx.export_model(boosters[name], export_onnx.onnx_path(directory, name))
    return boosters, directory


def test_models_use_categorical_splits(models: tuple[dict[str, lgb.Booster], Path]) -> None:
    boosters, _ = models
    dump = boosters["p50"].dump_model()
    decisions: list[str] = []

    def walk(node: dict) -> None:
        if "decision_type" in node:
            decisions.append(node["decision_type"])
            walk(node["left_child"])
            walk(node["right_child"])

    for tree in dump["tree_info"]:
        walk(tree["tree_structure"])
    assert "==" in decisions, "expected native categorical splits in the test model"


def test_onnx_matches_lightgbm(models: tuple[dict[str, lgb.Booster], Path]) -> None:
    boosters, directory = models
    x, _ = synthetic(np.random.default_rng(1), 500)
    x.loc[::7, "town"] = np.nan  # missing category
    x.loc[::11, "flat_type"] = 99.0  # code never seen in training
    x.loc[::13, "floor_area_sqm"] = np.nan  # missing numeric
    worst = export_onnx.check_onnx(boosters, directory, x)
    assert worst < 1e-5


def test_float32_thresholds_keep_predictions_and_fix_branch_flips(tmp_path: Path) -> None:
    # Values one float32 step apart: LightGBM splits at their double midpoint, which rounds
    # to float32 on one side of an input, so unaligned ONNX sends that input the other way.
    rng = np.random.default_rng(3)
    x, _ = synthetic(rng, N_ROWS)
    lo = np.float32(1234.5678)
    x["dist_mrt_m"] = np.where(rng.random(N_ROWS) < 0.5, lo, np.nextafter(lo, np.float32(np.inf)))
    y = np.where(x["dist_mrt_m"] > lo, 14.0, 13.0) + 0.01 * rng.normal(size=N_ROWS)
    params = {"objective": "regression", "verbose": -1, "seed": 0, "min_data_in_leaf": 5}
    booster = lgb.train(params, lgb.Dataset(x, y), num_boost_round=20)
    aligned = lgb.Booster(
        model_str=export_onnx.float32_thresholds(booster.model_to_string())
    )  # fmt: skip
    assert np.array_equal(booster.predict(x), aligned.predict(x))
    export_onnx.export_model(booster, tmp_path / "model_raw.onnx")
    export_onnx.export_model(aligned, tmp_path / "model_aligned.onnx")
    raw_diff = np.abs(export_onnx.run_onnx(tmp_path / "model_raw.onnx", x) - booster.predict(x))
    assert raw_diff.max() > 0.1, "expected the unaligned export to flip branches"
    assert export_onnx.check_onnx({"aligned": aligned}, tmp_path, x) < 1e-5


def test_shift_model_moves_every_prediction(
    models: tuple[dict[str, lgb.Booster], Path], tmp_path: Path
) -> None:
    boosters, _ = models
    x, _ = synthetic(np.random.default_rng(5), 300)
    x.loc[::9, "town"] = np.nan
    shifted = lgb.Booster(model_str=calibrate.shift_model(boosters["p90"].model_to_string(), 0.07))
    assert np.allclose(shifted.predict(x), boosters["p90"].predict(x) + 0.07, rtol=0, atol=1e-12)
    export_onnx.export_model(shifted, export_onnx.onnx_path(tmp_path, "p90"))
    assert export_onnx.check_onnx({"p90": shifted}, tmp_path, x) < 1e-5


def test_export_drops_unit_hitrates(models: tuple[dict[str, lgb.Booster], Path]) -> None:
    _, directory = models
    model = onnx.load(str(export_onnx.onnx_path(directory, "p50")))
    names = {a.name for node in model.graph.node for a in node.attribute}
    assert "nodes_values" in names
    assert "nodes_hitrates" not in names


def test_check_onnx_rejects_non_finite_predictions(
    models: tuple[dict[str, lgb.Booster], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    boosters, directory = models
    x, _ = synthetic(np.random.default_rng(4), 10)
    monkeypatch.setattr(export_onnx, "run_onnx", lambda path, rows: np.full(len(rows), np.nan))
    with pytest.raises(RuntimeError, match="non-finite"):
        export_onnx.check_onnx(boosters, directory, x)


def test_check_onnx_rejects_a_wrong_export(models: tuple[dict[str, lgb.Booster], Path]) -> None:
    boosters, directory = models
    x, _ = synthetic(np.random.default_rng(2), 100)
    swapped = {"p10": boosters["p90"]}
    with pytest.raises(RuntimeError, match="differs from LightGBM"):
        export_onnx.check_onnx(swapped, directory, x)
