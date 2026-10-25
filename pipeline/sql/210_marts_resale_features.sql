-- One row per resale transaction: staging columns plus the block's features.
--
-- Transactions on blocks that could not be located (geocode status `failed`, all before 2015)
-- are kept, with NULL block features and NULL geocode_status. Leak-free rolling features are
-- added in Phase 7.

DROP TABLE IF EXISTS marts.resale_features;

CREATE TABLE marts.resale_features AS
SELECT
    r.*,
    -- Months since January 1990.
    (extract(year FROM r.month)::integer - 1990) * 12
        + extract(month FROM r.month)::integer - 1 AS month_index,
    bf.geocode_status,
    bf.lat,
    bf.lon,
    bf.planning_area,
    bf.region,
    bf.dist_mrt_m,
    bf.nearest_mrt_name,
    bf.n_mrt_exits_500m,
    bf.n_primary_schools_1km,
    bf.dist_nearest_primary_m,
    bf.dist_hawker_m,
    bf.dist_park_m,
    bf.dist_cbd_m,
    bf.block_max_floor,
    bf.block_year_completed,
    bf.block_info_source,
    -- storey_mid is the middle of a range (e.g. 5 for "04 TO 06"), which can exceed a
    -- low block's top floor; the flat is on that floor at most, so cap it there.
    round(least(r.storey_mid, bf.block_max_floor)::numeric / bf.block_max_floor, 3) AS rel_storey
FROM staging.hdb_resale AS r
LEFT JOIN marts.block_features AS bf USING (address_key);

ALTER TABLE marts.resale_features
    ADD PRIMARY KEY (txn_id),
    ALTER COLUMN month_index SET NOT NULL;

CREATE INDEX resale_features_month_idx ON marts.resale_features (month);
CREATE INDEX resale_features_town_idx ON marts.resale_features (town);
CREATE INDEX resale_features_address_key_idx ON marts.resale_features (address_key);
