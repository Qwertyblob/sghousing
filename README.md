# sghousing: Singapore HDB resale price explorer and predictor

A local data platform and static website for Singapore HDB resale flat prices. The pipeline
downloads official data, cleans and enriches it in Postgres + PostGIS, trains a price model
with an uncertainty range, and exports everything to a static site that runs predictions in
the browser.

> Status: **Phase 4 (spatial features)**. Later pipeline stages are stubs until their phase lands.

<!-- Screenshot: added in Phase 9 -->

## Architecture

<!-- Diagram: added as the pipeline takes shape -->

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Compose v2)
- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 for you)
- Node 22 LTS (for the site, Phase 9)
- GNU Make (macOS ships 3.81, which is fine; Windows: use WSL2)

### Apple Silicon

The database image `postgis/postgis:16-3.4` is published for amd64 only, so Compose pins
`platform: linux/amd64`. For acceptable speed, enable **Docker Desktop → Settings → General →
"Use Rosetta for x86_64/amd64 emulation on Apple Silicon"**. Without it Docker falls back to
QEMU, which works but is noticeably slower for spatial queries.

## Quickstart

```bash
git clone git@github.com:qwertyblob/sghousing.git
cd sghousing
make setup      # Python 3.12 + locked deps, .env, git hooks
make db-up      # Postgres/PostGIS on 127.0.0.1:5432, schemas raw/staging/marts
make db-check   # verify Python can reach the database
make test
```

### Changing the database port

If port 5432 is taken (e.g. by Postgres.app), set **both** in `.env`:

```
POSTGRES_PORT=55432
DATABASE_URL=postgresql+psycopg://sg:sg@localhost:55432/sg_housing
```

Settings validation refuses to start if the two disagree.

<!-- Full-pipeline and snapshot routes: added in Phase 10 -->

## Make targets

Run `make help` for the current list.

| Target | What it does |
|---|---|
| `setup` | Install Python 3.12 and locked dependencies, create `.env`, install git hooks |
| `db-up` / `db-down` | Start (and create schemas) / stop the database |
| `db-shell` | psql inside the container |
| `db-reset` | Destroy the database volume and recreate it empty |
| `db-check` | Connect from Python via `DATABASE_URL`, check PostGIS and schemas |
| `test` / `test-db` | Unit tests / database integration tests |
| `lint` | Ruff lint and format check |
| `ingest` … `export` | Pipeline stages (Phases 1–8) |
| `site-dev`, `site-build`, `publish` | Website (Phases 9–10) |
| `snapshot`, `restore-snapshot` | Parquet snapshot of marts tables (Phase 10) |
| `all` | Ingest through export, in order |
| `clean` | Remove caches and build output (keeps data and DB) |

## Ingestion

`make ingest` downloads every dataset in data.gov.sg collection 189 (HDB resale prices; the
dataset list is read from the collection metadata on each run) plus the reference datasets in
`pipeline/ingest/reference.py`, and loads them into `raw` tables with every source value kept
as text. Re-running is safe: unchanged files are skipped by SHA-256 and a changed file replaces
that dataset's rows in a single transaction.

- **API key (optional).** Set `DATAGOV_API_KEY` in `.env` for higher rate limits. It is sent
  only to the data.gov.sg API hosts, never to the storage URLs files are downloaded from.
- **Rate limits.** data.gov.sg's own pages disagree (5 requests/minute vs 2 per 10 seconds
  without a key), so the client adapts instead: it retries 429/5xx responses, honouring
  `Retry-After`, with exponential backoff.
- **Snapshots.** Files land in `data/raw/{source}/{dataset_id}/` as
  `{YYYY-MM-DD}_{sha256}.{csv|geojson}` with a `.meta.json` sidecar, and are never overwritten.
  `current.json` names the snapshot in use. A dataset already retrieved today is reused without
  downloading; `uv run python -m pipeline.ingest --force-download` fetches again.
