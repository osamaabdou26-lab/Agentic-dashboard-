"""The tool layer, the offline planner, and the digest.

The model path cannot be exercised without a live API key, so these tests cover
the parts that determine whether a model answer would be trustworthy: that the
tool registry is well-formed, that tools reject what they should reject, and
that the offline planner reaches the same numbers through the same tools.
"""

from __future__ import annotations

import json

import pytest

from searchiq.agent import agent as agent_module
from searchiq.agent import tools as toolkit
from searchiq.agent.digest import generate_digest


class TestToolRegistry:
    def test_every_tool_is_described_for_the_model(self) -> None:
        for tool in toolkit.TOOLS:
            assert tool.name
            assert len(tool.description) > 40, f"{tool.name} needs a usable description"
            assert tool.input_schema["type"] == "object"
            assert "properties" in tool.input_schema

    def test_required_arguments_exist_in_the_schema(self) -> None:
        for tool in toolkit.TOOLS:
            properties = set(tool.input_schema.get("properties", {}))
            assert set(tool.input_schema.get("required", [])) <= properties

    def test_names_are_unique(self) -> None:
        names = [tool.name for tool in toolkit.TOOLS]
        assert len(names) == len(set(names))

    def test_schema_order_is_stable(self) -> None:
        # The tool list is part of the cached request prefix; reshuffling it
        # would invalidate the prompt cache on every single call.
        assert [t["name"] for t in toolkit.api_schemas()] == [t.name for t in toolkit.TOOLS]

    def test_every_result_is_json_serialisable(self, discovered) -> None:
        for tool in toolkit.TOOLS:
            if tool.input_schema.get("required"):
                continue
            result = toolkit.run_tool(discovered, tool.name, {})
            json.dumps(result, ensure_ascii=False, default=str)


class TestToolExecution:
    def test_an_unknown_tool_returns_an_error_not_an_exception(self, connection) -> None:
        # The agent must be able to read and recover from its own mistake.
        result = toolkit.run_tool(connection, "no_such_tool", {})
        assert "error" in result
        assert "available" in result

    def test_an_unknown_argument_is_refused(self, connection) -> None:
        result = toolkit.run_tool(connection, "get_search_health", {"nope": 1})
        assert "error" in result

    def test_limits_are_clamped(self, connection) -> None:
        result = toolkit.run_tool(connection, "list_recent_searches", {"limit": 10_000})
        assert len(result) <= 100

    def test_health_carries_its_caveats(self, connection) -> None:
        result = toolkit.run_tool(connection, "get_search_health", {})
        assert result["notes"]

    def test_query_detail_for_an_unknown_query_says_so(self, connection) -> None:
        result = toolkit.run_tool(connection, "get_query_detail", {"query": "ارز بسمتي"})
        assert result["found"] is False

    def test_query_detail_includes_what_was_returned(self, connection) -> None:
        result = toolkit.run_tool(connection, "get_query_detail", {"query": "حليبن"})
        assert result["sample_results"]

    def test_coverage_separates_retrieval_from_assortment(self, connection) -> None:
        stocked = toolkit.run_tool(connection, "check_catalogue_coverage", {"term": "حليب"})
        absent = toolkit.run_tool(connection, "check_catalogue_coverage", {"term": "زعفران"})
        assert stocked["exact_product_count"] > 0
        assert absent["exact_product_count"] == 0

    def test_dataset_profile_reports_missing_click_data(self, connection) -> None:
        result = toolkit.run_tool(connection, "describe_dataset", {})
        assert result["click_data_available"] is False


