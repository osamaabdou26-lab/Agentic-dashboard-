"""Misspelling and partial-query discovery.

Conservative on purpose: a wrong correction silently rewrites what the shopper
asked for, and nothing downstream would catch it. A proposal needs the typed
term to be absent from every product name, a close term to be present and well
stocked, and the evidence to agree.

Direction comes from the catalogue, never from frequency. `حليبن` and `حليب`
differ by one letter; only the second names 322 products, so it is the correct
spelling whichever gets typed more.

A shopper who simply had not finished typing (`pas` towards `pasta`) is a
different thing entirely, and is proposed as a partial query for autocomplete
rather than as a spelling correction.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from math import log10
from typing import Any

from searchiq.analytics.behaviour import (
    FollowUp,
    Reformulation,
    SearchRow,
    analyse_sessions,
)
from searchiq.analytics.catalog import CatalogIndex
from searchiq.text.normalize import isolate
from searchiq.text.similarity import is_prefix_of

# Below this, a proposal is too weak to spend a reviewer's attention on.
MIN_CONFIDENCE = 0.45

# Confidence contributed by edit distance alone.
_DISTANCE_CONFIDENCE = {1: 0.90, 2: 0.72}

# Product count at which catalogue support is considered conclusive. 300+
# products using a term leaves no doubt it is the real word.
_FULL_SUPPORT = 300


@dataclass
class Candidate:
    """A proposed correction, with the evidence that produced it."""

    kind: str  # "misspelling" or "partial_query"
    source_term: str
    target_term: str
    lang: str
    confidence: float
    rationale: str
    evidence: dict[str, Any] = field(default_factory=dict)


def discover(
    connection: sqlite3.Connection,
    *,
    catalog: CatalogIndex | None = None,
    min_confidence: float = MIN_CONFIDENCE,
) -> list[Candidate]:
    """Find spelling and partial-query corrections in the logged traffic."""
    catalog = catalog or CatalogIndex.for_connection(connection)

    frequencies = _query_frequencies(connection)
    self_corrections = _self_corrections(connection)

    candidates: list[Candidate] = []
    for norm_query, searches in frequencies.items():
        candidate = _evaluate(
            norm_query=norm_query,
            searches=searches,
            catalog=catalog,
            self_corrections=self_corrections,
        )
        if candidate is not None and candidate.confidence >= min_confidence:
            candidates.append(candidate)

    candidates.sort(key=lambda c: (-c.confidence, -c.evidence.get("searches", 0)))
    return candidates


def _evaluate(
    *,
    norm_query: str,
    searches: int,
    catalog: CatalogIndex,
    self_corrections: dict[str, Counter],
) -> Candidate | None:
    """Judge a single query term against the catalogue."""
    # Multi-word queries are left alone. Correcting one word of a phrase changes
    # its meaning in ways this evidence cannot support.
    if not norm_query or " " in norm_query:
        return None

    # Bar 1: a term the catalogue already uses is spelled correctly by
    # definition, whatever its results look like.
    if catalog.coverage(norm_query) > 0:
        return None

    # Bar 2: something close must exist, and be well stocked.
    match = catalog.nearest_term(norm_query)
    if match is None or match.distance == 0:
        return None

    # A shopper who had not finished typing needs autocomplete, not a
    # dictionary. The giveaway is that the weaker term is a prefix of the
    # stronger one: `pas` -> `pasta`. When the weaker term is the *longer* one
    # (`حليب` -> `حليبن`) the shopper typed a letter too many, which is a
    # genuine misspelling.
    kind = "partial_query" if is_prefix_of(norm_query, match.term) else "misspelling"

    observed = self_corrections.get(norm_query, Counter())
    corrected_in_session = observed.get(match.term, 0)

    confidence = _confidence(
        distance=match.distance,
        support=match.product_count,
        corrected_in_session=corrected_in_session,
    )

    if kind == "partial_query":
        rationale = (
            f"“{isolate(norm_query)}” is not a catalogue word but is the start of "
            f"“{isolate(match.term)}”, which {match.product_count:,} products use. "
            "Likely a shopper who had not finished typing."
        )
    else:
        rationale = (
            f"“{isolate(norm_query)}” appears in no product name; "
            f"“{isolate(match.term)}” is {match.distance} edit"
            f"{'s' if match.distance > 1 else ''} away and names "
            f"{match.product_count:,} products."
        )
    if corrected_in_session:
        rationale += (
            f" Shoppers re-typed it as “{isolate(match.term)}” "
            f"{corrected_in_session} time{'s' if corrected_in_session > 1 else ''} "
            "in the same session."
        )

    return Candidate(
        kind=kind,
        source_term=norm_query,
        target_term=match.term,
        lang=match.lang,
        confidence=confidence,
        rationale=rationale,
        evidence={
            "searches": searches,
            "edit_distance": match.distance,
            "target_product_count": match.product_count,
            "source_product_count": 0,
            "self_corrections_observed": corrected_in_session,
        },
    )


def _confidence(*, distance: int, support: int, corrected_in_session: int) -> float:
    """Combine the three strands of evidence into a 0..1 score.

    Distance sets the ceiling, catalogue support scales it, and a shopper seen
    making the same correction themselves adds the final margin. Capped below
    1.0 because no automatic proposal should present itself as certain.
    """
    base = _DISTANCE_CONFIDENCE.get(distance, 0.5)

    # Logarithmic, because the difference between 3 and 30 supporting products
    # matters far more than between 300 and 3,000.
    support_factor = min(1.0, log10(max(support, 1) + 1) / log10(_FULL_SUPPORT + 1))

    score = base * (0.6 + 0.4 * support_factor)
    if corrected_in_session:
        score += 0.05
    return round(min(score, 0.99), 3)


def _query_frequencies(connection: sqlite3.Connection) -> dict[str, int]:
    """How often each normalised query was searched.

    Merges the event log with the lifetime counters the production service
    keeps, so terms that stopped being searched before the log window still
    carry their historical weight.
    """
    frequencies: Counter = Counter()
    for norm_query, searches in connection.execute(
        "SELECT norm_query, COUNT(*) FROM search_event GROUP BY norm_query"
    ):
        frequencies[norm_query] += searches
    for norm_query, count in connection.execute(
        "SELECT norm_query, count FROM query_count"
    ):
        frequencies[norm_query] = max(frequencies[norm_query], count)
    return dict(frequencies)


def _self_corrections(connection: sqlite3.Connection) -> dict[str, Counter]:
    """Corrections shoppers made for themselves, from the session log.

    When someone searches `فاراولة`, gets pasta, and immediately searches
    `فراولة`, they have labelled their own typo. This is the strongest evidence
    available for a spelling proposal, and it costs nothing to collect.
    """
    rows = [
        SearchRow(
            event_id=row["id"],
            session_id=row["session_id"] or "s0000",
            norm_query=row["norm_query"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            result_signature=row["result_signature"],
        )
        for row in connection.execute(
            "SELECT id, session_id, norm_query, occurred_at, result_signature "
            "FROM search_event ORDER BY occurred_at, id"
        )
    ]
    query_by_event = {row.event_id: row.norm_query for row in rows}

    corrections: dict[str, Counter] = {}
    for behaviour in analyse_sessions(rows).values():
        if (
            behaviour.follow_up is FollowUp.REFORMULATION
            and behaviour.reformulation
            in (Reformulation.TYPO_FIX, Reformulation.REFINEMENT)
            and behaviour.next_norm_query
        ):
            source = query_by_event[behaviour.event_id]
            corrections.setdefault(source, Counter())[behaviour.next_norm_query] += 1
    return corrections
