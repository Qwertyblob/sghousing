"""`python -m pipeline.export.site_data` (`make export`): data files for the website.

Writes `site/public/data/` (settings.site_data_dir): minified JSON with ISO dates ("2026-10"
for months, full ISO 8601 for timestamps). Large tables are columnar, {"columns": [...],
"rows": [[...], ...]}, which keeps the whole directory under MAX_TOTAL_BYTES next to the
models. Every file has a JSON Schema in `site/src/schemas/` (settings.site_schema_dir).

- meta.json: coverage, last refresh, model, towns, sources and licence
- overview.json: the national monthly series and the latest 12 months against the 12 before,
  by flat type; small, as the landing page loads it first
- town_month.json: marts.agg_town_month from TOWN_MONTH_FROM, with 'ALL' rollups
- planning_areas.geojson: simplified boundaries, marts.agg_planning_area and a yearly trend
- lease_decay.json: marts.lease_decay_curve (bins with at least MIN_CURVE_SALES sales)
- blocks.json: every located block with a resale since TRAIN_FROM, with its model inputs as
  of the month after the latest data, plus town-level rolling medians and flat models
- recent/{town-slug}.json: the last RECENT_MONTHS months of sales in each town
- model/: features.json of the latest model and its model_p10/p50/p90.onnx gzipped
  (`.onnx.gz`, about a fifth of the size; static hosts need not compress them, and the site
  decompresses them in the browser)

The directory is built next to the old one, validated (`validation_errors`: schemas, cross
references, sizes) and only then swapped in, so a failed or invalid export leaves the previous
files in place, and files for vanished towns never linger.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import shutil
import sys
import tempfile
from datetime import date, datetime
from decimal import Decimal
from itertools import islice
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import Connection, text

from config.settings import Settings, get_settings
from pipeline.common.db import get_engine
from pipeline.common.log import setup_logging
from pipeline.ingest.snapshots import read_current
from pipeline.train.dataset import TRAIN_FROM
from pipeline.train.predict import MIN_BLOCK_SALES, next_month
from pipeline.train.train_lgbm import latest_model_dir, publish

logger = logging.getLogger("pipeline.export")

TOWN_MONTH_FROM = date(2000, 1, 1)
RECENT_MONTHS = 24
MIN_CURVE_SALES = 30
MIN_TREND_SALES = 20
# ST_SimplifyPreserveTopology tolerances (SVY21 metres), tried in order until the GeoJSON
# fits in MAX_GEOJSON_BYTES.
SIMPLIFY_TOLERANCES_M = (5, 10, 20, 40, 80)
MAX_GEOJSON_BYTES = 1_000_000
MAX_TOTAL_BYTES = 60_000_000
COORD_DECIMALS = 5
LICENCE = {
    "name": "Singapore Open Data Licence version 1.0",
    "url": "https://data.gov.sg/open-data-licence",
}
GEOCODER = {"name": "OneMap search API", "url": "https://www.onemap.gov.sg/apidocs/"}
DISCLAIMER = "Indicative only: statistical estimates from past resales, not valuations."
# Dataset credits of a restored data snapshot (pipeline.snapshot), kept apart from
# raw.ingestion_log, which records only what this machine ingested.
RESTORED_SOURCES = "staging.restored_sources"
# Schema (site_schema_dir/{name}.schema.json) of every fixed file; recent/*.json use "recent".
SCHEMA_NAMES = {
    "meta.json": "meta",
    "overview.json": "overview",
    "town_month.json": "town_month",
    "planning_areas.geojson": "planning_areas",
    "lease_decay.json": "lease_decay",
    "blocks.json": "blocks",
    "model/features.json": "features",
}


class ExportError(RuntimeError):
    """The built site data failed validation and was not published."""


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def dumps(obj: Any) -> str:
    """Minified JSON; NaN/Infinity are rejected rather than written as invalid JSON."""
    return json.dumps(
        obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=_plain
    )


def write_json(path: Path, obj: Any) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dumps(obj).encode("utf-8")
    path.write_bytes(payload)
    return len(payload)


def town_slug(town: str) -> str:
    """File-name form of a town: "KALLANG/WHAMPOA" -> "kallang-whampoa"."""
    return re.sub(r"[^a-z0-9]+", "-", town.lower()).strip("-")


def display_label(block: str, street: str) -> str:
    """'Blk 406A Ang Mo Kio Ave 10' from HDB's upper-case block and street."""
    words = (w if w[0].isdigit() else w.capitalize() for w in street.split())
    return f"Blk {block} {' '.join(words)}"