class TestOfflinePlanner:
    @pytest.mark.parametrize(
        ("question", "expected_tool"),
        [
            ("How is search doing?", "get_search_health"),
            ("What are the worst queries?", "list_problem_queries"),
            ("What synonyms did you find?", "list_suggestions"),
            ("Show me the misspellings", "list_suggestions"),
            ("Do we sell strawberries?", "check_catalogue_coverage"),
            ("How much data is loaded?", "describe_dataset"),
        ],
    )
    def test_routes_questions_to_the_right_tool(
        self, discovered, question: str, expected_tool: str
    ) -> None:
        answer = agent_module.ask(discovered, question)
        assert [call.name for call in answer.tool_calls] == [expected_tool]

    def test_a_named_query_routes_to_that_query(self, discovered) -> None:
        answer = agent_module.ask(discovered, "tell me about حليبن")
        assert [call.name for call in answer.tool_calls] == ["get_query_detail"]
        assert "حليب" in answer.answer

    def test_a_quoted_query_wins_over_keyword_routing(self, discovered) -> None:
        answer = agent_module.ask(discovered, 'what is wrong with "حليبن"?')
        assert [call.name for call in answer.tool_calls] == ["get_query_detail"]

    def test_reports_which_path_answered(self, discovered) -> None:
        answer = agent_module.ask(discovered, "How is search doing?")
        assert answer.source == "deterministic"
        assert answer.model is None

    def test_every_answer_carries_its_trace(self, discovered) -> None:
        answer = agent_module.ask(discovered, "What needs attention?")
        assert answer.tool_calls
        assert all(call.result_preview for call in answer.tool_calls)

    def test_an_empty_question_is_refused(self, connection) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            agent_module.ask(connection, "   ")

    def test_the_answer_states_that_engagement_is_inferred(self, discovered) -> None:
        answer = agent_module.ask(discovered, "How is search doing?")
        assert "click" in answer.answer.lower()

    def test_a_comparison_question_without_dates_asks_for_them(self, discovered) -> None:
        answer = agent_module.ask(discovered, "what changed since last week?")
        assert "date" in answer.answer.lower()
        assert not answer.tool_calls

    def test_a_comparison_question_with_dates_runs_the_comparison(
        self, discovered
    ) -> None:
        answer = agent_module.ask(
            discovered,
            "compare 2025-02-24 to 2025-03-02 against 2025-03-03 to 2025-03-04",
        )
        call = answer.tool_calls[0]
        assert call.name == "compare_periods"
        assert call.arguments["baseline_start"] == "2025-02-24"
        assert call.arguments["current_end"] == "2025-03-04"

    def test_one_date_range_takes_the_window_before_it_as_the_baseline(
        self, discovered
    ) -> None:
        # "How did this week compare?" has one obvious answer — against the week
        # before — and no other reading that avoids guessing a period length.
        answer = agent_module.ask(discovered, "compare 2025-03-01 to 2025-03-04")
        arguments = answer.tool_calls[0].arguments
        assert arguments["current_start"] == "2025-03-01"
        assert arguments["baseline_start"] == "2025-02-26"
        assert arguments["baseline_end"] == "2025-03-01"

    def test_every_bound_tool_is_reachable_without_a_model(self, discovered) -> None:
        # The offline planner is not a reduced version of the agent: it is the
        # same tools, routed by intent. A tool no question can reach is a tool
        # the deployment without an API key silently does not have.
        questions = [
            "How is search doing?",
            "What are the worst queries?",
            'what is wrong with "حليبن"?',
            "What synonyms did you find?",
            "Do we sell strawberries?",
            "compare 2025-02-24 to 2025-03-02 against 2025-03-03 to 2025-03-04",
            "show me the most recent searches",
            "How much data is loaded?",
        ]
        reached = {
            call.name
            for question in questions
            for call in agent_module.ask(discovered, question).tool_calls
        }
        assert reached == set(toolkit.TOOLS_BY_NAME)


