"""Result-quality assessment for a single search.

The production engine is embedding-based and returns its top k neighbours, so it
practically never comes back empty. That makes zero-result rate almost useless
here: in the shipped log not one search of 66 returned nothing, yet several
returned plainly wrong things. A mistyped "milk" is answered with tea sachets
and a beef burger.

So a result set is judged three ways instead: did the engine fill its slate,
does anything returned actually mention the query, and do the results belong
together. None is sufficient alone, which is why they stay separate rather than
collapsing into one number too early.
"""

from __future__ import annotations

from dataclasses import dataclass

from searchiq.analytics.catalog import CatalogIndex
from searchiq.text.normalize import normalize

# Shorter terms match too many words to treat a prefix hit as evidence.
_MIN_PREFIX_MATCH = 3

# Coherence over a single product is trivially 1.0 and says nothing, so it is
# reported as unknown instead of as a perfect score.
_MIN_RESULTS_FOR_COHERENCE = 2


@dataclass(frozen=True)
class EventQuality:
    """Quality assessment of one logged search."""

    event_id: int
    norm_query: str
    result_count: int
    under_filled: bool
    zero_results: bool
    lexical_hit_rate: float
    coherence: float | None
    dominant_category: str | None
    catalog_coverage: int
    intended_term: str | None
    intended_coverage: int
    correction_distance: int | None

    @property
    def lexical_miss(self) -> bool:
        """True when no returned product mentions the query at all."""
        return self.result_count > 0 and self.lexical_hit_rate == 0.0

    @property
    def retrieval_gap(self) -> bool:
        """True when the catalogue stocks the term but search did not surface it.

        The strongest failure this system can report, because it names a fix:
        the merchandise is there, so the problem is retrieval, not assortment.

        `intended_coverage` rather than `catalog_coverage` is what counts here.
        A shopper who types `حليبن` has still asked for milk, and the engine
        answering with beef burgers is a failure whether or not the exact
        characters they typed appear in any product name.
        """
        return self.intended_coverage > 0 and (self.lexical_miss or self.zero_results)


def assess_event(
    *,
    event_id: int,
    norm_query: str,
    result_count: int,
    results_ar: str,
    results_en: str,
    catalog: CatalogIndex,
    result_cap: int,
) -> EventQuality:
    """Score one search against its result set."""
    terms = [term for term in norm_query.split() if term]
    ar_titles = _lines(results_ar)
    en_titles = _lines(results_en)

    hits = 0
    comparable = max(len(ar_titles), len(en_titles))
    for index in range(comparable):
        title_ar = ar_titles[index] if index < len(ar_titles) else ""
        title_en = en_titles[index] if index < len(en_titles) else ""
        if mentions(terms, title_ar) or mentions(terms, title_en):
            hits += 1

    coherence, dominant = _coherence(ar_titles, en_titles, catalog)
    intended = catalog.best_query_match(norm_query)

    return EventQuality(
        event_id=event_id,
        norm_query=norm_query,
        result_count=result_count,
        under_filled=result_count < result_cap,
        zero_results=result_count == 0,
        lexical_hit_rate=(hits / comparable) if comparable else 0.0,
        coherence=coherence,
        dominant_category=dominant,
        catalog_coverage=catalog.query_coverage(norm_query),
        intended_term=intended.term if intended else None,
        intended_coverage=intended.product_count if intended else 0,
        correction_distance=intended.distance if intended else None,
    )


def mentions(terms: list[str], title: str) -> bool:
    """Whether a product title mentions any of the query terms.

    Prefix matches count: `pas` returning "Basata Spaghetti Pasta" is the engine
    doing its job for a shopper who has not finished typing, and marking it a
    miss would blame retrieval for an autocomplete gap.
    """
    if not title or not terms:
        return False
    tokens = normalize(title).split()
    for term in terms:
        if term in tokens:
            return True
        if len(term) >= _MIN_PREFIX_MATCH and any(token.startswith(term) for token in tokens):
            return True
    return False


def _coherence(
    ar_titles: list[str], en_titles: list[str], catalog: CatalogIndex
) -> tuple[float | None, str | None]:
    """Share of returned products sitting in their single most common category.

    1.0 means every result belongs to one part of the shop; 0.2 over five
    results means no two of them are related. Returns `None` when the result set
    is too small, or when the products carry no taxonomic category, to avoid
    reporting a number with nothing behind it.
    """
    product_ids: list[int] = []
    for index in range(max(len(ar_titles), len(en_titles))):
        resolved = None
        if index < len(en_titles):
            resolved = catalog.resolve_title(en_titles[index])
        if resolved is None and index < len(ar_titles):
            resolved = catalog.resolve_title(ar_titles[index])
        if resolved is not None:
            product_ids.append(resolved)

    if len(product_ids) < _MIN_RESULTS_FOR_COHERENCE:
        return None, None

    tally: dict[int, int] = {}
    for product_id in product_ids:
        for category_id in catalog.categories_of(product_id):
            tally[category_id] = tally.get(category_id, 0) + 1

    if not tally:
        return None, None

    top_category, top_count = max(tally.items(), key=lambda item: (item[1], -item[0]))
    return top_count / len(product_ids), catalog.category_label(top_category)


def _lines(block: str) -> list[str]:
    return [line.strip() for line in (block or "").splitlines() if line.strip()]