def _rows(conn: Connection, sql: str, **params: Any) -> tuple[list[str], list[list[Any]]]:
    result = conn.execute(text(sql), params)
    return list(result.keys()), [list(r) for r in result]


def table(conn: Connection, sql: str, **params: Any) -> dict[str, Any]:
    columns, rows = _rows(conn, sql, **params)
    return {"columns": columns, "rows": rows}


def last_month(conn: Connection) -> date:
    return conn.execute(text("SELECT max(month) FROM staging.hdb_resale")).scalar_one()


def town_month(conn: Connection) -> dict[str, Any]:
    return {
        "from": f"{TOWN_MONTH_FROM:%Y-%m}",
        **table(
            conn,
            """SELECT town, flat_type, to_char(month, 'YYYY-MM') AS month, sales,
                      median_psm, median_price
               FROM marts.agg_town_month
               WHERE month >= :start
               ORDER BY town, flat_type, month""",
            start=TOWN_MONTH_FROM,
        ),
    }


def overview(conn: Connection, end: date) -> dict[str, Any]:
    """National monthly medians, and the latest 12 months (to `end`) against the 12 before,
    for all flats and by flat type."""
    national = table(
        conn,
        """SELECT to_char(month, 'YYYY-MM') AS month, sales, median_psm, median_price
           FROM marts.agg_town_month
           WHERE town = 'ALL' AND flat_type = 'ALL' AND month >= :start
           ORDER BY month""",
        start=TOWN_MONTH_FROM,
    )
    windows = table(
        conn,
        """WITH sales AS (
               SELECT flat_type, resale_price, price_psm,
                      month > :end - interval '12 months' AS latest
               FROM staging.hdb_resale
               WHERE month > :end - interval '24 months' AND month <= :end
           )
           SELECT coalesce(flat_type, 'ALL') AS flat_type,
                  count(*) FILTER (WHERE latest)::integer AS sales_12m,
                  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY resale_price)
                      FILTER (WHERE latest)::numeric) AS median_price_12m,
                  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm)
                      FILTER (WHERE latest)::numeric, 2) AS median_psm_12m,
                  count(*) FILTER (WHERE NOT latest)::integer AS sales_prev_12m,
                  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY resale_price)
                      FILTER (WHERE NOT latest)::numeric) AS median_price_prev_12m,
                  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm)
                      FILTER (WHERE NOT latest)::numeric, 2) AS median_psm_prev_12m
           FROM sales
           GROUP BY GROUPING SETS ((flat_type), ())
           ORDER BY GROUPING(flat_type) DESC, 1""",
        end=end,
    )
    first = conn.execute(
        text("SELECT to_char(:end - interval '11 months', 'YYYY-MM')"), {"end": end}
    ).scalar_one()
    return {
        "from": first,
        "to": f"{end:%Y-%m}",
        "national": {"from": f"{TOWN_MONTH_FROM:%Y-%m}", **national},
        "by_flat_type": windows,
    }


def lease_decay(conn: Connection) -> dict[str, Any]:
    window = conn.execute(
        text("""SELECT to_char(max(month) - interval '59 months', 'YYYY-MM'),
                       to_char(max(month), 'YYYY-MM')
                FROM marts.resale_features""")
    ).one()
    return {
        "from": window[0],
        "to": window[1],
        "min_sales": MIN_CURVE_SALES,
        **table(
            conn,
            """SELECT flat_type, lease_years, sales, median_psm, median_rel_psm
               FROM marts.lease_decay_curve
               WHERE sales >= :min_sales
               ORDER BY flat_type, lease_years""",
            min_sales=MIN_CURVE_SALES,
        ),
    }


