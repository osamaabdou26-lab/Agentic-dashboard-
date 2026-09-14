"""Normalisation and similarity: the foundation every other metric rests on.

If `فراولة` and `فراوله` do not fold to the same string, the strawberry problem
fragments across six rows and no amount of downstream cleverness recovers it.
"""

from __future__ import annotations

import pytest

from searchiq.text.normalize import (
    Script,
    isolate,
    light_stem,
    normalize,
    script_of,
    term_variants,
    tokenize,
)
from searchiq.text.similarity import damerau_levenshtein, is_prefix_of, similarity


class TestNormalize:
    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            ("فراولة", "فراوله"),      # ta marbuta -> ha
            ("فراوله", "فراوله"),      # already canonical
            ("أحمد", "احمد"),          # alef with hamza
            ("إسلام", "اسلام"),
            ("آمال", "امال"),
            ("علىي", "عليي"),          # alef maqsura -> ya
            ("مُحَمَّد", "محمد"),        # diacritics removed
            ("فـــراولة", "فراوله"),    # tatweel removed
            ("٢٥٠ مل", "250 مل"),      # Arabic-Indic digits
        ],
    )
    def test_arabic_variants_fold_together(self, written: str, expected: str) -> None:
        assert normalize(written) == expected

    def test_the_two_spellings_of_strawberry_are_one_term(self) -> None:
        assert normalize("فراولة") == normalize("فراوله")

    def test_a_real_misspelling_survives_normalisation(self) -> None:
        # The whole system depends on this: folding must remove noise without
        # erasing the difference it is meant to detect.
        assert normalize("حليب") != normalize("حليبن")

    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            ("  Juhayna   Milk - 1 L ", "juhayna milk 1 l"),
            ("Chef's Choice", "chef s choice"),
            ("", ""),
            ("   ", ""),
        ],
    )
    def test_latin_text(self, written: str, expected: str) -> None:
        assert normalize(written) == expected

    def test_punctuation_does_not_change_tokenisation(self) -> None:
        assert normalize("جهينه - 1 لتر") == normalize("جهينه 1 لتر")


class TestTokenize:
    def test_drops_single_characters(self) -> None:
        assert tokenize("a milk 1 لتر") == ["milk", "لتر"]

    def test_keeps_meaningful_digits(self) -> None:
        assert "400" in tokenize("Givrex Frozen Strawberries - 400 Gr")

    def test_min_length_is_configurable(self) -> None:
        assert tokenize("a b cd", min_length=1) == ["a", "b", "cd"]


class TestLightStem:
    def test_strips_the_definite_article(self) -> None:
        assert light_stem("الفاكهه") == "فاكهه"

    def test_leaves_latin_alone(self) -> None:
        assert light_stem("milk") == "milk"

    def test_leaves_short_remainders_alone(self) -> None:
        # Stripping here would leave a one-letter fragment, which is noise.
        assert light_stem("الى") == "الى"

    def test_variants_keep_both_forms(self) -> None:
        # Over-generating is deliberate: a missed stem under-reports catalogue
        # coverage, an extra index entry costs nothing.
        assert term_variants("الفاكهه") == {"الفاكهه", "فاكهه"}


class TestScript:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("حليب", Script.ARABIC),
            ("pasta", Script.LATIN),
            ("2", Script.DIGITS),
            ("حليب milk", Script.MIXED),
            ("", Script.OTHER),
        ],
    )
    def test_classification(self, text: str, expected: Script) -> None:
        assert script_of(text) is expected


class TestIsolate:
    def test_wraps_in_directional_isolates(self) -> None:
        assert isolate("حليب") == "⁨حليب⁩"

    def test_an_isolated_term_does_not_set_paragraph_direction(self) -> None:
        # The first strong character of the sentence must remain the Latin `a`,
        # not the Arabic term, or the whole line renders right-to-left.
        sentence = f"a misspelling of {isolate('حليب')}"
        assert sentence.index("a") < sentence.index("⁨")


class TestDamerauLevenshtein:
    @pytest.mark.parametrize(
        ("left", "right", "distance"),
        [
            ("حليب", "حليب", 0),
            ("حليب", "حليبن", 1),       # one letter too many
            ("فراوله", "فراولت", 1),    # substitution
            ("فراوله", "فاراوله", 1),   # insertion
            ("فراوله", "فراول", 1),     # deletion
            ("ab", "ba", 1),            # transposition
            ("pas", "pasta", 2),
            ("", "abc", 3),
            ("abc", "", 3),
        ],
    )
    def test_distances(self, left: str, right: str, distance: int) -> None:
        assert damerau_levenshtein(left, right) == distance

    def test_is_symmetric(self) -> None:
        assert damerau_levenshtein("حليبن", "حليب") == damerau_levenshtein("حليب", "حليبن")

    def test_transposition_is_cheaper_than_two_substitutions(self) -> None:
        # Plain Levenshtein scores this 2; modelling the swap as one slip is why
        # `فاراولة` is recognised as a typo of `فراولة`.
        assert damerau_levenshtein("teh", "the") == 1


class TestSimilarity:
    def test_identical_strings_score_one(self) -> None:
        assert similarity("حليب", "حليب") == 1.0

    def test_one_slip_matters_more_in_a_short_word(self) -> None:
        short = similarity("cat", "car")
        long = similarity("strawberries", "strawberrios")
        assert short < long


class TestIsPrefixOf:
    def test_partial_query(self) -> None:
        assert is_prefix_of("pas", "pasta")

    def test_identical_strings_are_not_prefixes(self) -> None:
        assert not is_prefix_of("pasta", "pasta")

    def test_direction_matters(self) -> None:
        assert not is_prefix_of("pasta", "pas")

    def test_a_trailing_typo_is_also_a_prefix_relation(self) -> None:
        # Documents the trap: prefix-ness alone cannot tell "still typing" from
        # "one letter too many". Only catalogue attestation decides direction.
        assert is_prefix_of("حليب", "حليبن")
