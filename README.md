# sghousing: Singapore HDB resale price explorer and predictor

A local data platform and static website for Singapore HDB resale flat prices. The pipeline
downloads official data, cleans and enriches it in Postgres + PostGIS, trains a price model
with an uncertainty range, and exports everything to a static site that runs predictions in
the browser.

> Status: **Phase 0 (setup)**. Pipeline stages are stubs until their phase lands.

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

## Adding a new reference dataset

<!-- Phase 1 -->

## Data sources and licences

Data from [data.gov.sg](https://data.gov.sg) is used under the
[Singapore Open Data Licence](https://data.gov.sg/open-data-licence). Each source is credited
on the site.

The resale data excludes non-market transactions such as sales between relatives. Prices are
indicative only.

## Model results

<!-- Phase 7: summary + link to model card -->

## Limitations

<!-- Phase 7 -->

## Licence

MIT, see [LICENSE](LICENSE).
