-- Parsing helpers for the staging transforms. Pure and strict: input in a shape we have not
-- seen raises an error, so a format change in the source fails the build instead of
-- silently producing NULLs.

-- Trim, collapse internal whitespace to single spaces; empty becomes NULL.
CREATE OR REPLACE FUNCTION staging.norm_text(value text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT NULLIF(regexp_replace(regexp_replace(value, '\s+', ' ', 'g'), '^ | $', '', 'g'), '')
$$;

-- 'YYYY-MM' -> first day of that month.
CREATE OR REPLACE FUNCTION staging.parse_month(value text) RETURNS date
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    m text[] := regexp_match(staging.norm_text(value), '^(\d{4})-(\d{2})$');
BEGIN
    IF m IS NULL THEN
        RAISE EXCEPTION 'unrecognised month value: %', value;
    END IF;
    RETURN make_date(m[1]::int, m[2]::int, 1);
END
$$;

-- '10 TO 12' -> (10, 12).
CREATE OR REPLACE FUNCTION staging.parse_storey_range(value text, OUT lo integer, OUT hi integer)
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    m text[] := regexp_match(staging.norm_text(value), '^(\d+) TO (\d+)$');
BEGIN
    IF m IS NULL OR m[1]::int > m[2]::int THEN
        RAISE EXCEPTION 'unrecognised storey_range value: %', value;
    END IF;
    lo := m[1]::int;
    hi := m[2]::int;
END
$$;

-- Remaining lease as reported, in months. Shapes seen in the source:
--   '61 years 04 months', '61 years 01 month', '66 years 0 months', '61 years'  (2017+)
--   '70'  (2015-2016: whole years)
-- NULL or empty (pre-2015 files) returns NULL; the caller computes it from the lease start.
CREATE OR REPLACE FUNCTION staging.parse_remaining_lease_months(value text) RETURNS integer
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    v text := staging.norm_text(value);
    m text[];
BEGIN
    IF v IS NULL THEN
        RETURN NULL;
    END IF;
    m := regexp_match(v, '^(\d+) years?(?: (\d+) months?)?$');
    IF m IS NOT NULL THEN
        RETURN m[1]::int * 12 + coalesce(m[2]::int, 0);
    END IF;
    m := regexp_match(v, '^(\d+)$');
    IF m IS NOT NULL THEN
        RETURN m[1]::int * 12;
    END IF;
    RAISE EXCEPTION 'unrecognised remaining_lease value: %', value;
END
$$;

-- GeoJSON geometry (WGS84) -> 2D geometry in SVY21 (EPSG:3414).
CREATE OR REPLACE FUNCTION staging.geojson_to_svy21(value text) RETURNS geometry
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT ST_Transform(ST_SetSRID(ST_Force2D(ST_GeomFromGeoJSON(value)), 4326), 3414)
$$;

-- Polygonal GeoJSON -> valid MultiPolygon in SVY21.
CREATE OR REPLACE FUNCTION staging.geojson_to_svy21_multipolygon(value text) RETURNS geometry
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT ST_Multi(ST_CollectionExtract(ST_MakeValid(staging.geojson_to_svy21(value)), 3))
$$;
