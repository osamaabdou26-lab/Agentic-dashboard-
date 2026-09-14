"""Discovery and the review queue.

The bar for a proposal is deliberately high, so most of these tests assert what
the system *refuses* to propose. A wrong synonym silently rewrites what a
shopper asked for, and there is no feedback loop that would catch it.
"""

from __future__ import annotations

import pytest

from searchiq.analytics.catalog import CatalogIndex
from searchiq.discovery import misspellings, review, synonyms


def _by_source(candidates):
    return {candidate.source_term: candidate for candidate in candidates}


class TestMisspellings:
    def test_finds_a_one_letter_typo(self, connection) -> None:
        found = _by_source(misspellings.discover(connection))
        assert "حليبن" in found
        assert found["حليبن"].target_term == "حليب"
        assert found["حليبن"].kind == "misspelling"

    def test_direction_is_decided_by_the_catalogue_not_frequency(self, connection) -> None:
        # `حليبن` is searched here and `حليب` is stocked, so the correction runs
        # towards the stocked word regardless of which was typed more.
        found = _by_source(misspellings.discover(connection))
        assert "حليب" not in found  # never proposed as a misspelling of anything

    def test_never_corrects_a_word_the_catalogue_uses(self, connection) -> None:
        for candidate in misspellings.discover(connection):
            catalog = CatalogIndex.for_connection(connection)
            assert catalog.coverage(candidate.source_term) == 0

    def test_a_partial_query_is_not_called_a_misspelling(self, connection) -> None:
        # `str` begins `strawberries`; the shopper had not finished typing, and
        # rewriting their query would be wrong.
        candidates = misspellings.discover(connection)
        for candidate in candidates:
            if candidate.source_term == "str":
                assert candidate.kind == "partial_query"

    def test_leaves_multi_word_queries_alone(self, connection) -> None:
        for candidate in misspellings.discover(connection):
            assert " " not in candidate.source_term

    def test_confidence_rises_with_catalogue_support(self) -> None:
        thin = misspellings._confidence(distance=1, support=3, corrected_in_session=0)
        thick = misspellings._confidence(distance=1, support=400, corrected_in_session=0)
        assert thick > thin

    def test_confidence_falls_as_the_correction_travels_further(self) -> None:
        near = misspellings._confidence(distance=1, support=300, corrected_in_session=0)
        far = misspellings._confidence(distance=2, support=300, corrected_in_session=0)
        assert near > far

    def test_no_proposal_claims_certainty(self, connection) -> None:
        for candidate in misspellings.discover(connection):
            assert candidate.confidence < 1.0

    def test_every_proposal_carries_its_evidence(self, connection) -> None:
        for candidate in misspellings.discover(connection):
            assert candidate.rationale
            assert "target_product_count" in candidate.evidence


