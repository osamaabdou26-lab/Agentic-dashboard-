"""The mock data generator.

The generator exists so the system works with no source database, and that
claim is only worth anything if the generated file goes through the *real* ETL
and comes out carrying the patterns the analytics layer is supposed to find. So
these tests run the genuine loader over generated output and then assert on the
store, rather than inspecting the generator's own bookkeeping.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from searchiq.ingest.dump_reader import iter_table_rows
from searchiq.ingest.loader import SOURCE_TABLES, load
from searchiq.ingest.sample_data import generate_sample_dump
from searchiq.store.db import connect

# A fixed end date keeps every assertion about the log window exact. The
# generator otherwise anchors to "now", which is right for a user and useless
# for a test.
_END = datetime(2025, 6, 30, 20, 0, 0)


@pytest.fixture
def sample_dump(tmp_path: Path) -> Path:
    path = tmp_path / "sample.sql"
    generate_sample_dump(path, days=28, sessions_per_day=6, seed=7, end=_END)
    return path


@pytest.fixture
def sample_store(configured: Path, sample_dump: Path) -> Path:
    load(db_path=configured, dump_path=sample_dump)
    return configured


class TestGeneration:
    def test_emits_every_table_the_loader_reads(self, sample_dump: Path) -> None:
        # A generator that skipped one source table would produce a store that
        # loads cleanly and is quietly missing a dimension.
        tables = {table for table, _ in iter_table_rows(sample_dump, SOURCE_TABLES)}
        assert tables == SOURCE_TABLES

    def test_skips_tables_outside_the_whitelist(self, sample_dump: Path) -> None:
        tables = {table for table, _ in iter_table_rows(sample_dump, SOURCE_TABLES)}
        assert "recommendations_productembedding" not in tables

    def test_the_same_seed_produces_the_same_file(self, tmp_path: Path) -> None:
        first, second = tmp_path / "a.sql", tmp_path / "b.sql"
        generate_sample_dump(first, days=6, seed=99, end=_END)
        generate_sample_dump(second, days=6, seed=99, end=_END)
        assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")

    def test_a_different_seed_produces_a_different_log(self, tmp_path: Path) -> None:
        first, second = tmp_path / "a.sql", tmp_path / "b.sql"
        generate_sample_dump(first, days=6, seed=1, end=_END)
        generate_sample_dump(second, days=6, seed=2, end=_END)
        assert first.read_text(encoding="utf-8") != second.read_text(encoding="utf-8")

    def test_refuses_a_period_too_short_to_have_a_baseline(self, tmp_path: Path) -> None:
        # The digest compares a period against the one before it. A single-day
        # log cannot support that, so the generator refuses rather than shipping
        # data the digest will silently report nothing about.
        with pytest.raises(ValueError, match="baseline"):
            generate_sample_dump(tmp_path / "x.sql", days=1)

    def test_reports_what_it_planted(self, tmp_path: Path) -> None:
        report = generate_sample_dump(tmp_path / "x.sql", days=28, seed=7, end=_END)
        assert report.products > 0
        assert report.zero_result_searches > 0
        assert report.typo_searches > 0
        assert report.automated_searches > 0


class TestLoadedStore:
    def test_the_real_etl_loads_it(self, sample_store: Path) -> None:
        with connect(sample_store) as connection:
            searches = connection.execute("SELECT COUNT(*) FROM search_event").fetchone()[0]
            products = connection.execute("SELECT COUNT(*) FROM product").fetchone()[0]
        assert searches > 100
        assert products > 100

    def test_every_product_carries_both_languages(self, sample_store: Path) -> None:
        # Cross-language synonym discovery depends on it, so a generator that
        # quietly emitted one language would disable half the system.
        with connect(sample_store) as connection:
            missing = connection.execute(
                "SELECT COUNT(*) FROM product p WHERE NOT EXISTS ("
                "  SELECT 1 FROM product_name n WHERE n.product_id = p.id AND n.lang = 'ar')"
                " OR NOT EXISTS ("
                "  SELECT 1 FROM product_name n WHERE n.product_id = p.id AND n.lang = 'en')"
            ).fetchone()[0]
        assert missing == 0

    def test_the_log_spans_the_requested_period(self, sample_store: Path) -> None:
        with connect(sample_store) as connection:
            first, last = connection.execute(
                "SELECT MIN(occurred_at), MAX(occurred_at) FROM search_event"
            ).fetchone()
        assert first.startswith("2025-06-0")
        assert last.startswith("2025-06-30")

    def test_contains_zero_result_searches(self, sample_store: Path) -> None:
        with connect(sample_store) as connection:
            empty = connection.execute(
                "SELECT COUNT(*) FROM search_event WHERE result_count = 0"
            ).fetchone()[0]
        assert empty > 0

    def test_sessions_are_derived_not_collapsed_into_one(self, sample_store: Path) -> None:
        # Journeys are spaced beyond the session gap deliberately: behaviour
        # analysis is per session, and one giant session would make every
        # consecutive search look like a reformulation of the last.
        with connect(sample_store) as connection:
            sessions = connection.execute(
                "SELECT COUNT(DISTINCT session_id) FROM search_event"
            ).fetchone()[0]
        assert sessions > 50


class TestDiscoverableByTheRealPipeline:
    """The point of the planted patterns is that discovery finds them."""

    @pytest.fixture
    def proposals(self, sample_store: Path) -> list:
        from searchiq.discovery import refresh
        from searchiq.discovery.review import list_suggestions

        with connect(sample_store) as connection:
            refresh(connection)
            return list_suggestions(connection, limit=500)

    def test_finds_the_planted_misspellings(self, proposals: list) -> None:
        corrections = {
            (p.source_term, p.target_term) for p in proposals if p.kind == "misspelling"
        }
        assert ("حليبن", "حليب") in corrections
        assert ("chiken", "chicken") in corrections

    def test_finds_cross_language_synonyms(self, proposals: list) -> None:
        pairs = {
            frozenset((p.source_term, p.target_term))
            for p in proposals
            if p.kind == "synonym"
        }
        assert frozenset(("milk", "حليب")) in pairs
        assert frozenset(("حليب", "لبن")) in pairs

    def test_a_shopper_who_stopped_typing_is_not_called_a_misspelling(
        self, proposals: list
    ) -> None:
        # `coffe` is one edit from `coffee` and is also its prefix, so the two
        # classifiers both have a claim on it. Prefix wins: rewriting the query
        # of someone who simply had not finished typing would be wrong.
        partials = {p.source_term for p in proposals if p.kind == "partial_query"}
        misspelled = {p.source_term for p in proposals if p.kind == "misspelling"}
        assert "coffe" in partials
        assert not (partials & misspelled)

    def test_short_prefixes_surface_as_terms_rather_than_as_rewrite_rules(
        self, sample_store: Path
    ) -> None:
        # `pas` is three characters: too short for any edit-distance budget, so
        # discovery proposes nothing for it and should not. The term rollup
        # still names it, as a partial query autocomplete would catch — which is
        # the honest reading, and a different team's fix.
        from searchiq.analytics.terms import compute_term_drivers

        with connect(sample_store) as connection:
            verdicts = {
                driver.term: driver.verdict
                for driver in compute_term_drivers(connection, limit=60)
            }
        assert verdicts.get("pas") == "partial query"

    def test_unstocked_terms_are_called_assortment_gaps_not_search_faults(
        self, sample_store: Path
    ) -> None:
        from searchiq.analytics.terms import compute_term_drivers

        with connect(sample_store) as connection:
            verdicts = {
                driver.term: driver.verdict
                for driver in compute_term_drivers(connection, limit=60)
            }
        assert verdicts.get("quinoa") == "assortment gap"

    def test_everything_arrives_pending(self, proposals: list) -> None:
        assert proposals
        assert {p.status for p in proposals} == {"pending"}


class TestWhatTheGeneratedDataMakesPossible:
    """The generated log is long enough for the whole product to work on it."""

    def test_the_digest_can_compare_two_periods(self, sample_store: Path) -> None:
        # A single-day log leaves the comparison table permanently withheld. The
        # generator's default period is deliberately long enough that a reviewer
        # sees the feature rather than the caveat.
        from searchiq.agent.digest import generate_digest

        with connect(sample_store) as connection:
            digest = generate_digest(connection, days=7)

        assert digest.metrics["has_baseline"] is True
        assert "| Metric | Previous | This period | Change |" in digest.body_md
        assert "| Zero-result rate |" in digest.body_md

    def test_the_digest_names_the_empty_searches(self, sample_store: Path) -> None:
        from searchiq.agent.digest import generate_digest

        with connect(sample_store) as connection:
            digest = generate_digest(connection, days=7)

        assert "## Searches that came back empty" in digest.body_md
        assert digest.metrics["zero_result_queries"]

    def test_the_digest_surfaces_the_pending_queue(self, sample_store: Path) -> None:
        from searchiq.agent.digest import generate_digest
        from searchiq.discovery import refresh

        with connect(sample_store) as connection:
            refresh(connection)
            digest = generate_digest(connection, days=7)

        assert digest.metrics["pending_suggestions"]["total"] > 0

    def test_the_agent_answers_from_it(self, sample_store: Path) -> None:
        from searchiq.agent import agent as agent_module

        with connect(sample_store) as connection:
            answer = agent_module.ask(connection, "How is search doing?")

        assert answer.source == "deterministic"
        assert answer.tool_calls
        assert "health" in answer.answer.lower()