def planning_areas(conn: Connection) -> dict[str, Any]:
    """Boundaries with statistics, simplified until the file fits in MAX_GEOJSON_BYTES."""
    trends: dict[str, list[list[Any]]] = {}
    for area, year, sales, psm in conn.execute(
        text("""SELECT planning_area, extract(year FROM month)::integer, count(*),
                       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm)::numeric, 2)
                FROM marts.resale_features
                WHERE planning_area IS NOT NULL AND month >= :start
                GROUP BY 1, 2
                HAVING count(*) >= :min_sales
                ORDER BY 1, 2"""),
        {"start": TRAIN_FROM, "min_sales": MIN_TREND_SALES},
    ):
        trends.setdefault(area, []).append([year, sales, psm])
    for tolerance in SIMPLIFY_TOLERANCES_M:
        features = []
        for row in conn.execute(
            text("""
            WITH reduced AS (
                -- Snapping to the output grid with ST_ReducePrecision keeps polygons valid;
                -- rounding in ST_AsGeoJSON alone can make them self-intersect.
                SELECT planning_area, ST_ReducePrecision(
                           ST_Transform(ST_SimplifyPreserveTopology(geom, :tolerance), 4326),
                           :grid) AS geom
                FROM staging.planning_areas
            )
            SELECT ST_AsGeoJSON(p.geom, :decimals) AS geometry,
                   ST_IsValid(p.geom) AND NOT ST_IsEmpty(p.geom) AS valid,
                   a.planning_area, a.region, a.sales_12m, a.median_psm_12m,
                   a.sales_prev_12m, a.median_psm_prev_12m, a.yoy_change
            FROM reduced AS p
            JOIN marts.agg_planning_area AS a USING (planning_area)
            ORDER BY a.planning_area"""),
            {"tolerance": tolerance, "grid": 10.0**-COORD_DECIMALS, "decimals": COORD_DECIMALS},
        ):
            props = dict(row._mapping)
            if not props.pop("valid"):
                raise RuntimeError(f"{props['planning_area']}: invalid geometry at {tolerance} m")
            geometry = json.loads(props.pop("geometry"))
            props["trend"] = trends.get(props["planning_area"], [])
            features.append({"type": "Feature", "geometry": geometry, "properties": props})
        collection = {
            "type": "FeatureCollection",
            "simplify_tolerance_m": tolerance,
            "trend_columns": ["year", "sales", "median_psm"],
            "features": features,
        }
        size = len(dumps(collection).encode("utf-8"))
        if size < MAX_GEOJSON_BYTES:
            logger.info("Planning areas simplified at %d m: %s bytes", tolerance, f"{size:,}")
            return collection
    raise RuntimeError(f"planning areas exceed {MAX_GEOJSON_BYTES:,} bytes at every tolerance")


# One row per located block with a resale since TRAIN_FROM, built as
# pipeline.train.predict.build_features builds one flat (tests/test_site_export.py checks
# that they agree): the latest sale gives town and lease; the building generation of that
# lease gives the block facts; rolling block medians cover the 24 months before :as_of.
_BLOCKS_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (address_key)
           address_key, block, street_name, town, lease_commence_year, month,
           remaining_lease_months, remaining_lease_basis
    FROM staging.hdb_resale
    ORDER BY address_key, month DESC, txn_id
),
flat_models AS (
    SELECT address_key, jsonb_object_agg(flat_type, flat_model ORDER BY flat_type) AS models
    FROM (
        SELECT address_key, flat_type, mode() WITHIN GROUP (ORDER BY flat_model) AS flat_model
        FROM staging.hdb_resale
        GROUP BY 1, 2
    ) AS m
    GROUP BY 1
),
block_rolling AS (
    SELECT address_key, count(*)::integer AS block_sales_24m,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm) AS block_psm_24m
    FROM staging.hdb_resale
    WHERE month >= :as_of - interval '24 months' AND month < :as_of
    GROUP BY 1
)
SELECT
    l.address_key,
    l.block,
    l.street_name,
    l.town,
    b.planning_area,
    b.region,
    -- Model inputs are not rounded: a rounded value can fall on the other side of a split.
    b.lat,
    b.lon,
    l.lease_commence_year,
    -- HDB's reported remaining lease at the latest sale carried forward to :as_of, as the
    -- CLI does; the staging formula where none was reported.
    least(greatest(
        CASE WHEN l.remaining_lease_basis = 'reported'
             THEN l.remaining_lease_months
                  - ((extract(year FROM :as_of) - extract(year FROM l.month)) * 12
                     + extract(month FROM :as_of) - extract(month FROM l.month))::integer
             ELSE 99 * 12 - greatest(0, (extract(year FROM :as_of)::integer
                                         - l.lease_commence_year) * 12
                                        + extract(month FROM :as_of)::integer - 1)
        END, 0), 99 * 12) AS remaining_lease_months,
    g.block_max_floor,
    g.block_year_completed,
    b.dist_mrt_m,
    b.nearest_mrt_name,
    b.n_mrt_exits_500m,
    b.n_primary_schools_1km,
    b.dist_nearest_primary_m,
    b.dist_hawker_m,
    b.dist_park_m,
    b.dist_cbd_m,
    CASE WHEN coalesce(r.block_sales_24m, 0) >= :min_block_sales
         THEN r.block_psm_24m END AS block_psm_24m,
    coalesce(r.block_sales_24m, 0) AS block_sales_24m,
    fm.models::text AS flat_models
