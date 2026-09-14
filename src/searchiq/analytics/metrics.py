"""Query-level and dashboard-level metrics.

Joins the three views of a search: what came back (analytics.events), what the
shopper did next (analytics.behaviour), and what the catalogue could have
offered (analytics.catalog).

Two numbers per query, kept apart on purpose. Severity is how badly a query
performs regardless of how often it is asked. Impact is severity weighted by
share of traffic, and it is what the dashboard ranks by, because fixing the
worst query nobody searches for helps nobody.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from searchiq.analytics.behaviour import (
    EventBehaviour,
    FollowUp,
    SearchRow,
    analyse_sessions,
)
from searchiq.analytics.catalog import CatalogIndex
from searchiq.analytics.events import EventQuality, assess_event
from searchiq.config import settings
from searchiq.text.normalize import isolate

# How the severity score is composed. The weights encode a product judgement:
# returning the wrong thing is worse than returning too few things, and a
# shopper visibly retrying is stronger evidence than any single heuristic.
# They sum to 1.0, so severity is always on a 0..1 scale.
SEVERITY_WEIGHTS: dict[str, float] = {
    "lexical_miss_rate": 0.30,
    "dissatisfaction_rate": 0.25,
    "incoherence": 0.20,
    "under_fill": 0.15,
    "instability": 0.10,
}

# Severity at or above this is reported as a problem query.
PROBLEM_THRESHOLD = 0.30


@dataclass
class QueryQuality:
    """Everything known about one distinct (normalised) query."""

    norm_query: str
    display_query: str
    script: str

    searches: int
    sessions: int
    first_seen: str
    last_seen: str

    mean_results: float
    min_results: int
    zero_result_rate: float
    under_fill: float
    lexical_miss_rate: float
    incoherence: float | None
    instability: float
    dissatisfaction_rate: float | None
    engagement_rate: float | None

    catalog_coverage: int
    retrieval_gap: bool
    dominant_category: str | None
    rapid_repeat_rate: float
    intended_term: str | None
    intended_coverage: int

    severity: float
    impact: float
    reasons: list[str] = field(default_factory=list)

    @property
    def is_problem(self) -> bool:
        return self.severity >= PROBLEM_THRESHOLD

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["is_problem"] = self.is_problem
        return data


@dataclass
class Overview:
    """Dashboard-level summary for a period."""

    period_start: str | None
    period_end: str | None
    total_searches: int
    distinct_queries: int
    sessions: int
    zero_result_rate: float
    under_filled_rate: float
    lexical_miss_rate: float
    retrieval_gap_queries: int
    dissatisfaction_rate: float | None
    engagement_rate: float | None
    health_score: float
    problem_queries: int
    engagement_source: str
    automated_traffic_rate: float

    # Caveats that belong with these numbers. Computed here rather than in any
    # one presenter, so the dashboard, the CLI, the agent and the digest all
    # state the same limitations in the same words.
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_query_quality(
    connection: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
    catalog: CatalogIndex | None = None,
) -> list[QueryQuality]:
    """Score every distinct query searched in the period, worst impact first."""
    catalog = catalog or CatalogIndex.for_connection(connection)
    result_cap = settings().result_cap

    events = _fetch_events(connection, since=since, until=until)
    if not events:
        return []

    behaviours = analyse_sessions(
        SearchRow(
            event_id=row["id"],
            session_id=row["session_id"] or "s0000",
            norm_query=row["norm_query"],
            occurred_at=_parse(row["occurred_at"]),
            result_signature=row["result_signature"],
        )
        for row in events
    )

    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in events:
        grouped.setdefault(row["norm_query"], []).append(row)

    total_searches = len(events)
    results = [
        _roll_up(
            norm_query=norm_query,
            rows=rows,
            behaviours=behaviours,
            catalog=catalog,
            result_cap=result_cap,
            total_searches=total_searches,
        )
        for norm_query, rows in grouped.items()
    ]
    results.sort(key=lambda item: (-item.impact, -item.searches, item.norm_query))
    return results


def compute_overview(
    connection: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
    catalog: CatalogIndex | None = None,
) -> Overview:
    """Summarise search health for the period."""
    catalog = catalog or CatalogIndex.for_connection(connection)
    events = _fetch_events(connection, since=since, until=until)
    qualities = compute_query_quality(connection, since=since, until=until, catalog=catalog)

    if not events:
        return Overview(
            period_start=since,
            period_end=until,
            total_searches=0,
            distinct_queries=0,
            sessions=0,
            zero_result_rate=0.0,
            under_filled_rate=0.0,
            lexical_miss_rate=0.0,
            retrieval_gap_queries=0,
            dissatisfaction_rate=None,
            engagement_rate=None,
            health_score=100.0,
            problem_queries=0,
            engagement_source="no data",
            automated_traffic_rate=0.0,
            notes=["The log holds no searches for this period."],
        )

    total = len(events)
    clicked = [row for row in events if row["clicked_rank"] is not None]

    # Severity is averaged across searches, not across distinct queries, so a
    # failure on a heavily-searched term moves the score more than one on a
    # long-tail term.
    weighted_severity = sum(q.severity * q.searches for q in qualities) / total

    dissatisfaction = _weighted_mean(
        [(q.dissatisfaction_rate, q.searches) for q in qualities]
    )

    overview = Overview(
        period_start=min(row["occurred_at"] for row in events),
        period_end=max(row["occurred_at"] for row in events),
        total_searches=total,
        distinct_queries=len(qualities),
        sessions=len({row["session_id"] for row in events}),
        zero_result_rate=sum(1 for row in events if row["result_count"] == 0) / total,
        under_filled_rate=sum(
            1 for row in events if row["result_count"] < settings().result_cap
        ) / total,
        lexical_miss_rate=_weighted_mean(
            [(q.lexical_miss_rate, q.searches) for q in qualities]
        ) or 0.0,
        retrieval_gap_queries=sum(1 for q in qualities if q.retrieval_gap),
        dissatisfaction_rate=dissatisfaction,
        engagement_rate=(len(clicked) / total) if clicked else None,
        health_score=round(100.0 * (1.0 - weighted_severity), 1),
        problem_queries=sum(1 for q in qualities if q.is_problem),
        engagement_source="measured clicks" if clicked else "behavioural proxy",
        automated_traffic_rate=round(
            sum(q.rapid_repeat_rate * q.searches for q in qualities) / total, 3
        ),
    )
    overview.notes = _measurement_notes(overview)
    return overview


def _measurement_notes(overview: Overview) -> list[str]:
    """The caveats a reader needs to interpret these numbers correctly.

    Kept next to the calculation rather than in a presenter, because a caveat
    that only some surfaces remember to show is worse than none at all.
    """
    notes: list[str] = []
    if overview.engagement_source == "behavioural proxy":
        notes.append(
            "No click or order data exists in this dataset, so engagement is "
            "inferred from whether shoppers searched again. Searches that ended "
            "a session are excluded rather than assumed successful."
        )
    if overview.automated_traffic_rate > 0.02:
        notes.append(
            f"{overview.automated_traffic_rate:.0%} of searches were re-fired within "
            "two seconds with identical results and look automated rather than "
            "human; they are excluded from the engagement figures."
        )
    if overview.zero_result_rate == 0:
        notes.append(
            "No search returned zero results: the engine is embedding-based and "
            "always returns its nearest neighbours, so relevance failures appear "
            "as wrong results rather than empty ones."
        )
    return notes


def _roll_up(
    *,
    norm_query: str,
    rows: list[sqlite3.Row],
    behaviours: dict[int, EventBehaviour],
    catalog: CatalogIndex,
    result_cap: int,
    total_searches: int,
) -> QueryQuality:
    qualities: list[EventQuality] = [
        assess_event(
            event_id=row["id"],
            norm_query=norm_query,
            result_count=row["result_count"],
            results_ar=row["results_ar"],
            results_en=row["results_en"],
            catalog=catalog,
            result_cap=result_cap,
        )
        for row in rows
    ]
    searches = len(rows)

    mean_results = sum(q.result_count for q in qualities) / searches
    under_fill = sum(
        max(0.0, (result_cap - q.result_count) / result_cap) for q in qualities
    ) / searches
    lexical_miss_rate = sum(1 for q in qualities if q.lexical_miss) / searches
    zero_result_rate = sum(1 for q in qualities if q.zero_results) / searches

    known_coherence = [q.coherence for q in qualities if q.coherence is not None]
    incoherence = (
        1.0 - (sum(known_coherence) / len(known_coherence)) if known_coherence else None
    )

    # Instability: the same query answered differently on different occasions.
    # One distinct answer over N searches is perfectly stable; N distinct
    # answers over N searches means ranking is non-deterministic.
    signatures = {row["result_signature"] for row in rows}
    instability = (len(signatures) - 1) / (searches - 1) if searches > 1 else 0.0

    own_behaviours = [behaviours[row["id"]] for row in rows if row["id"] in behaviours]
    # Machine-speed repeats leave the denominator entirely. Counting them as
    # satisfied would be as wrong as counting them as frustrated; the honest
    # answer is that they carry no information about a shopper.
    scored = [
        b
        for b in own_behaviours
        if b.follow_up is not FollowUp.NONE and not b.rapid_repeat
    ]
    dissatisfaction = (
        sum(1 for b in scored if b.dissatisfied) / len(scored) if scored else None
    )
    rapid_repeat_rate = (
        sum(1 for b in own_behaviours if b.rapid_repeat) / searches if searches else 0.0
    )

    clicked = [row for row in rows if row["clicked_rank"] is not None]
    engagement = (len(clicked) / searches) if clicked else None

    coverage = max(q.catalog_coverage for q in qualities)
    retrieval_gap = any(q.retrieval_gap for q in qualities)
    intended = next((q for q in qualities if q.intended_term), None)
    intended_term = intended.intended_term if intended else None
    intended_coverage = intended.intended_coverage if intended else 0
    misspelled = bool(intended and intended.correction_distance)

    dominant = next(
        (q.dominant_category for q in qualities if q.dominant_category), None
    )

    severity = _severity(
        lexical_miss_rate=lexical_miss_rate,
        dissatisfaction_rate=dissatisfaction,
        incoherence=incoherence,
        under_fill=under_fill,
        instability=instability,
    )

    return QueryQuality(
        norm_query=norm_query,
        display_query=rows[0]["raw_query"],
        script=rows[0]["script"],
        searches=searches,
        sessions=len({row["session_id"] for row in rows}),
        first_seen=min(row["occurred_at"] for row in rows),
        last_seen=max(row["occurred_at"] for row in rows),
        mean_results=round(mean_results, 2),
        min_results=min(q.result_count for q in qualities),
        zero_result_rate=round(zero_result_rate, 3),
        under_fill=round(under_fill, 3),
        lexical_miss_rate=round(lexical_miss_rate, 3),
        incoherence=round(incoherence, 3) if incoherence is not None else None,
        instability=round(instability, 3),
        dissatisfaction_rate=round(dissatisfaction, 3) if dissatisfaction is not None else None,
        engagement_rate=round(engagement, 3) if engagement is not None else None,
        catalog_coverage=coverage,
        retrieval_gap=retrieval_gap,
        dominant_category=dominant,
        rapid_repeat_rate=round(rapid_repeat_rate, 3),
        intended_term=intended_term,
        intended_coverage=intended_coverage,
        severity=round(severity, 3),
        impact=round(severity * (searches / total_searches), 4),
        reasons=_reasons(
            lexical_miss_rate=lexical_miss_rate,
            incoherence=incoherence,
            under_fill=under_fill,
            instability=instability,
            dissatisfaction=dissatisfaction,
            coverage=coverage,
            retrieval_gap=retrieval_gap,
            mean_results=mean_results,
            result_cap=result_cap,
            intended_term=intended_term,
            intended_coverage=intended_coverage,
            misspelled=misspelled,
            rapid_repeat_rate=rapid_repeat_rate,
        ),
    )


def _severity(
    *,
    lexical_miss_rate: float,
    dissatisfaction_rate: float | None,
    incoherence: float | None,
    under_fill: float,
    instability: float,
) -> float:
    """Blend the failure signals into one 0..1 score.

    Signals that could not be measured for this query are dropped and the
    remaining weights renormalised, so a query with no coherence reading is not
    silently credited with perfect coherence.
    """
    available = {
        "lexical_miss_rate": lexical_miss_rate,
        "dissatisfaction_rate": dissatisfaction_rate,
        "incoherence": incoherence,
        "under_fill": under_fill,
        "instability": instability,
    }
    measured = {k: v for k, v in available.items() if v is not None}
    total_weight = sum(SEVERITY_WEIGHTS[k] for k in measured)
    if total_weight == 0:
        return 0.0
    return sum(SEVERITY_WEIGHTS[k] * v for k, v in measured.items()) / total_weight


def _reasons(
    *,
    lexical_miss_rate: float,
    incoherence: float | None,
    under_fill: float,
    instability: float,
    dissatisfaction: float | None,
    coverage: int,
    retrieval_gap: bool,
    mean_results: float,
    result_cap: int,
    intended_term: str | None,
    intended_coverage: int,
    misspelled: bool,
    rapid_repeat_rate: float,
) -> list[str]:
    """Plain-language explanations, in the order a reader should read them.

    Every problem shown in the dashboard carries its own justification, so no
    number appears without the reason it is there.
    """
    reasons: list[str] = []
    if retrieval_gap and misspelled and intended_term:
        reasons.append(
            f"Retrieval gap: this looks like a misspelling of “{isolate(intended_term)}”, "
            f"which {intended_coverage:,} products use, and none of them came back."
        )
    elif retrieval_gap:
        reasons.append(
            f"Retrieval gap: the catalogue has {intended_coverage:,} matching products, "
            "but none of them came back."
        )
    if lexical_miss_rate > 0:
        reasons.append(
            f"{lexical_miss_rate:.0%} of searches returned nothing that mentions the query."
        )
    if incoherence is not None and incoherence >= 0.5:
        reasons.append(
            f"Results are scattered across unrelated categories "
            f"({1 - incoherence:.0%} share a category)."
        )
    if under_fill > 0:
        reasons.append(
            f"Only {mean_results:.1f} of {result_cap} result slots filled on average."
        )
    if instability > 0:
        reasons.append(
            f"Ranking is unstable: {instability:.0%} of repeat searches returned a "
            "different set of products."
        )
    if dissatisfaction is not None and dissatisfaction > 0:
        reasons.append(
            f"{dissatisfaction:.0%} of these searches were immediately retried or reworded."
        )
    if rapid_repeat_rate > 0:
        reasons.append(
            f"{rapid_repeat_rate:.0%} of these searches were re-fired within two "
            "seconds with identical results, which looks automated rather than "
            "human; those are excluded from the engagement figures."
        )
    if coverage == 0 and intended_coverage == 0:
        reasons.append(
            "No product in the catalogue uses this word or anything close to it, "
            "so this may be an assortment gap rather than a search fault."
        )
    return reasons


def _fetch_events(
    connection: sqlite3.Connection, *, since: str | None, until: str | None
) -> list[sqlite3.Row]:
    clauses, params = [], []
    if since:
        clauses.append("occurred_at >= ?")
        params.append(since)
    if until:
        clauses.append("occurred_at <= ?")
        params.append(until)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return connection.execute(
        f"SELECT id, raw_query, norm_query, script, result_count, results_ar, results_en, "  # noqa: S608 - clauses are literals
        f"result_signature, occurred_at, session_id, clicked_rank, converted "
        f"FROM search_event {where} ORDER BY occurred_at, id",
        params,
    ).fetchall()


def _weighted_mean(pairs: list[tuple[float | None, int]]) -> float | None:
    measured = [(value, weight) for value, weight in pairs if value is not None]
    total = sum(weight for _, weight in measured)
    if total == 0:
        return None
    return sum(value * weight for value, weight in measured) / total


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)
