# Tech Stack

Generated from a full scan of `src/`, `tests/`, `web/`, and the dependency
manifests. Versions listed as "verified" are what the project was last run and
tested against; the constraints are what `pyproject.toml` and the requirements
files actually enforce.

## 1. Overview

Search Pulse is a Python application with no compiled components, no container
runtime, and no JavaScript build step. It reads a MySQL dump, builds a local
SQLite analytics store, and exposes that store through three front ends and two
optional LLM integrations.

```
source dump ──▶ ingest ──▶ analytics store ──┬──▶ analytics ──┬──▶ FastAPI ──▶ dashboard
(mysqldump)     (stream)    (SQLite)         │                │
                                             ├──▶ discovery ──┤
                                             │   (review queue)
                                             └──▶ agent ──────┴──▶ CLI
                                                                └──▶ Streamlit
```

Three properties shape the dependency list:

**The analysis layer is standard library only.** Arabic normalisation, light
stemming, Damerau-Levenshtein distance, prefix matching, bidi isolation, session
reconstruction, and every metric are implemented directly in `src/searchiq/`.
There is no NLP or ML package anywhere in the tree.

**No embedding or vector search runs here.** The system analyses the output of an
embedding-based search engine that lives upstream, in the source database. It
never computes an embedding, so there is no vector store, no FAISS, and no
model-serving stack.

**The two LLM integrations are optional and separate.** The CLI and FastAPI paths
use Anthropic; the Streamlit app uses Google Gemini. Both fall back to a
deterministic planner that reads the same tools, so the application runs to
completion with no API key and no network access.

## 2. Core Languages and Runtime Environments

| Item | Constraint | Verified |
|---|---|---|
| Python | `>=3.11` (`pyproject.toml`) | 3.14.0 local, 3.12.14 on Railway |
| SQL | SQLite dialect, schema in `src/searchiq/store/schema.sql` | SQLite via stdlib `sqlite3` |
| JavaScript | ES2017+, no transpiler, no bundler | `web/app.js`, plain browser runtime |
| HTML / CSS | Static, hand written | `web/index.html`, `web/styles.css` |

**The deployment build no longer pins a Python version.** `.python-version`, which
pinned 3.12, was removed from the repository. Railpack resolves the version in
this order: `RAILPACK_PYTHON_VERSION`, then a version file
(`.python-version`, `.tool-versions`, `mise.toml`), then `runtime.txt`, then
`Pipfile`, and otherwise defaults to **3.13.2**. With none of those present, the
next build takes the default.

That is worth knowing before the next deploy. Railpack installs Python from
precompiled binaries and fails the build outright when one is unavailable for a
requested version rather than compiling from source. To pin it again, set
`RAILPACK_PYTHON_VERSION=3.12` as a service variable, or restore the file.

There is no C or C++ in the project, no CUDA, and no Node.js runtime. Node is not
required to build or serve the frontend.

## 3. Core Libraries and Frameworks

### Backend and API

| Package | Constraint | Verified | Used in |
|---|---|---|---|
| `fastapi` | `>=0.115` | 0.141.1 | `api/app.py` |
| `uvicorn[standard]` | `>=0.32` | 0.52.4 | `cli.py` (`searchiq serve`) |
| `pydantic` | transitive via FastAPI | 2.13.5 | Request validation, OpenAPI schema |
| `starlette` | transitive via FastAPI | 1.6.0 | ASGI layer, `TestClient` |

### Dashboard and data presentation

| Package | Constraint | Verified | Used in |
|---|---|---|---|
| `streamlit` | unpinned | 1.64.0 | `web/app.py` |
| `plotly` | unpinned | 7.1.0 | `web/app.py`, all charts |
| `pandas` | unpinned | 3.0.6 | `web/app.py`, table and chart frames |
| `numpy` | transitive via pandas | 2.5.3 | Not imported directly |
| `altair` | transitive via Streamlit | 6.3.0 | Not imported directly |

The FastAPI dashboard shares none of these. It is vanilla JavaScript with no
external libraries, no CDN references, and no package manifest.