FROM latest AS l
JOIN marts.block_features AS b USING (address_key)
JOIN marts.block_generations AS g
  ON g.address_key = l.address_key
 AND l.lease_commence_year BETWEEN g.lease_year_from AND g.lease_year_to
LEFT JOIN block_rolling AS r ON r.address_key = l.address_key
JOIN flat_models AS fm ON fm.address_key = l.address_key
WHERE l.month >= :train_from
ORDER BY l.town, l.street_name, l.block
"""


def blocks(conn: Connection, as_of: date) -> dict[str, Any]:
    """Model inputs per block as of `as_of`, plus town-level rolling medians and flat models."""
    columns, rows = _rows(
        conn, _BLOCKS_SQL, as_of=as_of, train_from=TRAIN_FROM, min_block_sales=MIN_BLOCK_SALES
    )
    models_at = columns.index("flat_models")
    label_at = columns.index("street_name") + 1
    for row in rows:
        row[models_at] = json.loads(row[models_at])
        row.insert(label_at, display_label(row[1], row[2]))
    columns.insert(label_at, "label")

    town_rolling: dict[str, dict[str, list[Any]]] = {}
    for town, flat_type, m3, m6, m12 in conn.execute(
        text("""
        SELECT town, flat_type,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm)
                   FILTER (WHERE month >= :as_of - interval '3 months'),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm)
                   FILTER (WHERE month >= :as_of - interval '6 months'),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm)
        FROM staging.hdb_resale
        WHERE month >= :as_of - interval '12 months' AND month < :as_of
        GROUP BY 1, 2
        ORDER BY 1, 2"""),
        {"as_of": as_of},
    ):
        town_rolling.setdefault(town, {})[flat_type] = [m3, m6, m12]
    town_flat_model: dict[str, dict[str, str]] = {}
    for town, flat_type, model in conn.execute(
        text("""SELECT town, flat_type, mode() WITHIN GROUP (ORDER BY flat_model)
                FROM staging.hdb_resale GROUP BY 1, 2 ORDER BY 1, 2""")
    ):
        town_flat_model.setdefault(town, {})[flat_type] = model
    return {
        "as_of": f"{as_of:%Y-%m}",
        "town_rolling_columns": ["town_psm_3m", "town_psm_6m", "town_psm_12m"],
        "town_rolling": town_rolling,
        "town_flat_model": town_flat_model,
        "columns": columns,
        "rows": rows,
    }


def recent_sales(conn: Connection, end: date) -> dict[str, dict[str, Any]]:
    """The last RECENT_MONTHS months of sales (to `end`), keyed by town."""
    columns, rows = _rows(
        conn,
        """SELECT town, to_char(month, 'YYYY-MM') AS month, block, street_name, flat_type,
                  flat_model, storey_lo, storey_hi, floor_area_sqm, lease_commence_year,
                  resale_price
           FROM staging.hdb_resale
           WHERE month > :end - make_interval(months => :months)
           ORDER BY town, month DESC, street_name, block, txn_id""",
        end=end,
        months=RECENT_MONTHS,
    )
    first = conn.execute(
        text("SELECT to_char(:end - make_interval(months => :months - 1), 'YYYY-MM')"),
        {"end": end, "months": RECENT_MONTHS},
    ).scalar_one()
    by_town: dict[str, dict[str, Any]] = {}
    for town, *values in rows:
        entry = by_town.setdefault(
            town,
            {"town": town, "from": first, "to": f"{end:%Y-%m}", "columns": columns[1:], "rows": []},
        )
        entry["rows"].append(values)
    return by_town


def _credit(
    source: str, dataset_id: str, name: str, last_updated: Any, retrieved: Any
) -> dict[str, Any]:
    return {
        "source": source,
        "dataset_id": dataset_id,
        "name": name,
        "url": f"https://data.gov.sg/datasets/{dataset_id}/view",
        "last_updated": last_updated,
        "retrieved": retrieved,
    }


def sources(conn: Connection, settings: Settings) -> list[dict[str, Any]]:
    """The datasets behind the staging data: a restored data snapshot's credits while its
    tables are in use (RESTORED_SOURCES, which `make transform` drops), otherwise every
    dataset in raw.ingestion_log, named from its downloaded snapshot's metadata."""
    if conn.execute(text("SELECT to_regclass(:t)"), {"t": RESTORED_SOURCES}).scalar_one():
        rows = conn.execute(
            text(f"""SELECT source, dataset_id, name, last_updated, retrieved
                     FROM {RESTORED_SOURCES} ORDER BY source, dataset_id""")
        )
        return [_credit(*row) for row in rows]
    out = []
    for source, dataset_id, downloaded_at in conn.execute(
        text("""SELECT source, dataset_id, max(downloaded_at)
                FROM raw.ingestion_log GROUP BY 1, 2 ORDER BY 1, 2""")
    ):
        current = read_current(
            settings.data_dir / "raw" / source / dataset_id, verify_checksum=False
        )
        metadata = current[0].metadata if current else None
        if metadata and metadata.name:
            name, last_updated = metadata.name, metadata.last_updated_at
        else:
            name, last_updated = dataset_id, None
        out.append(_credit(source, dataset_id, name, last_updated, downloaded_at))
    return out


