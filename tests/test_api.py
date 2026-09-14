"""The HTTP API.

Three things matter beyond the happy path: that read endpoints cannot mutate the
store, that a missing store produces an instruction rather than a stack trace,
and that the review endpoints are the only door a decision can come through.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(discovered_store: Path) -> TestClient:
    """A client against a fully loaded, fully discovered store.

    `discovered_store` closes its connection before yielding, so the app is the
    only writer while the test runs.
    """
    from searchiq.api.app import app

    # The store fixture already pointed settings at the temporary database, and
    # the app opens it per request, so no further wiring is needed.
    return TestClient(app)


class TestStatus:
    def test_reports_readiness_and_provenance(self, client: TestClient) -> None:
        body = client.get("/api/status").json()
        assert body["ready"] is True
        assert body["meta"]["rows.search_event"] == "10"

    def test_reports_which_agent_path_is_active(self, client: TestClient) -> None:
        body = client.get("/api/status").json()
        assert body["agent_mode"] == "deterministic"
        assert body["model"] is None

    def test_a_missing_store_gives_an_instruction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from searchiq.api.app import app
        from searchiq.config import settings

        monkeypatch.setenv("SEARCHIQ_DB", str(tmp_path / "absent.db"))
        settings.cache_clear()
        try:
            response = TestClient(app).get("/api/overview")
            assert response.status_code == 503
            assert "searchiq etl" in response.json()["detail"]
        finally:
            settings.cache_clear()


class TestAnalyticsEndpoints:
    def test_overview(self, client: TestClient) -> None:
        body = client.get("/api/overview").json()
        assert body["total_searches"] == 10
        assert body["notes"]

    def test_overview_accepts_a_period(self, client: TestClient) -> None:
        body = client.get("/api/overview?since=2030-01-01&until=2030-12-31").json()
        assert body["total_searches"] == 0

    def test_queries_can_be_narrowed_to_failures(self, client: TestClient) -> None:
        every = client.get("/api/queries").json()
        failing = client.get("/api/queries?problems_only=true").json()
        assert len(failing) <= len(every)
        assert all(item["is_problem"] for item in failing)

    def test_query_detail_returns_bilingual_results(self, client: TestClient) -> None:
        body = client.get("/api/queries/حليبن").json()
        assert body["retrieval_gap"] is True
        assert body["sample_results"][0]["ar"]
        assert body["sample_results"][0]["en"]

    def test_an_unlogged_query_is_a_404(self, client: TestClient) -> None:
        assert client.get("/api/queries/ارز").status_code == 404

    def test_terms_fold_spellings_together(self, client: TestClient) -> None:
        body = client.get("/api/terms?limit=10").json()
        assert any(item["distinct_spellings"] > 1 for item in body)

    def test_catalogue_lookup(self, client: TestClient) -> None:
        body = client.get("/api/catalogue/حليب").json()
        assert body["exact_product_count"] == 4
        assert body["examples"]

    def test_limits_are_validated(self, client: TestClient) -> None:
        assert client.get("/api/queries?limit=0").status_code == 422
        assert client.get("/api/terms?limit=9999").status_code == 422


class TestReviewEndpoints:
    def test_lists_pending_proposals(self, client: TestClient) -> None:
        body = client.get("/api/suggestions?status=pending").json()
        assert body
        assert all(item["status"] == "pending" for item in body)

    def test_approving_moves_a_proposal(self, client: TestClient) -> None:
        first = client.get("/api/suggestions?status=pending").json()[0]

        response = client.post(
            f"/api/suggestions/{first['id']}/approved", json={"reviewer": "ana"}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "approved"
        assert response.json()["reviewer"] == "ana"

    def test_an_invalid_decision_is_refused(self, client: TestClient) -> None:
        first = client.get("/api/suggestions?status=pending").json()[0]
        response = client.post(f"/api/suggestions/{first['id']}/maybe", json={})
        assert response.status_code == 400

    def test_deciding_on_a_missing_proposal_is_a_404(self, client: TestClient) -> None:
        assert client.post("/api/suggestions/999999/approved", json={}).status_code == 404

    def test_export_only_carries_approved_rules(self, client: TestClient) -> None:
        before = client.get("/api/suggestions/export").json()
        assert before["approved_count"] == 0

        first = client.get("/api/suggestions?status=pending&kind=misspelling").json()[0]
        client.post(f"/api/suggestions/{first['id']}/approved", json={})

        after = client.get("/api/suggestions/export").json()
        assert after["approved_count"] == 1
        assert after["spelling_corrections"][first["source_term"]] == first["target_term"]

    def test_export_is_offered_as_a_download(self, client: TestClient) -> None:
        response = client.get("/api/suggestions/export")
        assert "attachment" in response.headers["content-disposition"]

    def test_a_decision_can_be_undone_through_the_api(self, client: TestClient) -> None:
        first = client.get("/api/suggestions?status=pending&kind=misspelling").json()[0]
        client.post(f"/api/suggestions/{first['id']}/approved", json={"reviewer": "ana"})
        assert client.get("/api/suggestions/export").json()["approved_count"] == 1

        response = client.post(
            f"/api/suggestions/{first['id']}/pending", json={"reviewer": "ana"}
        )

        assert response.json()["status"] == "pending"
        assert client.get("/api/suggestions/export").json()["approved_count"] == 0

    def test_summary_counts_what_is_waiting(self, client: TestClient) -> None:
        before = client.get("/api/suggestions/summary").json()
        assert before["by_status"]["pending"] == before["total"]
        assert before["pending_by_kind"]

        first = client.get("/api/suggestions?status=pending").json()[0]
        client.post(f"/api/suggestions/{first['id']}/approved", json={})

        after = client.get("/api/suggestions/summary").json()
        assert after["by_status"]["pending"] == before["by_status"]["pending"] - 1
        assert after["by_status"]["approved"] == 1
        assert after["total"] == before["total"]

    def test_refresh_does_not_overrule_a_decision(self, client: TestClient) -> None:
        first = client.get("/api/suggestions?status=pending").json()[0]
        client.post(f"/api/suggestions/{first['id']}/rejected", json={})

        report = client.post("/api/suggestions/refresh").json()

        assert report["unchanged_by_decision"] >= 1
        after = client.get("/api/suggestions?status=rejected").json()
        assert any(item["id"] == first["id"] for item in after)


class TestAgentEndpoints:
    def test_ask_returns_an_answer_and_its_trace(self, client: TestClient) -> None:
        body = client.post("/api/ask", json={"question": "What needs attention?"}).json()
        assert body["answer"]
        assert body["source"] == "deterministic"
        assert body["tool_calls"]

    def test_ask_requires_a_question(self, client: TestClient) -> None:
        assert client.post("/api/ask", json={"question": "  "}).status_code == 400

    def test_digest_is_generated_and_stored(self, client: TestClient) -> None:
        body = client.get("/api/digest?days=7").json()
        assert "# Search quality" in body["body_md"]
        assert body["narrative_source"] == "deterministic"

    def test_digest_period_is_validated(self, client: TestClient) -> None:
        assert client.get("/api/digest?days=0").status_code == 422

    def test_digest_can_be_downloaded_as_markdown(self, client: TestClient) -> None:
        response = client.get("/api/digest?days=7&format=markdown")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/markdown")
        assert "attachment" in response.headers["content-disposition"]
        assert response.text.startswith("# Search quality")

    def test_an_unknown_digest_format_is_refused(self, client: TestClient) -> None:
        assert client.get("/api/digest?format=pdf").status_code == 422

    def test_publishes_the_tools_the_agent_is_bound_to(self, client: TestClient) -> None:
        # The agent's guarantee is that it answers only through these, and a
        # claim about scope is only checkable if the scope is visible.
        from searchiq.agent import tools as toolkit

        body = client.get("/api/agent/tools").json()

        assert body["mode"] == "deterministic"
        assert body["tool_count"] == len(toolkit.TOOLS)
        assert {tool["name"] for tool in body["tools"]} == set(toolkit.TOOLS_BY_NAME)
        assert all(tool["description"] and tool["input_schema"] for tool in body["tools"])


class TestDashboard:
    def test_serves_the_dashboard(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "Search Pulse" in response.text

    def test_serves_its_static_assets(self, client: TestClient) -> None:
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/styles.css").status_code == 200
