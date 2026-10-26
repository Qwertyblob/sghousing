-- Leak-free rolling price features, one row per resale transaction.
--
-- Every window covers only months strictly before the sale's month, so nothing from the sale's
-- own month or later is used:
--   town_psm_{3,6,12}m: median price per sqm of the same town and flat type over the 3, 6 and
--                       12 months before the sale's month (months M-k .. M-1)
--   block_psm_24m:      median price per sqm at the same address over the 24 months before,
--                       NULL when fewer than 3 sales
--   block_sales_24m:    the number of those sales
-- Windows are pooled medians over individual sales, not medians of monthly medians.
-- tests/test_rolling_features.py checks that changing later prices leaves these unchanged.

DROP TABLE IF EXISTS marts.rolling_features;

CREATE TABLE marts.rolling_features AS
WITH group_months AS (
    SELECT DISTINCT town, flat_type, month FROM staging.hdb_resale
),
town_windows AS (
    SELECT g.town, g.flat_type, g.month,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY s.price_psm)
               FILTER (WHERE s.month >= g.month - interval '3 months') AS town_psm_3m,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY s.price_psm)
               FILTER (WHERE s.month >= g.month - interval '6 months') AS town_psm_6m,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY s.price_psm) AS town_psm_12m
    FROM group_months AS g
    LEFT JOIN staging.hdb_resale AS s
      ON s.town = g.town
     AND s.flat_type = g.flat_type
     AND s.month >= g.month - interval '12 months'
     AND s.month < g.month
    GROUP BY g.town, g.flat_type, g.month
),
block_months AS (
    SELECT DISTINCT address_key, month FROM staging.hdb_resale
),
block_windows AS (
    SELECT b.address_key, b.month,
           count(s.price_psm)::integer AS block_sales_24m,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY s.price_psm) AS block_psm_24m
    FROM block_months AS b
    LEFT JOIN staging.hdb_resale AS s
      ON s.address_key = b.address_key
     AND s.month >= b.month - interval '24 months'
     AND s.month < b.month
    GROUP BY b.address_key, b.month
)
SELECT
    r.txn_id,
    t.town_psm_3m,
    t.town_psm_6m,
    t.town_psm_12m,
    CASE WHEN b.block_sales_24m >= 3 THEN b.block_psm_24m END AS block_psm_24m,
    b.block_sales_24m
FROM staging.hdb_resale AS r
JOIN town_windows AS t USING (town, flat_type, month)
JOIN block_windows AS b USING (address_key, month);

ALTER TABLE marts.rolling_features
    ADD PRIMARY KEY (txn_id),
    ALTER COLUMN block_sales_24m SET NOT NULL;