def meta(
    conn: Connection, settings: Settings, model_dir: Path, towns: list[str], as_of: date
) -> dict[str, Any]:
    first, last, count = conn.execute(
        text("SELECT min(month), max(month), count(*) FROM staging.hdb_resale")
    ).one()
    credits = sources(conn, settings)
    refreshed = max((c["retrieved"] for c in credits), default=None)
    spec = json.loads((model_dir / "features.json").read_text(encoding="utf-8"))
    model: dict[str, Any] = {
        "version": spec["model_version"],
        "trained_on": spec["trained_on"],
        "data_window": spec["data_window"],
    }
    metrics_path = model_dir / "metrics.json"
    if metrics_path.exists():
        test = json.loads(metrics_path.read_text(encoding="utf-8"))["test"]
        p50 = test["lightgbm_p50"]
        model["test"] = {
            "sales": p50["n"],
            "mae": round(p50["mae"]),
            "mape": round(p50["mape"], 4),
            "r2": round(p50["r2"], 4),
            "interval_80_coverage": round(test["interval_80_coverage"], 4),
        }
    return {
        "coverage": {"from": f"{first:%Y-%m}", "to": f"{last:%Y-%m}", "transactions": count},
        "last_refresh": refreshed,
        "priced_for": f"{as_of:%Y-%m}",
        "model": model,
        "towns": [{"town": t, "file": f"recent/{town_slug(t)}.json"} for t in towns],
        "sources": credits,
        "licence": LICENCE,
        "geocoder": GEOCODER,
        "disclaimer": DISCLAIMER,
    }


def directory_bytes(directory: Path) -> int:
    return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())


def schema_name(relative: str) -> str | None:
    """The schema a data file follows, from its path in the data directory (None: no schema,
    as for the ONNX models)."""
    if re.fullmatch(r"recent/[^/]+\.json", relative):
        return "recent"
    return SCHEMA_NAMES.get(relative)


