"""`python -m pipeline.geocode.spot_check`: compare MRT distances with OneMap's own.

For a few known blocks, OneMap's nearest-MRT service gives the nearest stations as points.
The check passes when our nearest station (by exit) is OneMap's nearest MRT station and our
distance to that station (centroid of its exits) is within TOLERANCE_M of OneMap's station
point. `dist_mrt_m` itself is measured to the nearest exit, so it is reported alongside.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

from sqlalchemy import Engine, text

from config.settings import get_settings
from pipeline.common.db import get_engine
from pipeline.common.log import setup_logging
from pipeline.geocode.onemap import OneMapAuthError, OneMapClient

logger = logging.getLogger("pipeline.geocode.spot_check")

SPOT_CHECK_BLOCKS = (
    "406 ANG MO KIO AVE 10",
    "640 ROWELL RD",
    "303 WOODLANDS ST 31",
    "273A PUNGGOL PL",
    "925 JURONG WEST ST 92",
)
SEARCH_RADIUS_M = 2000
# The spec asked for 50 m; widened to 75 m by decision (2026-10-25). OneMap measures to one
# station point, which lies 19-60 m from the exits our distances use.
TOLERANCE_M = 75.0


@dataclass(frozen=True)
class SpotCheck:
    """One block's comparison."""

    address_key: str
    our_station: str
    our_exit_m: float
    onemap_station: str | None
    onemap_m: float | None
    our_station_m: float | None

    @property
    def passed(self) -> bool:
        return (
            self.onemap_station == self.our_station
            and self.onemap_m is not None
            and self.our_station_m is not None
            and abs(self.our_station_m - self.onemap_m) <= TOLERANCE_M
        )


def check_block(engine: Engine, client: OneMapClient, address_key: str) -> SpotCheck:
    """Compare one block's nearest MRT station and distance with OneMap's.

    OneMap lists one point per line at a station, including lines still being built (e.g.
    CR11 at Ang Mo Kio), so among OneMap entries with our station's name the one nearest our
    exits (the operating platform) is used for the distance.
    """
    with engine.connect() as conn:
        lat, lon, our_station, our_exit_m = conn.execute(
            text(
                "SELECT lat, lon, nearest_mrt_name, dist_mrt_m "
                "FROM marts.block_features WHERE address_key = :k"
            ),
            {"k": address_key},
        ).one()
        stations = [
            s
            for s in client.nearest_mrt_stations(lat, lon, SEARCH_RADIUS_M)
            if str(s["name"]).upper().endswith("MRT STATION")
        ]
        params = {"k": address_key, "station": our_station}
        distances = [
            conn.execute(
                text("""
                WITH ours AS (
                    SELECT ST_Centroid(ST_Collect(geom)) AS geom
                    FROM staging.mrt_exits
                    WHERE serves_mrt AND station_base || ' MRT STATION' = :station
                ), theirs AS (
                    SELECT ST_Transform(ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), 3414) AS geom
                )
                SELECT ST_Distance(b.geom, theirs.geom),
                       ST_Distance(theirs.geom, ours.geom),
                       ST_Distance(b.geom, ours.geom)
                FROM marts.block_features AS b, ours, theirs
                WHERE b.address_key = :k"""),
                {**params, "lat": float(s["lat"]), "lon": float(s["lon"])},
            ).one()
            for s in stations
        ]
    if not stations:
        return SpotCheck(address_key, our_station, our_exit_m, None, None, None)
    # OneMap's nearest MRT station (any line), by straight-line distance from the block.
    nearest = min(range(len(stations)), key=lambda i: distances[i][0])
    onemap_station = str(stations[nearest]["name"]).upper()
    if onemap_station != our_station:
        return SpotCheck(address_key, our_station, our_exit_m, onemap_station, None, None)
    # Same station: compare with the OneMap entry for the platform nearest our exits.
    same = [i for i, s in enumerate(stations) if str(s["name"]).upper() == our_station]
    platform = min(same, key=lambda i: distances[i][1])
    onemap_m, _, our_station_m = distances[platform]
    return SpotCheck(address_key, our_station, our_exit_m, onemap_station, onemap_m, our_station_m)


def main() -> int:
    """Run the spot check and log one line per block; exit 1 if any block fails."""
    setup_logging()
    settings = get_settings()
    engine = get_engine()
    try:
        with OneMapClient.from_settings(settings) as client:
            results = [check_block(engine, client, key) for key in SPOT_CHECK_BLOCKS]
    except OneMapAuthError as exc:
        logger.error("%s", exc)
        return 1
    for r in results:
        logger.info(
            "%-24s %s  ours: %s (%s m to station, %.0f m to nearest exit)  OneMap: %s (%s m)",
            r.address_key,
            "PASS" if r.passed else "FAIL",
            r.our_station,
            f"{r.our_station_m:.0f}" if r.our_station_m is not None else "-",
            r.our_exit_m,
            r.onemap_station,
            f"{r.onemap_m:.0f}" if r.onemap_m is not None else "-",
        )
    failed = sum(not r.passed for r in results)
    logger.info(
        "%d / %d blocks within %.0f m of OneMap", len(results) - failed, len(results), TOLERANCE_M
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
