"""Command line tools: `uv run python -m pipeline.cli <command>`.

predict: price one flat with the latest trained model, e.g.

    uv run python -m pipeline.cli predict --block 122 --street "ANG MO KIO AVE 3" \\
        --flat-type "4 ROOM" --area 92 --storey 10

Prints the p10, p50 and p90 prices. Indicative only: not a valuation.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys

from config.settings import get_settings
from pipeline.common.db import get_engine
from pipeline.common.log import setup_logging
from pipeline.train.predict import (
    Flat,
    UnknownAddressError,
    build_features,
    encode_one,
    normalise,
    predict,
)
from pipeline.train.train_lgbm import latest_model_dir

logger = logging.getLogger("pipeline.cli")

DISCLAIMER = "Indicative only: a statistical estimate from past resales, not a valuation."


def positive_area(value: str) -> float:
    """argparse type: a finite floor area above zero (float() accepts "nan" and "inf")."""
    area = float(value)
    if not math.isfinite(area) or area <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number of square metres: {value}")
    return area


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m pipeline.cli", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("predict", help="price one flat (p10/p50/p90)")
    p.add_argument("--block", required=True, help='block number, e.g. "122" or "406A"')
    p.add_argument("--street", required=True, help='street name, e.g. "ANG MO KIO AVE 3"')
    p.add_argument("--flat-type", required=True, help='e.g. "4 ROOM" or "EXECUTIVE"')
    p.add_argument("--area", type=positive_area, required=True, help="floor area in sqm")
    p.add_argument("--storey", type=int, required=True, help="floor the flat is on")
    return parser.parse_args(argv)


def run_predict(args: argparse.Namespace) -> int:
    directory = latest_model_dir(get_settings())
    spec = json.loads((directory / "features.json").read_text(encoding="utf-8"))
    flat_types = spec["categories"]["flat_type"]
    if normalise(args.flat_type) not in flat_types:
        logger.error("Unknown flat type %r; one of: %s", args.flat_type, ", ".join(flat_types))
        return 2
    flat = Flat(args.block, args.street, args.flat_type, args.area, args.storey)
    engine = get_engine()
    try:
        with engine.connect() as conn:
            features, month = build_features(conn, flat)
    except (UnknownAddressError, ValueError) as exc:
        logger.error("%s", exc)
        return 2
    finally:
        engine.dispose()
    prices = predict(directory, encode_one(features, spec["categories"]))
    lines = [
        f"{flat.address_key} ({features['town']}), {features['flat_type']}, "
        f"{args.area:g} sqm, storey {args.storey}",
        f"Priced for {month:%Y-%m} with model {spec['model_version']}:",
        *(f"  {name}  S${price:>11,.0f}" for name, price in prices.items()),
        DISCLAIMER,
    ]
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = parse_args(argv)
    if args.command == "predict":
        return run_predict(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
