# sghousing: Singapore HDB resale price explorer and predictor

A local data platform and static website for Singapore HDB resale flat prices. The pipeline
downloads official data, cleans and enriches it in Postgres + PostGIS, trains a price model
with an uncertainty range, and exports everything to a static site that runs predictions in
the browser.

> Status: **Phase 2 (staging transforms)**. Later pipeline stages are stubs until their phase
> lands.

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
- `txn_id` depends on row order in the source files. If HDB re-issues a file with rows inserted
  mid-file, IDs for later rows in that file change.

<!-- Phase 7: model limitations -->

## Licence

MIT, see [LICENSE](LICENSE).
