"""`python -m pipeline.train.evaluate` (`make evaluate`): metrics, SHAP figures, model card.

Uses the latest trained model (`models/LATEST`). This is the one place the test split is
scored. Writes:

- `models/{version}/metrics.json`: MAE, MAPE, RMSE and R² in dollars for LightGBM and both
  baselines on validation and test; test breakdowns by flat type, region and month; the 80%
  interval (p10-p90) coverage on test. Validation metrics are the ones recorded at training
  time by the models fit on the training split only (the final models have seen the
  validation months); on test, the baselines are refit on training + validation like the
  final models.
- `reports/figures/shap_*.{png,svg}`: summary, dependence plots for the top 5 features, and
  3 example explanations.
- `models/{version}/model_card.md`.
- `tests/parity/cases.json`: 200 test rows through Python onnxruntime, for the Node check.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from config.settings import Settings, get_settings
from pipeline.common.db import get_engine
from pipeline.common.log import setup_logging
from pipeline.train import export_onnx
from pipeline.train.baselines import RidgeBaseline, TownMedianBaseline
from pipeline.train.dataset import (
    CATEGORICAL,
    FEATURES,
    NUMERIC,
    Split,
    assign_split,
    combine,
    encode,
    load_frame,
)
from pipeline.train.metrics import grouped_metrics, interval_coverage, regression_metrics
from pipeline.train.train_lgbm import QUANTILES, latest_model_dir, ordered_quantiles

logger = logging.getLogger("pipeline.evaluate")

SHAP_SAMPLE = 2000
TOP_DEPENDENCE = 5
PARITY_ROWS = 200
SEED = 7
PARITY_CASES = Path(__file__).resolve().parents[2] / "tests" / "parity" / "cases.json"
# Earlier scorings of a version's test split, disclosed in its model card. 2026-10: the
# first models (trained to 2023-12, uncalibrated) covered 64.1% of test prices with their 80%
# interval (target 72-88%). The refit with offsets calibrated on 2024 alone fixed the point
# accuracy but not coverage (62.6%): 2024's upward drift reversed in 2025-26. The symmetric
# multi-year calibration that followed is chosen on pre-2025 data only.
PRIOR_TEST_SCORES: dict[str, list[dict[str, Any]]] = {
    "2026-10": [
        {
            "scored_on": "2026-10-25",
            "model": "trained to 2023-12, uncalibrated intervals",
            "mae": 40_058,
            "interval_80_coverage": 0.641,
        },
        {
            "scored_on": "2026-10-26",
            "model": "refit to 2024-12, interval offsets calibrated on 2024 only",
            "mae": 29_147,
            "interval_80_coverage": 0.626,
        },
    ]
}
METRIC_LABELS = {
    "lightgbm_p50": "LightGBM (median)",
    "town_median": "Town median baseline",
    "ridge": "Ridge baseline",
}


def _month(value: str) -> date:
    return datetime.strptime(value, "%Y-%m").date()


def load_splits(window: dict[str, str], maps: dict[str, dict[str, int]]) -> dict[str, Split]:
    """The model's own train/validation/test splits, encoded with its saved category codes.

    The split boundaries and the last month come from the model's training record, not the
    current settings, so changing TRAIN_END/VALID_END or loading newer data afterwards does
    not move sales between splits or add months the model was not versioned with.
    """
    engine = get_engine()
    df = load_frame(engine)
    engine.dispose()
    df = df[df["month"] <= pd.Timestamp(_month(window["last_month"]))]
    part = assign_split(df["month"], _month(window["train_end"]), _month(window["valid_end"]))
    splits = {}
    for name in ("train", "valid", "test"):
        rows = df[part == name].reset_index(drop=True)
        splits[name] = Split(name, encode(rows, maps), rows["log_price"].to_numpy(), rows)
    return splits


def _prices(boosters: dict[str, lgb.Booster], split: Split) -> dict[str, np.ndarray]:
    return {name: np.exp(booster.predict(split.x)) for name, booster in boosters.items()}


def compute_metrics(
    boosters: dict[str, lgb.Booster], splits: dict[str, Split], training: dict[str, Any]
) -> dict[str, Any]:
    """Every metric in metrics.json."""
    fit = combine("train_valid", splits["train"], splits["valid"])
    test = splits["test"]
    baselines = {
        "town_median": TownMedianBaseline().fit(fit),
        "ridge": RidgeBaseline().fit(fit),
    }
    raw = _prices(boosters, test)
    p10, p50, p90 = ordered_quantiles(raw).values()
    scores = {"lightgbm_p50": regression_metrics(test.price, p50)}
    for name, model in baselines.items():
        scores[name] = regression_metrics(test.price, model.predict_price(test))
    month = test.rows["month"].dt.strftime("%Y-%m")
    return {
        "model_version": training["model_version"],
        "evaluated_on": date.today().isoformat(),
        "validation": training["validation"],
        "test": {
            **scores,
            "interval_80_coverage": interval_coverage(test.price, p10, p90),
            # Rows where the separately trained models crossed (sorted before scoring).
            "interval_crossings": int(
                np.sum((raw["p10"] > raw["p50"]) | (raw["p50"] > raw["p90"]))
            ),
            "by_flat_type": grouped_metrics(test.rows["flat_type"], test.price, p50),
            "by_region": grouped_metrics(test.rows["region"], test.price, p50),
            "by_month": grouped_metrics(month, test.price, p50),
        },
    }


def _decoded(x: pd.DataFrame, maps: dict[str, dict[str, int]]) -> pd.DataFrame:
    """Inputs with category codes replaced by their names, for readable explanations."""
    out = x.astype(object).copy()
    for col in CATEGORICAL:
        names = {code: name for name, code in maps[col].items()}
        out[col] = [names.get(int(v), "?") if v == v else "?" for v in x[col]]
    return out


def shap_figures(
    booster: lgb.Booster, test: Split, maps: dict[str, dict[str, int]], directory: Path
) -> list[str]:
    """SHAP summary, top-feature dependence plots and three example explanations."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import shap

    from pipeline.eda import figures

    figures.apply_style()
    rng = np.random.default_rng(SEED)
    rows = np.sort(rng.choice(len(test.y), size=min(SHAP_SAMPLE, len(test.y)), replace=False))
    x = test.x.iloc[rows].reset_index(drop=True)
    explainer = shap.TreeExplainer(booster)
    values = explainer.shap_values(x)
    names: list[str] = []

    # shap shuffles the drawing order with numpy's global RNG; seed it so reruns write
    # identical figures.
    np.random.seed(SEED)  # noqa: NPY002
    shap.summary_plot(values, x, show=False, max_display=15)
    plt.title("SHAP values of the median model (log price), test sample", loc="left")
    figures.save_figure(plt.gcf(), "shap_summary", directory)
    plt.close("all")
    names.append("shap_summary")

    importance = np.abs(values).mean(axis=0)
    top = [FEATURES[i] for i in np.argsort(importance)[::-1][:TOP_DEPENDENCE]]
    for feature in top:
        np.random.seed(SEED)  # noqa: NPY002
        shap.dependence_plot(feature, values, x, interaction_index=None, show=False)
        label = " (category code)" if feature in CATEGORICAL else ""
        plt.title(f"SHAP dependence: {feature}{label}", loc="left")
        figures.save_figure(plt.gcf(), f"shap_dependence_{feature}", directory)
        plt.close("all")
        names.append(f"shap_dependence_{feature}")

    # Examples at the 10th, 50th and 90th percentile of predicted price.
    predicted = booster.predict(x)
    order = np.argsort(predicted)
    display = _decoded(x, maps)
    for i, q in enumerate((0.1, 0.5, 0.9), start=1):
        row = int(order[int(q * (len(order) - 1))])
        explanation = shap.Explanation(
            values=values[row],
            base_values=float(explainer.expected_value),
            data=x.iloc[row].to_numpy(),
            display_data=display.iloc[row].to_numpy(),
            feature_names=list(FEATURES),
        )
        shap.plots.waterfall(explanation, max_display=12, show=False)
        plt.title(
            f"Example {i}: predicted S${np.exp(predicted[row]):,.0f} (log-price contributions)",
            loc="left",
        )
        figures.save_figure(plt.gcf(), f"shap_example_{i}", directory)
        plt.close("all")
        names.append(f"shap_example_{i}")
    return names