- `--only resale|reference|<key>` limits a run to part of the data.

### Adding a new reference dataset

1. Find the dataset on data.gov.sg and confirm its ID with
   `curl https://api-production.data.gov.sg/v2/public/api/datasets/<id>/metadata`.
2. Add a `ReferenceDataset(key, dataset_id, description)` entry to `REFERENCE_DATASETS` in
   `pipeline/ingest/reference.py`, with a comment linking to
   `https://data.gov.sg/datasets/<id>/view`.
3. Run `make ingest`. CSVs load as text columns and GeoJSON as `properties jsonb` plus
   `geometry_geojson text`, into `raw.ref_<key>`.

## Staging transforms

`make transform` runs the numbered SQL files in `pipeline/sql/` (`0*` then `1*`), turning
`raw` text tables into typed `staging` tables. Every table is dropped and rebuilt, so
re-running gives the same result. Parsers are strict: a value in a shape not seen before stops
the build with an error instead of becoming NULL.

`staging.hdb_resale` has one row per raw row (no de-duplication, because the source has no
unit numbers, so identical-looking rows can be separate sales):

- `txn_id` is `md5(source_dataset_id:source_row_number)`.
- `date_basis` is `approval` before March 2012 and `registration` from then on, matching
  HDB's change in what the month means.
- `remaining_lease_months` is the reported value where the source has one (2015 onwards).
  Otherwise it is computed as 99 years minus the months since January of the lease start year.
  `remaining_lease_basis` records which.
- `flat_type` merges `MULTI GENERATION` into `MULTI-GENERATION`. `flat_model` uses one
  spelling per model, the mixed-case form where one exists (`MODEL A` becomes `Model A`).
- `address_key` is `BLOCK STREET_NAME`, uppercased with whitespace collapsed, and matches
  `staging.hdb_block_info.address_key`.

Reference tables (`mrt_exits`, `planning_areas`, `hawker_centres`, `parks`, `schools`,
`hdb_block_info`, `resale_price_index`) store geometry in SVY21 (EPSG:3414, metres) with GiST
indexes. Schools have no coordinates in the source; they are geocoded in Phase 3.

## Geocoding

