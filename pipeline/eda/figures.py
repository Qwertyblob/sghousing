"""Figures for the exploratory notebooks, and saving them as PNG and SVG.

Colours follow a fixed categorical order (a flat type keeps its colour in every chart) and a
single-hue blue ramp for magnitude. Saved files carry no timestamps, so re-running a notebook
on the same data rewrites identical files.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

import geopandas as gpd
import matplotlib as mpl
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.figure import Figure

logger = logging.getLogger(__name__)

SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e4e3df"
NO_DATA = "#f0efec"
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
BLUE = SERIES[0]
RED = SERIES[7]
BLUE_RAMP = (
    "#cde2fb",
    "#9ec5f4",
    "#6da7ec",
    "#3987e5",
    "#256abf",
    "#184f95",
    "#0d366b",
)
# 1-room and multi-generation flats are too few for a monthly median.
TREND_FLAT_TYPES = ("2 ROOM", "3 ROOM", "4 ROOM", "5 ROOM", "EXECUTIVE")
FLAT_TYPE_COLORS = dict(zip(TREND_FLAT_TYPES, SERIES, strict=False))
DATE_BASIS_CHANGE = pd.Timestamp("2012-03-01")
# Market events marked on the volume chart.
VOLUME_EVENTS = (
    (pd.Timestamp("2013-01-01"), "Jan 2013\ncooling measures"),
    (pd.Timestamp("2020-04-01"), "Apr–May 2020\ncircuit breaker"),
    (pd.Timestamp("2021-12-01"), "Dec 2021\ncooling measures"),
)
DPI = 150


def apply_style() -> None:
    """Set matplotlib defaults: light surface, recessive grid and axes, stable SVG ids."""
    mpl.rcParams.update(
        {
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "axes.edgecolor": GRID,
            "axes.labelcolor": TEXT_SECONDARY,
            "axes.titlecolor": TEXT,
            "axes.titlesize": 12,
            "axes.titlelocation": "left",
            "axes.grid": True,
            "axes.axisbelow": True,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "grid.color": GRID,
            "grid.linewidth": 0.6,
            "xtick.color": TEXT_SECONDARY,
            "ytick.color": TEXT_SECONDARY,
            "text.color": TEXT,
            "font.size": 9,
            "legend.frameon": False,
            "lines.linewidth": 1.5,
            "svg.hashsalt": "sghousing",
        }
    )


def save_figure(fig: Figure, name: str, directory: Path) -> tuple[Path, Path]:
    """Save `fig` as `{name}.png` and `{name}.svg` in `directory`, without timestamps."""
    directory.mkdir(parents=True, exist_ok=True)
    png, svg = directory / f"{name}.png", directory / f"{name}.svg"
    fig.savefig(png, dpi=DPI, bbox_inches="tight", metadata={"Software": None})
    fig.savefig(svg, bbox_inches="tight", metadata={"Date": None, "Creator": None})
    logger.info("Saved %s and %s", png, svg)
    return png, svg


def _source_note(fig: Figure, text: str) -> None:
    fig.text(0.01, -0.02, text, ha="left", va="top", fontsize=7.5, color=TEXT_SECONDARY)


def price_by_flat_type(df: pd.DataFrame) -> Figure:
    """Monthly median price per sqm by flat type, with the 2012 dating change marked."""
    fig, ax = plt.subplots(figsize=(9, 4.5))
    wide = df.pivot(index="month", columns="flat_type", values="median_psm")
    for flat_type in TREND_FLAT_TYPES:
        ax.plot(wide.index, wide[flat_type], color=FLAT_TYPE_COLORS[flat_type], label=flat_type)
    ax.axvline(DATE_BASIS_CHANGE, color=TEXT_SECONDARY, linestyle="--", linewidth=1)
    ax.annotate(
        "Mar 2012: sales dated by registration\n(approval date before)",
        (DATE_BASIS_CHANGE, 0.84),
        xycoords=("data", "axes fraction"),
        xytext=(-6, 0),
        textcoords="offset points",
        ha="right",
        va="top",
        fontsize=8,
        color=TEXT_SECONDARY,
    )
    ax.set_title("Median resale price per sqm by flat type, monthly")
    ax.set_ylabel("S$ per sqm")
    ax.yaxis.set_major_formatter(mpl.ticker.StrMethodFormatter("{x:,.0f}"))
    ax.legend(loc="upper left", ncols=5)
    _source_note(fig, "1-room and multi-generation flats omitted (too few sales per month).")
    return fig


def monthly_volume(df: pd.DataFrame) -> Figure:
    """Sales per month with market events marked."""
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(df["month"], df["sales"], color=BLUE)
    top = df["sales"].max()
    events = sorted((*VOLUME_EVENTS, (DATE_BASIS_CHANGE, "Mar 2012\ndating change")))
    for i, (when, label) in enumerate(events):
        ax.axvline(when, color=TEXT_SECONDARY, linestyle=":", linewidth=1)
        # Alternate two label rows so neighbouring events do not overlap.
        ax.annotate(
            label,
            (when, 0.99 if i % 2 == 0 else 0.86),
            xycoords=("data", "axes fraction"),
            xytext=(3, 0),
            textcoords="offset points",
            va="top",
            fontsize=7.5,
            color=TEXT_SECONDARY,
        )
    ax.set_title("HDB resale transactions per month")
    ax.set_ylabel("Sales")
    ax.set_ylim(0, top * 1.05)
    ax.yaxis.set_major_formatter(mpl.ticker.StrMethodFormatter("{x:,.0f}"))
    return fig


def town_ranking(df: pd.DataFrame, window: str) -> Figure:
    """Horizontal bars of each town's median price per sqm, labelled with its 2-year change."""
    ordered = df.sort_values("latest_median_psm")
    fig, ax = plt.subplots(figsize=(8, 0.28 * len(ordered) + 1.2))
    bars = ax.barh(ordered["town"], ordered["latest_median_psm"], color=BLUE, height=0.7)
    for bar, change in zip(bars, ordered["change"], strict=True):
        ax.annotate(
            "n/a" if pd.isna(change) else f"{change:+.0%}",
            (bar.get_width(), bar.get_y() + bar.get_height() / 2),
            xytext=(4, 0),
            textcoords="offset points",
            va="center",
            fontsize=8,
            color=TEXT_SECONDARY,
        )
    ax.set_title(f"Median price per sqm by town, {window} (label: change over 2 years)")
    if ordered["change"].isna().any():
        _source_note(fig, "n/a: fewer than 50 sales in the same 12 months two years earlier.")
    ax.set_xlabel("S$ per sqm")
    ax.xaxis.set_major_formatter(mpl.ticker.StrMethodFormatter("{x:,.0f}"))
    ax.grid(axis="y", visible=False)
    ax.tick_params(axis="y", length=0)
    ax.margins(x=0.08)
    return fig


