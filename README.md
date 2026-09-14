# Search Pulse

Search-quality intelligence for the Spinneys Egypt grocery catalogue: a dashboard
that surfaces failing searches, automated synonym and misspelling discovery that
proposes fixes for human review, and an agent that answers questions about search
performance and writes a weekly digest.

Built against the real dataset — 66 logged searches and 25,881 products with
Arabic and English names — not a synthetic stand-in. It also ships a generator
for people who do not have that dump, so the whole thing runs out of the box;
every finding below is from the real data, and the generated data is labelled as
such wherever it appears.

```bash
pip install -e ".[dev]"
searchiq sample-data    # generate a mock dataset and load it (~5s)
searchiq discover       # find synonyms and misspellings
searchiq serve          # dashboard on http://127.0.0.1:8000
```

Swap the first line for `searchiq etl` when you have the real 828 MB dump.
Full setup, including running the tests and the digest, is under
[Getting started](#getting-started).

---

## What it found in the real data

These are outputs of the shipped code against `spinneys_database.sql`, not
illustrations.

| Shopper typed | Search returned | What it should have returned |
|---|---|---|
| `حليبن` (mistyped "milk") | tea sachets, chicken cordon bleu, a beef burger | one of 322 milk products |
| `فاراولت` (mistyped "strawberry") | Agnesi **Faralle Pasta**, and nothing else | one of 308 strawberry products |
| `فاكهة` ("fruit") | a **kitchen knife**, a serving platter, a guava, a bag of sugar | fruit |

Three findings shaped the whole design:

**Zero-result rate is blind here.** Not one search of the 66 returned nothing.
The engine is embedding-based and always hands back its five nearest neighbours,
so a failure looks like *wrong* results, never empty ones. A dashboard built
around "zero results" would have reported a perfect score on a log full of
failures.

**"Strawberry" is one problem wearing six masks.** `فراولة`, `فاراولة`,
`فاراولت`, `فراولت`, `فراوله` and `فراول` are six rows in a query report and one
word in the shop. Ranked as queries none looks urgent; rolled up to the term it
is the single largest source of failed searches in the log.

**There is no engagement data at all.** The source database records no clicks, no
add-to-cart, no orders. Rather than invent an engagement number, the system
measures what shoppers did next and says so everywhere it reports it. See
[Engagement](#engagement-what-the-data-actually-supports).

---

## Getting started

Python 3.11 or newer. Everything below runs from the project root.

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
```

Copy the configuration template. Every value has a working default, so this is
optional — do it when you want to point at your own data or enable the model.

```bash
copy .env.example .env          # macOS/Linux: cp .env.example .env
```

### 1. Get some data in

Pick whichever applies. All three build the same analytics store, and everything
downstream is identical afterwards.

```bash
searchiq sample-data            # no data to hand: generate a mock dataset (~5s)
searchiq etl                    # the real mysqldump extract (~30s)
searchiq etl --source mysql     # a live MySQL server (needs pip install -e ".[mysql]")
```

`searchiq sample-data` writes a mock `mysqldump` file to `data/sample_dump.sql`
and loads it through the ordinary ETL — the same reader and loader the real dump
goes through, so what you exercise is the production code path with mock input.
The generated log deliberately contains zero-result queries, misspellings with
the shopper's own retype, partial queries, low-engagement terms, machine-like
repeats, and a bilingual catalogue that the cross-language synonym miner can work
on. Generation is seeded, so the same command twice gives the same dashboard.

```bash
searchiq sample-data --days 28 --sessions-per-day 6 --seed 20250109
searchiq sample-data --no-load -o data/mine.sql   # write the file, load it later
```

Check what landed at any point:

```bash
searchiq status                 # configuration and load provenance
searchiq report                 # a search-quality summary in the terminal
```

### 2. Run the discovery pass

```bash
searchiq discover               # proposes synonyms and misspellings for review
searchiq suggestions            # read the queue
```

Nothing is applied. See [Synonym and misspelling
discovery](#2-synonym-and-misspelling-discovery-proposed-not-applied).

### 3. Launch the API and web UI

```bash
searchiq serve                          # http://127.0.0.1:8000
searchiq serve --port 9000 --reload     # another port, auto-reloading
```

The dashboard is at <http://127.0.0.1:8000> and interactive API documentation at
<http://127.0.0.1:8000/docs>. One process serves both; there is no separate
frontend build step, and no `npm install`.

The **Review queue** tab is where proposals are approved or rejected; the tab
carries a badge with the number still waiting. **Ask** is the agent, and
**Digest** generates the period summary and offers it as Markdown.

### 4. Generate the weekly digest

```bash
searchiq digest                         # last 7 days, to the console
searchiq digest --days 7 -o digest.md   # write it to a file
searchiq digest --days 30 --end 2025-01-09
```

Or over HTTP, which is the same generator:

```bash
curl "http://127.0.0.1:8000/api/digest?days=7"                    # JSON + metrics
curl -OJ "http://127.0.0.1:8000/api/digest?days=7&format=markdown"  # the document
```

The digest covers search-quality performance against the previous period, the
searches that came back empty — separated into retrieval gaps and assortment
gaps, because those have different owners — the queries needing attention, and
everything still pending review. Periods are anchored to the newest search in the
log, not to today, so a digest run against a historical extract describes that
extract.

To schedule it, run the CLI form from cron or Task Scheduler; it needs no server.

### 5. Run the tests

```bash
pytest tests/                   # ~240 tests, about ten seconds
pytest tests/ -q                # quieter
pytest tests/test_discovery.py  # one file
ruff check src tests            # lint
```

The suite never touches the 828 MB dump and needs no API key, no network and no
database server. It builds small dumps of its own and runs the genuine ETL over
them.

---

## The three deliverables

### 1. A dashboard surfacing search-quality problems

`searchiq serve`, then open <http://127.0.0.1:8000>.

- **Overview** — health score, the failure-rate KPIs, and the worst queries
  ranked by *impact* (severity weighted by share of traffic, so the worst query
  nobody searches for does not top the list).
- **Queries** — every query with its metrics and a one-line diagnosis; select a
  row to see the products that query actually returned.
- **Terms** — the words driving failures, with every spelling folded onto one
  row and a verdict naming who can fix it: *spelling*, *retrieval gap*,
  *assortment gap*, *partial query*, or *relevance*.
- **Review queue**, **Ask**, **Digest** — below.

Arabic and English render correctly side by side; every embedded term is
bidi-isolated so a right-to-left word cannot reorder the English around it.

The interface is dark by default, on a true black ground rather than a dark
navy, with three accents that *are* the severity scale — green healthy, amber
needs attention, red severe — and a fourth, blue, for labels that categorise
rather than rank. Every severity colour is still paired with a text label, so
the dashboard is readable in greyscale and to colour-blind readers. Light and
follow-the-system remain available from the toggle in the masthead.

### 2. Synonym and misspelling discovery, proposed not applied

`searchiq discover` populates a review queue. Nothing is ever written to live
search. A proposal enters as `pending`, a person approves or rejects it, and the
approved set leaves as a configuration file that someone deploys deliberately:

```bash
searchiq suggestions          # read the queue
searchiq approve 3            # accept one
searchiq reject 4 --note "brand name, not a synonym"
searchiq reopen 3             # undo; back to the queue, out of the export
searchiq export -o rules.json # emit the approved set
```

The same decisions are made in the dashboard's **Review queue** tab, where each
proposal carries its evidence, an **Approve** and a **Reject** button, and a free
-text reason recorded with the decision. Once decided, a proposal shows who
decided it and offers **Undo** — a reviewer who cannot take a decision back
hesitates over every borderline case, and the queue stops moving. The number
still waiting is shown on the tab itself, so a queue nobody is working through is
visible rather than quiet.

Re-running discovery never overrules a person: it re-scores what is still
pending and leaves approved and rejected proposals exactly as the reviewer left
them.

**Misspellings** must clear three bars: the typed term appears in no product
name, something within a length-appropriate edit distance does, and that target
is used by enough products to be a real word. Direction is decided by the
catalogue, never by frequency — `حليب` is correct because 322 products use it,
regardless of which spelling is typed more often. A shopper who simply had not
finished typing (`pas` → `pasta`) is classified as a *partial query*, because
rewriting their search would be wrong.

**Synonyms** come from four signals, and the weakest one cannot act alone.
Consecutive searches look identical whether a shopper reworded one intent or
moved to the next item on their list, so a rewording scores below the review
threshold by itself and surfaces only when another signal agrees. The signal that
usually agrees is **cross-language catalogue alignment**: because every product
carries an Arabic and an English name, mining co-occurrence across 25,881
products induces a bilingual lexicon with no dictionary —

```
حليب ↔ milk (293 products)      فراوله ↔ strawberry (276)
مكرونه ↔ pasta (323)             فاكهه ↔ fruit (39)
```

— and two terms that translate to the same English word are synonyms of each
other, which is how `لبن ≡ حليب` is established rather than assumed.

### 3. An agent, and a weekly digest

```bash
searchiq ask "which queries are wasting the most traffic?"
searchiq ask "do we actually sell strawberries?" --trace
searchiq tools                        # what it is bound to
searchiq digest --days 7 -o digest.md
```

The agent answers only through the tools in `agent/tools.py`. It never writes SQL
and never sees the database, so it cannot invent a metric or read a table it has
no business reading — and every answer comes back with the exact sequence of
tools it called, so the reasoning can be checked rather than trusted.

Eight tools are bound: search health, problem queries, one query in detail, the
review queue, catalogue coverage, a period-over-period comparison, recent raw
searches, and a description of the loaded dataset. `searchiq tools` and
`GET /api/agent/tools` print the registry as the model is offered it — same list,
same schemas, no separate description to drift out of date. Every one of them is
reachable on both the model path and the offline one, which the suite asserts:
an offline deployment is the same agent with plainer wording, not a reduced one.

**It works without an API key.** With `ANTHROPIC_API_KEY` set it runs a
tool-calling loop on `claude-opus-5`; without one it falls back to a
deterministic planner that routes the question by intent. The wording is plainer;
the numbers are identical, because both paths read through the same tools. Which
path answered is always reported, never implied.

The digest reports search-quality performance against the previous period, the
searches that came back empty — split into retrieval gaps (stocked, not
surfaced) and assortment gaps (nobody stocks it), because those have different
owners — the queries needing attention, and everything still pending review. It
is available as `searchiq digest`, as `GET /api/digest`, and as a Markdown
download from the dashboard's **Digest** tab.

It is assembled deterministically and then, optionally, given an opening
paragraph by the model. Every figure, table and ranked list is computed from the
store; the model is handed those figures and asked only to characterise them, so
it is never in a position to produce a number. Periods are anchored to the newest
search in the log rather than to today's date, so a digest run against a
historical extract describes that extract instead of reporting an empty week.

---

## Engagement: what the data actually supports

The brief asks for low-engagement queries. The source database has no click
stream and no order stream, so true engagement cannot be computed from it.

What the log does carry is **what the shopper searched next**, and the reasoning
is well established: a shopper who finds what they wanted stops searching; one
who does not tries again.

- Followed by a repeat or a rewording → the result set did not satisfy.
- Nothing followed → **ambiguous, and deliberately not counted.** A session
  ending might mean the shopper found the product and went to check out, or gave
  up. The log cannot tell those apart, so a terminal search is never scored as a
  failure.
- Re-fired within two seconds with identical results → not a human reaction at
  all. Machine-like traffic is flagged and excluded from engagement, not deleted,
  because which queries attract it is itself worth knowing. It is 7.6% of the
  shipped log.

`search_event.clicked_rank` and `.converted` already exist in the schema. A
deployment that populates them gets measured engagement with no schema change,
and these proxies become a cross-check rather than a substitute.

---

## How a search is judged

Because zero-result rate is blind here, a result set is scored on five
independent signals, kept separate rather than collapsed into one number too
early:

| Signal | Question it answers |
|---|---|
| **Lexical miss** | Does any returned product mention what was asked for? |
| **Coherence** | Do the returned products belong together, or span five unrelated parts of the shop? |
| **Under-fill** | Did a top-5 engine manage to fill five slots? |
| **Instability** | Does the same query return different products on different occasions? |
| **Dissatisfaction** | Did the shopper visibly react? |

These blend into a **severity** score (0–1). A signal that could not be measured
for a given query is dropped and the remaining weights renormalised, so a query
with no coherence reading is never silently credited with perfect coherence.

**Retrieval gap** is the strongest finding the system can report, because it
names a fix: the catalogue stocks the term and search did not surface it. It is
evaluated against the *intended* term, not the typed one — a shopper who types
`حليبن` has still asked for milk, and answering with beef burgers is a failure
whether or not those exact characters appear in any product name.

---

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
| `ingest/` | Streaming `mysqldump` reader, the loader that builds the store, and the mock-data generator |
| `store/` | SQLite schema and connection handling |
| `text/` | Arabic normalisation, light stemming, edit distance, bidi isolation |
| `analytics/` | Catalogue index, per-search quality, session behaviour, metrics, term rollup |
| `discovery/` | Misspelling and synonym discovery, and the review queue |
| `agent/` | Tool registry, the agent loop and offline planner, the digest |
| `api/` | FastAPI application serving the dashboard and the API |
| `reporting/` | Flat tables for Power BI, over CSV and JSON |
| `web/` | The dashboard: HTML, CSS and vanilla JS, no build step |

**Why the dump is read directly.** Only about 12 MB of the 828 MB source is
relevant to search quality; the rest is vector embeddings, stock levels and price
history. Streaming the file means no MySQL server to install, no 828 MB import to
wait through, and **no credentials to handle**. A live-MySQL path exists alongside
it (`searchiq etl --source mysql`) for deployments that have the server anyway;
its credentials come from the environment and are never persisted.

**Why a separate analytics store.** Analysis never touches production tables, the
whole store rebuilds from one command, and nothing has to re-read 828 MB per
query. A full rebuild takes about 30 seconds and preserves reviewer decisions.

---

## Commands

| Command | What it does |
|---|---|
| `searchiq sample-data [--days N --seed N]` | Generate mock query logs and load them |
| `searchiq etl [--source dump\|mysql]` | Build the analytics store |
| `searchiq report [--since --until]` | Print a search-quality summary |
| `searchiq discover` | Find synonyms and misspellings, queue them for review |
| `searchiq suggestions [--status --kind]` | Read the review queue |
| `searchiq approve <id>` / `reject <id>` | Record a decision |
| `searchiq reopen <id>` | Undo a decision; back to the queue, out of the export |
| `searchiq export [-o FILE]` | Emit approved rules as deployable JSON |
| `searchiq ask "..." [--trace]` | Ask about search performance |
| `searchiq tools` | List the analytics tools the agent is bound to |
| `searchiq digest [--days N] [-o FILE]` | Generate the period digest |
| `searchiq serve [--port N]` | Run the dashboard and API |
| `searchiq export-bi [-o DIR] [--base-url URL]` | Write flat CSV tables for Power BI |
| `searchiq status` | Configuration and load provenance |

## HTTP API

`GET /api/status` · `/api/overview` · `/api/queries` · `/api/queries/{q}` ·
`/api/terms` · `/api/catalogue/{term}` · `/api/suggestions` ·
`/api/suggestions/summary` · `/api/suggestions/export` ·
`/api/digest[?days=N&format=json|markdown]` · `/api/agent/tools` ·
`/api/bi/tables` · `/api/bi/{table}`
`POST /api/ask` · `/api/suggestions/refresh` ·
`/api/suggestions/{id}/{approved|rejected|pending}`

Interactive documentation at `/docs`. Read endpoints open the store read-only.
The endpoints that write are the deliberate exceptions — recording a decision,
re-running discovery, storing a generated digest — and the decision endpoint is
the only route by which a proposal ever changes status.

`/api/agent/tools` publishes the registry the agent answers through, because a
claim about what it can and cannot read is only checkable if the scope is
visible.

## Power BI

The analytics are available as flat tables, so a BI tool can load them without
any Power Query reshaping. Two routes, one definition — both call the same
builders, so a file export and a live connection cannot drift apart.

```bash
searchiq export-bi                                     # CSVs into ./bi
searchiq export-bi -o reports/bi --days 180
searchiq export-bi --base-url http://127.0.0.1:8000    # adds a .pbids file
```

In Power BI Desktop: **Get Data → Folder**, point at the export directory, then
**Combine & Load**. For a live connection instead, start `searchiq serve` and use
**Get Data → Web** against `/api/bi/<table>`, or open the generated
`search-pulse.pbids`, which starts Power BI already connected.

| Table | One row is | What it holds |
|---|---|---|
| `fact_search` | one logged search | The event grain: timestamp, session, result count, and the scores of the query it belongs to |
| `dim_query` | one distinct query | Every metric the dashboard shows — severity, impact, irrelevance, instability, coverage, diagnosis |
| `dim_term` | one catalogue term | Spellings folded onto one row, with the verdict naming who can fix it |
| `fact_daily` | one day with traffic | The time series for trend visuals |
| `fact_suggestion` | one proposal | The review queue, with the human decision on it |
| `dim_overview` | the whole period | Headline figures for card visuals, plus the caveats that belong with them |

**Every figure is read from the same functions the dashboard, the CLI and the
agent read.** Nothing is recomputed for BI, and there is no SQL in the reporting
layer that touches a metric — the suite asserts the exported scores equal the
analytics layer's, row for row. That constraint is the reason to prefer this over
pointing Power BI at the SQLite file directly: the moment a report can disagree
with the dashboard about the health score, both become untrustworthy.

**What not to recompute in DAX.** `health_score`, `severity` and `impact` are not
sums of columns. Severity blends five independent signals and renormalises the
weights whenever one could not be measured for a given query; health is the
severity-weighted mean across *searches*, not across queries. Averaging
`health_score` across rows gives a number that disagrees with the dashboard. The
raw counts (`total_searches`, `problem_queries`, `is_zero_result`) are additive
and can be aggregated freely.

CSVs are UTF-8 with a BOM and booleans are emitted as 0/1, both so Arabic names
and boolean columns survive Excel and Power Query's type inference. Each export
carries a `manifest.json` and a `README.md` with the modelling notes.

## Configuration

Copy `.env.example` to `.env`. Every value has a working default, so an empty
`.env` still produces a working system. Secrets are read from the environment
only and never persisted by the application.

| Setting | Default | Purpose |
|---|---|---|
| `SEARCHIQ_DB` | `data/searchiq.db` | Analytics store |
| `SEARCHIQ_DUMP_PATH` | the Downloads path | Source SQL dump |
| `SEARCHIQ_SAMPLE_DUMP_PATH` | `data/sample_dump.sql` | Where `sample-data` writes |
| `SEARCHIQ_RESULT_CAP` | `5` | Results the engine returns; sets the under-fill baseline |
| `SEARCHIQ_SESSION_GAP_SECONDS` | `1800` | Gap that starts a new session |
| `SEARCHIQ_MODEL` | `claude-opus-5` | Model id for the agent and digest narrative |
| `ANTHROPIC_API_KEY` | *(unset)* | Optional; without it the agent uses the offline planner |

`SEARCHIQ_MODEL` is an **API model identifier, not a path to a local file**.
No weights are downloaded and nothing model-shaped lives on disk; the name is
sent to the Claude API alongside the request. `claude-opus-5` is the default,
`claude-sonnet-5` is faster and cheaper, `claude-haiku-4-5-20251001` cheaper
still. `ANTHROPIC_API_KEY` is the only credential the agent needs, and without
it the offline planner answers instead — see
[The three deliverables](#3-an-agent-and-a-weekly-digest).

`.env.example` documents every setting inline, including which of the three
ingest sources each group belongs to.

## Tests

```bash
pytest tests/          # 240 tests, about ten seconds
ruff check src tests
```

No API key, no network and no database server: the offline planner covers the
agent, and the fixtures build their own dumps.

The suite never touches the 828 MB dump. It builds a small one that reproduces
the structural features that matter — extended inserts, backslash escapes,
embedded newlines, SQL `NULL` beside the literal string `'NULL'`, Arabic text,
and the real misspelling patterns — and runs the genuine ETL over it, so the
tests exercise the code path production uses rather than a stub.

Much of the suite asserts what the system *refuses* to do: that a rewording alone
is not enough to propose a synonym, that two failing queries which return the
same wrong product are not called synonyms, that a machine-speed repeat is not a
frustrated shopper, and that re-running discovery never overrules a person.

## Known limitations

- **The log is one QA session.** All 66 searches fall on 2025-01-09 within 4.5
  hours, and 7.6% of them look automated. Rates are computed honestly but rest on
  a small, unrepresentative sample; the digest's period-over-period table appears
  only once a second period exists.
- **Sessions are derived, not recorded.** The source has no session or user
  identifier, so sessions are inferred from gaps between searches. On
  single-tenant traffic this reconstructs journeys well; on multi-user traffic it
  would interleave them. No metric depends on session identity alone.
- **The model-driven agent path is untested here.** No API key was available, so
  the tool-calling loop has not been exercised against the live API. The offline
  planner, which reads the same tools and reports the same numbers, is covered by
  the suite.
- **Category coherence depends on catalogue hygiene.** Merchandising buckets
  ("Top Selling Products", "Default Category") are excluded by an
  inverse-frequency filter rather than a curated list.
- **The generated dataset is a demonstration, not evidence.** `searchiq
  sample-data` exists so the dashboard and the agent have something to work on
  without the 828 MB dump, and its patterns are planted on purpose — finding
  them proves the pipeline runs end to end, and proves nothing about a real
  shop. Every claim in this README is from the real extract.
