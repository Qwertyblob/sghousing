-- One row per located HDB block (address_key): position, nearby amenities and block facts.
--
-- Nearest-neighbour distances use the KNN operator `<->` in a LATERAL subquery, then the exact
-- ST_Distance; counts use ST_DWithin. Distances are in metres (SVY21, EPSG:3414).
-- Amenity data is a current snapshot: an MRT line or hawker centre that opened in 2020 also
-- counts for a 1995 sale.

DROP TABLE IF EXISTS marts.block_features;

CREATE TABLE marts.block_features AS
WITH blocks AS (
    -- Located blocks that appear in the resale data (`approx` = street-centroid location).
    SELECT c.address_key, c.status AS geocode_status, c.lat, c.lon, c.geom
    FROM staging.geocode_cache AS c
    WHERE c.status IN ('ok', 'fallback', 'override', 'approx')
      AND EXISTS (SELECT 1 FROM staging.hdb_resale AS r WHERE r.address_key = c.address_key)
),
txn_block AS (
    -- Fallbacks for blocks HDB property information does not describe (mostly demolished
    -- blocks, see block_info below).
    SELECT address_key,
           max(storey_hi) AS max_storey_seen,
           min(lease_commence_year) AS first_lease_year,
           min(extract(year FROM month))::integer AS first_sale_year
    FROM staging.hdb_resale
    GROUP BY address_key
),
block_info AS (
    -- Property information describes the building standing today. Some demolished blocks'
    -- numbers were reused for a new structure (e.g. a 2017 non-residential building at an
    -- address with resales from 1990), so only trust a residential record completed no later
    -- than the block's first resale.
    SELECT bi.address_key, bi.max_floor_lvl, bi.year_completed
    FROM staging.hdb_block_info AS bi
    JOIN txn_block AS tb USING (address_key)
    WHERE bi.is_residential AND bi.year_completed <= tb.first_sale_year
),
cbd AS (
    -- The spec's CBD reference point: Raffles Place MRT, as the centroid of its exits.
    SELECT ST_Centroid(ST_Collect(geom)) AS geom
    FROM staging.mrt_exits
    WHERE station_name = 'RAFFLES PLACE MRT STATION'
)
SELECT
    b.address_key,
    b.geocode_status,
    b.lat,
    b.lon,
    b.geom,
    pa.planning_area,
    pa.region,
    mrt.dist_m AS dist_mrt_m,
    mrt.station_name AS nearest_mrt_name,
    (
        SELECT count(*)::integer
        FROM staging.mrt_exits AS e
        WHERE e.serves_mrt AND ST_DWithin(e.geom, b.geom, 500)
    ) AS n_mrt_exits_500m,
    (
        SELECT count(*)::integer
        FROM staging.schools AS s
        WHERE s.is_primary AND ST_DWithin(s.geom, b.geom, 1000)
    ) AS n_primary_schools_1km,
    sch.dist_m AS dist_nearest_primary_m,
    hawker.dist_m AS dist_hawker_m,
    park.dist_m AS dist_park_m,
    ST_Distance(b.geom, cbd.geom) AS dist_cbd_m,
    coalesce(bi.max_floor_lvl, tb.max_storey_seen) AS block_max_floor,
    coalesce(bi.year_completed, tb.first_lease_year) AS block_year_completed,
    CASE
        WHEN bi.max_floor_lvl IS NOT NULL AND bi.year_completed IS NOT NULL
            THEN 'hdb_property_info'
        ELSE 'transactions'
    END AS block_info_source
FROM blocks AS b
CROSS JOIN cbd
JOIN txn_block AS tb USING (address_key)
LEFT JOIN block_info AS bi USING (address_key)
LEFT JOIN LATERAL (
    SELECT p.planning_area, p.region
    FROM staging.planning_areas AS p
    WHERE ST_Contains(p.geom, b.geom)
    LIMIT 1
) AS pa ON true
LEFT JOIN LATERAL (
    SELECT e.station_base || ' MRT STATION' AS station_name, ST_Distance(e.geom, b.geom) AS dist_m
    FROM staging.mrt_exits AS e
    WHERE e.serves_mrt
    ORDER BY e.geom <-> b.geom
    LIMIT 1
) AS mrt ON true
LEFT JOIN LATERAL (
    SELECT ST_Distance(s.geom, b.geom) AS dist_m
    FROM staging.schools AS s
    WHERE s.is_primary AND s.geom IS NOT NULL
    ORDER BY s.geom <-> b.geom
    LIMIT 1
) AS sch ON true
LEFT JOIN LATERAL (
    SELECT ST_Distance(h.geom, b.geom) AS dist_m
    FROM staging.hawker_centres AS h
    WHERE h.status <> 'Under Construction'
    ORDER BY h.geom <-> b.geom
    LIMIT 1
) AS hawker ON true
LEFT JOIN LATERAL (
    -- Parks are polygons: take a few KNN candidates and the exact minimum distance to their
    -- edges (0 inside a park).
    SELECT min(ST_Distance(k.geom, b.geom)) AS dist_m
    FROM (
        SELECT p.geom
        FROM staging.parks AS p
        ORDER BY p.geom <-> b.geom
        LIMIT 10
    ) AS k
) AS park ON true;

-- Every located block must have every feature: a NULL here fails the build.
ALTER TABLE marts.block_features
    ADD PRIMARY KEY (address_key),
    ALTER COLUMN geocode_status SET NOT NULL,
    ALTER COLUMN lat SET NOT NULL,
    ALTER COLUMN lon SET NOT NULL,
    ALTER COLUMN geom SET NOT NULL,
    ALTER COLUMN planning_area SET NOT NULL,
    ALTER COLUMN region SET NOT NULL,
    ALTER COLUMN dist_mrt_m SET NOT NULL,
    ALTER COLUMN nearest_mrt_name SET NOT NULL,
    ALTER COLUMN n_mrt_exits_500m SET NOT NULL,
    ALTER COLUMN n_primary_schools_1km SET NOT NULL,
    ALTER COLUMN dist_nearest_primary_m SET NOT NULL,
    ALTER COLUMN dist_hawker_m SET NOT NULL,
    ALTER COLUMN dist_park_m SET NOT NULL,
    ALTER COLUMN dist_cbd_m SET NOT NULL,
    ALTER COLUMN block_max_floor SET NOT NULL,
    ALTER COLUMN block_year_completed SET NOT NULL;

CREATE INDEX block_features_geom_idx ON marts.block_features USING gist (geom);
CREATE INDEX block_features_planning_area_idx ON marts.block_features (planning_area);
