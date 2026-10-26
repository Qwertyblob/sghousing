"""Data quality checks on the resale data, run against the live database.

`run_checks` returns one `CheckResult` per check and `render_report` turns them into
`reports/data_quality.md`. `tests/test_data_quality.py` (`make check`) writes the report and
fails on any check whose status is `fail`. Price outliers are flagged in the report, never
removed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from sqlalchemy import Connection, Engine, text

from pipeline.geocode.run import DONE_STATUSES, EXACT_STATUSES

logger = logging.getLogger(__name__)

Status = Literal["pass", "fail", "flag"]


class MissingInputError(RuntimeError):
    """A table the checks read is absent, or there are no sales to check."""


@dataclass(frozen=True)
class Sources:
    """The tables the checks read. Tests point these at small fixture copies."""

    resale: str = "staging.hdb_resale"
    geocode_cache: str = "staging.geocode_cache"
    features: str = "marts.resale_features"


# Columns every later step relies on; a NULL in any of them fails the check.
# Keys are `Sources` fields.
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "resale": (
        "txn_id",
        "month",
        "date_basis",
        "town",
        "flat_type",
        "flat_model",
        "block",
        "street_name",
        "address_key",
        "storey_lo",
        "storey_hi",
        "storey_mid",
        "floor_area_sqm",
        "lease_commence_year",
        "remaining_lease_months",
        "remaining_lease_basis",
        "flat_age_years",
        "resale_price",
        "price_psm",
        "source_dataset_id",
        "source_row_number",
    ),
    "features": (
        "month_index",
        "block_generation",
        "block_max_floor",
        "block_year_completed",
        "rel_storey",
    ),
}

KNOWN_FLAT_TYPES = frozenset(
    {"1 ROOM", "2 ROOM", "3 ROOM", "4 ROOM", "5 ROOM", "EXECUTIVE", "MULTI-GENERATION"}
)
# HDB's 26 towns, plus Lim Chu Kang, whose flats were resold until 1998.
KNOWN_TOWNS = frozenset(
    {
        "ANG MO KIO",
        "BEDOK",
        "BISHAN",
        "BUKIT BATOK",
        "BUKIT MERAH",
        "BUKIT PANJANG",
        "BUKIT TIMAH",
        "CENTRAL AREA",
        "CHOA CHU KANG",
        "CLEMENTI",
        "GEYLANG",
        "HOUGANG",
        "JURONG EAST",
        "JURONG WEST",
        "KALLANG/WHAMPOA",
        "LIM CHU KANG",
        "MARINE PARADE",
        "PASIR RIS",
        "PUNGGOL",
        "QUEENSTOWN",
        "SEMBAWANG",
        "SENGKANG",
        "SERANGOON",
        "TAMPINES",
        "TOA PAYOH",
        "WOODLANDS",
        "YISHUN",
    }
)
KNOWN_VALUES = {"flat_type": KNOWN_FLAT_TYPES, "town": KNOWN_TOWNS}


@dataclass(frozen=True)
class RangeRule:
    """A valid range for one column, with an optional set of known genuine exceptions."""

    label: str
    valid_sql: str
    allowed_sql: str | None = None
    allowed_reason: str = ""


# HDB terrace houses in Whampoa include their land; the largest sold so far is 366.7 sqm.
TERRACE_MAX_SQM = 400
RANGE_RULES = (
    RangeRule(
        "floor_area_sqm between 25 and 300",
        "floor_area_sqm BETWEEN 25 AND 300",
        allowed_sql=(
            "flat_model = 'Terrace' AND town = 'KALLANG/WHAMPOA' "
            f"AND floor_area_sqm > 300 AND floor_area_sqm <= {TERRACE_MAX_SQM}"
        ),
        allowed_reason=(
            "Whampoa terrace houses include their land: Terrace model in KALLANG/WHAMPOA, "
            f"over 300 and up to {TERRACE_MAX_SQM} sqm"
        ),
    ),
    RangeRule("resale_price above 0", "resale_price > 0"),
    RangeRule("storey_mid between 1 and 60", "storey_mid BETWEEN 1 AND 60"),
)

# A year (scaled to 12 months if partial) below this share of the previous year fails.
MIN_YEAR_RATIO = 0.5
# Phase 3 acceptance: share of distinct addresses with a location.
MIN_GEOCODE_COVERAGE = 0.995
# Every sale from this year on must be located (model training data starts later).
LOCATED_FROM_YEAR = 2015
# Location features: NULL only for sales on blocks that could not be located.
LOCATION_FEATURES = (
    "lat",
    "lon",
    "planning_area",
    "region",
    "dist_mrt_m",
    "nearest_mrt_name",
    "n_mrt_exits_500m",
    "n_primary_schools_1km",
    "dist_nearest_primary_m",
    "dist_hawker_m",
    "dist_park_m",
    "dist_cbd_m",
)
# Block facts and derived columns: present for every sale.
SALE_FEATURES = ("block_max_floor", "block_year_completed", "rel_storey", "month_index")
# Price per sqm further than this many scaled median absolute deviations from the median of
# its town, flat type and year is flagged. Scaling the MAD by 1.4826 makes it estimate the
# standard deviation for normal data, so the limit is about 4 robust standard deviations.
MAD_LIMIT = 4.0
MAD_SCALE = 1.4826
TOP_OUTLIERS = 25


@dataclass(frozen=True)
class Table:
    """A markdown table in a check's report section."""

    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    title: str | None = None


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one check."""

    name: str
    title: str
    status: Status
    summary: str
    tables: tuple[Table, ...] = ()


@dataclass(frozen=True)
class QualityReport:
    """All check results plus what data they ran on."""

    sales: int
    first_month: date
    last_month: date
    results: tuple[CheckResult, ...]


def _pct(part: int, whole: int) -> str:
    return f"{part / whole:.2%}" if whole else "-"


def check_year_counts(conn: Connection, src: Sources) -> CheckResult:
    """Fail on any month without sales, or a year with under half the previous year's sales.

    Months are checked first: only with every month present is scaling a partial first or
    last year to 12 months a fair comparison.
    """
    missing = (
        conn.execute(
            text(f"""
            SELECT to_char(m, 'YYYY-MM')
            FROM generate_series(
                (SELECT min(month) FROM {src.resale}),
                (SELECT max(month) FROM {src.resale}),
                interval '1 month'
            ) AS m
            WHERE NOT EXISTS (SELECT 1 FROM {src.resale} AS r WHERE r.month = m::date)
            ORDER BY 1
            """)
        )
        .scalars()
        .all()
    )
    rows = conn.execute(
        text(f"""
        WITH years AS (
            SELECT generate_series(
                min(extract(year FROM month))::integer, max(extract(year FROM month))::integer
            ) AS year
            FROM {src.resale}
        )
        SELECT y.year, count(r.month) AS sales, count(DISTINCT r.month) AS months
        FROM years AS y
        LEFT JOIN {src.resale} AS r ON extract(year FROM r.month) = y.year
        GROUP BY y.year
        ORDER BY y.year
        """)
    ).all()
    table: list[tuple[str, ...]] = []
    flagged: list[int] = []
    previous: float | None = None
    for year, sales, months in rows:
        per_12 = sales * 12 / months if months else 0.0
        ratio = per_12 / previous if previous else None
        low = months == 0 or (ratio is not None and ratio < MIN_YEAR_RATIO)
        if low:
            flagged.append(year)
        table.append(
            (
                str(year),
                f"{sales:,}",
                str(months),
                f"{per_12:,.0f}",
                "-" if ratio is None else f"{ratio:.0%}",
                "**below half**" if low else "",
            )
        )
        previous = per_12
    parts = [
        f"Months with no sales: {', '.join(missing)}."
        if missing
        else f"Every month from {rows[0][0] if rows else '-'} on has sales.",
        f"Below half the previous year: {', '.join(map(str, flagged))}."
        if flagged
        else f"No year below half the previous year across {len(rows)} years.",
        "The partial first and last years are scaled to 12 months before comparing.",
    ]
    return CheckResult(
        "year_counts",
        "Row counts per year",
        "fail" if flagged or missing else "pass",
        " ".join(parts),
        (
            Table(
                ("Year", "Sales", "Months", "Sales per 12 months", "vs previous year", "Flag"),
                tuple(table),
            ),
        ),
    )


def check_required_nulls(conn: Connection, src: Sources) -> CheckResult:
    """Count NULLs in every required column."""
    table: list[tuple[str, ...]] = []
    bad = 0
    for source, columns in REQUIRED_COLUMNS.items():
        relation = getattr(src, source)
        select = ", ".join(f'count(*) FILTER (WHERE "{c}" IS NULL)' for c in columns)
        counts = conn.execute(text(f"SELECT {select} FROM {relation}")).one()
        for column, nulls in zip(columns, counts, strict=True):
            bad += bool(nulls)
            table.append((relation, column, f"{nulls:,}"))
    total = sum(len(c) for c in REQUIRED_COLUMNS.values())
    return CheckResult(
        "required_nulls",
        "Nulls in required columns",
        "fail" if bad else "pass",
        f"{bad} of {total} required columns have NULLs.",
        (Table(("Table", "Column", "NULLs"), tuple(table)),),
    )


def check_ranges(conn: Connection, src: Sources) -> CheckResult:
    """Count values outside each valid range; known genuine exceptions are listed, not failed."""
    table: list[tuple[str, ...]] = []
    tables: list[Table] = []
    failures = 0
    for rule in RANGE_RULES:
        outside = f"NOT coalesce({rule.valid_sql}, false)"
        allowed = rule.allowed_sql or "false"
        violations, excepted = conn.execute(
            text(
                f"SELECT count(*) FILTER (WHERE NOT ({allowed})), "
                f"count(*) FILTER (WHERE {allowed}) "
                f"FROM {src.resale} WHERE {outside}"
            )
        ).one()
        failures += violations
        table.append((f"`{rule.label}`", f"{violations:,}", f"{excepted:,}", rule.allowed_reason))
        if excepted:
            rows = conn.execute(
                text(
                    "SELECT to_char(month, 'YYYY-MM'), town, flat_type, flat_model, address_key, "
                    f"floor_area_sqm, resale_price FROM {src.resale} "
                    f"WHERE {outside} AND {allowed} ORDER BY month, txn_id"
                )
            ).all()
            tables.append(
                Table(
                    ("Month", "Town", "Flat type", "Model", "Block", "Area (sqm)", "Price"),
                    tuple(
                        (m, t, ft, fm, a, f"{float(area):g}", f"{price:,.0f}")
                        for m, t, ft, fm, a, area, price in rows
                    ),
                    title=f"Allowed exceptions to `{rule.label}`",
                )
            )
    return CheckResult(
        "ranges",
        "Valid ranges",
        "fail" if failures else "pass",
        f"{failures:,} values outside their valid range.",
        (Table(("Rule", "Violations", "Allowed exceptions", "Why allowed"), tuple(table)), *tables),
    )


def check_known_values(conn: Connection, src: Sources) -> CheckResult:
    """Every flat type and town must come from a known list."""
    table: list[tuple[str, ...]] = []
    unknown_total = 0
    for column, known in KNOWN_VALUES.items():
        found = conn.execute(
            text(f"SELECT {column}, count(*) FROM {src.resale} GROUP BY 1 ORDER BY 1")
        ).all()
        unknown = [(value, n) for value, n in found if value not in known]
        unknown_total += len(unknown)
        table.append(
            (
                f"`{column}`",
                str(len(found)),
                str(len(known)),
                ", ".join(f"{v!r} ({n:,})" for v, n in unknown) or "none",
            )
        )
    return CheckResult(
        "known_values",
        "Known flat types and towns",
        "fail" if unknown_total else "pass",
        f"{unknown_total} unknown values.",
        (Table(("Column", "Values seen", "Known values", "Unknown values"), tuple(table)),),
    )


def check_geocode_coverage(conn: Connection, src: Sources) -> CheckResult:
    """Share of distinct addresses with a location, and every recent sale located."""
    rows = conn.execute(
        text(f"""
        SELECT coalesce(c.status, 'not attempted') AS status,
               count(DISTINCT r.address_key) AS blocks,
               count(*) AS sales,
               count(*) FILTER (WHERE r.month >= make_date(:year, 1, 1)) AS recent
        FROM {src.resale} AS r
        LEFT JOIN {src.geocode_cache} AS c USING (address_key)
        GROUP BY 1
        ORDER BY 2 DESC, 1
        """),
        {"year": LOCATED_FROM_YEAR},
    ).all()
    blocks = sum(r.blocks for r in rows)
    located = sum(r.blocks for r in rows if r.status in DONE_STATUSES)
    exact = sum(r.blocks for r in rows if r.status in EXACT_STATUSES)
    recent_missing = sum(r.recent for r in rows if r.status not in DONE_STATUSES)
    coverage = located / blocks if blocks else 0.0
    passed = coverage >= MIN_GEOCODE_COVERAGE and recent_missing == 0
    summary = (
        f"{_pct(located, blocks)} of {blocks:,} addresses located "
        f"({_pct(exact, blocks)} exactly; `approx` is the street centroid), "
        f"against a minimum of {MIN_GEOCODE_COVERAGE:.1%}. "
        f"{recent_missing:,} sales since {LOCATED_FROM_YEAR} are on unlocated addresses."
    )
    return CheckResult(
        "geocode_coverage",
        "Geocode coverage",
        "pass" if passed else "fail",
        summary,
        (
            Table(
                ("Status", "Addresses", "Sales", f"Sales since {LOCATED_FROM_YEAR}"),
                tuple((r.status, f"{r.blocks:,}", f"{r.sales:,}", f"{r.recent:,}") for r in rows),
            ),
        ),
    )


def check_feature_coverage(conn: Connection, src: Sources) -> CheckResult:
    """Check every transaction: in the feature mart, and each feature filled exactly when due.

    The mart must hold the same `txn_id`s as staging. A sale has location features exactly
    when its address is located in the geocode cache; block facts and derived columns are
    filled for every sale.
    """
    missing_txns, extra_txns = conn.execute(
        text(f"""
        SELECT
            (SELECT count(*) FROM {src.resale} AS r
             WHERE NOT EXISTS (SELECT 1 FROM {src.features} AS f WHERE f.txn_id = r.txn_id)),
            (SELECT count(*) FROM {src.features} AS f
             WHERE NOT EXISTS (SELECT 1 FROM {src.resale} AS r WHERE r.txn_id = f.txn_id))
        """)
    ).one()
    location = ", ".join(
        f'count(*) FILTER (WHERE f.geocode_status IS NOT NULL AND f."{c}" IS NULL), '
        f'count(*) FILTER (WHERE f.geocode_status IS NULL AND f."{c}" IS NOT NULL)'
        for c in LOCATION_FEATURES
    )
    sale = ", ".join(f'count(*) FILTER (WHERE f."{c}" IS NULL)' for c in SALE_FEATURES)
    sales, located, wrong_status, *counts = conn.execute(
        text(f"""
        SELECT count(*),
               count(f.geocode_status),
               -- The mart's status must be the cache's status for a located address, and
               -- NULL otherwise.
               count(*) FILTER (
                   WHERE f.geocode_status IS DISTINCT FROM
                         CASE WHEN c.status = ANY(:located) THEN c.status END
               ),
               {location},
               {sale}
        FROM {src.features} AS f
        LEFT JOIN {src.geocode_cache} AS c ON c.address_key = f.address_key
        """),
        {"located": list(DONE_STATUSES)},
    ).one()
    n_location = 2 * len(LOCATION_FEATURES)
    location_counts = [tuple(counts[i : i + 2]) for i in range(0, n_location, 2)]
    sale_counts = counts[n_location:]
    table = [
        (f"`{c}`", "located sales", f"{missing:,}", f"{unexpected:,}")
        for c, (missing, unexpected) in zip(LOCATION_FEATURES, location_counts, strict=True)
    ] + [
        (f"`{c}`", "every sale", f"{missing:,}", "-")
        for c, missing in zip(SALE_FEATURES, sale_counts, strict=True)
    ]
    problems: list[str] = []
    if missing_txns or extra_txns:
        problems.append(
            f"{missing_txns:,} staged sales missing from the mart and {extra_txns:,} mart rows "
            "not in staging"
        )
    if wrong_status:
        problems.append(f"{wrong_status:,} sales whose geocode_status disagrees with the cache")
    for c, (missing, unexpected) in zip(LOCATION_FEATURES, location_counts, strict=True):
        if missing:
            problems.append(f"`{c}` missing for {missing:,} located sales")
        if unexpected:
            problems.append(f"`{c}` set on {unexpected:,} unlocated sales")
    problems += [
        f"`{c}` missing for {missing:,} sales"
        for c, missing in zip(SALE_FEATURES, sale_counts, strict=True)
        if missing
    ]
    summary = (
        "Problems: " + "; ".join(problems) + "."
        if problems
        else (
            f"All {sales:,} sales are in the mart. Location features are filled for exactly the "
            f"{located:,} sales ({_pct(located, sales)}) on located addresses; block facts for "
            "every sale."
        )
    )
    return CheckResult(
        "feature_coverage",
        "Feature coverage",
        "fail" if problems else "pass",
        summary,
        (Table(("Feature", "Expected for", "Missing", "Unexpected"), tuple(table)),),
    )


def check_price_outliers(conn: Connection, src: Sources) -> CheckResult:
    """Flag sales whose price per sqm is far from their town, flat type and year median.

    The deviation is measured in scaled median absolute deviations (MAD x MAD_SCALE, a
    robust standard deviation). Groups whose MAD is 0 (a handful of tiny groups) cannot be
    scored and are skipped.
    """
    conn.execute(text("DROP TABLE IF EXISTS pg_temp.price_deviation"))
    conn.execute(
        text(f"""
        CREATE TEMP TABLE price_deviation AS
        WITH sales AS (
            SELECT txn_id, month, town, flat_type, address_key, floor_area_sqm, resale_price,
                   price_psm, extract(year FROM month)::integer AS year
            FROM {src.resale}
        ),
        medians AS (
            SELECT town, flat_type, year,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm) AS median_psm
            FROM sales
            GROUP BY town, flat_type, year
        ),
        deviations AS (
            SELECT s.*, m.median_psm, abs(s.price_psm - m.median_psm) AS deviation
            FROM sales AS s
            JOIN medians AS m USING (town, flat_type, year)
        ),
        mads AS (
            SELECT town, flat_type, year,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY deviation) AS mad
            FROM deviations
            GROUP BY town, flat_type, year
        )
        SELECT d.*, a.mad, d.deviation / nullif(a.mad * {MAD_SCALE}, 0) AS mads
        FROM deviations AS d
        JOIN mads AS a USING (town, flat_type, year)
        """)
    )
    limit = {"limit": MAD_LIMIT}
    total, flagged, high, unscored_sales, unscored_groups = conn.execute(
        text("""
        SELECT count(*),
               count(*) FILTER (WHERE mads > :limit),
               count(*) FILTER (WHERE mads > :limit AND price_psm > median_psm),
               count(*) FILTER (WHERE mad = 0),
               count(DISTINCT (town, flat_type, year)) FILTER (WHERE mad = 0)
        FROM price_deviation
        """),
        limit,
    ).one()
    by_type = conn.execute(
        text("""
        SELECT flat_type, count(*),
               count(*) FILTER (WHERE mads > :limit AND price_psm > median_psm),
               count(*) FILTER (WHERE mads > :limit AND price_psm < median_psm)
        FROM price_deviation
        GROUP BY flat_type
        ORDER BY flat_type
        """),
        limit,
    ).all()
    top = conn.execute(
        text("""
        SELECT to_char(month, 'YYYY-MM'), town, flat_type, address_key, floor_area_sqm,
               resale_price, price_psm, median_psm, mads
        FROM price_deviation
        WHERE mads > :limit
        ORDER BY mads DESC, txn_id
        LIMIT :top
        """),
        {**limit, "top": TOP_OUTLIERS},
    ).all()
    conn.execute(text("DROP TABLE pg_temp.price_deviation"))
    summary = (
        f"{flagged:,} sales ({_pct(flagged, total)}) are more than {MAD_LIMIT:g} scaled MADs "
        f"(robust standard deviations) from the median price per sqm of their town, flat type "
        f"and year: {high:,} above, "
        f"{flagged - high:,} below. They are flagged here and kept in the data. "
        f"{unscored_sales:,} sales in {unscored_groups:,} groups with a MAD of 0 are not scored."
    )
    return CheckResult(
        "price_outliers",
        "Price outliers",
        "flag" if flagged else "pass",
        summary,
        (
            Table(
                ("Flat type", "Sales", "Flagged high", "Flagged low", "Share flagged"),
                tuple(
                    (ft, f"{n:,}", f"{hi:,}", f"{lo:,}", _pct(hi + lo, n))
                    for ft, n, hi, lo in by_type
                ),
                title="By flat type",
            ),
            Table(
                (
                    "Month",
                    "Town",
                    "Flat type",
                    "Block",
                    "Area (sqm)",
                    "Price",
                    "Price per sqm",
                    "Group median",
                    "Scaled MADs",
                ),
                tuple(
                    (
                        m,
                        t,
                        ft,
                        a,
                        f"{float(area):g}",
                        f"{price:,.0f}",
                        f"{psm:,.0f}",
                        f"{med:,.0f}",
                        f"{mads:.1f}",
                    )
                    for m, t, ft, a, area, price, psm, med, mads in top
                ),
                title=f"The {TOP_OUTLIERS} most extreme",
            ),
        ),
    )


CHECKS = (
    check_year_counts,
    check_required_nulls,
    check_ranges,
    check_known_values,
    check_geocode_coverage,
    check_feature_coverage,
    check_price_outliers,
)


def require_inputs(conn: Connection, src: Sources) -> tuple[int, date, date]:
    """Return the sale count and month range; raise if a table is absent or has no sales."""
    tables = (src.resale, src.geocode_cache, src.features)
    absent = [
        t for t in tables if conn.execute(text("SELECT to_regclass(:t)"), {"t": t}).scalar() is None
    ]
    if absent:
        raise MissingInputError(
            f"missing tables: {', '.join(absent)} "
            "(run `make transform`, `make geocode` and `make features`)"
        )
    sales, first_month, last_month = conn.execute(
        text(f"SELECT count(*), min(month), max(month) FROM {src.resale}")
    ).one()
    if not sales:
        raise MissingInputError(
            f"{src.resale} has no sales (run `make ingest` and `make transform`)"
        )
    return sales, first_month, last_month


def run_checks(engine: Engine, src: Sources | None = None) -> QualityReport:
    """Run every check on one connection (read-only apart from a temporary table).

    Raises MissingInputError when an input table is absent or empty, so a missing pipeline
    step fails `make check` instead of producing an empty, passing report.
    """
    src = src or Sources()
    with engine.connect() as conn:
        sales, first_month, last_month = require_inputs(conn, src)
        results = []
        for check in CHECKS:
            result = check(conn, src)
            logger.info("%-18s %-4s %s", result.name, result.status.upper(), result.summary)
            results.append(result)
        conn.rollback()
    return QualityReport(sales, first_month, last_month, tuple(results))


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _md_table(table: Table) -> list[str]:
    lines = [f"### {table.title}", ""] if table.title else []
    lines.append("| " + " | ".join(table.headers) + " |")
    lines.append("|" + "---|" * len(table.headers))
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in table.rows]
    return [*lines, ""]


STATUS_LABELS: dict[Status, str] = {"pass": "PASS", "fail": "**FAIL**", "flag": "FLAGGED"}


def render_report(report: QualityReport) -> str:
    """Render the report as markdown. The output depends only on the data, not on run time."""
    lines = [
        "# Data quality report",
        "",
        "Generated by `make check` (`tests/test_data_quality.py`) from `staging.hdb_resale` and "
        f"`marts.resale_features`: {report.sales:,} sales from "
        f"{report.first_month:%Y-%m} to {report.last_month:%Y-%m}.",
        "",
        "| Check | Status | Result |",
        "|---|---|---|",
    ]
    lines += [
        f"| {r.title} | {STATUS_LABELS[r.status]} | {_cell(r.summary)} |" for r in report.results
    ]
    lines.append("")
    for r in report.results:
        lines += [f"## {r.title}", "", r.summary, ""]
        for table in r.tables:
            lines += _md_table(table)
    return "\n".join(lines).rstrip() + "\n"


def write_report(report: QualityReport, path: Path) -> Path:
    """Write the markdown report to `path`, creating its directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(report), encoding="utf-8")
    logger.info("Wrote %s", path)
    return path
