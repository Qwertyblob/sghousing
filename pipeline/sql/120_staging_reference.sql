-- Reference tables in staging, geometry in SVY21 (EPSG:3414, metres).
-- Supermarkets are intentionally absent: no current location dataset exists on data.gov.sg.

-- MRT and LRT station exits.
-- `serves_mrt`: the exit leads into an MRT station. At integrated interchanges (Punggol,
-- Choa Chu Kang, Bukit Panjang) some exits are labelled with the LRT station's name, so an
-- LRT-labelled exit counts when an MRT station has the same base name.
DROP TABLE IF EXISTS staging.mrt_exits;
CREATE TABLE staging.mrt_exits AS
WITH exits AS (
    SELECT
        feature_index AS exit_id,
        upper(staging.norm_text(properties ->> 'STATION_NA')) AS station_name,
        staging.norm_text(properties ->> 'EXIT_CODE') AS exit_code,
        staging.geojson_to_svy21(geometry_geojson)::geometry(Point, 3414) AS geom
    FROM raw.ref_mrt_exits
),
named AS (
    SELECT
        exit_id,
        station_name,
        regexp_replace(station_name, ' (MRT|LRT) STATION$', '') AS station_base,
        exit_code,
        CASE WHEN station_name LIKE '% LRT STATION' THEN 'LRT' ELSE 'MRT' END AS rail_type,
        geom
    FROM exits
)
SELECT
    n.exit_id,
    n.station_name,
    n.station_base,
    n.exit_code,
    n.rail_type,
    n.rail_type = 'MRT' OR EXISTS (
        SELECT 1 FROM named AS m WHERE m.station_base = n.station_base AND m.rail_type = 'MRT'
    ) AS serves_mrt,
    n.geom
FROM named AS n;
ALTER TABLE staging.mrt_exits
    ADD PRIMARY KEY (exit_id),
    ALTER COLUMN station_name SET NOT NULL,
    ALTER COLUMN geom SET NOT NULL;
CREATE INDEX mrt_exits_geom_idx ON staging.mrt_exits USING gist (geom);

-- URA Master Plan 2019 planning areas (no sea).
DROP TABLE IF EXISTS staging.planning_areas;
CREATE TABLE staging.planning_areas AS
SELECT
    upper(staging.norm_text(properties ->> 'PLN_AREA_N')) AS planning_area,
    staging.norm_text(properties ->> 'PLN_AREA_C') AS planning_area_code,
    upper(staging.norm_text(properties ->> 'REGION_N')) AS region,
    staging.norm_text(properties ->> 'REGION_C') AS region_code,
    staging.geojson_to_svy21_multipolygon(geometry_geojson)::geometry(MultiPolygon, 3414) AS geom
FROM raw.ref_planning_areas;
ALTER TABLE staging.planning_areas
    ADD PRIMARY KEY (planning_area),
    ALTER COLUMN region SET NOT NULL,
    ALTER COLUMN geom SET NOT NULL;
CREATE INDEX planning_areas_geom_idx ON staging.planning_areas USING gist (geom);

-- NEA hawker centres (all statuses; feature building decides which to use).
DROP TABLE IF EXISTS staging.hawker_centres;
CREATE TABLE staging.hawker_centres AS
SELECT
    feature_index AS hawker_id,
    staging.norm_text(properties ->> 'NAME') AS name,
    staging.norm_text(properties ->> 'STATUS') AS status,
    staging.norm_text(properties ->> 'ADDRESS_MYENV') AS address,
    staging.norm_text(properties ->> 'ADDRESSPOSTALCODE') AS postal_code,
    (properties ->> 'NUMBER_OF_COOKED_FOOD_STALLS')::integer AS n_cooked_food_stalls,
    staging.geojson_to_svy21(geometry_geojson)::geometry(Point, 3414) AS geom
FROM raw.ref_hawker_centres;
ALTER TABLE staging.hawker_centres
    ADD PRIMARY KEY (hawker_id),
    ALTER COLUMN geom SET NOT NULL;