### LLM integrations

| Package | Constraint | Verified | Used in |
|---|---|---|---|
| `anthropic` | `>=1.0` | 1.5.0 | `agent/agent.py`, `agent/digest.py` |
| `google-genai` | unpinned | 2.24.0 | `web/app.py` |

### Configuration and transport

| Package | Constraint | Verified | Used in |
|---|---|---|---|
| `python-dotenv` | `>=1.0` | 1.2.3 | `config.py`, `web/app.py` |
| `truststore` | unpinned | 0.10.4 | `web/app.py` |

`truststore` redirects TLS verification to the operating system certificate
store. Interception proxies, including consumer antivirus such as Avast's Web
and Mail Shield, re-sign HTTPS with their own root. Windows trusts that root but
certifi's bundle does not, so without this every Gemini call fails with
`CERTIFICATE_VERIFY_FAILED`. The import is wrapped and best-effort.

### Optional and development

| Package | Extra | Verified | Used in |
|---|---|---|---|
| `mysql-connector-python` | `[mysql]` | not installed | `ingest/mysql_source.py` |
| `pytest` | `[dev]` `>=8.3` | 9.1.1 | 289 tests across `tests/` |
| `httpx` | `[dev]` `>=0.27` | 0.28.1 | Required by `fastapi.testclient`, never imported directly |
| `ruff` | not declared | 0.16.7 | Lint, configured in `pyproject.toml` |

`ruff` is configured under `[tool.ruff]` with `line-length = 100` and the rule
set `F, E, W, I, UP, B, SIM, RET, C4`, but is not declared as a dependency. It
has to be installed separately.

## 4. Third-Party APIs and External Services

| Service | Purpose | Credential | Default model | Behaviour without it |
|---|---|---|---|---|
| Anthropic Messages API | Agent and digest narrative on the CLI and FastAPI paths | `ANTHROPIC_API_KEY` | `claude-opus-5` | Falls back to the deterministic planner |
| Google Gemini API | Agent and digest narrative in the Streamlit app | `GEMINI_API_KEY` | `gemini-3.6-flash` | Panel disabled, analytics unaffected |
| Railway | Hosting for the Streamlit app | Platform account | n/a | Runs locally instead |
| GitHub Pages | Hosting for the static export | Repository setting | n/a | Serve the folder anywhere |

Both model settings are API identifiers, not paths. No weights are downloaded and
nothing model-shaped is stored on disk. Credentials are read from the environment
only and are never written back by the application.

Quota note: the Gemini free tier allows 20 requests per day per project for
`gemini-3.6-flash`, and one tool-calling turn costs two or three requests.

## 5. Development and Infrastructure Tools

### Data store

SQLite, accessed through the standard library, schema in
`src/searchiq/store/schema.sql`:

- `PRAGMA journal_mode = WAL` and `PRAGMA foreign_keys = ON`
- Tables: `meta`, `product`, `product_name`, `category`, `category_name`,
  `product_category`, `catalog_term`, `search_event`, `query_count`,
  `suggestion`, `digest`
- Indexes on `search_event(norm_query)`, `(occurred_at)`,
  `(session_id, occurred_at)`, `suggestion(status, confidence DESC)`,
  `catalog_term(product_count DESC)`, `product_category(category_id)`
- No FTS virtual tables and no vector extension

MySQL is a read-only ingest source, never a target.

### Packaging and environment

| Tool | Role |
|---|---|
| setuptools (`>=68`) | Build backend, declared in `[build-system]` |
| pip | Install path for every documented workflow |
| `pyproject.toml` | Package metadata, dependencies, pytest and ruff config |
| `requirements.txt` (root) | Streamlit deployment, resolved from the repository root |
| `web/requirements.txt` | Same set, for running `web/` on its own |
| `.env` / `.env.example` | Runtime configuration, every value has a default |

No Poetry, PDM, uv, Pipenv, or Conda. No lock file, which is what makes the
deployment builder resolve with pip and `requirements.txt` rather than
`pyproject.toml`.

