-- Typed, cleaned resale transactions: exactly one row per raw row.
--
-- No de-duplication: identical-looking rows can be separate sales, because the source does
-- not publish unit numbers. txn_id is derived from the source dataset and row number.
-- Required columns carry NOT NULL / CHECK constraints so bad data fails the build.

DROP TABLE IF EXISTS staging.hdb_resale;

CREATE TABLE staging.hdb_resale AS
WITH parsed AS (
    SELECT
        r.source_dataset_id,
        r.source_row_number,
        staging.parse_month(r.month) AS month,
        upper(staging.norm_text(r.town)) AS town,
        replace(upper(staging.norm_text(r.flat_type)), 'MULTI GENERATION', 'MULTI-GENERATION')
            AS flat_type,
        staging.norm_text(r.flat_model) AS flat_model_raw,
        upper(staging.norm_text(r.block)) AS block,
        upper(staging.norm_text(r.street_name)) AS street_name,
        s.lo AS storey_lo,
        s.hi AS storey_hi,
        staging.norm_text(r.floor_area_sqm)::numeric AS floor_area_sqm,
        staging.norm_text(r.lease_commence_date)::integer AS lease_commence_year,
        staging.parse_remaining_lease_months(r.remaining_lease) AS reported_lease_months,
        staging.norm_text(r.resale_price)::numeric AS resale_price
    FROM raw.hdb_resale AS r
    CROSS JOIN LATERAL staging.parse_storey_range(r.storey_range) AS s
),
dated AS (
    SELECT
        p.*,
        -- Months from January of the lease start year to the transaction month, floored at
        -- 0: a few pre-2012 sales are dated (approval date) before the lease start year.
        greatest(
            0,
            (extract(year FROM p.month)::integer - p.lease_commence_year) * 12
                + extract(month FROM p.month)::integer - 1
        ) AS months_since_lease_start
    FROM parsed AS p
),
model_spelling AS (
    -- One spelling per model: the most common mixed-case spelling (newer files), otherwise
    -- the most common spelling overall (keeps acronyms such as DBSS intact).
    SELECT
        upper(flat_model_raw) AS model_key,
        coalesce(
            mode() WITHIN GROUP (ORDER BY flat_model_raw)
                FILTER (WHERE flat_model_raw <> upper(flat_model_raw)),
            mode() WITHIN GROUP (ORDER BY flat_model_raw)
        ) AS flat_model
    FROM parsed
    GROUP BY 1
)
SELECT
    md5(d.source_dataset_id || ':' || d.source_row_number) AS txn_id,
    d.month,
    CASE WHEN d.month < DATE '2012-03-01' THEN 'approval' ELSE 'registration' END AS date_basis,
    d.town,
    d.flat_type,
    ms.flat_model,
    d.block,
    d.street_name,
    d.block || ' ' || d.street_name AS address_key,
    d.storey_lo,
    d.storey_hi,
    (d.storey_lo + d.storey_hi) / 2 AS storey_mid,
    d.floor_area_sqm,
    d.lease_commence_year,
    coalesce(d.reported_lease_months, 99 * 12 - d.months_since_lease_start)
        AS remaining_lease_months,
    CASE WHEN d.reported_lease_months IS NULL THEN 'computed' ELSE 'reported' END
        AS remaining_lease_basis,
    round(d.months_since_lease_start / 12.0, 2) AS flat_age_years,
    d.resale_price,
    round(d.resale_price / d.floor_area_sqm, 2) AS price_psm,
    d.source_dataset_id,
    d.source_row_number
FROM dated AS d
LEFT JOIN model_spelling AS ms ON ms.model_key = upper(d.flat_model_raw);

ALTER TABLE staging.hdb_resale
    ADD PRIMARY KEY (txn_id),
    ALTER COLUMN month SET NOT NULL,
    ALTER COLUMN date_basis SET NOT NULL,
    ALTER COLUMN town SET NOT NULL,
    ALTER COLUMN flat_type SET NOT NULL,
    ALTER COLUMN address_key SET NOT NULL,
    ALTER COLUMN floor_area_sqm SET NOT NULL,
    ALTER COLUMN resale_price SET NOT NULL,
    ALTER COLUMN remaining_lease_months SET NOT NULL,
    ADD CONSTRAINT hdb_resale_remaining_lease_range
        CHECK (remaining_lease_months BETWEEN 0 AND 1188),
    ADD CONSTRAINT hdb_resale_source_row_unique UNIQUE (source_dataset_id, source_row_number);

CREATE INDEX hdb_resale_month_idx ON staging.hdb_resale (month);
CREATE INDEX hdb_resale_town_idx ON staging.hdb_resale (town);
CREATE INDEX hdb_resale_address_key_idx ON staging.hdb_resale (address_key);
