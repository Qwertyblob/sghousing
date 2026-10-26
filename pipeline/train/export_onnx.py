"""ONNX export of the quantile models, `features.json`, and the parity cases.

Each LightGBM model becomes `model_{name}.onnx` with one float32 input named "input" of shape
[n, len(FEATURES)] in FEATURES order, and one output: log price. Categorical columns carry the
integer codes in `features.json`; NaN means missing or unseen. Saved models have their split
thresholds rounded to float32 first (`float32_thresholds`), and `check_onnx` compares every
exported model with LightGBM before anything else uses it.
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import onnx
import onnxruntime as ort
import pandas as pd
from onnxmltools import convert_lightgbm
from onnxmltools.convert.common.data_types import FloatTensorType

from pipeline.train.dataset import CATEGORICAL, FEATURES, CategoryMaps

logger = logging.getLogger(__name__)

INPUT_NAME = "input"
TARGET_OPSET = 15
# Largest |ONNX - LightGBM| allowed on log price (float32 rounding is about 1e-6).
MAX_EXPORT_DIFF = 1e-4


def _float32_floor(value: float) -> float:
    """The largest float32 value <= `value`."""
    rounded = np.float32(value)
    if float(rounded) > value:
        rounded = np.nextafter(rounded, np.float32(-np.inf))
    return float(rounded)


def float32_thresholds(model_text: str) -> str:
    """A LightGBM model (text format) with every split threshold rounded down to float32.

    LightGBM keeps thresholds as doubles; ONNX TreeEnsemble stores them as float32. A double
    threshold t rounds to a float32 that can land on the other side of an input value, so
    that row takes the other branch (up to 7e-3 in log price on the real models). For float32
    inputs, `x <= t` and `x <= largest float32 <= t` are the same test, so this changes no
    LightGBM prediction on float32 data and makes ONNX compare exactly. Categorical splits
    store small integer indices here, which float32 represents exactly. The `tree_sizes`
    header (byte offsets for parallel loading) no longer matches and is dropped; LightGBM
    then parses trees sequentially.
    """

    def align(match: re.Match[str]) -> str:
        values = (repr(_float32_floor(float(v))) for v in match.group(1).split(" "))
        return "threshold=" + " ".join(values)

    text = re.sub(r"^tree_sizes=.*\n", "", model_text, flags=re.MULTILINE)
    return re.sub(r"^threshold=(.*)$", align, text, flags=re.MULTILINE)


def onnx_path(directory: Path, name: str) -> Path:
    return directory / f"model_{name}.onnx"


def export_model(booster: lgb.Booster, path: Path) -> Path:
    """Convert one booster to ONNX and save it."""
    model = convert_lightgbm(
        booster,
        initial_types=[(INPUT_NAME, FloatTensorType([None, len(FEATURES)]))],
        target_opset=TARGET_OPSET,
        zipmap=False,
    )
    _drop_unit_hitrates(model)
    path.write_bytes(model.SerializeToString())
    return path


def _drop_unit_hitrates(model: onnx.ModelProto) -> None:
    """Remove `nodes_hitrates` from tree ensembles when every value is 1.0.

    The attribute is optional, only a hint, and ignored by onnxruntime; the converter writes
    it for every node, about 13% of the file (3.8 MB of the p10 model), which the site has to
    download. Predictions are unchanged (`check_onnx` verifies every export).
    """
    for node in model.graph.node:
        rates = [a for a in node.attribute if a.name == "nodes_hitrates"]
        if rates and all(v == 1.0 for v in rates[0].floats):
            node.attribute.remove(rates[0])


def session(path: Path) -> ort.InferenceSession:
    return ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])


def run_onnx(path: Path, x: pd.DataFrame | np.ndarray) -> np.ndarray:
    """Log-price predictions from one exported model."""
    values = np.asarray(x, dtype=np.float32)
    return session(path).run(None, {INPUT_NAME: values})[0].ravel()


def check_onnx(boosters: dict[str, lgb.Booster], directory: Path, x: pd.DataFrame) -> float:
    """Largest absolute difference between ONNX and LightGBM over `x`; raises if too large
    or if either returns a non-finite value (NaN would otherwise compare as "not too large")."""
    worst = 0.0
    for name, booster in boosters.items():
        exported = run_onnx(onnx_path(directory, name), x)
        expected = booster.predict(x)
        if not (np.isfinite(exported).all() and np.isfinite(expected).all()):
            raise RuntimeError(f"{name}: non-finite prediction from ONNX or LightGBM")
        worst = max(worst, float(np.abs(exported - expected).max()))
    if worst > MAX_EXPORT_DIFF:
        raise RuntimeError(f"ONNX export differs from LightGBM by {worst:.2e} in log price")
    return worst


def write_features_json(
    path: Path,
    maps: CategoryMaps,
    version: str,
    window: dict[str, str],
    outputs: dict[str, str],
    trained_on: date,
) -> Path:
    """Everything a client needs to build the model input."""
    spec = {
        "model_version": version,
        "trained_on": trained_on.isoformat(),
        "data_window": window,
        "target": "log(resale_price)",
        "input": {"name": INPUT_NAME, "dtype": "float32", "missing": "NaN"},
        "features": [
            {"name": f, "kind": "categorical" if f in CATEGORICAL else "numeric"} for f in FEATURES
        ],
        "categories": maps,
        "outputs": outputs,
    }
    path.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def export_models(
    boosters: dict[str, lgb.Booster],
    directory: Path,
    maps: CategoryMaps,
    version: str,
    window: dict[str, str],
    check_rows: pd.DataFrame,
    trained_on: date,
) -> Path:
    """Write every `model_{name}.onnx` and `features.json`, verified against LightGBM."""
    outputs = {
        name: export_model(b, onnx_path(directory, name)).name for name, b in boosters.items()
    }
    worst = check_onnx(boosters, directory, check_rows)
    logger.info(
        "Exported %d ONNX models; largest difference from LightGBM %.1e", len(outputs), worst
    )
    return write_features_json(
        directory / "features.json", maps, version, window, outputs, trained_on
    )


def _json_number(value: float) -> float | None:
    return None if math.isnan(value) else float(value)


def write_parity_cases(directory: Path, x: pd.DataFrame, path: Path) -> Path:
    """Run `x` through every exported model with Python onnxruntime and save inputs and
    outputs (log price) for the Node parity check. NaN inputs are written as null."""
    spec = json.loads((directory / "features.json").read_text(encoding="utf-8"))
    cases: dict[str, Any] = {
        "model_version": spec["model_version"],
        "model_dir": str(directory.resolve()),
        "input_name": INPUT_NAME,
        "features": list(FEATURES),
        "inputs": [[_json_number(v) for v in row] for row in x.to_numpy(dtype=np.float64)],
        "outputs": {
            name: [float(v) for v in run_onnx(directory / file, x)]
            for name, file in spec["outputs"].items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cases) + "\n", encoding="utf-8")
    return path
