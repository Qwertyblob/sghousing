-- Fill staging.schools.geom from the geocode cache (keyed 'POSTAL {postal_code}').
-- Runs in `make transform` and again at the end of `make geocode`. A no-op until the cache
-- exists; schools without a cached location keep a NULL geom.
DO $$
BEGIN
    IF to_regclass('staging.geocode_cache') IS NOT NULL THEN
        -- Every row is set, so a location that has left the cache (failed on retry, or a
        -- removed override) is cleared rather than kept.
        UPDATE staging.schools AS s
        SET geom = (
            SELECT c.geom
            FROM staging.geocode_cache AS c
            WHERE c.address_key = 'POSTAL ' || s.postal_code
              AND c.status <> 'failed'
        );
    END IF;
END
$$;