### Deployment

| File | Purpose |
|---|---|
| `Procfile` | Start command, binds Streamlit to `$PORT` and `0.0.0.0` |
| `build_standalone.py` | Folds a static export into one self-contained HTML file |
| `netlify.toml` | Written into the export by `searchiq export-site` |

**Not present:** no Dockerfile, no `docker-compose.yml`, no `.dockerignore`, and
no CI configuration of any kind. There is no `.github/` directory, so tests and
lint are run manually.

### Entry points

| Command | Target |
|---|---|
| `searchiq` | `searchiq.cli:main`, 15 subcommands |
| `streamlit run web/app.py` | Streamlit dashboard |
| `searchiq serve` | Uvicorn serving `searchiq.api.app:app` |

## 6. Detailed Dependency Breakdown

### Third-party, imported directly

| Package | Where | Why |
|---|---|---|
| `fastapi` | `api/app.py`, `tests/test_api.py`, `tests/test_reporting.py` | HTTP routing, dependency injection, and the OpenAPI docs at `/docs` |
| `uvicorn` | `cli.py` | ASGI server started by `searchiq serve` |
| `anthropic` | `agent/agent.py`, `agent/digest.py` | Tool-calling loop and digest narrative; imported inside the function so the offline path needs no SDK |
| `google.genai` | `web/app.py` | Same two jobs for the Streamlit front end |
| `dotenv` | `config.py`, `web/app.py` | Loads `.env` so configuration works without exported shell variables |
| `streamlit` | `web/app.py` | Deployable dashboard: layout, widgets, caching, session state |
| `pandas` | `web/app.py` | DataFrames backing the tables and charts |
| `plotly.graph_objects` | `web/app.py` | Every chart in the Streamlit app |
| `truststore` | `web/app.py` | Verifies TLS against the OS certificate store so intercepting proxies do not break API calls |
| `mysql.connector` | `ingest/mysql_source.py` | Optional live-MySQL ingest, behind the `[mysql]` extra |
| `pytest` | `tests/` | Test framework and fixtures |

### Standard library, load-bearing

| Module | Where | Why |
|---|---|---|
| `sqlite3` | 13 files | Every read and write of the analytics store |
| `dataclasses` | 15 files | Result and report types across analytics, discovery, and the agent |
| `pathlib` | throughout | All filesystem paths |
| `json` | throughout | Tool payloads, exports, static site data, API bodies |
| `re` | ingest, text, discovery | Dump parsing and tokenisation |
| `unicodedata` | `text/normalize.py` | Arabic normalisation and script detection |
| `csv` | `reporting/bi.py` | Power BI exports, written UTF-8 with BOM |
| `argparse` | `cli.py` | The 15-subcommand parser |
| `functools` | 3 files | `lru_cache` on settings and catalogue indexes |
| `itertools` | analytics | Pairwise iteration over searches within a session |
| `hashlib` | ingest | Stable identifiers for derived rows |
| `contextlib` | store | Connection context managers |
| `collections` | analytics, discovery | `Counter` and `defaultdict` for term frequency |
| `datetime` | throughout | Period arithmetic, anchored to the newest search rather than today |
| `math` | analytics | Severity weighting and renormalisation |
| `random` | `ingest/sample_data.py` | Seeded generation, so the same seed gives the same dashboard |
| `shutil` | `export_site.py` | Copies the front end into the export |
| `enum`, `types`, `typing`, `os`, `sys`, `time` | throughout | Ordinary support |

### Not used anywhere

Verified absent from the entire tree: `numpy` as a direct import, `scikit-learn`,
`torch`, `transformers`, `sentence-transformers`, `faiss`, `chromadb`, `langchain`,
`sqlalchemy`, `celery`, `redis`, `requests`, and any ORM. Queries are written as
SQL against `sqlite3` directly.

### Frontend

`web/index.html`, `web/app.js`, and `web/styles.css` have **zero external
dependencies**. No CDN links, no npm manifest, no framework. The only non-inline
reference in the HTML is an SVG namespace URI in the favicon data URL.