def _metric_row(name: str, m: dict[str, float]) -> str:
    return f"| {name} | {m['mae']:,.0f} | {m['mape']:.2%} | {m['rmse']:,.0f} | {m['r2']:.3f} |"


def model_card(training: dict[str, Any], metrics: dict[str, Any]) -> str:
    """The model card markdown."""
    window = training["data_window"]
    rows = training["rows"]
    params = training["best_params"]
    offsets = training["calibration"]["offsets_log_price"]
    folds = training["calibration"]["folds"]
    years = [label for label in folds if label != "validation"] + [window["valid_end"][:4]]
    fold_table = "\n".join(
        [
            "| Year | Sales | Inside p10-p90 | Below p10 | Above p90 |",
            "|---|---|---|---|---|",
            *(
                f"| {window['valid_end'][:4] if label == 'validation' else label} | {f['n']:,} "
                f"| {f['coverage']:.1%} | {f['below_p10']:.1%} | {f['above_p90']:.1%} |"
                for label, f in folds.items()
            ),
        ]
    )
    coverage = metrics["validation"]["interval_80_coverage"]
    prior = PRIOR_TEST_SCORES.get(training["model_version"], [])
    history = ""
    if prior:
        lines = [
            f"- {p['scored_on']}: {p['model']}: MAE S${p['mae']:,.0f}, 80% interval coverage "
            f"{p['interval_80_coverage']:.1%}."
            for p in prior
        ]
        history = "\n".join(
            [
                "## Earlier test scores\n",
                "The test split was scored before these results. The changes since were chosen",
                "on data from before the test period only, but knowing the earlier results is a",
                "form of test-set reuse.\n",
                *lines,
                "",
            ]
        )
    header = "| Model | MAE (S$) | MAPE | RMSE (S$) | R² |\n|---|---|---|---|---|"

    def table(split: str) -> str:
        lines = [_metric_row(label, metrics[split][k]) for k, label in METRIC_LABELS.items()]
        return "\n".join([header, *lines])

    def breakdown(key: str) -> str:
        lines = [_metric_row(k, v) for k, v in metrics["test"][key].items()]
        return "\n".join([header.replace("Model", "Group"), *lines])

    return f"""# Model card: HDB resale price model {training["model_version"]}

**Indicative only.** These are statistical estimates from past HDB resale transactions. They
are not valuations, and must not be used as the basis for buying, selling or financing a
flat.

## What it predicts

The resale price of an HDB flat, as three quantiles: p10, p50 (the point estimate) and p90.
The p10-p90 range is meant to contain 80% of actual prices. Each is a LightGBM model on
`log(resale_price)`, exported to ONNX (`model_p10.onnx`, `model_p50.onnx` and
`model_p90.onnx`, with inputs described in `features.json`).

## Data

- Source: HDB resale flat prices on data.gov.sg (collection 189), with locations from
  OneMap and amenities from LTA, MOE, NEA, NParks and URA datasets.
- Training: {window["train_from"][:7]} to {window["train_end"]} ({rows["train"]:,} sales).
- Validation (tuning, early stopping, interval calibration): to {window["valid_end"]}
  ({rows["valid"]:,} sales).
- Final models: refit on training + validation, {window["train_from"][:7]} to
  {window["fit_end"]} ({rows["train"] + rows["valid"]:,} sales).
- Test: after {window["valid_end"]} to {window["last_month"]} ({rows["test"]:,} sales).
- Trained on {training["trained_on"]} with LightGBM {training["lightgbm_version"]}.

## Features

- Categorical (fixed integer codes, LightGBM categorical splits): {", ".join(CATEGORICAL)}.
- Numeric: {", ".join(NUMERIC)}.
- `town_psm_*`, `block_psm_24m` and `block_sales_24m` are rolling medians and counts over
  months strictly before the sale (`pipeline/sql/205_marts_rolling_features.sql`).

## Training

Random search over {len(training["trials"])} parameter sets for the median model, scored by
validation MAE in dollars, with early stopping on validation. Best: learning rate
{params["learning_rate"]:.3f}, {params["num_leaves"]} leaves, at least
{params["min_data_in_leaf"]} sales per leaf. The p10 and p90 models use the same parameters.

Out of sample the raw intervals drift with the market, in whichever direction it moves: fit
to 2023, the p10-p90 range held {coverage["uncalibrated"]:.1%} of 2024 prices. The interval
is therefore widened with conformalized quantile regression on a yearly backtest: each year
from {years[0]} to {years[-1]} is predicted by p10/p90 models fit only on earlier years, and
both bounds move out by {offsets["p90"]:.4f} in log price (about {np.expm1(offsets["p90"]):.1%})
so that 80% of those out-of-sample prices fall inside. Raw coverage by year:

{fold_table}

The three models were then refit on training + validation with the same parameters and
numbers of trees, and the widening applied. Quantiles that cross are sorted so that
p10 <= p50 <= p90.

## Metrics

Validation (models fit on the training split only, before refitting):

{table("validation")}

Test (final models; baselines refit on training + validation too):

{table("test")}

80% interval (p10-p90) coverage on test: {metrics["test"]["interval_80_coverage"]:.1%}
({metrics["test"]["interval_crossings"]:,} sales where the models crossed and were sorted).

Test by flat type (LightGBM median):

{breakdown("by_flat_type")}

Test by region (LightGBM median):

{breakdown("by_region")}

Monthly test metrics are in `metrics.json` (`test.by_month`).

{history}
## Known limitations

- No unit-level information: condition, renovation, facing, view, noise and exact floor
  (only a 3-storey range) are unknown.
- Prices are nominal Singapore dollars, with no inflation adjustment.
- Amenity locations are today's, so new stations or hawker centres also count for older
  sales.
- Trained on sales up to {window["fit_end"]}. The rolling price features carry recent price
  levels forward, but the model cannot anticipate faster price growth or sudden shocks
  (policy changes, a pandemic); accuracy and interval coverage fall the further a sale is
  from the training data.
- Rare flats (1-room, multi-generation, terrace houses) have little data and wider errors.
"""