CREATE INDEX hawker_centres_geom_idx ON staging.hawker_centres USING gist (geom);

-- NParks parks and nature reserves (polygons, so distances are to the park edge).
DROP TABLE IF EXISTS staging.parks;
CREATE TABLE staging.parks AS
SELECT
    feature_index AS park_id,
    staging.norm_text(properties ->> 'NAME') AS name,
    staging.norm_text(properties ->> 'L_CODE') AS park_code,
    coalesce((properties ->> 'N_RESERVE')::integer = 1, false) AS is_nature_reserve,
    staging.geojson_to_svy21_multipolygon(geometry_geojson)::geometry(MultiPolygon, 3414) AS geom
FROM raw.ref_parks;
ALTER TABLE staging.parks
    ADD PRIMARY KEY (park_id),
    ALTER COLUMN geom SET NOT NULL;
CREATE INDEX parks_geom_idx ON staging.parks USING gist (geom);

-- MOE schools. The source has addresses and postal codes but no coordinates; geom is
-- filled from the geocode cache in Phase 3.
DROP TABLE IF EXISTS staging.schools;
CREATE TABLE staging.schools AS
SELECT
    staging.norm_text(school_name) AS school_name,
    staging.norm_text(address) AS address,
    -- Singapore postal codes have 6 digits; the source drops the leading zero of a few
    -- central-area codes (e.g. 88256 for 088256).
    CASE
        WHEN staging.norm_text(postal_code) ~ '^\d{5}$' THEN '0' || staging.norm_text(postal_code)
        ELSE staging.norm_text(postal_code)
    END AS postal_code,
    staging.norm_text(mainlevel_code) AS mainlevel_code,
    staging.norm_text(zone_code) AS zone_code,
    (staging.norm_text(mainlevel_code) = 'PRIMARY'
        OR staging.norm_text(mainlevel_code) LIKE 'MIXED LEVEL (P1%') AS is_primary,
    NULL::geometry(Point, 3414) AS geom
FROM raw.ref_schools;
ALTER TABLE staging.schools
    ADD PRIMARY KEY (school_name),
    ALTER COLUMN postal_code SET NOT NULL;
CREATE INDEX schools_geom_idx ON staging.schools USING gist (geom);

-- HDB property information: one row per block, keyed like staging.hdb_resale.address_key.
DROP TABLE IF EXISTS staging.hdb_block_info;
CREATE TABLE staging.hdb_block_info AS
SELECT
    upper(staging.norm_text(blk_no)) || ' ' || upper(staging.norm_text(street)) AS address_key,
    upper(staging.norm_text(blk_no)) AS block,
    upper(staging.norm_text(street)) AS street_name,
    staging.norm_text(max_floor_lvl)::integer AS max_floor_lvl,
    staging.norm_text(year_completed)::integer AS year_completed,
    staging.norm_text(residential) = 'Y' AS is_residential,
    staging.norm_text(total_dwelling_units)::integer AS total_dwelling_units
FROM raw.ref_hdb_block_info;
ALTER TABLE staging.hdb_block_info
    ADD PRIMARY KEY (address_key),
    ALTER COLUMN max_floor_lvl SET NOT NULL;

-- HDB resale price index (1Q2009 = 100), quarterly.
DROP TABLE IF EXISTS staging.resale_price_index;
CREATE TABLE staging.resale_price_index AS
SELECT
    q.quarter,
    make_date(q.parts[1]::integer, (q.parts[2]::integer - 1) * 3 + 1, 1) AS quarter_start,
    staging.norm_text(q.index)::numeric AS index_value
FROM (
    SELECT
        staging.norm_text(quarter) AS quarter,
        index,
        regexp_match(staging.norm_text(quarter), '^(\d{4})-Q([1-4])$') AS parts
    FROM raw.ref_resale_price_index
) AS q;
ALTER TABLE staging.resale_price_index
    ADD PRIMARY KEY (quarter),
    ALTER COLUMN quarter_start SET NOT NULL,
    ALTER COLUMN index_value SET NOT NULL;
