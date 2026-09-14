"""String-similarity measures used to relate one query term to another.

Misspelling discovery needs an edit distance that models how people actually
mistype: substituting a letter, dropping one, adding one, or swapping two
neighbours. Damerau-Levenshtein covers all four, which plain Levenshtein does
not — and transposition is common enough in Arabic (`فاراولة` for `فراولة`)
that ignoring it would miss real pairs.
"""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=100_000)
def damerau_levenshtein(a: str, b: str) -> int:
    """Optimal string alignment distance between `a` and `b`.

    Counts insertions, deletions, substitutions, and transpositions of adjacent
    characters, each at cost 1.

    This is the *restricted* (optimal string alignment) variant: a substring is
    never edited more than once, so `ca` -> `abc` costs 3, not 2. The restriction
    is irrelevant for the single-slip typos this system looks for and keeps the
    implementation to one O(len(a) x len(b)) pass.

    >>> damerau_levenshtein("حليب", "حليبن")
    1
    >>> damerau_levenshtein("فراوله", "فاراوله")
    1
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    # Two-and-a-half rows are enough: the transposition rule only reaches back
    # one further row, so the full matrix is never materialised.
    prev_prev: list[int] = []
    prev = list(range(len(b) + 1))

    for i, ca in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current[j] = min(
                current[j - 1] + 1,      # insertion
                prev[j] + 1,             # deletion
                prev[j - 1] + cost,      # substitution
            )
            if (
                i > 1
                and j > 1
                and ca == b[j - 2]
                and a[i - 2] == cb
            ):
                current[j] = min(current[j], prev_prev[j - 2] + cost)  # transposition
        prev_prev, prev = prev, current

    return prev[len(b)]


def similarity(a: str, b: str) -> float:
    """Edit distance rescaled to 1.0 (identical) .. 0.0 (nothing in common).

    Normalising by the longer string makes the score comparable across word
    lengths: one slip in a 4-letter word matters more than one in a 12-letter
    word, and the score reflects that.
    """
    if not a and not b:
        return 1.0
    longest = max(len(a), len(b))
    return 1.0 - (damerau_levenshtein(a, b) / longest)


def is_prefix_of(short: str, long: str) -> bool:
    """True when `short` is a strictly shorter prefix of `long`.

    One input to classifying a term pair, never the whole test. Both of these
    are prefix relations, and they mean opposite things:

        pas   -> pasta   the shopper was still typing
        حليب  -> حليبن   the shopper typed one letter too many

    What separates them is *which side the catalogue attests*. When the weaker
    term is the shorter one, the shopper had not finished typing; when it is the
    longer one, they slipped. `discovery.misspellings` combines this test with
    catalogue attestation to decide, and only the second case is ever proposed
    as a spelling correction.
    """
    return bool(short) and len(short) < len(long) and long.startswith(short)
