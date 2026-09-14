"""Metrics, behaviour classification, and term rollup.

The assertions here are mostly about *judgement*, not arithmetic: that a typo
which returns a beef burger is scored worse than a term that works, that a
machine-speed repeat is not mistaken for a frustrated shopper, and that an
unmeasurable signal is dropped rather than assumed perfect.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from searchiq.analytics.behaviour import (
    EventBehaviour,
    FollowUp,
    Reformulation,
    SearchRow,
    analyse_sessions,
    dissatisfaction_rate,
)
from searchiq.analytics.catalog import CatalogIndex
from searchiq.analytics.events import assess_event
from searchiq.analytics.metrics import (
    SEVERITY_WEIGHTS,
    compute_overview,
    compute_query_quality,
)
from searchiq.analytics.terms import compute_term_drivers


def _quality(connection, query: str):
    target = {"فراولة": "فراوله", "حليبن": "حليبن"}.get(query, query)
    for item in compute_query_quality(connection):
        if item.norm_query == target or item.display_query == query:
            return item
    raise AssertionError(f"no quality row for {query!r}")


class TestCatalogIndex:
    def test_counts_products_for_a_term(self, connection) -> None:
        catalog = CatalogIndex.for_connection(connection)
        assert catalog.coverage("حليب") == 4

    def test_finds_the_intended_spelling(self, connection) -> None:
        catalog = CatalogIndex.for_connection(connection)
        match = catalog.nearest_term("حليبن")
        assert match is not None
        assert match.term == "حليب"
        assert match.distance == 1

    def test_refuses_to_correct_very_short_terms(self, connection) -> None:
        # Two edits turn `pas` into any number of unrelated three-letter words;
        # a correction that far from a short query is a guess, not a fix.
        catalog = CatalogIndex.for_connection(connection)
        assert catalog.nearest_term("pas") is None

    def test_completes_a_partial_query(self, connection) -> None:
        catalog = CatalogIndex.for_connection(connection)
        completion = catalog.best_completion("str")
        assert completion is not None
        assert completion.term.startswith("str")

    def test_resolves_a_logged_title_to_its_product(self, connection) -> None:
        catalog = CatalogIndex.for_connection(connection)
        assert catalog.resolve_title("Mero Strawberry Jam - 420 Gr") is not None

    def test_is_cached_between_calls(self, connection) -> None:
        assert CatalogIndex.for_connection(connection) is CatalogIndex.for_connection(connection)


class TestEventQuality:
    def test_detects_a_result_set_that_never_mentions_the_query(self, connection) -> None:
        catalog = CatalogIndex.for_connection(connection)
        quality = assess_event(
            event_id=1,
            norm_query="حليبن",
            result_count=2,
            results_ar="شاي بلبن 2 في 1 من مذاق - 19 جم\nبرجر بيف من حلواني - 1 كيلو",
            results_en="Mazaq Tea Latte 2In1 - 19 Gr\nHalwani Beef Burger - 1 Kg",
            catalog=catalog,
            result_cap=5,
        )
        assert quality.lexical_miss
        assert quality.retrieval_gap  # the shop sells milk; none of it came back

    def test_a_working_query_is_not_flagged(self, connection) -> None:
        catalog = CatalogIndex.for_connection(connection)
        quality = assess_event(
            event_id=2,
            norm_query="حليب",
            result_count=3,
            results_ar="حليب جهينه خالي الدسم - 1 لتر",
            results_en="Juhayna Skimmed Milk - 1 L",
            catalog=catalog,
            result_cap=5,
        )
        assert not quality.lexical_miss
        assert not quality.retrieval_gap

    def test_a_prefix_match_counts_as_relevant(self, connection) -> None:
        # `pas` returning a pasta product is the engine doing its job for a
        # shopper mid-word, not a retrieval failure.
        catalog = CatalogIndex.for_connection(connection)
        quality = assess_event(
            event_id=3,
            norm_query="str",
            result_count=1,
            results_ar="فراولة مجمدة من جيفريكس - 400 جم",
            results_en="Givrex Frozen Strawberries - 400 Gr",
            catalog=catalog,
            result_cap=5,
        )
        assert not quality.lexical_miss

    def test_coherence_is_unknown_for_a_single_result(self, connection) -> None:
        # One product trivially shares a category with itself; reporting 1.0
        # would be a number with nothing behind it.
        catalog = CatalogIndex.for_connection(connection)
        quality = assess_event(
            event_id=4,
            norm_query="حليب",
            result_count=1,
            results_ar="حليب جهينه خالي الدسم - 1 لتر",
            results_en="Juhayna Skimmed Milk - 1 L",
            catalog=catalog,
            result_cap=5,
        )
        assert quality.coherence is None

    def test_under_fill_is_relative_to_the_engine_cap(self, connection) -> None:
        catalog = CatalogIndex.for_connection(connection)
        quality = assess_event(
            event_id=5,
            norm_query="حليب",
            result_count=2,
            results_ar="حليب جهينه خالي الدسم - 1 لتر",
            results_en="Juhayna Skimmed Milk - 1 L",
            catalog=catalog,
            result_cap=5,
        )
        assert quality.under_filled


class TestBehaviour:
    def _rows(self, *entries: tuple[str, int, str]) -> list[SearchRow]:
        base = datetime(2025, 3, 3, 9, 0, 0)
        return [
            SearchRow(
                event_id=index,
                session_id="s1",
                norm_query=query,
                occurred_at=base + timedelta(seconds=offset),
                result_signature=signature,
            )
            for index, (query, offset, signature) in enumerate(entries, start=1)
        ]

    def test_a_terminal_search_has_no_follow_up(self) -> None:
        behaviours = analyse_sessions(self._rows(("حليب", 0, "a")))
        assert behaviours[1].follow_up is FollowUp.NONE
        # Its outcome is unknown, so it counts neither for nor against.
        assert not behaviours[1].dissatisfied

    def test_an_identical_repeat_is_a_repeat(self) -> None:
        behaviours = analyse_sessions(self._rows(("حليب", 0, "a"), ("حليب", 30, "a")))
        assert behaviours[1].follow_up is FollowUp.REPEAT
        assert behaviours[1].dissatisfied

    def test_a_machine_speed_repeat_is_not_a_frustrated_shopper(self) -> None:
        behaviours = analyse_sessions(self._rows(("pas", 0, "a"), ("pas", 1, "a")))
        assert behaviours[1].rapid_repeat
        assert not behaviours[1].dissatisfied

    def test_a_late_search_is_a_new_intent(self) -> None:
        behaviours = analyse_sessions(self._rows(("حليب", 0, "a"), ("خبز", 900, "b")))
        assert behaviours[1].follow_up is FollowUp.NEW_INTENT
        assert not behaviours[1].dissatisfied

    @pytest.mark.parametrize(
        ("before", "after", "expected"),
        [
            ("حليبن", "حليب", Reformulation.REFINEMENT),  # one is a prefix of the other
            ("فراولت", "فراوله", Reformulation.TYPO_FIX),
            ("لبن", "حليب", Reformulation.SWITCH),
        ],
    )
    def test_reformulation_kinds(self, before: str, after: str, expected) -> None:
        behaviours = analyse_sessions(self._rows((before, 0, "a"), (after, 20, "b")))
        assert behaviours[1].reformulation is expected

    def test_rate_excludes_unknown_outcomes_from_the_denominator(self) -> None:
        behaviours = [
            EventBehaviour(1, "s1", FollowUp.REPEAT, None, "x", 30.0, True),
            EventBehaviour(2, "s1", FollowUp.NONE, None, None, None, False),
        ]
        # One scored search, and it was dissatisfied: 100%, not 50%.
        assert dissatisfaction_rate(behaviours) == 1.0

    def test_rate_is_none_when_nothing_is_measurable(self) -> None:
        unknown = EventBehaviour(1, "s1", FollowUp.NONE, None, None, None, False)
        assert dissatisfaction_rate([unknown]) is None


class TestQueryQuality:
    def test_a_typo_scores_worse_than_the_word_it_misspells(self, connection) -> None:
        assert _quality(connection, "حليبن").severity > _quality(connection, "حليب").severity

    def test_a_typo_is_reported_as_a_retrieval_gap(self, connection) -> None:
        quality = _quality(connection, "حليبن")
        assert quality.retrieval_gap
        assert quality.intended_term == "حليب"
        assert quality.intended_coverage == 4

    def test_every_flagged_query_explains_itself(self, connection) -> None:
        for quality in compute_query_quality(connection):
            if quality.is_problem:
                assert quality.reasons, f"{quality.norm_query} flagged with no reason"

    def test_impact_weights_severity_by_traffic(self, connection) -> None:
        for quality in compute_query_quality(connection):
            expected = quality.severity * (quality.searches / 10)
            assert quality.impact == pytest.approx(expected, abs=0.001)

    def test_results_are_ranked_by_impact(self, connection) -> None:
        impacts = [q.impact for q in compute_query_quality(connection)]
        assert impacts == sorted(impacts, reverse=True)

    def test_severity_weights_form_a_complete_scale(self) -> None:
        assert sum(SEVERITY_WEIGHTS.values()) == pytest.approx(1.0)

    def test_an_unmeasurable_signal_is_dropped_not_assumed_perfect(self, connection) -> None:
        from searchiq.analytics.metrics import _severity

        # A query with no coherence reading must score the same as one where
        # coherence was measured at the level of the other signals — not better.
        with_all = _severity(
            lexical_miss_rate=1.0,
            dissatisfaction_rate=1.0,
            incoherence=1.0,
            under_fill=1.0,
            instability=1.0,
        )
        without_one = _severity(
            lexical_miss_rate=1.0,
            dissatisfaction_rate=1.0,
            incoherence=None,
            under_fill=1.0,
            instability=1.0,
        )
        assert with_all == pytest.approx(1.0)
        assert without_one == pytest.approx(1.0)

    def test_rapid_repeats_are_reported_separately(self, connection) -> None:
        assert _quality(connection, "pas").rapid_repeat_rate > 0


class TestOverview:
    def test_summarises_the_period(self, connection) -> None:
        overview = compute_overview(connection)
        assert overview.total_searches == 10
        # Ten searches over six distinct terms: `فراولة` folds onto `فراوله`,
        # so the two spellings count once.
        assert overview.distinct_queries == 6
        assert 0 <= overview.health_score <= 100

    def test_engagement_source_is_stated_not_implied(self, connection) -> None:
        overview = compute_overview(connection)
        assert overview.engagement_source == "behavioural proxy"
        assert overview.engagement_rate is None

    def test_carries_its_own_caveats(self, connection) -> None:
        notes = compute_overview(connection).notes
        assert any("click" in note for note in notes)

    def test_an_empty_period_is_handled(self, connection) -> None:
        overview = compute_overview(connection, since="2030-01-01", until="2030-12-31")
        assert overview.total_searches == 0
        assert overview.health_score == 100.0


class TestTermDrivers:
    def test_folds_every_spelling_onto_one_row(self, connection) -> None:
        drivers = {driver.term: driver for driver in compute_term_drivers(connection)}
        strawberry = drivers["فراوله"]
        # `فراولة` and `فراولت` are one word to a merchandiser.
        assert strawberry.distinct_spellings >= 2
        assert strawberry.verdict == "spelling"

    def test_names_the_owner_of_each_failure(self, connection) -> None:
        verdicts = {driver.verdict for driver in compute_term_drivers(connection)}
        assert verdicts <= {
            "spelling",
            "retrieval gap",
            "assortment gap",
            "partial query",
            "relevance",
            "healthy",
        }

    def test_every_driver_has_a_headline(self, connection) -> None:
        for driver in compute_term_drivers(connection):
            assert driver.headline
