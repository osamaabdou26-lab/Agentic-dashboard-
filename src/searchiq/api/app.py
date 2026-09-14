"""HTTP API backing the dashboard, the review queue, and the agent.

Read endpoints open the store read-only, so nothing serving a page can mutate
it. The endpoints that do write are the deliberate exceptions: recording a
reviewer's decision, re-running discovery, and storing a generated digest. The
decision endpoint is the only route by which a proposal changes status, and it
is always driven by a person clicking Approve, Reject or Undo — discovery
proposes, and never adopts.

Every metric is recomputed per request; no result is cached. The one thing held
between requests is the catalogue index, and it is keyed on the store file's
modification time, so an ETL run invalidates it automatically. The dashboard
therefore cannot show a number that is stale with respect to the loaded data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from searchiq import __version__
from searchiq.agent import agent as agent_module
from searchiq.agent import tools as toolkit
from searchiq.agent.digest import generate_digest
from searchiq.analytics.catalog import CatalogIndex
from searchiq.analytics.metrics import compute_overview, compute_query_quality
from searchiq.analytics.terms import compute_term_drivers
from searchiq.config import settings
from searchiq.discovery import review
from searchiq.reporting import bi
from searchiq.store.db import connect, read_meta
from searchiq.text.normalize import normalize

WEB_ROOT = Path(__file__).resolve().parents[3] / "web"

app = FastAPI(
    title="Search Pulse",
    version=__version__,
    description=(
        "Search Pulse: search-quality insights, synonym and misspelling "
        "discovery, and an analytics agent for the grocery catalogue."
    ),
)


def _require_store() -> None:
    """Fail with a clear instruction if the ETL has not run yet."""
    if not settings().db_path.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                "The analytics store does not exist yet. "
                "Build it with: searchiq etl"
            ),
        )


@app.get("/api/status")
def status() -> dict[str, Any]:
    """Provenance and readiness, so the dashboard can show where data came from."""
    config = settings()
    if not config.db_path.exists():
        return {
            "ready": False,
            "message": "No analytics store. Run: searchiq etl",
            "agent_mode": "deterministic",
        }
    with connect(read_only=True) as connection:
        meta = read_meta(connection)
    return {
        "ready": True,
        "version": __version__,
        "meta": meta,
        "result_cap": config.result_cap,
        "agent_mode": "model" if config.agent_is_live else "deterministic",
        "model": config.model if config.agent_is_live else None,
    }


@app.get("/api/overview")
def overview(
    since: str | None = None, until: str | None = None
) -> dict[str, Any]:
    """Headline search-quality numbers for a period."""
    _require_store()
    with connect(read_only=True) as connection:
        return compute_overview(connection, since=since, until=until).to_dict()


@app.get("/api/queries")
def queries(
    since: str | None = None,
    until: str | None = None,
    problems_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    """Every distinct query, ranked by the traffic its failures affect."""
    _require_store()
    with connect(read_only=True) as connection:
        results = compute_query_quality(connection, since=since, until=until)
    if problems_only:
        results = [item for item in results if item.is_problem]
    return [item.to_dict() for item in results[:limit]]


@app.get("/api/queries/{query}")
def query_detail(query: str) -> dict[str, Any]:
    """Full metrics for one query, plus what it most recently returned."""
    _require_store()
    target = normalize(query)
    with connect(read_only=True) as connection:
        for item in compute_query_quality(connection):
            if item.norm_query == target:
                detail = item.to_dict()
                detail["sample_results"] = _sample_results(connection, target)
                return detail
    raise HTTPException(status_code=404, detail=f"No searches logged for {query!r}.")


@app.get("/api/terms")
def terms(
    since: str | None = None,
    until: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> list[dict[str, Any]]:
    """The terms driving failures, with every spelling folded onto one row."""
    _require_store()
    with connect(read_only=True) as connection:
        drivers = compute_term_drivers(
            connection, since=since, until=until, limit=limit
        )
    return [driver.to_dict() for driver in drivers]


@app.get("/api/catalogue/{term}")
def catalogue_coverage(term: str) -> dict[str, Any]:
    """What the catalogue stocks for a term, and the nearest spelling to it."""
    _require_store()
    normalised = normalize(term)
    with connect(read_only=True) as connection:
        catalog = CatalogIndex.for_connection(connection)
        match = catalog.nearest_term(normalised)
        examples = [
            row["name"]
            for row in connection.execute(
                "SELECT name FROM product_name WHERE norm_name LIKE ? LIMIT 8",
                (f"%{normalised}%",),
            )
        ]
    return {
        "term": term,
        "normalised": normalised,
        "exact_product_count": catalog.coverage(normalised),
        "closest_term": match.term if match else None,
        "closest_term_product_count": match.product_count if match else 0,
        "edit_distance": match.distance if match else None,
        "examples": examples,
    }


@app.get("/api/suggestions")
def suggestions(
    status: str | None = None,
    kind: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[dict[str, Any]]:
    """The discovery queue. Nothing here is live until it is approved and exported."""
    _require_store()
    with connect(read_only=True) as connection:
        return [
            item.to_dict()
            for item in review.list_suggestions(
                connection, status=status, kind=kind, limit=limit
            )
        ]


@app.get("/api/suggestions/summary")
def suggestions_summary() -> dict[str, Any]:
    """How much is waiting for a reviewer, by status and by kind.

    The dashboard shows the pending count on the tab itself. A queue nobody can
    see the size of is a queue that quietly grows, which would undermine the one
    guarantee this system makes: that a person decides.
    """
    _require_store()
    with connect(read_only=True) as connection:
        rows = connection.execute(
            "SELECT status, kind, COUNT(*) AS total FROM suggestion GROUP BY status, kind"
        ).fetchall()

    by_status: dict[str, int] = {"pending": 0, "approved": 0, "rejected": 0}
    by_kind: dict[str, int] = {}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + row["total"]
        if row["status"] == "pending":
            by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + row["total"]

    return {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "pending_by_kind": by_kind,
    }


@app.post("/api/suggestions/{suggestion_id}/{decision}")
def decide_suggestion(
    suggestion_id: int,
    decision: str,
    payload: Annotated[dict[str, Any] | None, Body()] = None,
) -> dict[str, Any]:
    """Record a reviewer's decision on one proposal.

    `approved` and `rejected` are decisions; `pending` is the undo, and takes an
    approved proposal back out of the export. This is the only route by which
    any of those states is reached — discovery never sets one.
    """
    _require_store()
    if decision not in review.DECISIONS:
        raise HTTPException(
            status_code=400,
            detail=f"decision must be one of {list(review.DECISIONS)}",
        )
    payload = payload or {}
    with connect() as connection:
        try:
            updated = review.decide(
                connection,
                suggestion_id,
                decision=decision,
                reviewer=str(payload.get("reviewer") or "dashboard"),
                note=payload.get("note"),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return updated.to_dict()


@app.post("/api/suggestions/refresh")
def refresh_suggestions() -> dict[str, Any]:
    """Re-run discovery. Decisions already made are never overwritten."""
    _require_store()
    with connect() as connection:
        report = review.refresh(connection)
    return {
        "proposed": report.proposed,
        "updated": report.updated,
        "withdrawn": report.withdrawn,
        "unchanged_by_decision": report.unchanged_by_decision,
    }


@app.get("/api/suggestions/export")
def export_suggestions() -> JSONResponse:
    """The approved set, as a configuration document for a search engine.

    Returned as a download. This system never writes to live search; deploying
    the file is a separate, deliberate act.
    """
    _require_store()
    with connect(read_only=True) as connection:
        document = review.export_approved(connection)
    return JSONResponse(
        content=document,
        headers={
            "Content-Disposition": 'attachment; filename="search-rules.json"'
        },
    )


@app.get("/api/agent/tools")
def agent_tools() -> dict[str, Any]:
    """The tools the agent is bound to, exactly as the model is offered them.

    Published because "the agent cannot invent a metric" is a claim, and a claim
    about scope is only checkable if the scope is visible. This is the same
    registry `/api/ask` executes against, not a description of it.
    """
    config = settings()
    return {
        "mode": "model" if config.agent_is_live else "deterministic",
        "model": config.model if config.agent_is_live else None,
        "tool_count": len(toolkit.TOOLS),
        "tools": toolkit.api_schemas(),
    }


@app.post("/api/ask")
def ask(payload: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
    """Answer a natural-language question about search performance."""
    _require_store()
    question = str(payload.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="'question' is required.")
    with connect(read_only=True) as connection:
        return agent_module.ask(connection, question).to_dict()


@app.get("/api/digest")
def digest(
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    end: str | None = None,
    format: Annotated[str, Query(pattern="^(json|markdown)$")] = "json",
) -> Any:
    """Generate (and store) the digest for the period ending at `end`.

    `format=markdown` returns the rendered document as a download, so the same
    endpoint that feeds the dashboard also serves the artefact a person
    circulates — no second code path, and no chance of the two disagreeing.
    """
    _require_store()
    with connect() as connection:
        generated = generate_digest(connection, days=days, end=end)

    if format == "markdown":
        return Response(
            content=generated.body_md,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": (
                    'attachment; filename="search-quality-digest-'
                    f'{generated.period_end[:10]}.md"'
                )
            },
        )
    return generated.to_dict()


@app.get("/api/bi/tables")
def bi_tables() -> dict[str, Any]:
    """The flat tables a BI tool can load, and what one row of each means.

    Power BI's Web connector wants a starting point it can enumerate; this is
    it. Each entry's `url` is directly loadable.
    """
    return {
        "tables": [
            {
                "name": table.name,
                "grain": table.grain,
                "description": table.description,
                "url": f"/api/bi/{table.name}",
            }
            for table in bi.TABLES
        ]
    }


@app.get("/api/bi/{table}")
def bi_table(
    table: str,
    days: Annotated[int, Query(ge=1, le=730)] = bi.DEFAULT_DAILY_DAYS,
) -> list[dict[str, Any]]:
    """One flat table as a JSON array of records.

    Returned as a bare array rather than wrapped in an envelope: Power Query
    turns an array of flat records straight into a table, and an envelope adds
    a navigation step every person connecting has to get right.

    These are the same rows `searchiq export-bi` writes to CSV, built by the
    same code, so the live and file routes cannot drift apart.
    """
    _require_store()
    with connect(read_only=True) as connection:
        try:
            return bi.build_table(connection, table, days=days)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown table {table!r}. Available: {bi.table_names()}",
            ) from exc


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


if WEB_ROOT.is_dir():
    app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")


def _sample_results(connection: Any, norm_query: str) -> list[dict[str, str]]:
    row = connection.execute(
        "SELECT results_ar, results_en FROM search_event WHERE norm_query = ? "
        "ORDER BY occurred_at DESC LIMIT 1",
        (norm_query,),
    ).fetchone()
    if row is None:
        return []
    arabic = [line.strip() for line in (row["results_ar"] or "").splitlines() if line.strip()]
    english = [line.strip() for line in (row["results_en"] or "").splitlines() if line.strip()]
    return [
        {
            "ar": arabic[index] if index < len(arabic) else "",
            "en": english[index] if index < len(english) else "",
        }
        for index in range(max(len(arabic), len(english)))
    ]