def run(settings: Settings) -> Path:
    """Evaluate the latest model and write metrics, figures, model card and parity cases."""
    directory = latest_model_dir(settings)
    spec = json.loads((directory / "features.json").read_text(encoding="utf-8"))
    training = json.loads((directory / "training.json").read_text(encoding="utf-8"))
    maps = spec["categories"]
    boosters = {
        name: lgb.Booster(model_file=str(directory / f"model_{name}.txt")) for name in QUANTILES
    }
    splits = load_splits(training["data_window"], maps)
    metrics = compute_metrics(boosters, splits, training)
    (directory / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    test = metrics["test"]
    logger.info(
        "Test: LightGBM MAE S$%s (town median S$%s, ridge S$%s); 80%% coverage %.1f%%",
        f"{test['lightgbm_p50']['mae']:,.0f}",
        f"{test['town_median']['mae']:,.0f}",
        f"{test['ridge']['mae']:,.0f}",
        100 * test["interval_80_coverage"],
    )
    shap_figures(boosters["p50"], splits["test"], maps, settings.reports_dir / "figures")
    (directory / "model_card.md").write_text(model_card(training, metrics), encoding="utf-8")
    rng = np.random.default_rng(SEED)
    test_x = splits["test"].x
    rows = np.sort(rng.choice(len(test_x), size=min(PARITY_ROWS, len(test_x)), replace=False))
    export_onnx.write_parity_cases(directory, test_x.iloc[rows], PARITY_CASES)
    logger.info("Wrote metrics, model card, SHAP figures and %s", PARITY_CASES)
    return directory


def main() -> int:
    setup_logging()
    run(get_settings())
    return 0


if __name__ == "__main__":
    sys.exit(main())
