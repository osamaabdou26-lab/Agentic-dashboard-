"""Catalogue lookups that search-quality metrics are measured against.

Every question this module answers is of the form "what *should* this query have
returned?". Knowing that the catalogue sells 322 milk products is what turns a
result set of tea lattes from an opinion into a measurable failure.

The index is loaded once per analysis run and held in memory. The catalogue is
small enough (about 26k products) that dictionaries beat per-event SQL by a wide
margin, and every metric needs the same three lookups.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from searchiq.text.normalize import normalize, term_variants, tokenize
from searchiq.text.similarity import damerau_levenshtein

# A category attached to more than this share of the catalogue describes
# merchandising, not taxonomy. "Top Selling Products" and "Default Category"
# say nothing about what a product *is*, so counting them would make an
# incoherent result set look coherent.
_MAX_CATEGORY_SHARE = 0.10


# How far a correction may travel, by term length. Edit distance has to be read
# relative to the word: two edits turn `pas` into the unrelated `pcs`, but only
# nudge `فاراولت` to `فراوله`. A flat threshold would either miss the long
# misspellings or invent corrections for the short ones.
_MAX_CORRECTION_DISTANCE = 2
_DISTANCE_BY_LENGTH = ((3, 0), (6, 1))  # (max term length, max edit distance)

# A correction target used by fewer products than this is too thin to treat as
# evidence that the catalogue stocks what the shopper asked for.
_MIN_CORRECTION_SUPPORT = 3

# A prefix shorter than this completes to too many unrelated words to be useful.
_MIN_COMPLETION_PREFIX = 3

# Built indexes, keyed by store path and its modification time. Process-local
# and bounded by the number of stores a process opens, which is one.
_INDEX_CACHE: dict[Path, tuple[int, CatalogIndex]] = {}


def _max_distance_for(term: str) -> int:
    """The largest edit distance that still counts as the same word."""
    for max_length, distance in _DISTANCE_BY_LENGTH:
        if len(term) <= max_length:
            return distance
    return _MAX_CORRECTION_DISTANCE

@dataclass(frozen=True)
class TermMatch:
    """A catalogue term close enough to a query term to be what was meant."""

    term: str
    lang: str
    distance: int
    product_count: int

    @property
    def is_exact(self) -> bool:
        return self.distance == 0


@dataclass
class CatalogIndex:
    """In-memory view of the catalogue, built once from the analytics store."""

    product_by_norm_name: dict[str, int] = field(default_factory=dict)
    categories_by_product: dict[int, frozenset[int]] = field(default_factory=dict)
    category_names: dict[int, str] = field(default_factory=dict)
    discriminative_categories: frozenset[int] = frozenset()
    term_product_count: dict[tuple[str, str], int] = field(default_factory=dict)
    product_count: int = 0
    _terms_by_length: dict[int, list[tuple[str, str, int]]] = field(
        default_factory=dict, repr=False
    )
    _match_cache: dict[tuple[str, str | None], TermMatch | None] = field(
        default_factory=dict, repr=False
    )

    @classmethod
    def for_connection(cls, connection: sqlite3.Connection) -> CatalogIndex:
        """Return a cached index for this store, rebuilding only when it changes.

        Building the index reads about 130k rows and takes a couple of seconds.
        Every metric needs it, so a dashboard that rebuilt it per request would
        spend nearly all its time here.

        The cache key is the store file plus its modification time, so an ETL run
        invalidates it automatically. There is no manual invalidation to forget,
        and no way to serve numbers from a catalogue that no longer exists.
        """
        path = _database_path(connection)
        if path is None:  # in-memory database: nothing stable to key on
            return cls.load(connection)

        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return cls.load(connection)

        cached = _INDEX_CACHE.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]

        index = cls.load(connection)
        _INDEX_CACHE[path] = (stamp, index)
        return index

    @classmethod
    def load(cls, connection: sqlite3.Connection) -> CatalogIndex:
        """Read the catalogue slices the metrics need."""
        product_by_norm_name = {
            row["norm_name"]: row["product_id"]
            for row in connection.execute(
                "SELECT product_id, norm_name FROM product_name WHERE norm_name <> ''"
            )
        }

        categories: dict[int, set[int]] = {}
        for product_id, category_id in connection.execute(
            "SELECT product_id, category_id FROM product_category"
        ):
            categories.setdefault(product_id, set()).add(category_id)

        # Prefer the English category label for display; fall back to Arabic.
        category_names: dict[int, str] = {}
        for row in connection.execute(
            "SELECT category_id, lang, name FROM category_name ORDER BY lang DESC"
        ):
            category_names.setdefault(row["category_id"], row["name"].strip())

        product_count = connection.execute("SELECT COUNT(*) FROM product").fetchone()[0]
        usage: dict[int, int] = {}
        for members in categories.values():
            for category_id in members:
                usage[category_id] = usage.get(category_id, 0) + 1
        ceiling = max(1, int(product_count * _MAX_CATEGORY_SHARE))
        discriminative = frozenset(
            category_id for category_id, count in usage.items() if count <= ceiling
        )

        term_product_count = {
            (row["term"], row["lang"]): row["product_count"]
            for row in connection.execute("SELECT term, lang, product_count FROM catalog_term")
        }

        # Bucketing the vocabulary by length makes near-match lookup cheap:
        # two strings more than `_MAX_CORRECTION_DISTANCE` apart in length
        # cannot be that close in edit distance, so most buckets are skipped
        # without a single comparison.
        terms_by_length: dict[int, list[tuple[str, str, int]]] = {}
        for (term, lang), count in term_product_count.items():
            terms_by_length.setdefault(len(term), []).append((term, lang, count))

        return cls(
            product_by_norm_name=product_by_norm_name,
            categories_by_product={k: frozenset(v) for k, v in categories.items()},
            category_names=category_names,
            discriminative_categories=discriminative,
            term_product_count=term_product_count,
            product_count=product_count,
            _terms_by_length=terms_by_length,
        )

    # lookups

    def resolve_title(self, title: str) -> int | None:
        """Map a logged result title back to the product it names.

        The search log stores titles, not identifiers. Matching on the
        normalised name recovers the product, which is what makes category-level
        analysis of a result set possible at all.
        """
        return self.product_by_norm_name.get(normalize(title))

    def coverage(self, term: str, lang: str | None = None) -> int:
        """How many products the catalogue sells whose name contains `term`.

        A non-zero coverage for a term whose searches return nothing relevant is
        the definition of a retrieval gap: the merchandise exists and search is
        failing to surface it.
        """
        languages = (lang,) if lang else ("ar", "en")
        return max(
            (
                self.term_product_count.get((variant, language), 0)
                for variant in term_variants(term)
                for language in languages
            ),
            default=0,
        )

    def query_coverage(self, norm_query: str, lang: str | None = None) -> int:
        """Coverage for a whole query, taken as its best-covered term.

        The maximum rather than the sum or the minimum: a shopper searching
        `حليب مكثف` ("condensed milk") should be credited with the catalogue's
        milk range even if `مكثف` is rare, and a two-word query should not score
        double.
        """
        terms = tokenize(norm_query)
        return max((self.coverage(term, lang) for term in terms), default=0)

    def nearest_term(self, term: str, lang: str | None = None) -> TermMatch | None:
        """The closest catalogue term to `term`, exact match preferred.

        This is what lets a misspelling be recognised as a retrieval failure
        rather than dismissed as an unknown word. `حليبن` appears nowhere in the
        catalogue, so a literal lookup reports zero coverage and the search
        looks blameless. One keystroke away is `حليب`, which 322 products use —
        and the search returned none of them. That is a retrieval gap, and only
        a near-match lookup can see it.

        Ties are broken towards the better-stocked term, since a correction
        towards 322 products is safer than one towards 3.
        """
        key = (term, lang)
        if key in self._match_cache:
            return self._match_cache[key]

        match = self._nearest_term_uncached(term, lang)
        self._match_cache[key] = match
        return match

    def _nearest_term_uncached(self, term: str, lang: str | None) -> TermMatch | None:
        if not term:
            return None

        for variant in term_variants(term):
            for language in ((lang,) if lang else ("ar", "en")):
                count = self.term_product_count.get((variant, language))
                if count:
                    return TermMatch(variant, language, 0, count)

        budget = _max_distance_for(term)
        if budget == 0:
            return None

        best: TermMatch | None = None
        for length in range(len(term) - budget, len(term) + budget + 1):
            for candidate, candidate_lang, count in self._terms_by_length.get(length, ()):
                if lang is not None and candidate_lang != lang:
                    continue
                if count < _MIN_CORRECTION_SUPPORT:
                    continue
                distance = damerau_levenshtein(term, candidate)
                if distance > budget:
                    continue
                if best is None or (distance, -count) < (best.distance, -best.product_count):
                    best = TermMatch(candidate, candidate_lang, distance, count)
        return best

    def best_completion(self, prefix: str) -> TermMatch | None:
        """The best-stocked catalogue term starting with `prefix`.

        Separates a shopper who has not finished typing from one asking for
        something the shop does not sell. `pas` matches no product name, but it
        begins `pasta`, which 384 products use — that is an autocomplete gap,
        not an assortment gap, and the two have different owners.
        """
        if len(prefix) < _MIN_COMPLETION_PREFIX:
            return None

        best: TermMatch | None = None
        for (term, lang), count in self.term_product_count.items():
            if len(term) <= len(prefix) or not term.startswith(prefix):
                continue
            if best is None or count > best.product_count:
                best = TermMatch(term, lang, len(term) - len(prefix), count)
        return best

    def best_query_match(self, norm_query: str, lang: str | None = None) -> TermMatch | None:
        """The strongest catalogue match for any term in a query."""
        matches = [
            match
            for term in tokenize(norm_query)
            if (match := self.nearest_term(term, lang)) is not None
        ]
        if not matches:
            return None
        return min(matches, key=lambda m: (m.distance, -m.product_count))

    def categories_of(self, product_id: int) -> frozenset[int]:
        """Taxonomic categories of a product, excluding merchandising buckets."""
        attached = self.categories_by_product.get(product_id, frozenset())
        return attached & self.discriminative_categories

    def category_label(self, category_id: int) -> str:
        return self.category_names.get(category_id, f"category {category_id}")

    @cached_property
    def is_empty(self) -> bool:
        return self.product_count == 0


def _database_path(connection: sqlite3.Connection) -> Path | None:
    """The file backing the `main` database, or None if it is in memory."""
    for _, name, filename in connection.execute("PRAGMA database_list"):
        if name == "main":
            return Path(filename) if filename else None
    return None
