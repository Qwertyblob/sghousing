"""CSV and GeoJSON parsing against small fixture files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.ingest.parsers import ParseError, check_header, open_csv, read_geojson_features
from tests.conftest import FIXTURES

OLD_COLUMNS = [
    "month",
    "town",
    "flat_type",
    "block",
    "street_name",
    "storey_range",
    "floor_area_sqm",
    "flat_model",
    "lease_commence_date",
    "resale_price",
]
NEW_COLUMNS = [*OLD_COLUMNS[:-1], "remaining_lease", "resale_price"]


def read_all(path: Path) -> tuple[list[str], list[tuple[int, list[str]]]]:
    with open_csv(path) as (header, records):
        return header, list(records)


def as_dicts(path: Path) -> list[dict[str, str]]:
    header, records = read_all(path)
    return [dict(zip(header, values, strict=True)) for _, values in records]


def test_old_format_without_remaining_lease() -> None:
    header, records = read_all(FIXTURES / "resale_1990.csv")
    assert header == OLD_COLUMNS
    assert [n for n, _ in records] == [1, 2, 3]
    # Identical-looking rows are separate transactions and both kept.
    assert records[0][1] == records[1][1]


def test_2015_format_integer_remaining_lease() -> None:
    rows = as_dicts(FIXTURES / "resale_2015.csv")
    assert list(rows[0]) == NEW_COLUMNS
    assert rows[0]["remaining_lease"] == "70"


def test_2017_format_text_remaining_lease() -> None:
    rows = as_dicts(FIXTURES / "resale_2017.csv")
    assert rows[0]["remaining_lease"] == "61 years 04 months"
    assert rows[0]["resale_price"] == "232000"


def test_column_sets_differ_between_years() -> None:
    old, _ = read_all(FIXTURES / "resale_1990.csv")
    new, _ = read_all(FIXTURES / "resale_2017.csv")
    assert set(new) - set(old) == {"remaining_lease"}


def test_header_must_match_metadata() -> None:
    check_header(OLD_COLUMNS, OLD_COLUMNS)
    check_header(OLD_COLUMNS, None)  # non-tabular metadata: nothing to check
    with pytest.raises(ParseError, match="does not match"):
        check_header(OLD_COLUMNS, NEW_COLUMNS)


def test_bom_and_header_whitespace(tmp_path: Path) -> None:
    path = tmp_path / "bom.csv"
    path.write_bytes(b"\xef\xbb\xbf Postal_Code , Name \r\n018935,x\r\n")
    header, records = read_all(path)
    assert header == ["postal_code", "name"]
    assert records == [(1, ["018935", "x"])]


def test_quoted_comma_embedded_newline_and_empty_cell(tmp_path: Path) -> None:
    path = tmp_path / "tricky.csv"
    path.write_text('a,b,c\n"Shop, A","line1\nline2",\n', encoding="utf-8", newline="")
    header, records = read_all(path)
    assert records == [(1, ["Shop, A", "line1\nline2", ""])]
    # One record spread over two physical lines: never count records by lines.
    assert len(path.read_text().splitlines()) - 1 != len(records)


def test_values_kept_exactly(tmp_path: Path) -> None:
    path = tmp_path / "exact.csv"
    path.write_text("a,b\n  padded  ,007\n", encoding="utf-8")
    _, records = read_all(path)
    assert records == [(1, ["  padded  ", "007"])]


def test_blank_lines_are_not_records(tmp_path: Path) -> None:
    path = tmp_path / "blank.csv"
    path.write_text("a,b\n1,2\n\n3,4\n", encoding="utf-8")
    _, records = read_all(path)
    assert records == [(1, ["1", "2"]), (2, ["3", "4"])]


def test_ragged_record_raises(tmp_path: Path) -> None:
    path = tmp_path / "ragged.csv"
    path.write_text("a,b,c\n1,2,3\n4,5\n", encoding="utf-8")
    with pytest.raises(ParseError, match="record 2"):
        read_all(path)


@pytest.mark.parametrize("bad", ["drop table x", "a;b", "a-b", "é", ""])
def test_unsafe_column_names_rejected(tmp_path: Path, bad: str) -> None:
    path = tmp_path / "bad.csv"
    path.write_text(f'ok,"{bad}"\n1,2\n', encoding="utf-8")
    with pytest.raises(ParseError):
        read_all(path)


def test_leading_digit_column_allowed(tmp_path: Path) -> None:
    path = tmp_path / "digit.csv"
    path.write_text("blk_no,1room_sold\n1,2\n", encoding="utf-8")
    header, _ = read_all(path)
    assert header == ["blk_no", "1room_sold"]


def test_duplicate_column_names_rejected(tmp_path: Path) -> None:
    path = tmp_path / "dup.csv"
    path.write_text("a,A\n1,2\n", encoding="utf-8")
    with pytest.raises(ParseError, match="duplicate"):
        read_all(path)


def test_geojson_features() -> None:
    features = list(read_geojson_features(FIXTURES / "two_features.geojson"))
    assert [index for index, _, _ in features] == [0, 1]
    assert features[0][1] == {"NAME": "Alpha Park", "CODE": "01"}
    assert json.loads(features[0][2] or "null")["type"] == "Point"
    assert json.loads(features[1][2] or "null")["type"] == "Polygon"


def test_geojson_must_be_feature_collection(tmp_path: Path) -> None:
    path = tmp_path / "bad.geojson"
    path.write_text('{"type": "Feature", "properties": {}, "geometry": null}')
    with pytest.raises(ParseError, match="FeatureCollection"):
        list(read_geojson_features(path))


@pytest.mark.parametrize(
    "body",
    [
        'a,b\n1,"unterminated\n2,3\n',  # lenient csv merges this into one valid-looking record
        'a,b\n"x"y,2\n',  # text after a closing quote
    ],
)
def test_malformed_quoting_raises(tmp_path: Path, body: str) -> None:
    path = tmp_path / "malformed.csv"
    path.write_text(body, encoding="utf-8", newline="")
    with pytest.raises(ParseError, match="malformed CSV"):
        read_all(path)
