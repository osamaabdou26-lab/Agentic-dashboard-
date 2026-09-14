"""Natural-language agent over the search-quality data.

Ask which queries are wasting the most traffic, or whether the shop actually
stocks strawberries, and it picks tools from agent.tools, reads the results and
answers.

Uses a manual tool loop rather than the SDK runner, because every answer returns
the exact sequence of calls behind it — an analyst should be able to check the
reasoning instead of trusting it — and because an unattended digest job should
not carry a beta dependency.

Works without an API key. Without one, ask falls back to a planner that routes
by intent and renders from templates: plainer wording, identical numbers, since
both paths read through the same tools. Which path answered is always reported.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from searchiq.agent import tools as toolkit
from searchiq.config import settings
from searchiq.text.normalize import isolate, normalize

# Ceiling on tool calls per question. Reaching it means the agent is looping,
# and a partial answer with a visible trace beats an unbounded spend.
MAX_TOOL_ITERATIONS = 6

# Beta flag enabling server-side routing when a request is refused.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """\
You are the search-quality analyst for an Egyptian online grocery. Shoppers \
search in Arabic and English; the search engine is embedding-based and returns \
its five nearest products, so it practically never returns zero results — \
failures show up as *wrong* results, not empty ones.

Answer from the tools. Never state a number you did not read from a tool \
result, and never estimate one. If the tools cannot answer the question, say \
so and say what data would be needed.

Rules that matter here:
- This dataset has no click or order data. Engagement is inferred from whether \
shoppers searched again. Say so whenever you report an engagement figure.
- Distinguish a retrieval fault (the catalogue stocks it, search missed it) \
from an assortment gap (nobody stocks it). Use check_catalogue_coverage \
before attributing blame.
- Quote Arabic query terms exactly as the tool returned them.
- Call describe_dataset before making any claim about how much data there is \
or how recent it is.