def validation_errors(directory: Path, schema_dir: Path) -> list[str]:
    """Everything wrong with an export: missing files, files failing their JSON Schema, town
    files and models that meta.json and features.json do not match, sizes over budget."""
    errors = [f"{name}: missing" for name in SCHEMA_NAMES if not (directory / name).exists()]
    validators: dict[str, Draft202012Validator] = {}
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        relative = path.relative_to(directory).as_posix()
        name = schema_name(relative)
        if name is None:
            continue
        if name not in validators:
            schema = json.loads((schema_dir / f"{name}.schema.json").read_text(encoding="utf-8"))
            validators[name] = Draft202012Validator(schema, format_checker=FormatChecker())
        found = validators[name].iter_errors(json.loads(path.read_text(encoding="utf-8")))
        errors += [
            f"{relative}: /{'/'.join(map(str, e.absolute_path))}: {e.message[:200]}"
            for e in islice(found, 5)
        ]
    if errors:
        return errors

    meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    listed = {t["file"] for t in meta["towns"]}
    on_disk = {p.relative_to(directory).as_posix() for p in (directory / "recent").glob("*")}
    errors += [f"{f}: listed in meta.json but missing" for f in sorted(listed - on_disk)]
    errors += [f"{f}: not listed in meta.json" for f in sorted(on_disk - listed)]
    spec = json.loads((directory / "model" / "features.json").read_text(encoding="utf-8"))
    if meta["model"]["version"] != spec["model_version"]:
        errors.append("meta.json: model version differs from model/features.json")
    for file in spec["outputs"].values():
        model = directory / "model" / f"{file}.gz"
        if not model.exists() or model.stat().st_size == 0:
            errors.append(f"model/{file}.gz: missing or empty")
    geojson = (directory / "planning_areas.geojson").stat().st_size
    if geojson >= MAX_GEOJSON_BYTES:
        errors.append(f"planning_areas.geojson: {geojson:,} bytes, over {MAX_GEOJSON_BYTES:,}")
    total = directory_bytes(directory)
    if total >= MAX_TOTAL_BYTES:
        errors.append(f"site data is {total:,} bytes, over {MAX_TOTAL_BYTES:,}")
    return errors


def build(directory: Path, settings: Settings) -> None:
    """Write every site data file into `directory`."""
    model_dir = latest_model_dir(settings)
    engine = get_engine()
    try:
        with engine.connect() as conn:
            end = last_month(conn)
            as_of = next_month(end)
            write_json(directory / "overview.json", overview(conn, end))
            write_json(directory / "town_month.json", town_month(conn))
            write_json(directory / "lease_decay.json", lease_decay(conn))
            write_json(directory / "planning_areas.geojson", planning_areas(conn))
            write_json(directory / "blocks.json", blocks(conn, as_of))
            recent = recent_sales(conn, end)
            for town, payload in recent.items():
                write_json(directory / "recent" / f"{town_slug(town)}.json", payload)
            write_json(
                directory / "meta.json", meta(conn, settings, model_dir, sorted(recent), as_of)
            )
    finally:
        engine.dispose()
    spec = json.loads((model_dir / "features.json").read_text(encoding="utf-8"))
    (directory / "model").mkdir()
    shutil.copyfile(model_dir / "features.json", directory / "model" / "features.json")
    for file in spec["outputs"].values():
        # mtime=0 keeps the archive byte-identical across exports.
        packed = gzip.compress((model_dir / file).read_bytes(), compresslevel=9, mtime=0)
        (directory / "model" / f"{file}.gz").write_bytes(packed)


def run(settings: Settings) -> Path:
    """Build the site data next to the current directory and swap it in."""
    final = settings.site_data_dir
    final.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".data.", dir=final.parent))
    try:
        build(staging, settings)
        errors = validation_errors(staging, settings.site_schema_dir)
        if errors:
            raise ExportError(
                f"{len(errors)} problems; kept the current {final}:\n" + "\n".join(errors[:20])
            )
        publish(staging, final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    logger.info("Wrote %s (%s bytes)", final, f"{directory_bytes(final):,}")
    return final


def main() -> int:
    setup_logging()
    run(get_settings())
    return 0


if __name__ == "__main__":
    sys.exit(main())