def planning_area_map(gdf: gpd.GeoDataFrame, window: str) -> Figure:
    """Choropleth of median price per sqm by planning area."""
    cmap = LinearSegmentedColormap.from_list("blue", BLUE_RAMP)
    shapes = gdf.copy()
    shapes["geom"] = shapes.geometry.simplify(20)
    fig, ax = plt.subplots(figsize=(9, 4.6))
    shapes.plot(
        column="latest_median_psm",
        cmap=cmap,
        linewidth=0.4,
        edgecolor=SURFACE,
        ax=ax,
        legend=True,
        legend_kwds={"label": "S$ per sqm", "shrink": 0.7, "format": "{x:,.0f}"},
        missing_kwds={"color": NO_DATA, "edgecolor": GRID, "linewidth": 0.4},
    )
    priced = shapes.dropna(subset=["latest_median_psm"])
    # Label the dearest and cheapest areas, with leader lines out to sea where it is clear.
    for row, offset in (
        (priced.loc[priced["latest_median_psm"].idxmax()], (40, -45)),
        (priced.loc[priced["latest_median_psm"].idxmin()], (-30, -50)),
    ):
        point = row["geom"].representative_point()
        ax.annotate(
            f"{row['planning_area'].title()}: {row['latest_median_psm']:,.0f}",
            (point.x, point.y),
            xytext=offset,
            textcoords="offset points",
            ha="center",
            va="top",
            fontsize=8,
            color=TEXT,
            arrowprops={"arrowstyle": "-", "color": TEXT_SECONDARY, "linewidth": 0.8},
        )
    ax.set_axis_off()
    ax.set_title(f"Median HDB resale price per sqm by planning area, {window}")
    _source_note(
        fig, "Grey: fewer than 50 HDB resales in the window. Boundaries: URA Master Plan 2019."
    )
    return fig


