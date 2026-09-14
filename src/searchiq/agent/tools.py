"""The tools the agent answers questions with.

Every tool is a plain function over the analytics store that returns
JSON-serialisable data. The agent never writes SQL and never sees the database:
it chooses among these, and they enforce what a valid answer can be made of.

That indirection is the point. It means the agent cannot invent a metric, cannot
read a table it has no business reading, and cannot answer a question the data
does not support — the worst it can do is pick the wrong tool, which is visible
in the trace returned with every answer.

The same registry backs the offline planner in `agent.agent`, so a deployment
without an API key answers from exactly the same numbers.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from searchiq.analytics.catalog import CatalogIndex
from searchiq.analytics.metrics import compute_overview, compute_query_quality
from searchiq.discovery import review
from searchiq.store.db import read_meta
from searchiq.text.normalize import normalize

Handler = Callable[..., Any]


@dataclass(frozen=True)
class Tool:
    """One capability, described once for both the model and the CLI."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler

    def to_api_schema(self) -> dict[str, Any]:
        """The tool definition as the Messages API expects it."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def _overview(
    connection: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    return compute_overview(connection, since=since, until=until).to_dict()


def _problem_queries(
    connection: sqlite3.Connection,
    *,
    limit: int = 10,
    since: str | None = None,
    until: str | None = None,
) -> list[dict[str, Any]]:
    qualities = compute_query_quality(connection, since=since, until=until)
    return [
        {
            "query": q.display_query,
            "searches": q.searches,
            "severity": q.severity,
            "impact": q.impact,
            "retrieval_gap": q.retrieval_gap,
            "intended_term": q.intended_term,
            "catalogue_products_for_intended_term": q.intended_coverage,
            "reasons": q.reasons,
        }
        for q in qualities
        if q.is_problem
    ][: max(1, min(limit, 50))]


def _query_detail(connection: sqlite3.Connection, *, query: str) -> dict[str, Any]:
    target = normalize(query)
    for quality in compute_query_quality(connection):
        if quality.norm_query == target:
            detail = quality.to_dict()
            detail["sample_results"] = _sample_results(connection, target)
            return detail
    return {
        "query": query,
        "found": False,
        "message": f"No searches for {query!r} in the loaded log.",
    }


def _suggestions(
    connection: sqlite3.Connection,
    *,
    kind: str | None = None,
    status: str | None = "pending",
    limit: int = 20,
) -> list[dict[str, Any]]:
    return [
        {
            "id": s.id,
            "kind": s.kind,
            "from": s.source_term,
            "to": s.target_term,
            "confidence": s.confidence,
            "status": s.status,
            "rationale": s.rationale,
        }
        for s in review.list_suggestions(
            connection, kind=kind, status=status, limit=max(1, min(limit, 100))
        )
    ]


def _catalogue_coverage(connection: sqlite3.Connection, *, term: str) -> dict[str, Any]:
    catalog = CatalogIndex.for_connection(connection)
    normalised = normalize(term)
    match = catalog.nearest_term(normalised)
    examples = [
        row["name"]
        for row in connection.execute(
            "SELECT name FROM product_name WHERE norm_name LIKE ? LIMIT 5",
            (f"%{normalised}%",),
        )
    ]
    return {
        "term": term,
        "normalised": normalised,
        "exact_product_count": catalog.coverage(normalised),
        "closest_catalogue_term": match.term if match else None,
        "closest_term_product_count": match.product_count if match else 0,
        "edit_distance_to_closest": match.distance if match else None,
        "example_products": examples,
    }


def _compare_periods(
    connection: sqlite3.Connection,
    *,
    baseline_start: str,
    baseline_end: str,
    current_start: str,
    current_end: str,
) -> dict[str, Any]:
    baseline = compute_overview(connection, since=baseline_start, until=baseline_end)
    current = compute_overview(connection, since=current_start, until=current_end)

    def delta(field: str) -> float | None:
        before, after = getattr(baseline, field), getattr(current, field)
        if before is None or after is None:
            return None
        return round(after - before, 3)

    return {
        "baseline": baseline.to_dict(),
        "current": current.to_dict(),
        "changes": {
            field: delta(field)
            for field in (
                "health_score",
                "total_searches",
                "lexical_miss_rate",
                "under_filled_rate",
                "problem_queries",
            )
        },
    }


def _recent_searches(
    connection: sqlite3.Connection, *, limit: int = 20
) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT raw_query, result_count, occurred_at, session_id "
        "FROM search_event ORDER BY occurred_at DESC LIMIT ?",
        (max(1, min(limit, 100)),),
    ).fetchall()
    return [dict(row) for row in rows]


def _dataset_profile(connection: sqlite3.Connection) -> dict[str, Any]:
    """What the loaded data actually covers, so answers can be scoped honestly."""
    meta = read_meta(connection)
    window = connection.execute(
        "SELECT MIN(occurred_at), MAX(occurred_at), COUNT(*), COUNT(DISTINCT norm_query) "
        "FROM search_event"
    ).fetchone()
    return {
        "source": meta.get("source"),
        "loaded_at": meta.get("loaded_at"),
        "log_starts": window[0],
        "log_ends": window[1],
        "total_searches": window[2],
        "distinct_queries": window[3],
        "products": connection.execute("SELECT COUNT(*) FROM product").fetchone()[0],
        "click_data_available": bool(
            connection.execute(
                "SELECT 1 FROM search_event WHERE clicked_rank IS NOT NULL LIMIT 1"
            ).fetchone()
        ),
    }


# registry
_PERIOD_PROPERTIES = {
    "since": {
        "type": "string",
        "description": "Inclusive start, 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'. Omit for all time.",
    },
    "until": {
        "type": "string",
        "description": "Inclusive end, same format. Omit for all time.",
    },
}

TOOLS: tuple[Tool, ...] = (
    Tool(
        name="get_search_health",
        description=(
            "Headline search-quality numbers for a period: total searches, health "
            "score out of 100, zero-result and under-filled rates, how many "
            "queries are failing, and how engagement was measured. Start here for "
            "any 'how is search doing' question."
        ),
        input_schema={
            "type": "object",
            "properties": dict(_PERIOD_PROPERTIES),
            "required": [],
        },
        handler=_overview,
    ),
    Tool(
        name="list_problem_queries",
        description=(
            "The queries performing worst, ranked by impact (severity weighted by "
            "search volume), each with plain-language reasons. Use for 'what is "
            "broken', 'what needs attention', 'worst queries'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "How many to return (1-50)."},
                **_PERIOD_PROPERTIES,
            },
            "required": [],
        },
        handler=_problem_queries,
    ),
    Tool(
        name="get_query_detail",
        description=(
            "Every metric for one specific query, plus a sample of what it "
            "actually returned. Use when the user names a query."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The query as a shopper typed it."}
            },
            "required": ["query"],
        },
        handler=_query_detail,
    ),
    Tool(
        name="list_suggestions",
        description=(
            "The synonym, misspelling and partial-query proposals in the review "
            "queue, with confidence and rationale. Use for 'what should we fix', "
            "'what is pending review', 'what synonyms were found'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["misspelling", "synonym", "partial_query"],
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "approved", "rejected"],
                },
                "limit": {"type": "integer"},
            },
            "required": [],
        },
        handler=_suggestions,
    ),
    Tool(
        name="check_catalogue_coverage",
        description=(
            "What the catalogue stocks for a term: how many products use it, the "
            "closest catalogue spelling, and example products. Use to tell a "
            "retrieval fault (we stock it, search missed it) from an assortment "
            "gap (we do not stock it)."
        ),
        input_schema={
            "type": "object",
            "properties": {"term": {"type": "string"}},
            "required": ["term"],
        },
        handler=_catalogue_coverage,
    ),
    Tool(
        name="compare_periods",
        description=(
            "Search health in one period against another, with the deltas. Use for "
            "'what changed', 'is it getting worse', week-on-week questions."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "baseline_start": {"type": "string"},
                "baseline_end": {"type": "string"},
                "current_start": {"type": "string"},
                "current_end": {"type": "string"},
            },
            "required": [
                "baseline_start",
                "baseline_end",
                "current_start",
                "current_end",
            ],
        },
        handler=_compare_periods,
    ),
    Tool(
        name="list_recent_searches",
        description="The most recent raw searches, for spot-checking actual traffic.",
        input_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
            "required": [],
        },
        handler=_recent_searches,
    ),
    Tool(
        name="describe_dataset",
        description=(
            "What the loaded data covers: source, extract time, log window, row "
            "counts, and whether click data exists. Call this before making any "
            "claim about coverage or recency."
        ),
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=_dataset_profile,
    ),
)

TOOLS_BY_NAME: dict[str, Tool] = {tool.name: tool for tool in TOOLS}


def api_schemas() -> list[dict[str, Any]]:
    """Tool definitions for the Messages API, in a stable order.

    Stable ordering matters: the tool list is part of the cached request prefix,
    and reshuffling it would invalidate the prompt cache on every call.
    """
    return [tool.to_api_schema() for tool in TOOLS]


def run_tool(
    connection: sqlite3.Connection, name: str, arguments: dict[str, Any]
) -> Any:
    """Execute a tool by name, rejecting unknown names and bad arguments.

    Errors are returned as data rather than raised, so a mis-chosen tool becomes
    something the agent can read and recover from instead of a crashed turn.
    """
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return {"error": f"unknown tool {name!r}", "available": sorted(TOOLS_BY_NAME)}

    allowed = set(tool.input_schema.get("properties", {}))
    unknown = set(arguments) - allowed
    if unknown:
        return {
            "error": f"unknown argument(s) for {name}: {sorted(unknown)}",
            "accepted": sorted(allowed),
        }

    try:
        return tool.handler(connection, **arguments)
    except TypeError as exc:
        return {"error": f"invalid arguments for {name}: {exc}"}
    except (sqlite3.Error, ValueError) as exc:
        return {"error": f"{name} failed: {exc}"}


def _sample_results(connection: sqlite3.Connection, norm_query: str) -> list[str]:
    row = connection.execute(
        "SELECT results_en, results_ar FROM search_event WHERE norm_query = ? "
        "ORDER BY occurred_at DESC LIMIT 1",
        (norm_query,),
    ).fetchone()
    if row is None:
        return []
    block = row["results_en"] or row["results_ar"] or ""
    return [line.strip() for line in block.splitlines() if line.strip()]
