# Search Pulse

[![CI](https://github.com/osamaabdou26-lab/Agentic-dashboard-/actions/workflows/ci.yml/badge.svg)](https://github.com/osamaabdou26-lab/Agentic-dashboard-/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue)](.python-version)

Search-quality intelligence for the Spinneys Egypt grocery catalogue. It surfaces
failing searches, mines synonym and misspelling fixes for human review, and ships
an agent that answers questions about search performance and writes a weekly
digest.

Built against the real dataset: 66 logged searches and 25,881 products with
Arabic and English names. A generator ships alongside it so the project runs
without that dump; generated data is labelled as such everywhere it appears.

Requires Python 3.11 or newer. No database server, no Node toolchain, and no
API key: the agent falls back to a local planner when no key is configured.

```bash
pip install -e ".[dev]"
searchiq sample-data    # generate a mock dataset and load it (~5s)
searchiq discover       # find synonyms and misspellings
searchiq serve          # dashboard on http://127.0.0.1:8000
```

Use `searchiq etl` instead of `sample-data` if you have the 828 MB dump.

## What the real data showed

| Shopper typed | Search returned | Should have returned |
|---|---|---|
| `حليبن` (mistyped "milk") | tea sachets, chicken cordon bleu, a beef burger | one of 322 milk products |
| `فاراولت` (mistyped "strawberry") | Agnesi Faralle Pasta, nothing else | one of 308 strawberry products |
| `فاكهة` ("fruit") | a kitchen knife, a serving platter, a guava, sugar | fruit |

Three findings drove the design:

**Zero-result rate is useless here.** None of the 66 searches returned nothing.
The engine is embedding-based and always returns its five nearest neighbours, so
failures look like *wrong* results, never empty ones. A dashboard built on zero
results would have scored this log as perfect.

**One problem can wear six masks.** `فراولة`, `فاراولة`, `فاراولت`, `فراولت`,
`فراوله` and `فراول` are six rows in a query report and one word in the shop.
Ranked individually none looks urgent; rolled up to the term, it is the largest
single source of failed searches in the log.

**There is no engagement data.** No clicks, no add-to-cart, no orders. Rather
than invent a number, the system measures what shoppers did next and says so
wherever it reports it.

## Engagement

A shopper who finds what they wanted stops searching; one who does not tries
again. That is the only signal the log supports, so it is the one used:

- Followed by a repeat or rewording: the result set did not satisfy.
- Nothing followed: ambiguous, and deliberately not counted. A session ending
  could mean checkout or surrender, and the log cannot tell them apart.
- Re-fired within two seconds with identical results: machine traffic. Flagged
  and excluded from engagement rather than deleted, since which queries attract
  it is worth knowing. It is 7.6% of the shipped log.

`search_event.clicked_rank` and `.converted` already exist in the schema. A
deployment that populates them gets measured engagement with no schema change,
and these proxies become a cross-check instead of a substitute.

## How a search is judged

Since zero-result rate is blind here, each result set is scored on five
independent signals:

| Signal | Question |
|---|---|
| Lexical miss | Does any returned product mention what was asked for? |
| Coherence | Do the results belong together, or span unrelated parts of the shop? |
| Under-fill | Did a top-5 engine fill five slots? |
| Instability | Does the same query return different products on different runs? |
| Dissatisfaction | Did the shopper visibly react? |

These blend into a severity score from 0 to 1. A signal that could not be
measured is dropped and the remaining weights renormalised, so a query with no
coherence reading is never credited with perfect coherence.

*Retrieval gap* is the strongest finding available, because it names a fix: the
catalogue stocks the term and search failed to surface it. It is evaluated
against the intended term, not the typed one. A shopper who types `حليبن` has
still asked for milk.

## Interfaces

Three front ends over one engine. None of them reimplements a metric.

**FastAPI dashboard** (`searchiq serve`) at `http://127.0.0.1:8000`, with API
docs at `/docs`. One process serves both, with no frontend build step. Tabs:
Overview (health score, KPIs, worst queries by impact), Queries (per-query
metrics and the products actually returned), Terms (spellings folded onto one
row with a verdict naming who owns the fix), Review queue, Ask, Digest.

Arabic and English render side by side, with every embedded term bidi-isolated
so a right-to-left word cannot reorder the English around it. Severity colours
are always paired with a text label, so the dashboard reads in greyscale and to
colour-blind users.

**Streamlit app** (`streamlit run web/app.py`) is the deployable front end, with
Plotly charts and a Gemini-backed agent. It imports the same `searchiq` modules
through `sys.path` rather than an editable install, so `web/` keeps its own small
`requirements.txt`. If the analytics store is missing it builds a sample one on
first boot, which is what makes it deployable to a host with no terminal.

**CLI** for everything else, including scheduling the digest from cron or Task
Scheduler.

## Discovery: proposed, never applied

`searchiq discover` populates a review queue. Nothing reaches live search. A
proposal enters as `pending`, a person accepts or rejects it, and the approved
set leaves as a config file someone deploys deliberately.

```bash
searchiq suggestions            # read the queue
searchiq approve 3
searchiq reject 4 --note "brand name, not a synonym"
searchiq reopen 3               # undo; back to the queue, out of the export
searchiq export -o rules.json
```

The same decisions are available in the dashboard, where each proposal carries
its evidence, a free-text reason recorded with the decision, and an undo. A
reviewer who cannot take a decision back hesitates over every borderline case,
and the queue stops moving.

Re-running discovery never overrules a person. It re-scores what is still
pending and leaves decided proposals alone.

**Misspellings** must clear three bars: the typed term appears in no product
name, something within a length-appropriate edit distance does, and that target
is used by enough products to be a real word. Direction comes from the
catalogue, not frequency. `حليب` is correct because 322 products use it,
regardless of which spelling is typed more often. A shopper who had not finished
typing (`pas` into `pasta`) is classified as a partial query instead, because
rewriting their search would be wrong.

**Synonyms** come from four signals, and the weakest cannot act alone.
Consecutive searches look identical whether a shopper reworded one intent or
moved to the next item, so a rewording scores below threshold by itself. The
signal that usually corroborates it is cross-language catalogue alignment: every
product carries an Arabic and an English name, so mining co-occurrence across
25,881 products induces a bilingual lexicon with no dictionary.

```
حليب ↔ milk (293 products)      فراوله ↔ strawberry (276)
مكرونه ↔ pasta (323)             فاكهه ↔ fruit (39)
```

Two terms that translate to the same English word are synonyms of each other,
which is how `لبن ≡ حليب` is established rather than assumed.

## The agent

```bash
searchiq ask "which queries are wasting the most traffic?"
searchiq ask "do we actually sell strawberries?" --trace
searchiq tools
searchiq digest --days 7 -o digest.md
```

The agent answers only through the tools in `agent/tools.py`. It never writes SQL
and never sees the database, so it cannot invent a metric or read a table it has
no business reading. Every answer returns the exact sequence of tools it called.

Eight tools are bound: search health, problem queries, one query in detail, the
review queue, catalogue coverage, period-over-period comparison, recent raw
searches, and a description of the loaded dataset. `searchiq tools` and
`GET /api/agent/tools` print the registry exactly as the model receives it.

**It works without an API key.** The CLI and FastAPI paths use `ANTHROPIC_API_KEY`
when set and otherwise fall back to a deterministic planner that routes questions
by intent. The Streamlit app uses `GEMINI_API_KEY`, and falls back to the same
planner when Gemini is unavailable or out of quota. Wording is plainer on the
offline path; the numbers are identical, because both read through the same
tools. Which path answered is always reported.

The digest covers performance against the previous period, empty searches split
into retrieval gaps (stocked, not surfaced) and assortment gaps (nobody stocks
it), queries needing attention, and everything pending review. Every figure is
computed from the store; the model is handed those figures and asked only to
write the opening paragraph, so it is never in a position to produce a number.
Periods anchor to the newest search in the log rather than today, so a digest run
against a historical extract describes that extract.

## Architecture

```
source dump ──▶ ingest ──▶ analytics store ──┬──▶ analytics ──┬──▶ API ──▶ dashboard
(828 MB SQL)    (stream)    (SQLite)         │                │
                                             ├──▶ discovery ──┤
                                             │   (review queue)
                                             └──▶ agent ──────┴──▶ CLI
                                                 (tools · digest)
```

| Module | Responsibility |
|---|---|
| `ingest/` | Streaming `mysqldump` reader, store loader, mock-data generator |
| `store/` | SQLite schema and connection handling |
| `text/` | Arabic normalisation, light stemming, edit distance, bidi isolation |
| `analytics/` | Catalogue index, per-search quality, session behaviour, metrics, term rollup |
| `discovery/` | Misspelling and synonym discovery, review queue |
| `agent/` | Tool registry, agent loop, offline planner, digest |
| `api/` | FastAPI app serving the dashboard and the API |
| `reporting/` | Flat tables for Power BI over CSV and JSON |
| `web/` | Vanilla-JS dashboard (no build step) and the Streamlit app |

**Why the dump is read directly.** Only about 12 MB of the 828 MB source is
relevant to search quality; the rest is embeddings, stock levels and price
history. Streaming it means no MySQL server to install, no long import, and no
credentials to handle. A live-MySQL path exists (`searchiq etl --source mysql`)
for deployments that already run the server, with credentials read from the
environment and never persisted.

**Why a separate analytics store.** Analysis never touches production tables, the
store rebuilds from one command in about 30 seconds, and reviewer decisions
survive the rebuild.

## Commands

| Command | Purpose |
|---|---|
| `searchiq sample-data [--days N --seed N]` | Generate mock query logs and load them |
| `searchiq etl [--source dump\|mysql]` | Build the analytics store |
| `searchiq report [--since --until]` | Print a search-quality summary |
| `searchiq discover` | Queue synonyms and misspellings for review |
| `searchiq suggestions [--status --kind]` | Read the review queue |
| `searchiq approve <id>` / `reject <id>` | Record a decision |
| `searchiq reopen <id>` | Undo a decision |
| `searchiq export [-o FILE]` | Emit approved rules as deployable JSON |
| `searchiq ask "..." [--trace]` | Ask about search performance |
| `searchiq tools` | List the tools the agent is bound to |
| `searchiq digest [--days N] [-o FILE]` | Generate the period digest |
| `searchiq serve [--port N]` | Run the dashboard and API |
| `searchiq export-bi [-o DIR]` | Write flat CSV tables for Power BI |
| `searchiq export-site [-o DIR]` | Write a static, publishable copy of the dashboard |
| `searchiq status` | Configuration and load provenance |

## HTTP API

```
GET  /api/status · /api/overview · /api/queries · /api/queries/{q} · /api/terms
     /api/catalogue/{term} · /api/suggestions · /api/suggestions/summary
     /api/suggestions/export · /api/digest[?days=N&format=json|markdown]
     /api/agent/tools · /api/bi/tables · /api/bi/{table}
POST /api/ask · /api/suggestions/refresh
     /api/suggestions/{id}/{approved|rejected|pending}
```

Read endpoints open the store read-only. The writing endpoints are the
deliberate exceptions: recording a decision, re-running discovery, storing a
generated digest. Interactive docs at `/docs`.

## Power BI

```bash
searchiq export-bi                                     # CSVs into ./bi
searchiq export-bi --base-url http://127.0.0.1:8000    # also writes a .pbids file
```

Six flat tables (`fact_search`, `dim_query`, `dim_term`, `fact_daily`,
`fact_suggestion`, `dim_overview`) load without Power Query reshaping. Use
**Get Data → Folder** for the export, or **Get Data → Web** against
`/api/bi/<table>` for a live connection.

Every figure comes from the same functions the dashboard and agent use, and the
suite asserts exported scores equal the analytics layer's row for row. Do not
recompute `health_score`, `severity` or `impact` in DAX: severity blends five
signals with renormalised weights, and health is the severity-weighted mean
across searches, not across queries. Raw counts are additive and safe to
aggregate.

CSVs are UTF-8 with BOM and booleans are emitted as 0/1, so Arabic names and
boolean columns survive Excel's type inference.

## Deployment

**Static snapshot.** `searchiq export-site -o site` writes a folder that any
static host will serve, with every API response precomputed to `data/*.json`.
Approvals, the agent and re-running discovery need a process, so the exported
page removes those controls rather than showing dead ones.
`python build_standalone.py site out.html` folds the whole thing into a single
self-contained file.

**Streamlit app on a host.** The repo carries a `Procfile`, a root
`requirements.txt` and a `.python-version` pin, which is enough for Railway,
Render or Fly to build and run it. Set `GEMINI_API_KEY` as a service variable.
The pin is load-bearing: builders that default to a newer Python can fail
outright when a dependency has no wheel for it yet.
The container filesystem is usually ephemeral, so the app seeds a sample store on
each cold start and review decisions reset with it; mount a volume at the data
directory if they need to persist.

**Docker (FastAPI dashboard + API).** The `Dockerfile` builds the `searchiq`
package into a slim, non-root runtime image and runs `searchiq serve`. Same
ephemeral-storage caveat as above: `docker-entrypoint.sh` seeds a sample store
on first boot if `SEARCHIQ_DB` doesn't already point at one, so the container
runs with zero configuration.

```bash
docker build -t search-pulse .
docker run --rm -p 8000:8000 --env-file .env search-pulse
```

Mount a volume at `/app/data` to persist the analytics store (and review
decisions) across restarts, or point `SEARCHIQ_DB` at a file on a mounted
volume. `docker compose` works the same way with a single service plus a
named volume.

## Configuration

Copy `.env.example` to `.env`. Every value has a working default, so an empty
`.env` still produces a working system. Secrets are read from the environment and
never persisted.

| Setting | Default | Purpose |
|---|---|---|
| `SEARCHIQ_DB` | `data/searchiq.db` | Analytics store |
| `SEARCHIQ_DUMP_PATH` | see `.env.example` | Source SQL dump |
| `SEARCHIQ_SAMPLE_DUMP_PATH` | `data/sample_dump.sql` | Where `sample-data` writes |
| `SEARCHIQ_RESULT_CAP` | `5` | Results the engine returns; sets the under-fill baseline |
| `SEARCHIQ_SESSION_GAP_SECONDS` | `1800` | Gap that starts a new session |
| `SEARCHIQ_MODEL` | `claude-opus-5` | Model for the CLI and API agent |
| `ANTHROPIC_API_KEY` | unset | Optional; without it the offline planner answers |
| `GEMINI_MODEL` | `gemini-3.6-flash` | Model for the Streamlit agent |
| `GEMINI_API_KEY` | unset | Optional; same fallback applies |

Both model settings are API identifiers, not paths. No weights are downloaded and
nothing model-shaped lives on disk.

## Tests

```bash
pytest tests/          # 289 tests, about ten seconds
ruff check src tests
```

CI (`.github/workflows/ci.yml`) runs both on every push and pull request against
`main`, then builds the Docker image as a third check.

No API key, no network, no database server. The suite never touches the 828 MB
dump: it builds a small one reproducing the structural features that matter
(extended inserts, backslash escapes, embedded newlines, SQL `NULL` beside the
literal string `'NULL'`, Arabic text, real misspelling patterns) and runs the
genuine ETL over it.

Much of the suite asserts what the system refuses to do: that a rewording alone
cannot propose a synonym, that two failing queries returning the same wrong
product are not synonyms, that a machine-speed repeat is not a frustrated
shopper, and that re-running discovery never overrules a person.

## Limitations

- **The log is one QA session.** All 66 searches fall on 2025-01-09 within 4.5
  hours, and 7.6% look automated. Rates are computed honestly but rest on a
  small, unrepresentative sample. The digest's period-over-period table appears
  only once a second period exists.
- **Sessions are derived, not recorded.** The source has no session or user id,
  so sessions are inferred from gaps. This reconstructs journeys well on
  single-tenant traffic and would interleave them on multi-user traffic. No
  metric depends on session identity alone.
- **Category coherence depends on catalogue hygiene.** Merchandising buckets
  ("Top Selling Products", "Default Category") are excluded by an
  inverse-frequency filter rather than a curated list.
- **The generated dataset is a demonstration, not evidence.** Its patterns are
  planted on purpose. Finding them proves the pipeline runs end to end and proves
  nothing about a real shop. Every claim above is from the real extract.
