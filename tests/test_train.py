"""Unit tests for the modelling helpers (no database or trained model needed)."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from pipeline import cli
from pipeline.train import calibrate
from pipeline.train import predict as predict_module
from pipeline.train.baselines import TownMedianBaseline
from pipeline.train.dataset import (
    CATEGORICAL,
    FEATURES,
    NUMERIC,
    Split,
    assign_split,
    category_maps,
    combine,
    encode,
    model_version,
)
from pipeline.train.metrics import grouped_metrics, interval_coverage, regression_metrics
from pipeline.train.predict import (
    Flat,
    encode_one,
    lease_features,
    month_index,
    next_month,
    normalise,
    predict,
    relative_storey,
    sql_round,
    storey_mid,
)
from pipeline.train.train_lgbm import ordered_quantiles, publish, sample_params


def frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Rows with every feature column, NaN/None where not given."""
    base = {col: None for col in CATEGORICAL} | {col: np.nan for col in NUMERIC}
    return pd.DataFrame([base | row for row in rows])


def test_assign_split_uses_month_boundaries() -> None:
    months = pd.Series(pd.to_datetime(["2023-12-01", "2024-01-01", "2024-12-01", "2025-01-01"]))
    split = assign_split(months, date(2023, 12, 1), date(2024, 12, 1))
    assert split.tolist() == ["train", "valid", "valid", "test"]


def test_category_codes_are_sorted_training_values_and_unseen_is_nan() -> None:
    train = frame([{"town": "YISHUN"}, {"town": "ANG MO KIO"}, {"town": "YISHUN"}])
    maps = category_maps(train)
    assert maps["town"] == {"ANG MO KIO": 0, "YISHUN": 1}
    x = encode(frame([{"town": "YISHUN"}, {"town": "TENGAH"}, {"town": None}]), maps)
    assert list(x.columns) == list(FEATURES)
    assert (x.dtypes == np.float32).all()
    assert x["town"].tolist()[0] == 1.0
    assert np.isnan(x["town"].tolist()[1:]).all()


def test_model_version_is_last_month() -> None:
    df = pd.DataFrame({"month": pd.to_datetime(["2026-09-01", "2026-10-01"])})
    assert model_version(df) == "2026-10"


def test_town_median_baseline_falls_back_to_flat_type_median() -> None:
    rows = pd.DataFrame(
        {
            "flat_type": ["3 ROOM", "3 ROOM", "4 ROOM"],
            "floor_area_sqm": [70.0, 70.0, 100.0],
            "resale_price": [350_000.0, 420_000.0, 600_000.0],
            "town_psm_12m": [5000.0, np.nan, np.nan],
        }
    )
    split = Split("train", pd.DataFrame(), np.log(rows["resale_price"].to_numpy()), rows)
    predicted = TownMedianBaseline().fit(split).predict_price(split)
    # Row 0 uses its town median; rows 1-2 the training median psm of their flat type.
    assert predicted.tolist() == pytest.approx([350_000.0, 70 * 5500.0, 600_000.0])


def test_sample_params_is_reproducible_and_in_range() -> None:
    first = sample_params(np.random.default_rng(7))
    assert first == sample_params(np.random.default_rng(7))
    assert 0.02 <= first["learning_rate"] <= 0.15
    assert 31 <= first["num_leaves"] < 256
    assert 0.6 <= first["feature_fraction"] <= 1.0


def test_regression_metrics() -> None:
    m = regression_metrics(np.array([100.0, 200.0]), np.array([110.0, 180.0]))
    assert m["n"] == 2
    assert m["mae"] == pytest.approx(15.0)
    assert m["mape"] == pytest.approx(0.1)
    assert m["rmse"] == pytest.approx(np.sqrt((100 + 400) / 2))


def test_interval_coverage_includes_bounds() -> None:
    actual = np.array([1.0, 2.0, 3.0, 4.0])
    assert interval_coverage(actual, np.full(4, 2.0), np.full(4, 3.0)) == 0.5