class TestSynonyms:
    def test_induces_a_translation_from_the_bilingual_catalogue(
        self, connection, monkeypatch
    ) -> None:
        # Production requires eight co-occurring products before calling a pair
        # a translation; the ten-product fixture cannot reach that, so the bar
        # is lowered here to test the algorithm rather than the threshold.
        monkeypatch.setattr(synonyms, "_MIN_ALIGNMENT_SUPPORT", 3)

        pairs = {
            frozenset((candidate.source_term, candidate.target_term))
            for candidate in synonyms.discover(connection)
        }
        assert frozenset(("حليب", "milk")) in pairs

    def test_a_thinly_supported_translation_is_not_proposed(self, connection) -> None:
        # The same data at the production threshold yields nothing: four
        # products is not evidence of a translation.
        pairs = {
            frozenset((candidate.source_term, candidate.target_term))
            for candidate in synonyms.discover(connection)
        }
        assert frozenset(("حليب", "milk")) not in pairs

    def test_two_failing_queries_are_not_synonyms(self, connection) -> None:
        # `حليبن` and `فراولت` both return the same beef burger, because both
        # fail in the same direction. Shared failure is not shared meaning.
        pairs = {
            frozenset((candidate.source_term, candidate.target_term))
            for candidate in synonyms.discover(connection)
        }
        assert frozenset(("حليبن", "فراولت")) not in pairs

    def test_a_rewording_alone_is_not_enough(self) -> None:
        # Consecutive searches cannot distinguish "same intent, other word" from
        # "next item on the list", so the signal must not clear the bar alone.
        assert synonyms._confidence(["reworded_by_shopper"]) < synonyms.MIN_CONFIDENCE

    def test_corroboration_lifts_it_over_the_bar(self) -> None:
        assert (
            synonyms._confidence(["reworded_by_shopper", "shared_translation"])
            >= synonyms.MIN_CONFIDENCE
        )

    def test_agreement_between_methods_beats_any_single_method(self) -> None:
        single = synonyms._confidence(["catalogue_alignment"])
        both = synonyms._confidence(["catalogue_alignment", "result_overlap"])
        assert both > single

    def test_repeating_one_signal_does_not_inflate_confidence(self) -> None:
        once = synonyms._confidence(["catalogue_alignment"])
        twice = synonyms._confidence(["catalogue_alignment", "catalogue_alignment"])
        assert once == twice

    @pytest.mark.parametrize(
        ("left", "right"),
        [("حليب", "حليبن"), ("pas", "pasta"), ("فراوله", "فراولت")],
    )
    def test_spelling_variants_are_left_to_the_other_channel(self, left, right) -> None:
        assert synonyms._is_spelling_variant(left, right)

    def test_a_pair_is_proposed_once_not_twice(self, connection) -> None:
        seen = [
            frozenset((candidate.source_term, candidate.target_term))
            for candidate in synonyms.discover(connection)
        ]
        assert len(seen) == len(set(seen))


