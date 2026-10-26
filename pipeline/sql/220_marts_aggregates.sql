-- Summary tables for the site (exported by pipeline/export/site_data.py).
--
--   agg_town_month:    median price per sqm, median price and sales by town, flat type and
--                      month, every month since 1990, with 'ALL' rollups over towns, flat
--                      types or both
--   agg_planning_area: per planning area, the median price per sqm of the latest 12 months
--                      of data, of the 12 months before, and the change between them (NULL
--                      when either window has fewer than 20 sales); every planning area is
--                      listed, those without HDB resales with NULL statistics
--   lease_decay_curve: price per sqm by whole years of remaining lease and flat type (and
--                      'ALL'), over the latest 5 years of data; median_rel_psm is relative to
--                      the median of the same town, flat type and year, which removes
--                      location and market level as in the EDA (pipeline/eda/data.py)

DROP TABLE IF EXISTS marts.agg_town_month;

CREATE TABLE marts.agg_town_month AS
SELECT
    coalesce(town, 'ALL') AS town,
    coalesce(flat_type, 'ALL') AS flat_type,
    month,
    count(*)::integer AS sales,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm)::numeric, 2) AS median_psm,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY resale_price)::numeric) AS median_price
FROM staging.hdb_resale
GROUP BY GROUPING SETS ((town, flat_type, month), (town, month), (flat_type, month), (month));

ALTER TABLE marts.agg_town_month ADD PRIMARY KEY (town, flat_type, month);
CREATE INDEX agg_town_month_month_idx ON marts.agg_town_month (month);


DROP TABLE IF EXISTS marts.agg_planning_area;

CREATE TABLE marts.agg_planning_area AS
WITH bounds AS (
    SELECT max(month) AS last_month FROM marts.resale_features
),
windows AS (
    SELECT f.planning_area, f.price_psm,
           CASE WHEN f.month > b.last_month - interval '12 months' THEN 'latest'
                ELSE 'previous' END AS period
    FROM marts.resale_features AS f
    CROSS JOIN bounds AS b
    WHERE f.month > b.last_month - interval '24 months'
      AND f.planning_area IS NOT NULL
),
stats AS (
    SELECT planning_area,
           count(*) FILTER (WHERE period = 'latest')::integer AS sales_12m,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm)
               FILTER (WHERE period = 'latest') AS median_latest,
           count(*) FILTER (WHERE period = 'previous')::integer AS sales_prev_12m,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm)
               FILTER (WHERE period = 'previous') AS median_previous
    FROM windows
    GROUP BY planning_area
)
SELECT
    p.planning_area,
    p.region,
    coalesce(s.sales_12m, 0) AS sales_12m,
    round(s.median_latest::numeric, 2) AS median_psm_12m,
    coalesce(s.sales_prev_12m, 0) AS sales_prev_12m,
    round(s.median_previous::numeric, 2) AS median_psm_prev_12m,
    CASE WHEN s.sales_12m >= 20 AND s.sales_prev_12m >= 20
         THEN round((s.median_latest / s.median_previous - 1)::numeric, 4) END AS yoy_change,
    (SELECT last_month FROM bounds) AS last_month
FROM staging.planning_areas AS p
LEFT JOIN stats AS s USING (planning_area);

ALTER TABLE marts.agg_planning_area ADD PRIMARY KEY (planning_area);


DROP TABLE IF EXISTS marts.lease_decay_curve;

CREATE TABLE marts.lease_decay_curve AS
WITH recent AS (
    SELECT f.*, extract(year FROM f.month)::integer AS year
    FROM marts.resale_features AS f
    WHERE f.month > (SELECT max(month) FROM marts.resale_features) - interval '60 months'
),
medians AS (
    SELECT town, flat_type, year,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm) AS median_psm
    FROM recent
    GROUP BY 1, 2, 3
),
relative AS (
    SELECT r.flat_type, r.remaining_lease_months / 12 AS lease_years, r.price_psm,
           r.price_psm / m.median_psm AS rel_psm
    FROM recent AS r
    JOIN medians AS m USING (town, flat_type, year)
)
SELECT
    coalesce(flat_type, 'ALL') AS flat_type,
    lease_years,
    count(*)::integer AS sales,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY price_psm)::numeric, 2) AS median_psm,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY rel_psm)::numeric, 4) AS median_rel_psm
FROM relative
GROUP BY GROUPING SETS ((flat_type, lease_years), (lease_years));

ALTER TABLE marts.lease_decay_curve ADD PRIMARY KEY (flat_type, lease_years);