def test_grouped_metrics_drops_small_groups() -> None:
    keys = pd.Series(["A"] * 30 + ["B"] * 29)
    values = np.full(59, 100.0)
    assert list(grouped_metrics(keys, values, values)) == ["A"]


@pytest.mark.parametrize(
    ("storey", "mid"), [(1, 2), (3, 2), (4, 5), (10, 11), (12, 11), (13, 14), (50, 50)]
)
def test_storey_mid_matches_three_storey_ranges(storey: int, mid: int) -> None:
    assert storey_mid(storey) == mid


def test_storey_mid_rejects_basement() -> None:
    with pytest.raises(ValueError):
        storey_mid(0)


def test_month_helpers() -> None:
    assert next_month(date(2026, 10, 1)) == date(2026, 11, 1)
    assert next_month(date(2026, 12, 1)) == date(2027, 1, 1)
    assert month_index(date(1990, 1, 1)) == 0
    assert month_index(date(2026, 11, 1)) == 36 * 12 + 10


def test_lease_features_match_staging_formula() -> None:
    # Lease from 1985, priced in 2026-11: (2026 - 1985) * 12 + 11 - 1 = 502 months.
    assert lease_features(1985, date(2026, 11, 1)) == {
        "remaining_lease_months": 99 * 12 - 502.0,
        "flat_age_years": round(502 / 12, 2),
    }
    assert lease_features(2030, date(2026, 11, 1))["remaining_lease_months"] == 99 * 12


def test_lease_features_carry_reported_lease_forward() -> None:
    # HDB reported 690 months at a sale in 2026-06; five months later 685 remain.
    features = lease_features(1985, date(2026, 11, 1), reported=(690, date(2026, 6, 1)))
    assert features["remaining_lease_months"] == 685.0
    assert features["flat_age_years"] == round(502 / 12, 2)


def test_relative_storey_is_capped_at_top_floor() -> None:
    assert relative_storey(11, 12) == round(11 / 12, 3)
    assert relative_storey(14, 12) == 1.0


def test_rounding_matches_postgres_numeric_round() -> None:
    # Postgres round(5::numeric / 16, 3) = 0.313; Python's round(5 / 16, 3) = 0.312.
    assert relative_storey(5, 16) == 0.313
    assert sql_round(11, 12, 2) == 0.92
    assert sql_round(1, 8, 2) == 0.13


def test_ordered_quantiles_uncrosses_rows() -> None:
    raw = {
        "p10": np.array([1.0, 5.0]),
        "p50": np.array([2.0, 4.0]),
        "p90": np.array([3.0, 3.0]),
    }
    ordered = ordered_quantiles(raw)
    assert list(ordered) == ["p10", "p50", "p90"]
    assert ordered["p10"].tolist() == [1.0, 3.0]
    assert ordered["p50"].tolist() == [2.0, 4.0]
    assert ordered["p90"].tolist() == [3.0, 5.0]


def _fake_model_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logs: dict) -> Path:
    outputs = {name: f"model_{name}.onnx" for name in logs}
    (tmp_path / "features.json").write_text(json.dumps({"outputs": outputs}), encoding="utf-8")
    by_file = {file: np.array([logs[name]]) for name, file in outputs.items()}
    monkeypatch.setattr(
        predict_module.export_onnx, "run_onnx", lambda path, x: by_file[Path(path).name]
    )
    return tmp_path


def test_predict_returns_ordered_quantiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = {"p10": np.log(500_000.0), "p50": np.log(480_000.0), "p90": np.log(600_000.0)}
    prices = predict(_fake_model_dir(tmp_path, monkeypatch, logs), pd.DataFrame())
    assert list(prices) == ["p10", "p50", "p90"]
    assert prices["p10"] == pytest.approx(480_000.0)
    assert prices["p50"] == pytest.approx(500_000.0)


def test_predict_rejects_non_finite_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = {"p10": 13.0, "p50": np.nan, "p90": 13.5}
    with pytest.raises(ValueError, match="non-finite"):
        predict(_fake_model_dir(tmp_path, monkeypatch, logs), pd.DataFrame())


