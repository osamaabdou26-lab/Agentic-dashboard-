"""The weekly digest: what changed, and what needs attention.

The digest is assembled deterministically and then, optionally, given an opening
paragraph by the model. That split is deliberate. Every figure, table and ranked
list in the output is computed from the store; the model is handed those already
-computed figures and asked only to characterise them in prose. It is never in a
position to produce a number, so the digest cannot drift from the data even if
the narrative is wrong — and without an API key the digest is complete anyway,
merely plainer.

Periods are anchored to the newest search in the log rather than to today's
date. A digest run against a historical extract should describe that extract,
not report an empty week because the data is from January.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from searchiq.analytics.catalog import CatalogIndex
from searchiq.analytics.metrics import compute_overview, compute_query_quality
from searchiq.config import settings
from searchiq.discovery import review

DEFAULT_PERIOD_DAYS = 7

# Metrics reported as period-over-period changes, with the direction that
# counts as an improvement.
_TRACKED = (
    ("health_score", "Health score", "higher"),
    ("total_searches", "Searches", "neutral"),
    ("problem_queries", "Failing queries", "lower"),
    ("zero_result_rate", "Zero-result rate", "lower"),
    ("lexical_miss_rate", "Irrelevant-result rate", "lower"),
    ("under_filled_rate", "Under-filled rate", "lower"),
    ("retrieval_gap_queries", "Retrieval gaps", "lower"),
)

# How many zero-result queries the digest names before it stops listing them.
_ZERO_RESULT_LIMIT = 8

_NARRATIVE_PROMPT = """\
You are writing the opening of a weekly search-quality digest for an online \
grocery's product and engineering team.

Below is the complete set of figures for the period, already computed. Write \
two or three sentences that say what the week looked like and what most \
deserves attention. Use only figures that appear in the JSON. Do not invent \
numbers, do not add a heading, do not use bullet points, and do not restate \
every metric — pick what matters.

If there is no prior period to compare against, say the period stands alone \
rather than describing a change.