Be concise and concrete. Lead with the answer, then the evidence. Where a \
finding implies an action, name it in one line. Do not pad, do not repeat the \
question back, and do not describe what you are about to do."""


@dataclass
class ToolCall:
    """One tool invocation, recorded for the trace."""

    name: str
    arguments: dict[str, Any]
    result_preview: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentAnswer:
    """An answer plus the evidence trail behind it."""

    question: str
    answer: str
    source: str  # "model" or "deterministic"
    model: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "source": self.source,
            "model": self.model,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "truncated": self.truncated,
        }


def ask(
    connection: sqlite3.Connection,
    question: str,
    *,
    max_iterations: int = MAX_TOOL_ITERATIONS,
) -> AgentAnswer:
    """Answer a question about search performance."""
    question = question.strip()
    if not question:
        raise ValueError("question must not be empty")

    if settings().agent_is_live:
        return _ask_model(connection, question, max_iterations=max_iterations)
    return _ask_deterministic(connection, question)


def _ask_model(
    connection: sqlite3.Connection, question: str, *, max_iterations: int
) -> AgentAnswer:
    import anthropic  # imported on use so the offline path needs no SDK

    config = settings()
    client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    trace: list[ToolCall] = []
    truncated = True

    for _ in range(max_iterations):
        response = _create_message(client, config.model, messages)

        if response.stop_reason == "refusal":
            return AgentAnswer(
                question=question,
                answer=(
                    "The model declined to answer this request. "
                    "Rephrasing it, or asking a narrower question, usually helps."
                ),
                source="model",
                model=config.model,
                tool_calls=trace,
            )

        tool_uses = [block for block in response.content if block.type == "tool_use"]
        if not tool_uses:
            truncated = False
            break

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in tool_uses:
            arguments = dict(block.input or {})
            output = toolkit.run_tool(connection, block.name, arguments)
            payload = json.dumps(output, ensure_ascii=False, default=str)
            trace.append(
                ToolCall(
                    name=block.name,
                    arguments=arguments,
                    result_preview=_preview(payload),
                )
            )
            results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": payload}
            )
        messages.append({"role": "user", "content": results})
    else:
        response = None

    text = _text_of(response) if response is not None else ""
    if truncated and not text:
        text = (
            f"Stopped after {max_iterations} tool calls without reaching an answer. "
            "The trace below shows what was gathered."
        )

    return AgentAnswer(
        question=question,
        answer=text.strip(),
        source="model",
        model=config.model,
        tool_calls=trace,
        truncated=truncated,
    )


def _create_message(client: Any, model: str, messages: list[dict[str, Any]]) -> Any:
    """Send one turn, preferring the request shape with refusal fallbacks.

    Server-side fallbacks reroute a refused request instead of returning nothing,
    which matters for an agent that may run unattended in a scheduled digest. The
    parameter rides on the beta endpoint, so the stable endpoint is used if this
    SDK build does not accept it — the analytics behaviour is identical either
    way, and an unavailable beta should never take the agent down.
    """
    common = {
        "model": model,
        "max_tokens": 16_000,
        "system": SYSTEM_PROMPT,
        "tools": toolkit.api_schemas(),
        "messages": messages,
        "thinking": {"type": "adaptive"},
    }
    try:
        return client.beta.messages.create(
            **common, betas=[_FALLBACK_BETA], fallbacks="default"
        )
    except TypeError:
        return client.messages.create(**common)


def _text_of(response: Any) -> str:
    return "\n".join(
        block.text for block in response.content if block.type == "text"
    )


def _preview(payload: str, limit: int = 400) -> str:
    return payload if len(payload) <= limit else payload[: limit - 1] + "…"


# offline path
# Intent patterns, most specific first. The first match wins, so a question
# naming a query is routed to that query rather than to the generic overview.
_INTENTS: tuple[tuple[str, str], ...] = (
    ("suggestions", r"\b(synonym|misspell|spelling|suggestion|proposal|review queue|typo)\w*"),
    ("coverage", r"\b(do we (sell|stock|have)|catalogue|catalog|in stock|assortment)\w*"),
    ("recent", r"\b(recent|latest|last few|most recent|raw search|spot[- ]check)\w*"),
    ("compare", r"\b(chang|compare|week[- ]on[- ]week|trend|better|worse|since)\w*"),
    ("dataset", r"\b(how much data|what data|dataset|coverage of the log|how recent|source)\w*"),
    ("problems", r"\b(problem|worst|broken|failing|bad|attention|fix|issue|wrong)\w*"),
    ("health", r"\b(health|overall|summary|how is search|performance|doing)\w*"),
)

# `YYYY-MM-DD`, with or without a time. Dates in a question are what let the
# offline path reach `compare_periods`, which needs four explicit bounds.
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\b")


def _ask_deterministic(connection: sqlite3.Connection, question: str) -> AgentAnswer:
    """Answer without a model, by routing the question to the right tools."""
    lowered = question.casefold()
    trace: list[ToolCall] = []

    def call(name: str, **arguments: Any) -> Any:
        output = toolkit.run_tool(connection, name, arguments)
        trace.append(
            ToolCall(
                name=name,
                arguments=arguments,
                result_preview=_preview(
                    json.dumps(output, ensure_ascii=False, default=str)
                ),
            )
        )
        return output

    named = _named_query(connection, question)
    intent = next(
        (name for name, pattern in _INTENTS if re.search(pattern, lowered)), None
    )
    if named and intent in (None, "problems", "health"):
        intent = "query"

    if intent == "query":
        body = _render_query(call("get_query_detail", query=named))
    elif intent == "suggestions":
        body = _render_suggestions(call("list_suggestions", limit=15))
    elif intent == "coverage":
        term = named or _last_noun(question)
        body = _render_coverage(call("check_catalogue_coverage", term=term))
    elif intent == "dataset":
        body = _render_dataset(call("describe_dataset"))
    elif intent == "compare":
        periods = _periods_in(question)
        if periods is None:
            body = (
                "Period comparison needs two explicit date ranges. "
                "Run `searchiq digest` for a period-over-period summary, or ask with "
                "dates, for example: 'compare 2025-01-01 to 2025-01-07 against "
                "2025-01-08 to 2025-01-14'."
            )
        else:
            body = _render_comparison(call("compare_periods", **periods))
    elif intent == "recent":
        body = _render_recent(call("list_recent_searches", limit=10))
    elif intent == "problems":
        body = _render_problems(call("list_problem_queries", limit=5))
    else:
        body = _render_health(call("get_search_health"))

    return AgentAnswer(
        question=question,
        answer=body.strip(),
        source="deterministic",
        model=None,
        tool_calls=trace,
    )


def _named_query(connection: sqlite3.Connection, question: str) -> str | None:
    """Find a logged query mentioned in the question.

    Quoted text wins. Otherwise the longest logged query appearing verbatim in
    the question is used, which keeps `حليب` from matching when the user really
    wrote `حليب مكثف`.
    """
    quoted = re.findall(r"[\"'“‘]([^\"'”’]+)[\"'”’]", question)
    if quoted:
        return quoted[0].strip()

    normalised_question = normalize(question)
    known = [
        row[0]
        for row in connection.execute("SELECT DISTINCT norm_query FROM search_event")
    ]
    matches = [
        query
        for query in known
        if query and len(query) >= 3 and query in normalised_question
    ]
    return max(matches, key=len) if matches else None


def _periods_in(question: str) -> dict[str, str] | None:
    """Read two comparable date ranges out of the question, if they are there.

    Four dates name both ranges outright. Two name the period of interest, and
    the baseline is taken as the equally long window immediately before it —
    which is what "compared with the period before" means, and the only reading
    that does not require guessing a length.
    """
    dates = _DATE_RE.findall(question)
    if len(dates) >= 4:
        baseline_start, baseline_end, current_start, current_end = dates[:4]
    elif len(dates) == 2:
        current_start, current_end = dates
        try:
            span = date.fromisoformat(current_end) - date.fromisoformat(current_start)
        except ValueError:
            return None
        if span.days <= 0:
            return None
        baseline_end = current_start
        baseline_start = (date.fromisoformat(current_start) - span).isoformat()
    else:
        return None

    return {
        "baseline_start": baseline_start,
        "baseline_end": baseline_end,
        "current_start": current_start,
        "current_end": current_end,
    }


def _last_noun(question: str) -> str:
    """Best-effort subject of a coverage question, for the offline path."""
    words = [word for word in re.split(r"[^\w؀-ۿ]+", question) if word]
    return words[-1] if words else question


def _render_health(data: dict[str, Any]) -> str:
    if not data.get("total_searches"):
        return "The log is empty for this period, so there is nothing to report."

    lines = [
        f"Search health is {data['health_score']}/100 across "
        f"{data['total_searches']:,} searches "
        f"({data['distinct_queries']} distinct queries, {data['sessions']} sessions) "
        f"between {data['period_start']} and {data['period_end']}.",
        "",
        f"- {data['problem_queries']} queries are failing badly enough to need attention.",
        f"- {data['lexical_miss_rate']:.0%} of searches returned nothing mentioning the query.",
        f"- {data['under_filled_rate']:.0%} came back with fewer results than the engine allows.",
        f"- {data['retrieval_gap_queries']} queries are retrieval gaps: stocked, but not surfaced.",
    ]
    if data.get("dissatisfaction_rate") is not None:
        lines.append(
            f"- {data['dissatisfaction_rate']:.0%} of searches were retried or reworded "
            f"({data['engagement_source']})."
        )
    if data.get("notes"):
        lines += ["", "Worth knowing:"] + [f"- {note}" for note in data["notes"]]
    return "\n".join(lines)


def _render_problems(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No query is currently failing badly enough to be flagged."
    lines = [f"The {len(rows)} queries costing the most, worst first:", ""]
    for position, row in enumerate(rows, start=1):
        lines.append(
            f"{position}. “{isolate(row['query'])}” — {row['searches']} searches, "
            f"severity {row['severity']:.2f}"
        )
        lines += [f"   - {reason}" for reason in row["reasons"]]
        lines.append("")
    return "\n".join(lines)


def _render_query(data: dict[str, Any]) -> str:
    if data.get("found") is False:
        return data["message"]

    lines = [
        f"“{isolate(data['display_query'])}” was searched {data['searches']} time"
        f"{'s' if data['searches'] != 1 else ''} across {data['sessions']} session"
        f"{'s' if data['sessions'] != 1 else ''}, severity {data['severity']:.2f}"
        f"{' (flagged as a problem)' if data['is_problem'] else ''}.",
        "",
        f"- {data['mean_results']} results on average (lowest {data['min_results']}).",
        f"- {data['lexical_miss_rate']:.0%} of searches returned nothing mentioning the query.",
    ]
    if data.get("incoherence") is not None:
        lines.append(
            f"- Results share a category {1 - data['incoherence']:.0%} of the time."
        )
    if data.get("intended_term") and data["intended_term"] != data["norm_query"]:
        lines.append(
            f"- Looks like a misspelling of “{isolate(data['intended_term'])}”, which "
            f"{data['intended_coverage']:,} products use."
        )
    elif data.get("catalog_coverage"):
        lines.append(f"- The catalogue has {data['catalog_coverage']:,} matching products.")
    if data.get("reasons"):
        lines += ["", "Why it is flagged:"] + [f"- {r}" for r in data["reasons"]]
    if data.get("sample_results"):
        lines += ["", "Most recently returned:"] + [
            f"- {title}" for title in data["sample_results"]
        ]
    return "\n".join(lines)


def _render_suggestions(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "The review queue is empty. Run `searchiq discover` to populate it."
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["kind"], []).append(row)

    lines = [f"{len(rows)} proposals are awaiting review.", ""]
    for kind in sorted(grouped):
        lines.append(f"{kind.replace('_', ' ').title()}:")
        for row in grouped[kind]:
            lines.append(
                f"- “{isolate(row['from'])}” → “{isolate(row['to'])}” "
                f"(confidence {row['confidence']:.2f}) — {row['rationale']}"
            )
        lines.append("")
    lines.append("Nothing is applied until it is approved and exported.")
    return "\n".join(lines)


def _render_coverage(data: dict[str, Any]) -> str:
    if data["exact_product_count"]:
        opening = (
            f"Yes — {data['exact_product_count']:,} products use "
            f"“{isolate(data['normalised'])}” in their name."
        )
    elif data["closest_catalogue_term"]:
        opening = (
            f"Not under that spelling, but “{isolate(data['closest_catalogue_term'])}” "
            f"({data['edit_distance_to_closest']} edit(s) away) is used by "
            f"{data['closest_term_product_count']:,} products."
        )
    else:
        opening = (
            f"No product in the catalogue uses “{isolate(data['normalised'])}” or "
            "anything close to it. This is an assortment gap, not a search fault."
        )
    lines = [opening]
    if data.get("example_products"):
        lines += ["", "For example:"] + [f"- {name}" for name in data["example_products"]]
    return "\n".join(lines)


_COMPARED = (
    ("health_score", "Health score", "{:.1f}"),
    ("total_searches", "Searches", "{:,}"),
    ("problem_queries", "Failing queries", "{:,}"),
    ("lexical_miss_rate", "Irrelevant-result rate", "{:.1%}"),
    ("under_filled_rate", "Under-filled rate", "{:.1%}"),
)


def _render_comparison(data: dict[str, Any]) -> str:
    baseline, current = data["baseline"], data["current"]
    if not current["total_searches"] and not baseline["total_searches"]:
        return "Neither period contains any searches, so there is nothing to compare."

    lines = [
        f"{baseline['period_start']} to {baseline['period_end']}, "
        f"against {current['period_start']} to {current['period_end']}:",
        "",
    ]
    for key, label, template in _COMPARED:
        before, after = baseline.get(key), current.get(key)
        if before is None or after is None:
            continue
        delta = data["changes"].get(key)
        movement = "" if delta in (None, 0) else f"  ({delta:+.3g})"
        lines.append(
            f"- {label}: {template.format(before)} → {template.format(after)}{movement}"
        )
    if not baseline["total_searches"]:
        lines += [
            "",
            "The baseline period is empty, so these are this period's numbers "
            "standing alone rather than a trend.",
        ]
    return "\n".join(lines)


def _render_recent(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No searches are loaded."
    lines = [f"The {len(rows)} most recent searches in the log:", ""]
    lines += [
        f"- {row['occurred_at']}  “{isolate(row['raw_query'])}”  "
        f"({row['result_count']} result{'s' if row['result_count'] != 1 else ''})"
        for row in rows
    ]
    return "\n".join(lines)


def _render_dataset(data: dict[str, Any]) -> str:
    clicks = (
        "present"
        if data["click_data_available"]
        else "absent, so engagement is inferred from repeat searches"
    )
    return "\n".join(
        [
            f"Loaded from {data['source']} at {data['loaded_at']}.",
            "",
            f"- {data['total_searches']:,} searches, "
            f"{data['distinct_queries']} distinct queries.",
            f"- Log window: {data['log_starts']} to {data['log_ends']}.",
            f"- Catalogue: {data['products']:,} products.",
            f"- Click data: {clicks}.",
        ]
    )
