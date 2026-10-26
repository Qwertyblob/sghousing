"""ONNX parity between Python and Node onnxruntime (`make evaluate`).

`make evaluate` writes `tests/parity/cases.json` (200 test rows with the Python onnxruntime
outputs) and installs onnxruntime-node here; this test runs `parity.mjs`, which asserts the
largest relative price difference is below 1e-4 for every quantile model.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.parity

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases.json"


def test_node_onnxruntime_matches_python() -> None:
    if not CASES.exists():
        pytest.fail(f"{CASES} not found (run `make evaluate`)")
    if not (HERE / "node_modules" / "onnxruntime-node").is_dir():
        pytest.fail("onnxruntime-node not installed (run `npm ci` in tests/parity)")
    node = shutil.which("node")
    assert node, "node not found on PATH"
    proc = subprocess.run(
        [node, str(HERE / "parity.mjs"), str(CASES)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["rows"] == len(json.loads(CASES.read_text(encoding="utf-8"))["inputs"])
    assert set(report["max_relative_diff"]) == {"p10", "p50", "p90"}
    assert report["passed"]
