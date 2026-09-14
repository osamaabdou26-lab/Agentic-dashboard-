"""Synonym discovery from search traffic and the bilingual catalogue.

Four signals, combined. A shopper rewording mid-session is suggestive but never
enough alone: consecutive searches look the same whether someone reworded one
intent or just moved to the next item on their list, and timing cannot separate
those. So a reworded pair scores below the review threshold and only surfaces
when something else agrees.

What usually agrees is cross-language alignment. Every product carries an Arabic
and an English name, so mining co-occurrence across the catalogue induces a
bilingual lexicon without a dictionary, and two terms translating to the same
word are synonyms of each other.

Pairs a keystroke or two apart are misspellings; those belong to
discovery.misspellings so nothing is proposed twice under two different fixes.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime
from itertools import combinations
from typing import Any

from searchiq.analytics.behaviour import (
    FollowUp,
    Reformulation,
    SearchRow,
    analyse_sessions,
)
from searchiq.analytics.catalog import CatalogIndex
from searchiq.analytics.events import mentions
from searchiq.discovery.misspellings import Candidate
from searchiq.text.normalize import isolate, term_variants, tokenize
from searchiq.text.similarity import damerau_levenshtein, is_prefix_of

# Pairs at or below this edit distance are spelling variants, not synonyms.
_MISSPELLING_DISTANCE = 2

# Minimum share of results two queries must share to count as interchangeable.
_MIN_RESULT_OVERLAP = 0.30

# And at least this many products in common. Without an absolute floor, two
# queries that each returned a single product, and the same one, score a
# perfect Jaccard on one coincidence.
_MIN_SHARED_PRODUCTS = 2

# Minimum Dice coefficient for a cross-language pair to be a translation.
_MIN_ALIGNMENT = 0.25

# A translation candidate needs this many co-occurring products behind it.
_MIN_ALIGNMENT_SUPPORT = 8

# Below this, a proposal is not worth a reviewer's attention.
MIN_CONFIDENCE = 0.45

# Standalone confidence per signal. `reworded_by_shopper` sits deliberately
# below `MIN_CONFIDENCE`: a shopper switching terms cannot, by itself, tell a
# rewording apart from the next item on their list, so it only ever surfaces a
# proposal when a second signal agrees.
_SIGNAL_CONFIDENCE = {
    "reworded_by_shopper": 0.40,
    "shared_translation": 0.62,
    "result_overlap": 0.60,
    "catalogue_alignment": 0.58,
}

# Each additional agreeing signal adds this much confidence.
_CORROBORATION_BONUS = 0.12


def discover(
    connection: sqlite3.Connection,
    *,
    catalog: CatalogIndex | None = None,
    min_confidence: float = MIN_CONFIDENCE,
) -> list[Candidate]:
    """Propose synonym pairs for the terms shoppers actually search for."""
    catalog = catalog or CatalogIndex.for_connection(connection)
    query_terms = _searched_terms(connection)
    if not query_terms:
        return []

    evidence: dict[tuple[str, str], dict[str, Any]] = {}

    for pair, detail in _reworded_pairs(connection).items():
        _record(evidence, pair, "reworded_by_shopper", detail)
    for pair, detail in _overlapping_pairs(connection, catalog).items():
        _record(evidence, pair, "result_overlap", detail)

    aligned = _aligned_pairs(connection, query_terms)
    for pair, detail in aligned.items():
        _record(evidence, pair, "catalogue_alignment", detail)
    for pair, detail in _shared_translation_pairs(aligned).items():
        _record(evidence, pair, "shared_translation", detail)

    candidates: list[Candidate] = []
    for (source, target), detail in evidence.items():
        if _is_spelling_variant(source, target):
            continue
        confidence = _confidence(detail["signals"])
        if confidence < min_confidence:
            continue
        candidates.append(
            Candidate(
                kind="synonym",
                source_term=source,
                target_term=target,
                lang=detail.get("lang", "ar"),
                confidence=confidence,
                rationale=_rationale(source, target, detail),
                evidence=detail,
            )
        )

    candidates.sort(key=lambda c: (-c.confidence, c.source_term))
    return candidates


def _reworded_pairs(connection: sqlite3.Connection) -> dict[tuple[str, str], dict]:
    """Pairs where a shopper replaced one term with another mid-session."""
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

    tally: Counter = Counter()
    for behaviour in analyse_sessions(rows).values():
        if (
            behaviour.follow_up is FollowUp.REFORMULATION
            and behaviour.reformulation is Reformulation.SWITCH
            and behaviour.next_norm_query
        ):
            source = query_by_event[behaviour.event_id]
            tally[(source, behaviour.next_norm_query)] += 1

    return {
        pair: {"observations": count, "lang": "ar"}
        for pair, count in tally.items()
        if pair[0] and pair[1]
    }


def _overlapping_pairs(
    connection: sqlite3.Connection, catalog: CatalogIndex
) -> dict[tuple[str, str], dict]:
    """Pairs of queries whose result sets substantially coincide.

    Overlap is Jaccard similarity over resolved product identities, pooled
    across every search of each query, so an unstable ranking does not hide a
    genuine correspondence.

    Queries whose own results never mention them are excluded first, and this
    matters more than the threshold does. Two failing queries overlap precisely
    *because* they fail: `حليبن` and `فراولت` both return the same beef burger,
    which makes them look interchangeable when all they share is one bad
    neighbour in the embedding space. A query that cannot retrieve its own
    meaning cannot lend meaning to another one.
    """
    products_by_query: dict[str, set[int]] = {}
    relevant_by_query: dict[str, bool] = {}

    for row in connection.execute(
        "SELECT norm_query, results_ar, results_en FROM search_event"
    ):
        query = row["norm_query"]
        bucket = products_by_query.setdefault(query, set())
        terms = [term for term in query.split() if term]

        for block in (row["results_en"], row["results_ar"]):
            for title in (block or "").splitlines():
                resolved = catalog.resolve_title(title)
                if resolved is not None:
                    bucket.add(resolved)
                if mentions(terms, title):
                    relevant_by_query[query] = True
        relevant_by_query.setdefault(query, False)

    usable = sorted(q for q in products_by_query if relevant_by_query.get(q))

    pairs: dict[tuple[str, str], dict] = {}
    for left, right in combinations(usable, 2):
        first, second = products_by_query[left], products_by_query[right]
        if not first or not second:
            continue
        shared = first & second
        overlap = len(shared) / len(first | second)
        if len(shared) >= _MIN_SHARED_PRODUCTS and overlap >= _MIN_RESULT_OVERLAP:
            pairs[(left, right)] = {
                "result_overlap": round(overlap, 3),
                "shared_products": len(shared),
                "lang": "ar",
            }
    return pairs


def _aligned_pairs(
    connection: sqlite3.Connection, query_terms: set[str]
) -> dict[tuple[str, str], dict]:
    """Translation pairs induced from products that carry both language names.

    For each searched term, the products naming it in one language are
    collected, and the terms their *other*-language names use are counted. A
    term that shows up in nearly all of them, and rarely elsewhere, is the
    translation. Dice rather than raw count, so a term as common as `gr` or
    `من` cannot win by sheer ubiquity.
    """
    names: dict[int, dict[str, set[str]]] = {}
    for row in connection.execute("SELECT product_id, lang, norm_name FROM product_name"):
        terms: set[str] = set()
        for token in tokenize(row["norm_name"]):
            terms |= term_variants(token)
        names.setdefault(row["product_id"], {})[row["lang"]] = terms

    document_frequency: Counter = Counter()
    for per_language in names.values():
        for lang, terms in per_language.items():
            for term in terms:
                document_frequency[(term, lang)] += 1

    co_occurrence: dict[tuple[str, str, str], int] = {}
    source_totals: Counter = Counter()
    for per_language in names.values():
        for source_lang, target_lang in (("ar", "en"), ("en", "ar")):
            source_terms = per_language.get(source_lang, set()) & query_terms
            if not source_terms:
                continue
            target_terms = per_language.get(target_lang, set())
            for source in source_terms:
                source_totals[(source, source_lang)] += 1
                for target in target_terms:
                    key = (source, target, target_lang)
                    co_occurrence[key] = co_occurrence.get(key, 0) + 1

    pairs: dict[tuple[str, str], dict] = {}
    for (source, target, target_lang), shared in co_occurrence.items():
        if shared < _MIN_ALIGNMENT_SUPPORT or source == target:
            continue
        source_lang = "ar" if target_lang == "en" else "en"
        total = source_totals[(source, source_lang)] + document_frequency[(target, target_lang)]
        dice = (2 * shared) / total if total else 0.0
        if dice < _MIN_ALIGNMENT:
            continue
        existing = pairs.get((source, target))
        if existing is None or dice > existing["alignment"]:
            pairs[(source, target)] = {
                "alignment": round(dice, 3),
                "co_occurring_products": shared,
                "lang": source_lang,
                "translates_to": target_lang,
            }
    return pairs


def _shared_translation_pairs(
    aligned: dict[tuple[str, str], dict]
) -> dict[tuple[str, str], dict]:
    """Terms that translate to the same word, and so mean the same thing.

    `لبن` and `حليب` both align to `milk` across hundreds of products. Neither
    the search log nor either term alone establishes that they are synonyms, but
    a shared translation does — and it is the evidence that turns an ambiguous
    behavioural observation into a proposal worth reviewing.
    """
    sources_by_target: dict[str, list[tuple[str, dict]]] = {}
    for (source, target), detail in aligned.items():
        sources_by_target.setdefault(target, []).append((source, detail))

    pairs: dict[tuple[str, str], dict] = {}
    for target, sources in sources_by_target.items():
        if len(sources) < 2:
            continue
        for (left, left_detail), (right, right_detail) in combinations(sources, 2):
            if left_detail.get("lang") != right_detail.get("lang"):
                continue
            pairs[(left, right)] = {
                "shared_translation": target,
                "alignment_left": left_detail["alignment"],
                "alignment_right": right_detail["alignment"],
                "lang": left_detail.get("lang", "ar"),
            }
    return pairs


def _record(
    evidence: dict[tuple[str, str], dict[str, Any]],
    pair: tuple[str, str],
    signal: str,
    detail: dict[str, Any],
) -> None:
    """Merge one signal's finding into the evidence for a pair.

    Pairs are stored in a canonical order because synonymy is symmetric: a
    shopper going `لبن` -> `حليب` and another going the other way are two
    observations of one relationship, not two relationships.
    """
    key = (pair[0], pair[1]) if pair[0] <= pair[1] else (pair[1], pair[0])
    entry = evidence.setdefault(key, {"signals": []})
    if signal not in entry["signals"]:
        entry["signals"].append(signal)
    for name, value in detail.items():
        if name == "lang":
            entry.setdefault("lang", value)
        elif name == "observations":
            entry["observations"] = entry.get("observations", 0) + value
        else:
            entry[name] = value


def _confidence(signals: list[str]) -> float:
    """Score a pair from the signals that found it.

    The strongest signal sets the base and each corroborating one adds a fixed
    margin, so agreement between independent methods counts for more than any
    single method shouting louder.
    """
    distinct = set(signals)
    if not distinct:
        return 0.0
    base = max(_SIGNAL_CONFIDENCE.get(signal, 0.4) for signal in distinct)
    return round(min(0.99, base + _CORROBORATION_BONUS * (len(distinct) - 1)), 3)


def _rationale(source: str, target: str, detail: dict[str, Any]) -> str:
    parts: list[str] = []
    if "reworded_by_shopper" in detail["signals"]:
        count = detail.get("observations", 0)
        parts.append(
            f"shoppers searched “{isolate(source)}” then immediately “{isolate(target)}” "
            f"{count} time{'s' if count != 1 else ''}"
        )
    if "result_overlap" in detail["signals"]:
        parts.append(
            f"the two queries already return {detail['result_overlap']:.0%} of the "
            "same products"
        )
    if "catalogue_alignment" in detail["signals"]:
        parts.append(
            f"{detail['co_occurring_products']:,} products name both terms across "
            f"their Arabic and English titles"
        )
    if "shared_translation" in detail["signals"]:
        parts.append(
            f"both translate to “{detail['shared_translation']}” in the catalogue"
        )
    return (
        f"Treat “{isolate(source)}” and “{isolate(target)}” as equivalent: "
        + "; ".join(parts)
        + "."
    )


def _is_spelling_variant(source: str, target: str) -> bool:
    """Whether a pair belongs to the misspelling channel instead."""
    return (
        damerau_levenshtein(source, target) <= _MISSPELLING_DISTANCE
        or is_prefix_of(source, target)
        or is_prefix_of(target, source)
    )


def _searched_terms(connection: sqlite3.Connection) -> set[str]:
    """Every term shoppers have actually searched, with its spelling variants.

    Alignment is mined only for these. Inducing a lexicon for all 28k catalogue
    terms would cost far more and answer a question nobody asked.
    """
    terms: set[str] = set()
    for (norm_query,) in connection.execute("SELECT DISTINCT norm_query FROM search_event"):
        for token in tokenize(norm_query):
            terms |= term_variants(token)
    for (norm_query,) in connection.execute("SELECT DISTINCT norm_query FROM query_count"):
        for token in tokenize(norm_query):
            terms |= term_variants(token)
    return terms