def _relative_band(ax: plt.Axes, x: Sequence[float], df: pd.DataFrame) -> None:
    ax.fill_between(x, df["q25"], df["q75"], color=BLUE, alpha=0.15, linewidth=0)
    ax.plot(x, df["median"], color=BLUE)
    ax.axhline(1, color=TEXT_SECONDARY, linewidth=0.8)
    ax.set_ylabel("Price per sqm ÷ town, flat type\nand year median")


def lease_decay(df: pd.DataFrame) -> Figure:
    """Relative price per sqm against remaining lease, with the interquartile range."""
    fig, ax = plt.subplots(figsize=(8, 4.2))
    _relative_band(ax, df["lease_years"], df)
    ax.invert_xaxis()
    ax.set_xlabel("Remaining lease (years), older flats to the right")
    ax.set_title("Lease decay: price per sqm relative to similar flats (median and middle 50%)")
    _source_note(fig, "Sales since 2015.")
    return fig


def floor_premium(by_storey: pd.DataFrame, by_rel: pd.DataFrame) -> Figure:
    """Relative price per sqm by storey and by storey relative to the block's top floor."""
    fig, (left, right) = plt.subplots(1, 2, figsize=(10, 4.2), sharey=True)
    _relative_band(left, by_storey["storey_mid"], by_storey)
    left.set_xlabel("Storey (middle of the reported range)")
    left.set_title("By storey")
    _relative_band(right, by_rel["rel_storey"], by_rel)
    right.set_ylabel("")
    right.set_xlabel("Storey ÷ block's top floor")
    right.set_title("By relative storey")
    fig.suptitle(
        "Floor-level premium: price per sqm relative to similar flats",
        x=0.01,
        ha="left",
        fontsize=12,
    )
    _source_note(fig, "Sales since 2015; bins with fewer than 200 sales omitted.")
    return fig


def mrt_distance(df: pd.DataFrame, window: str) -> Figure:
    """Median price per sqm, and price relative to similar flats, by distance to the MRT."""
    fig, (left, right) = plt.subplots(1, 2, figsize=(10, 4.2))
    left.bar(df["distance"], df["latest_median_psm"], color=BLUE, width=0.7)
    left.set_title(f"Median price per sqm, {window}")
    left.set_ylabel("S$ per sqm")
    left.yaxis.set_major_formatter(mpl.ticker.StrMethodFormatter("{x:,.0f}"))
    right.bar(df["distance"], df["median"] - 1, color=BLUE, width=0.7)
    right.axhline(0, color=TEXT_SECONDARY, linewidth=0.8)
    right.set_title("Relative to town, flat type and year median, since 2015")
    right.yaxis.set_major_formatter(mpl.ticker.PercentFormatter(1, decimals=0))
    for ax in (left, right):
        ax.set_xlabel("Distance to nearest MRT exit (m)")
        ax.tick_params(axis="x", labelrotation=40)
        for label in ax.get_xticklabels():
            label.set_horizontalalignment("right")
            label.set_rotation_mode("anchor")
        ax.grid(axis="x", visible=False)
    fig.suptitle("Distance to the MRT", x=0.01, ha="left", fontsize=12)
    return fig


def feature_associations(df: pd.DataFrame) -> Figure:
    """Spearman correlation and mutual information of each feature with log price."""
    ordered = df.iloc[::-1]
    fig, (left, right) = plt.subplots(1, 2, figsize=(10, 5.5), sharey=True)
    colors = [BLUE if v >= 0 else RED for v in ordered["spearman"]]
    left.barh(ordered["feature"], ordered["spearman"], color=colors, height=0.7)
    left.axvline(0, color=TEXT_SECONDARY, linewidth=0.8)
    left.set_title("Spearman correlation (blue +, red −)")
    left.set_xlim(-1, 1)
    right.barh(ordered["feature"], ordered["mutual_info"], color=BLUE, height=0.7)
    right.set_title("Mutual information (nats)")
    for ax in (left, right):
        ax.grid(axis="y", visible=False)
        ax.tick_params(axis="y", length=0)
    fig.suptitle(
        "Association of each feature with log resale price", x=0.01, ha="left", fontsize=12
    )
    _source_note(fig, "Fixed sample of 50,000 sales since 2015, sorted by mutual information.")
    return fig