class TestReviewQueue:
    def test_discovery_populates_the_queue_as_pending(self, discovered) -> None:
        suggestions = review.list_suggestions(discovered)
        assert suggestions
        assert all(item.status == "pending" for item in suggestions)

    def test_approving_records_the_reviewer(self, discovered) -> None:
        first = review.list_suggestions(discovered, status="pending")[0]
        updated = review.approve(discovered, first.id, reviewer="ana", note="checked")
        assert updated.status == "approved"
        assert updated.reviewer == "ana"
        assert updated.review_note == "checked"

    def test_rerunning_discovery_never_overrules_a_person(self, discovered) -> None:
        first = review.list_suggestions(discovered, status="pending")[0]
        review.reject(discovered, first.id, reviewer="ana")

        report = review.refresh(discovered)

        assert report.unchanged_by_decision >= 1
        after = discovered.execute(
            "SELECT status FROM suggestion WHERE id = ?", (first.id,)
        ).fetchone()[0]
        assert after == "rejected"

    def test_rerunning_rescores_what_is_still_pending(self, discovered) -> None:
        report = review.refresh(discovered)
        assert report.proposed == 0  # nothing new on identical data
        assert report.updated > 0

    def test_a_pending_proposal_nothing_supports_any_more_is_withdrawn(
        self, discovered
    ) -> None:
        # Evidence is computed from the loaded data. Re-loading different data
        # can leave a proposal standing that nothing backs any more, and a
        # reviewer approving it would be acting on a rationale that has quietly
        # stopped being true.
        discovered.execute(
            "INSERT INTO suggestion (kind, source_term, target_term, lang, "
            "confidence, rationale, evidence, status, created_at, updated_at) "
            "VALUES ('misspelling','ghost','gone','en',0.9,'from another dataset',"
            "'{}','pending','2020-01-01','2020-01-01')"
        )

        report = review.refresh(discovered)

        assert report.withdrawn == 1
        assert not [
            item
            for item in review.list_suggestions(discovered, limit=500)
            if item.source_term == "ghost"
        ]

    def test_a_decided_proposal_is_never_withdrawn(self, discovered) -> None:
        # An approved or rejected proposal is the record of a human decision.
        # Losing support for it is not grounds for deleting that record.
        discovered.execute(
            "INSERT INTO suggestion (kind, source_term, target_term, lang, "
            "confidence, rationale, evidence, status, reviewer, created_at, updated_at) "
            "VALUES ('misspelling','ghost','gone','en',0.9,'from another dataset',"
            "'{}','rejected','ana','2020-01-01','2020-01-01')"
        )

        report = review.refresh(discovered)

        assert report.withdrawn == 0
        assert [
            item
            for item in review.list_suggestions(discovered, status="rejected")
            if item.source_term == "ghost"
        ]

    def test_an_unchanged_dataset_withdraws_nothing(self, discovered) -> None:
        assert review.refresh(discovered).withdrawn == 0

    def test_an_unknown_decision_is_rejected(self, discovered) -> None:
        first = review.list_suggestions(discovered)[0]
        with pytest.raises(ValueError, match="decision must be"):
            review.decide(discovered, first.id, decision="maybe")

    def test_deciding_on_a_missing_proposal_fails_loudly(self, discovered) -> None:
        with pytest.raises(KeyError):
            review.approve(discovered, 999_999)

    def test_a_decision_can_be_undone(self, discovered) -> None:
        # A reviewer who cannot take a decision back hesitates over every
        # borderline proposal, and the queue stops moving.
        first = review.list_suggestions(discovered, status="pending")[0]
        review.approve(discovered, first.id, reviewer="ana")

        reopened = review.reopen(discovered, first.id, reviewer="ana", note="on second thoughts")

        assert reopened.status == "pending"
        assert reopened.review_note == "on second thoughts"

    def test_undoing_an_approval_takes_it_out_of_the_export(self, discovered) -> None:
        pending = review.list_suggestions(discovered, kind="misspelling", status="pending")
        review.approve(discovered, pending[0].id, reviewer="ana")
        assert review.export_approved(discovered)["approved_count"] == 1

        review.reopen(discovered, pending[0].id, reviewer="ana")

        assert review.export_approved(discovered)["approved_count"] == 0

    def test_a_reopened_proposal_is_rescored_by_the_next_discovery_run(
        self, discovered
    ) -> None:
        # Reopening restores the proposal to the state discovery left it in, so
        # it stops being treated as already-reviewed.
        first = review.list_suggestions(discovered, status="pending")[0]
        review.reject(discovered, first.id, reviewer="ana")
        review.reopen(discovered, first.id, reviewer="ana")

        report = review.refresh(discovered)

        assert (
            discovered.execute(
                "SELECT status FROM suggestion WHERE id = ?", (first.id,)
            ).fetchone()[0]
            == "pending"
        )
        assert report.updated > 0

    def test_export_contains_only_approved_rules(self, discovered) -> None:
        pending = review.list_suggestions(discovered, kind="misspelling", status="pending")
        review.approve(discovered, pending[0].id, reviewer="ana")

        document = review.export_approved(discovered)

        assert document["approved_count"] == 1
        assert document["spelling_corrections"] == {
            pending[0].source_term: pending[0].target_term
        }

    def test_export_is_empty_before_anyone_approves_anything(self, discovered) -> None:
        document = review.export_approved(discovered)
        assert document["approved_count"] == 0
        assert document["synonym_groups"] == []

    def test_synonym_pairs_merge_into_transitive_groups(self) -> None:
        # a == b and b == c means all three expand to each other; a search
        # engine wants the group, not the pairs.
        groups = review._merge_into_groups([("a", "b"), ("b", "c"), ("x", "y")])
        assert ["a", "b", "c"] in groups
        assert ["x", "y"] in groups

    def test_kinds_are_exported_to_their_own_sections(self, discovered) -> None:
        for kind in ("misspelling", "synonym", "partial_query"):
            for item in review.list_suggestions(discovered, kind=kind, status="pending"):
                review.approve(discovered, item.id, reviewer="ana")

        document = review.export_approved(discovered)

        # Spelling corrections rewrite a query; autocomplete hints do not. They
        # must never end up in the same bucket.
        assert set(document["spelling_corrections"]) & set(document["autocomplete_hints"]) == set()
