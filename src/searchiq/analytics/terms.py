"""Failure rolled up by word rather than by query.

Query-level reporting scatters one problem across every spelling of it. فراولة,
فاراولة, فاراولت, فراولت, فراوله and فراول are six rows in a query table and one
word in the shop. Ranked separately none looks urgent; folded onto the word a
merchandiser would actually fix, strawberry is the largest single source of
failed searches in the log.

Each term also gets a verdict, because the failure modes have different owners:
spelling wants a dictionary, a retrieval gap wants relevance tuning, and an
assortment gap is a buying question rather than a search one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

from searchiq.analytics.catalog import CatalogIndex
from searchiq.analytics.metrics import QueryQuality, compute_query_quality
from searchiq.text.normalize import isolate, script_of, tokenize

# Terms this short carry no retrieval signal and would crowd the ranking.
_MIN_TERM_LENGTH = 2


@dataclass
class TermDriver:
    """One word, and the search traffic it is responsible for."""

    term: str
    script: str
    canonical_term: str
    searches: int
    failing_searches: int
    distinct_spellings: int
    spellings: list[str] = field(default_factory=list)
    mean_severity: float = 0.0
    total_impact: float = 0.0
    catalog_coverage: int = 0
    verdict: str = "healthy"
    headline: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_term_drivers(
    connection: sqlite3.Connection,
    *,
    since: str | None = None,
    until: str | None = None,
    catalog: CatalogIndex | None = None,
    limit: int = 25,
) -> list[TermDriver]:
    """Rank the terms behind the most failed search traffic."""
    catalog = catalog or CatalogIndex.for_connection(connection)
    qualities = compute_query_quality(
        connection, since=since, until=until, catalog=catalog
    )
    if not qualities:
        return []

    grouped: dict[str, list[tuple[str, QueryQuality]]] = {}
    for quality in qualities:
        for token in set(tokenize(quality.norm_query, min_length=_MIN_TERM_LENGTH)):
            # Fold the token onto the catalogue spelling it was aiming at, so
            # every misspelling of a word lands on the same row as the word.
            match = catalog.nearest_term(token)
            canonical = match.term if match else token
            grouped.setdefault(canonical, []).append((token, quality))

    drivers = [
        _build(canonical, members, catalog)
        for canonical, members in grouped.items()
    ]
    drivers.sort(key=lambda d: (-d.total_impact, -d.failing_searches, d.term))
    return drivers[:limit]


def _build(
    canonical: str,
    members: list[tuple[str, QueryQuality]],
    catalog: CatalogIndex,
) -> TermDriver:
    qualities = [quality for _, quality in members]
    spellings = sorted({quality.display_query for quality in qualities})

    searches = sum(q.searches for q in qualities)
    failing = sum(q.searches for q in qualities if q.is_problem)
    weighted_severity = (
        sum(q.severity * q.searches for q in qualities) / searches if searches else 0.0
    )
    coverage = catalog.coverage(canonical)
    has_retrieval_gap = any(q.retrieval_gap for q in qualities)
    misspelled = {token for token, _ in members if token != canonical}

    # A term the catalogue does not use might still be the start of one, which
    # is an autocomplete gap rather than a missing product.
    completion = catalog.best_completion(canonical) if coverage == 0 else None

    verdict, headline = _verdict(
        canonical=canonical,
        coverage=coverage,
        misspelled=misspelled,
        has_retrieval_gap=has_retrieval_gap,
        failing=failing,
        searches=searches,
        severity=weighted_severity,
        completion=completion,
    )

    return TermDriver(
        term=canonical,
        script=script_of(canonical).value,
        canonical_term=canonical,
        searches=searches,
        failing_searches=failing,
        distinct_spellings=len({token for token, _ in members}),
        spellings=spellings,
        mean_severity=round(weighted_severity, 3),
        total_impact=round(sum(q.impact for q in qualities), 4),
        catalog_coverage=coverage,
        verdict=verdict,
        headline=headline,
    )


def _verdict(
    *,
    canonical: str,
    coverage: int,
    misspelled: set[str],
    has_retrieval_gap: bool,
    failing: int,
    searches: int,
    severity: float,
    completion: Any = None,
) -> tuple[str, str]:
    """Name the failure mode, and say who can fix it."""
    if coverage == 0 and completion is not None:
        return (
            "partial query",
            f"“{isolate(canonical)}” is not a word the catalogue uses, but it "
            f"begins “{isolate(completion.term)}”, which {completion.product_count:,} "
            f"products use. Searched {searches} time{'s' if searches != 1 else ''} — "
            "shoppers typing ahead of the results, which autocomplete would catch.",
        )
    if coverage == 0:
        return (
            "assortment gap",
            f"No product uses “{isolate(canonical)}”. Shoppers searched it "
            f"{searches} time{'s' if searches != 1 else ''} and the shop does not "
            "stock it — a buying question, not a search one.",
        )
    if misspelled and has_retrieval_gap:
        return (
            "spelling",
            f"“{isolate(canonical)}” is stocked by {coverage:,} products but was "
            f"typed {len(misspelled)} other way"
            f"{'s' if len(misspelled) != 1 else ''} "
            f"({', '.join(sorted(misspelled))}), and those spellings returned "
            "nothing relevant. A spelling dictionary would recover them.",
        )
    if has_retrieval_gap:
        return (
            "retrieval gap",
            f"The catalogue has {coverage:,} products for “{isolate(canonical)}”, "
            "but searches for it returned none of them. This is a relevance fault.",
        )
    if failing:
        return (
            "relevance",
            f"{failing} of {searches} searches for “{isolate(canonical)}” were "
            f"flagged (mean severity {severity:.2f}), mostly for returning products "
            "from unrelated parts of the shop.",
        )
    return (
        "healthy",
        f"“{isolate(canonical)}” performs normally across {searches} "
        f"search{'es' if searches != 1 else ''}.",
    )