class TestDigest:
    def test_produces_a_complete_digest_without_a_model(self, discovered) -> None:
        digest = generate_digest(discovered, days=7)
        assert digest.narrative_source == "deterministic"
        assert "## What needs attention" in digest.body_md
        assert "## Awaiting review" in digest.body_md

    def test_says_so_when_there_is_nothing_to_compare_against(self, discovered) -> None:
        digest = generate_digest(discovered, days=7)
        assert digest.metrics["has_baseline"] is False
        assert "no earlier period" in digest.body_md.lower()

    def test_the_period_is_anchored_to_the_log_not_to_today(self, discovered) -> None:
        # A digest run today against a January extract should describe January,
        # not report an empty week and look like a broken pipeline.
        digest = generate_digest(discovered, days=7)
        assert digest.period_end.startswith("2025-03-03")

    def test_names_the_queries_needing_attention(self, discovered) -> None:
        digest = generate_digest(discovered, days=7)
        assert digest.metrics["problem_queries"]

    def test_surfaces_the_review_queue(self, discovered) -> None:
        digest = generate_digest(discovered, days=7)
        assert digest.metrics["pending_suggestions"]["total"] > 0
        assert "approves and exports" in digest.body_md

    def test_reports_zero_result_queries_and_who_owns_them(self, connection) -> None:
        # The fixture log has no empty result set, because the engine it models
        # always answers. The digest has to say so rather than leave the section
        # blank, which would read as "we checked and found nothing wrong".
        digest = generate_digest(connection, days=7)
        assert "## Searches that came back empty" in digest.body_md
        assert "nearest neighbours" in digest.body_md

    def test_names_the_empty_searches_when_there_are_any(
        self, configured, tmp_path
    ) -> None:
        from searchiq.ingest.loader import load
        from searchiq.store.db import connect

        # One unstocked term, searched twice and answered with nothing.
        dump = tmp_path / "empty.sql"
        dump.write_text(
            "\n".join(
                [
                    "INSERT INTO `catalog_category` VALUES (1,NULL,'a','cat1','b');",
                    "INSERT INTO `catalog_product` VALUES "
                    "(1,'a','400001',NULL,'b','milk',10.0,NULL);",
                    "INSERT INTO `catalog_productname` VALUES "
                    "(1,'Juhayna Milk 1 L','English','a',1);",
                    "INSERT INTO `recommendations_searches` VALUES "
                    "(1,'kombucha','','',0,'2025-03-03 09:00:00.000000'),"
                    "(2,'kombucha','','',0,'2025-03-03 09:05:00.000000');",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        load(db_path=configured, dump_path=dump)

        with connect(configured) as connection:
            digest = generate_digest(connection, days=7)

        rows = digest.metrics["zero_result_queries"]
        assert [row["query"] for row in rows] == ["kombucha"]
        assert rows[0]["empty_searches"] == 2
        # Nothing in the catalogue uses the term, so this is a buying decision
        # and must not be handed to the search team as a retrieval fault.
        assert rows[0]["cause"] == "assortment gap"
        assert "| “kombucha” |" in digest.body_md

    def test_tracks_the_zero_result_rate_against_the_previous_period(
        self, discovered
    ) -> None:
        # The fixture log is one 20-minute window, so the earlier period is
        # empty and the comparison table is deliberately withheld. The movement
        # is still computed, which is what this asserts; the rendered row is
        # covered where a real baseline exists (see `tests/test_sample_data.py`).
        digest = generate_digest(discovered, days=1, end="2025-03-03 23:00:00")
        assert "zero_result_rate" in digest.metrics["changes"]

    def test_is_stored_for_later(self, discovered) -> None:
        generate_digest(discovered, days=7)
        stored = discovered.execute("SELECT COUNT(*) FROM digest").fetchone()[0]
        assert stored == 1

    def test_regenerating_replaces_rather_than_duplicates(self, discovered) -> None:
        generate_digest(discovered, days=7)
        generate_digest(discovered, days=7)
        stored = discovered.execute("SELECT COUNT(*) FROM digest").fetchone()[0]
        assert stored == 1

    def test_an_empty_period_does_not_crash(self, discovered) -> None:
        digest = generate_digest(discovered, days=1, end="2030-01-01", store=False)
        assert "No searches were logged" in digest.body_md