`make geocode` looks up every HDB block (`address_key`) and every school postal code with the
[OneMap search API](https://www.onemap.gov.sg/apidocs/) and caches the results in
`staging.geocode_cache`. The cache is never dropped by `make transform` or `make geocode`.

- **Credentials (required).** OneMap needs an API token. Register at
  <https://www.onemap.gov.sg/apidocs/register>, then set `ONEMAP_EMAIL` and `ONEMAP_PASSWORD`
  in `.env`. The token is fetched and refreshed automatically and is never logged.
- **Matching.** The first query is `"{block} {street_name}"`. A result is accepted when its
  `BLK_NO` equals the block and its road name equals the street once abbreviations are
  expanded (`AVE` becomes `AVENUE`, `BT` becomes `BUKIT`, and so on; see `ABBREVIATIONS` in
  `pipeline/geocode/onemap.py`). If nothing matches, a second query with the abbreviations
  expanded is tried (status `fallback`). Schools are matched on postal code.
- **Demolished blocks.** Blocks OneMap no longer knows (mostly redeveloped estates) are placed
  at the centroid of the located blocks on the same street (status `approx`). This is
  recomputed every run. Blocks on streets with nothing located stay `failed`.
- **Overrides.** Rows in `data/manual/geocode_overrides.csv`
  (`address_key,lat,lon,postal,note`) are applied last with status `override`, and those
  addresses are never queried.
- **Resumable and rate limited.** Requests are spaced to `GEOCODE_REQUESTS_PER_SECOND`, and
  results are committed every 100 addresses. A stopped run resumes where it left off, and a
  fully cached run makes no API calls. `failed` and `approx` addresses are not queried again
  automatically; use `uv run python -m pipeline.geocode --retry-failed`, or add an override.
- **Failures** (`approx` and `failed`) are written to `reports/geocode_failures.csv`
  (not committed).

## Spatial features

`make features` runs `pipeline/sql/2*.sql` to build three tables in `marts`:

- `marts.block_generations`: one row per building generation at an address (see "Block
  facts" below), with `block_max_floor`, `block_year_completed` and `block_info_source`.

- `marts.block_features`: one row per located block.
  - Columns: `lat`, `lon`, `planning_area`, `region`, `dist_mrt_m`, `nearest_mrt_name`,
    `n_mrt_exits_500m`, `n_primary_schools_1km`, `dist_nearest_primary_m`, `dist_hawker_m`,
    `dist_park_m`, `dist_cbd_m` (to Raffles Place MRT), `block_max_floor` and
    `block_year_completed`.
  - It also carries `geocode_status` (`approx` means a street-centroid location) and
    `block_info_source`.
  - Every column is NOT NULL, so a missing feature fails the build.
- `marts.resale_features`: one row per transaction. It has the staging columns plus the block
  features, `block_generation`, `month_index` (months since January 1990) and `rel_storey`
  (storey over the block's top floor, capped at 1). Block facts and `rel_storey` come from the
  sale's own building generation, so they are filled for every sale, including the few on
  blocks that could not be located.

Choices worth knowing:

- **MRT only.** MRT distances and counts use exits that lead into an MRT station. At the
  Punggol, Choa Chu Kang and Bukit Panjang interchanges, LTA labels some of these exits with
  the LRT station's name, and they are included. Stand-alone LRT stations are not.
- **Hawker centres.** Those still under construction are excluded.
- **Parks.** Distances are to the park edge, so a block inside a park gets 0.
- **Block facts.** A demolished block's number is sometimes reused for a new building: 21
  Teban Gardens Road was a 15-storey block from 1978 and is now a 40-storey block completed
  in 2012. Sales at an address are grouped into generations by lease start year (a jump of
  more than 3 years starts a new one). HDB property information describes today's building,
  so it is used only for the latest generation, and only if it is residential and completed no
  later than that generation's first resale. Otherwise the highest storey sold and the
  earliest lease start year are used (`block_info_source = 'transactions'`).
- **Amenities are today's.** A station or hawker centre that opened in 2020 also counts for a
  1995 sale.

`uv run python -m pipeline.geocode.spot_check` compares five blocks' stored `dist_mrt_m` and
`nearest_mrt_name` with OneMap's nearest-MRT service. A block passes when the station matches
and the distances differ by at most 75 m. OneMap measures to a single station
point, which lies up to about 60 m from the exits these features use. It needs OneMap
credentials.

## Data sources and licences

Data from [data.gov.sg](https://data.gov.sg) is used under the
[Singapore Open Data Licence](https://data.gov.sg/open-data-licence). Each source is credited
on the site.

The resale data excludes non-market transactions such as sales between relatives. Prices are
indicative only.

## Model results

<!-- Phase 7: summary + link to model card -->

## Limitations

- No supermarket proximity feature: data.gov.sg has no current supermarket location dataset
  (the only candidate is a June 2016 licence list without coordinates).
- Before 2012 the lease start is a year only and the month is the approval date, so 51 sales
  are dated before their lease start year. Their flat age is floored at 0 and their remaining
  lease capped at 99 years.
- About 1.3% of resale transactions, all before 2015, are on demolished blocks. 125 blocks
  (11,763 sales) have street-centroid locations (status `approx`). 35 blocks (1,865 sales)
  could not be located at all. Every transaction from 2015 onwards has an exact location.
- `txn_id` depends on row order in the source files. If HDB re-issues a file with rows inserted
  mid-file, IDs for later rows in that file change.

<!-- Phase 7: model limitations -->

## Licence

MIT, see [LICENSE](LICENSE).