@pytest.mark.parametrize("area", ["nan", "inf", "-inf", "0", "-5"])
def test_cli_rejects_bad_floor_area(area: str) -> None:
    argv = ["predict", "--block", "1", "--street", "S", "--flat-type", "4 ROOM", "--storey", "1"]
    with pytest.raises(SystemExit) as exc:
        cli.parse_args([*argv, "--area", area])
    assert exc.value.code == 2


def test_publish_keeps_active_model_when_swap_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = tmp_path / "2026-10"
    final.mkdir()
    (final / "model_p50.txt").write_text("active", encoding="utf-8")
    staging = tmp_path / ".2026-10.tmp"
    staging.mkdir()
    rename = Path.rename

    def failing_rename(self: Path, target: Path) -> Path:
        if self == staging:
            raise OSError("disk full")
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", failing_rename)
    with pytest.raises(OSError, match="disk full"):
        publish(staging, final)
    assert (final / "model_p50.txt").read_text(encoding="utf-8") == "active"
    assert not (tmp_path / ".2026-10.old").exists()


def test_publish_replaces_model_directory(tmp_path: Path) -> None:
    final = tmp_path / "2026-10"
    final.mkdir()
    (final / "stale.txt").write_text("old", encoding="utf-8")
    staging = tmp_path / ".2026-10.tmp"
    staging.mkdir()
    (staging / "model_p50.txt").write_text("new", encoding="utf-8")
    assert publish(staging, final) == final
    assert sorted(p.name for p in final.iterdir()) == ["model_p50.txt"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-10"]


def test_address_key_is_normalised() -> None:
    flat = Flat(" 406a ", "ang  mo kio ave 10", "4 room", 92.0, 10)
    assert flat.address_key == "406A ANG MO KIO AVE 10"
    assert normalise(" 4  room ") == "4 ROOM"


def test_encode_one_warns_on_unseen_category(caplog: pytest.LogCaptureFixture) -> None:
    maps = {col: {"A": 0} for col in CATEGORICAL}
    features = {"town": "A", "flat_type": "B", "floor_area_sqm": 92, "storey_mid": None}
    with caplog.at_level(logging.WARNING):
        x = encode_one(features, maps)
    assert "flat_type 'B' was not in the training data" in caplog.text
    assert x["town"].iloc[0] == 0.0
    assert np.isnan(x["flat_type"].iloc[0])
    assert x["floor_area_sqm"].iloc[0] == 92.0
    assert np.isnan(x["storey_mid"].iloc[0])


def test_symmetric_offsets_reach_target_coverage() -> None:
    rng = np.random.default_rng(0)
    y = rng.normal(size=20_000)
    # Raw bounds too narrow and shifted down, like a drifting market.
    p10, p90 = np.full_like(y, -0.8), np.full_like(y, 0.4)
    offsets = calibrate.symmetric_offsets(calibrate.interval_scores(y, p10, p90))
    assert offsets["p10"] == -offsets["p90"] and offsets["p90"] > 0
    inside = (y >= p10 + offsets["p10"]) & (y <= p90 + offsets["p90"])
    assert 0.8 <= inside.mean() <= 0.805


def test_interval_scores_sign() -> None:
    scores = calibrate.interval_scores(np.array([0.0, 2.0, -2.0]), np.full(3, -1.0), np.ones(3))
    assert scores.tolist() == [-1.0, 1.0, 1.0]


def test_conformal_quantile_uses_finite_sample_level() -> None:
    scores = np.arange(1.0, 10.0)  # n = 9: ceil(10 * 0.9) / 9 = 1 -> the maximum
    assert calibrate.conformal_quantile(scores, 0.1) == 9.0


def test_combine_stacks_splits() -> None:
    a = Split("a", pd.DataFrame({"f": [1.0]}), np.array([1.0]), pd.DataFrame({"r": [1]}))
    b = Split("b", pd.DataFrame({"f": [2.0]}), np.array([2.0]), pd.DataFrame({"r": [2]}))
    both = combine("ab", a, b)
    assert both.x["f"].tolist() == [1.0, 2.0]
    assert both.y.tolist() == [1.0, 2.0]
    assert both.rows.index.tolist() == [0, 1]
