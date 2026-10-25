"""Readers for downloaded CSV and GeoJSON snapshots. Values are kept exactly as downloaded."""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# Lowercase letters, digits, underscore. Always quoted in SQL, so a leading digit is fine
# (HDB block data has columns like "1room_sold").
IDENTIFIER = re.compile(r"^[a-z0-9_]+$")


class ParseError(ValueError):
    """A snapshot does not have the structure we expect."""


def normalise_header(names: Sequence[str]) -> list[str]:
    """Strip and lowercase column names; require safe, unique SQL identifiers."""
    header = [name.strip().lower() for name in names]
    for name in header:
        if not IDENTIFIER.match(name):
            raise ParseError(f"unsafe column name {name!r}")
    if len(set(header)) != len(header):
        raise ParseError(f"duplicate column names in header {header}")
    return header


def check_header(header: Sequence[str], expected: Sequence[str] | None) -> None:
    """Fail if the CSV header differs from the column order in the dataset metadata."""
    if expected is None:
        return
    expected_norm = [name.strip().lower() for name in expected]
    if list(header) != expected_norm:
        raise ParseError(
            f"CSV header {list(header)} does not match dataset metadata {expected_norm}"
        )


@contextmanager
def open_csv(path: Path) -> Iterator[tuple[list[str], Iterator[tuple[int, list[str]]]]]:
    """Open a CSV snapshot and yield `(header, records)`.

    `records` yields `(record_number, values)` with 1-based record numbers. Quoted fields may
    contain commas and newlines; empty cells stay `''`. Blank lines are skipped and a record
    with the wrong number of fields raises ParseError.
    """
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        try:
            header = normalise_header(next(reader))
        except StopIteration as exc:
            raise ParseError(f"{path.name} is empty") from exc

        def records() -> Iterator[tuple[int, list[str]]]:
            number = 0
            for row in reader:
                if not row:
                    continue
                number += 1
                if len(row) != len(header):
                    raise ParseError(
                        f"{path.name} record {number}: {len(row)} fields, expected {len(header)}"
                    )
                yield number, row

        yield header, records()


def read_geojson_features(path: Path) -> Iterator[tuple[int, dict[str, Any], str | None]]:
    """Yield `(feature_index, properties, geometry_json)` from a FeatureCollection."""
    with path.open(encoding="utf-8-sig") as f:
        doc = json.load(f)
    if not isinstance(doc, dict) or doc.get("type") != "FeatureCollection":
        raise ParseError(f"{path.name} is not a GeoJSON FeatureCollection")
    features = doc.get("features")
    if not isinstance(features, list):
        raise ParseError(f"{path.name} has no features list")
    for index, feature in enumerate(features):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ParseError(f"{path.name} feature {index} is not a Feature")
        geometry = feature.get("geometry")
        yield (
            index,
            feature.get("properties") or {},
            json.dumps(geometry) if geometry is not None else None,
        )
