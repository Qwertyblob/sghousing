"""Phase 5 data quality checks on live data; writes `reports/data_quality.md`.

Run with `make check` after `make features`. The report is written before any assertion, so a
failing check still leaves a report explaining it. A missing or empty input table fails the
run rather than skipping it.
"""

from __future__ import annotations

import pytest

from config.settings import get_settings
from pipeline.common.db import get_engine
from pipeline.quality.checks import (
    CHECKS,
    KNOWN_FLAT_TYPES,
    CheckResult,
    MissingInputError,
    QualityReport,
    run_checks,
    write_report,
)

pytestmark = [pytest.mark.db, pytest.mark.quality]

REPORT_NAME = "data_quality.md"
CHECK_NAMES = (
    "year_counts",
    "required_nulls",
    "ranges",
    "known_values",
    "geocode_coverage",
    "feature_coverage",
)


@pytest.fixture(scope="module")
def report() -> QualityReport:
    get_settings.cache_clear()
    engine = get_engine()
    try:
        result = run_checks(engine)
    except MissingInputError as exc:
        pytest.fail(str(exc))
    finally:
        engine.dispose()
    write_report(result, get_settings().reports_dir / REPORT_NAME)
    return result


def by_name(report: QualityReport) -> dict[str, CheckResult]:
    return {r.name: r for r in report.results}


def test_report_is_written_with_every_check(report: QualityReport) -> None:
    body = (get_settings().reports_dir / REPORT_NAME).read_text(encoding="utf-8")
    assert len(report.results) == len(CHECKS)
    for result in report.results:
        assert f"## {result.title}" in body


@pytest.mark.parametrize("name", CHECK_NAMES)
def test_check_passes(report: QualityReport, name: str) -> None:
    result = by_name(report)[name]
    assert result.status == "pass", result.summary


def test_price_outliers_are_flagged_not_failed(report: QualityReport) -> None:
    result = by_name(report)["price_outliers"]
    assert result.status in {"flag", "pass"}
    by_type = result.tables[0]
    assert {row[0] for row in by_type.rows} <= KNOWN_FLAT_TYPES