JSON:
"""


@dataclass
class Digest:
    """A generated digest, ready to store, print, or email."""

    period_start: str
    period_end: str
    generated_at: str
    narrative_source: str
    body_md: str
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "period_start": self.period_start,
            "period_end": self.period_end,
            "generated_at": self.generated_at,
            "narrative_source": self.narrative_source,
            "body_md": self.body_md,
            "metrics": self.metrics,
        }


def generate_digest(
    connection: sqlite3.Connection,
    *,
    end: str | None = None,
    days: int = DEFAULT_PERIOD_DAYS,
    use_model: bool | None = None,
    store: bool = True,
) -> Digest:
    """Build the digest for the `days`-long period ending at `end`."""
    period_end = _resolve_end(connection, end)
    period_start = period_end - timedelta(days=days)
    baseline_start = period_start - timedelta(days=days)

    catalog = CatalogIndex.for_connection(connection)
    current = compute_overview(
        connection,
        since=_fmt(period_start),
        until=_fmt(period_end),
        catalog=catalog,
    )
    baseline = compute_overview(
        connection,
        since=_fmt(baseline_start),
        until=_fmt(period_start),
        catalog=catalog,
    )
    qualities = compute_query_quality(
        connection, since=_fmt(period_start), until=_fmt(period_end), catalog=catalog
    )
    problems = [q for q in qualities if q.is_problem]
    zero_result = _zero_result_queries(qualities, catalog=catalog)
    previous_queries = {
        q.norm_query
        for q in compute_query_quality(
            connection,
            since=_fmt(baseline_start),
            until=_fmt(period_start),
            catalog=catalog,
        )
        if q.is_problem
    }
    pending = review.list_suggestions(connection, status="pending", limit=100)

    metrics = {
        "current": current.to_dict(),
        "baseline": baseline.to_dict(),
        "has_baseline": baseline.total_searches > 0,
        "changes": _changes(current, baseline),
        "zero_result_queries": zero_result,
        "problem_queries": [
            {
                "query": q.display_query,
                "searches": q.searches,
                "severity": q.severity,
                "impact": q.impact,
                "is_new": q.norm_query not in previous_queries,
                "reasons": q.reasons,
            }
            for q in problems[:5]
        ],
        "pending_suggestions": {
            "total": len(pending),
            "by_kind": _count_by_kind(pending),
            "top": [
                {
                    "kind": s.kind,
                    "from": s.source_term,
                    "to": s.target_term,
                    "confidence": s.confidence,
                }
                for s in pending[:5]
            ],
        },
    }

    narrative, source = _narrative(metrics, use_model=use_model)
    body = _render(
        metrics,
        narrative=narrative,
        period_start=_fmt(period_start),
        period_end=_fmt(period_end),
        days=days,
    )

    digest = Digest(
        period_start=_fmt(period_start),
        period_end=_fmt(period_end),
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        narrative_source=source,
        body_md=body,
        metrics=metrics,
    )
    if store:
        _store(connection, digest)
    return digest


def _zero_result_queries(
    qualities: list[Any], *, catalog: CatalogIndex
) -> list[dict[str, Any]]:
    """Queries that came back empty, with the reason they came back empty.

    A zero-result query is only half a finding. Whether the catalogue stocks the
    term decides who owns the fix: if it does, retrieval failed and search can
    be corrected; if it does not, nobody stocks it and the answer is a buying
    decision or an honest "we do not sell that". The two are separated here so
    the digest never hands an assortment gap to the search team.
    """
    empties = [q for q in qualities if q.zero_result_rate > 0]
    empties.sort(key=lambda q: (-q.zero_result_rate * q.searches, -q.searches))

    rows = []
    for quality in empties[:_ZERO_RESULT_LIMIT]:
        nearest = catalog.nearest_term(quality.norm_query)
        stocked = quality.catalog_coverage > 0
        rows.append(
            {
                "query": quality.display_query,
                "searches": quality.searches,
                "empty_searches": round(quality.zero_result_rate * quality.searches),
                "always_empty": quality.zero_result_rate >= 1.0,
                "catalogue_products": quality.catalog_coverage,
                "cause": "retrieval gap" if stocked else "assortment gap",
                "nearest_catalogue_term": None if stocked or nearest is None else nearest.term,
            }
        )
    return rows


def _narrative(
    metrics: dict[str, Any], *, use_model: bool | None
) -> tuple[str, str]:
    """Produce the opening paragraph, from the model when one is configured."""
    config = settings()
    wants_model = config.agent_is_live if use_model is None else use_model
    if wants_model and config.agent_is_live:
        text = _model_narrative(metrics, model=config.model, api_key=config.anthropic_api_key)
        if text:
            return text, "model"
    return _deterministic_narrative(metrics), "deterministic"


def _model_narrative(metrics: dict[str, Any], *, model: str, api_key: str) -> str | None:
    """Ask the model to characterise the period. Returns None if it cannot.

    A failure here degrades the digest to its deterministic wording rather than
    failing the run: a scheduled digest that arrives plainly worded is far more
    useful than one that does not arrive.
    """
    try:
        import anthropic  # imported on use

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=1_000,
            system="You write concise, factual analytics summaries.",
            messages=[
                {
                    "role": "user",
                    "content": _NARRATIVE_PROMPT
                    + json.dumps(metrics, ensure_ascii=False, indent=2, default=str),
                }
            ],
            thinking={"type": "adaptive"},
        )
        if response.stop_reason == "refusal":
            return None
        text = "\n".join(
            block.text for block in response.content if block.type == "text"
        ).strip()
        return text or None
    except Exception:  # noqa: BLE001 - any SDK/transport failure degrades gracefully
        return None


def _deterministic_narrative(metrics: dict[str, Any]) -> str:
    current = metrics["current"]
    if not current["total_searches"]:
        return "No searches were logged in this period."

    opening = (
        f"Search handled {current['total_searches']:,} searches this period at a "
        f"health score of {current['health_score']}/100."
    )

    if metrics["has_baseline"]:
        delta = metrics["changes"].get("health_score", {}).get("delta")
        if delta is not None and abs(delta) >= 0.1:
            direction = "up" if delta > 0 else "down"
            opening += f" That is {direction} {abs(delta):.1f} points on the previous period."
        else:
            opening += " That is level with the previous period."
    else:
        opening += (
            " There is no earlier period in the loaded data, so this digest "
            "stands alone rather than reporting a trend."
        )

    if current["problem_queries"]:
        opening += (
            f" {current['problem_queries']} queries are failing badly enough to need "
            f"attention, and {current['retrieval_gap_queries']} of the flagged terms "
            "are products the catalogue stocks but search did not surface."
        )
    return opening


def _render(
    metrics: dict[str, Any],
    *,
    narrative: str,
    period_start: str,
    period_end: str,
    days: int,
) -> str:
    current = metrics["current"]
    lines = [
        f"# Search quality — {days} days to {period_end[:10]}",
        "",
        f"*{period_start} to {period_end}*",
        "",
        narrative,
        "",
        "## What changed",
        "",
    ]

    if metrics["has_baseline"]:
        lines += [
            "| Metric | Previous | This period | Change |",
            "| --- | ---: | ---: | ---: |",
        ]
        for key, label, _ in _TRACKED:
            change = metrics["changes"].get(key)
            if change is None:
                continue
            lines.append(
                f"| {label} | {_fmt_value(key, change['before'])} | "
                f"{_fmt_value(key, change['after'])} | {change['label']} |"
            )
    else:
        lines.append(
            "No earlier period exists in the loaded log, so there is nothing to "
            "compare against yet. The table appears from the second digest onwards."
        )

    lines += ["", "## Searches that came back empty", ""]
    zero_result = metrics.get("zero_result_queries") or []
    if not current["zero_result_rate"]:
        lines.append(
            "No search returned zero results this period. That is a property of "
            "the engine rather than a clean bill of health: it is embedding-based "
            "and always returns its nearest neighbours, so a failure here looks "
            "like wrong results, not empty ones."
        )
    else:
        lines.append(
            f"{current['zero_result_rate']:.1%} of searches returned nothing at all."
        )
        lines.append("")
        lines += [
            "| Query | Searches | Empty | Catalogue | Cause |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
        for row in zero_result:
            products = (
                f"{row['catalogue_products']:,} products"
                if row["catalogue_products"]
                else "not stocked"
            )
            lines.append(
                f"| “{row['query']}” | {row['searches']} | {row['empty_searches']} | "
                f"{products} | {row['cause']} |"
            )
        if any(row["cause"] == "retrieval gap" for row in zero_result):
            lines += [
                "",
                "A retrieval gap is a search fault: those products exist and were "
                "not returned. An assortment gap is a buying decision, not "
                "something search can fix.",
            ]

    lines += ["", "## What needs attention", ""]
    if not metrics["problem_queries"]:
        lines.append("No query crossed the severity threshold this period.")
    else:
        for position, problem in enumerate(metrics["problem_queries"], start=1):
            flag = " **(new this period)**" if problem["is_new"] else ""
            lines.append(
                f"**{position}. “{problem['query']}”**{flag} — "
                f"{problem['searches']} searches, severity {problem['severity']:.2f}"
            )
            lines += [f"- {reason}" for reason in problem["reasons"]]
            lines.append("")

    pending = metrics["pending_suggestions"]
    lines += ["## Awaiting review", ""]
    if not pending["total"]:
        lines.append("Nothing is pending review.")
    else:
        by_kind = ", ".join(
            f"{count} {kind.replace('_', ' ')}"
            for kind, count in sorted(pending["by_kind"].items())
        )
        lines.append(f"{pending['total']} proposals are pending ({by_kind}). Strongest first:")
        lines.append("")
        for item in pending["top"]:
            lines.append(
                f"- `{item['from']}` → `{item['to']}` "
                f"({item['kind'].replace('_', ' ')}, confidence {item['confidence']:.2f})"
            )
        lines += ["", "None of these are applied until a reviewer approves and exports them."]

    notes = current.get("notes") or []
    if notes:
        lines += ["", "## How to read these numbers", ""]
        lines += [f"- {note}" for note in notes]

    return "\n".join(lines).rstrip() + "\n"


def _changes(current: Any, baseline: Any) -> dict[str, dict[str, Any]]:
    """Period-over-period movement, with improvement direction accounted for."""
    changes: dict[str, dict[str, Any]] = {}
    for key, _, better in _TRACKED:
        before, after = getattr(baseline, key), getattr(current, key)
        if before is None or after is None:
            continue
        delta = after - before
        if better == "neutral" or delta == 0:
            marker = ""
        elif (better == "higher") == (delta > 0):
            marker = " (better)"
        else:
            marker = " (worse)"
        # Rates move by fractions of one, and `-0.0233` reads as noise next to a
        # column of percentages. Percentage points are the unit the row is in.
        shown = (
            f"{delta * 100:+.1f} pp" if key.endswith("_rate") else f"{delta:+.3g}"
        )
        changes[key] = {
            "before": before,
            "after": after,
            "delta": round(delta, 3),
            "label": f"{shown}{marker}",
        }
    return changes


def _fmt_value(key: str, value: Any) -> str:
    if key.endswith("_rate") and isinstance(value, (int, float)):
        return f"{value:.1%}"
    if isinstance(value, float):
        return f"{value:.1f}"
    return f"{value:,}" if isinstance(value, int) else str(value)


def _count_by_kind(suggestions: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for suggestion in suggestions:
        counts[suggestion.kind] = counts.get(suggestion.kind, 0) + 1
    return counts


def _store(connection: sqlite3.Connection, digest: Digest) -> None:
    connection.execute(
        "INSERT INTO digest (period_start, period_end, generated_at, "
        "narrative_source, body_md, metrics) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(period_start, period_end) DO UPDATE SET "
        "generated_at = excluded.generated_at, "
        "narrative_source = excluded.narrative_source, "
        "body_md = excluded.body_md, metrics = excluded.metrics",
        (
            digest.period_start,
            digest.period_end,
            digest.generated_at,
            digest.narrative_source,
            digest.body_md,
            json.dumps(digest.metrics, ensure_ascii=False, default=str),
        ),
    )


def _resolve_end(connection: sqlite3.Connection, end: str | None) -> datetime:
    """Anchor the period to the log, not to the wall clock.

    A digest run today against a January extract should describe January. Using
    `now()` would report an empty week and look like a broken pipeline.
    """
    if end:
        return _parse(end)
    latest = connection.execute("SELECT MAX(occurred_at) FROM search_event").fetchone()[0]
    return _parse(latest) if latest else datetime.now()


def _parse(value: str) -> datetime:
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return datetime.fromisoformat(text)


def _fmt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")
