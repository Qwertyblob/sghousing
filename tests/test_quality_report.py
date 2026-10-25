"""Unit tests for the data quality report renderer (no database)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from pipeline.quality.checks import CheckResult, QualityReport, Table, render_report, write_report


def make_report() -> QualityReport:
    return QualityReport(
        sales=1234,
        first_month=date(1990, 1, 1),
        last_month=date(2026, 10, 1),
        results=(
            CheckResult(
                "ranges",
                "Valid ranges",
                "fail",
                "2 values outside their valid range.",
                (Table(("Rule", "Violations"), (("a | b", "2"),)),),
            ),
            CheckResult(
                "price_outliers",
                "Price outliers",
                "flag",
                "3 sales flagged.",
                (Table(("Month",), (("2020-01",),), title="The 25 most extreme"),),
            ),
        ),
    )


def test_render_report_lists_every_check_with_status() -> None:
    body = render_report(make_report())
    assert "1,234 sales from 1990-01 to 2026-10" in body
    assert "| Valid ranges | **FAIL** | 2 values outside their valid range. |" in body
    assert "| Price outliers | FLAGGED | 3 sales flagged. |" in body
    assert "## Valid ranges" in body
    assert "### The 25 most extreme" in body


def test_render_report_escapes_pipes_in_cells() -> None:
    body = render_report(make_report())
    assert "| a \\| b | 2 |" in body


def test_write_report_matches_render(tmp_path: Path) -> None:
    path = write_report(make_report(), tmp_path / "reports" / "data_quality.md")
    assert path.read_text(encoding="utf-8") == render_report(make_report())
    assert path.read_text(encoding="utf-8").endswith("\n")
