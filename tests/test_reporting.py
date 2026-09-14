"""The Power BI export.

The thing worth testing here is not that CSV files appear. It is that a report
built on these tables reports the same numbers as the dashboard — because the
moment the two can disagree, both become untrustworthy and somebody has to
reconcile them by hand. So most of this file cross-checks the exported figures
against the analytics functions directly.

The second concern is shape. A BI tool types a column from the values it sees,
so a nested object or a Python `True` in a CSV is not a cosmetic problem: it
breaks the load, or silently types a column as text.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from searchiq.analytics.metrics import compute_overview, compute_query_quality
from searchiq.reporting import bi


@pytest.fixture
def client(discovered_store: Path) -> TestClient:
    from searchiq.api.app import app

    return TestClient(app)


class TestTableShape:
    """A BI tool loads rectangles. Anything else is a broken refresh."""

    @pytest.mark.parametrize("name", [table.name for table in bi.TABLES])
    def test_every_table_builds(self, discovered, name: str) -> None:
        rows = bi.build_table(discovered, name)
        assert isinstance(rows, list)

    @pytest.mark.parametrize("name", [table.name for table in bi.TABLES])
    def test_no_value_is_nested(self, discovered, name: str) -> None:
        # A dict or list in a cell means Power Query shows "Record"/"List" and
        # the person loading it has to expand by hand — every time a field is
        # added.
        for row in bi.build_table(discovered, name):
            for column, value in row.items():
                assert not isinstance(value, (dict, list)), f"{name}.{column} is nested"

    @pytest.mark.parametrize("name", [table.name for table in bi.TABLES])
    def test_booleans_are_emitted_as_integers(self, discovered, name: str) -> None:
        # `True` reaches a CSV as the text "True", which Power Query types as a
        # string and cannot sum.
        for row in bi.build_table(discovered, name):
            for column, value in row.items():
                assert not isinstance(value, bool), f"{name}.{column} is a bool"

    @pytest.mark.parametrize("name", [table.name for table in bi.TABLES])
    def test_every_row_has_the_same_columns(self, discovered, name: str) -> None:
        rows = bi.build_table(discovered, name)
        if not rows:
            return
        expected = set(rows[0])
        assert all(set(row) == expected for row in rows)

    def test_an_unknown_table_is_refused_by_name(self, discovered) -> None:
        with pytest.raises(KeyError, match="unknown table"):
            bi.build_table(discovered, "fact_nonsense")


class TestAgreementWithTheDashboard:
    """The whole point: one set of numbers, not two."""

    def test_query_scores_match_the_analytics_layer(self, discovered) -> None:
        expected = {
            item.norm_query: (item.severity, item.impact, item.searches)
            for item in compute_query_quality(discovered)
        }
        actual = {
            row["norm_query"]: (row["severity"], row["impact"], row["searches"])
            for row in bi.build_table(discovered, "dim_query")
        }
        assert actual == expected

    def test_headline_figures_match_the_overview(self, discovered) -> None:
        overview = compute_overview(discovered)
        row = bi.build_table(discovered, "dim_overview")[0]
        assert row["health_score"] == overview.health_score
        assert row["total_searches"] == overview.total_searches
        assert row["problem_queries"] == overview.problem_queries
        assert row["zero_result_rate"] == overview.zero_result_rate

    def test_the_fact_table_carries_every_logged_search(self, discovered) -> None:
        logged = discovered.execute("SELECT COUNT(*) FROM search_event").fetchone()[0]
        assert len(bi.build_table(discovered, "fact_search")) == logged

    def test_event_rows_join_to_query_rows(self, discovered) -> None:
        # The model documented in the export README is a many-to-one join on
        # `norm_query`. An orphan event would drop out of every visual built on
        # that relationship, silently.
        queries = {row["norm_query"] for row in bi.build_table(discovered, "dim_query")}
        events = bi.build_table(discovered, "fact_search")
        assert events
        assert all(row["norm_query"] in queries for row in events)

    def test_severity_is_joined_not_recomputed(self, discovered) -> None:
        by_query = {
            item.norm_query: item.severity for item in compute_query_quality(discovered)
        }
        for row in bi.build_table(discovered, "fact_search"):
            assert row["query_severity"] == by_query[row["norm_query"]]

    def test_the_review_queue_is_exported_whole(self, discovered) -> None:
        from searchiq.discovery import review

        expected = len(review.list_suggestions(discovered, limit=100_000))
        assert len(bi.build_table(discovered, "fact_suggestion")) == expected

    def test_a_decision_shows_up_in_the_export(self, discovered) -> None:
        from searchiq.discovery import review

        first = review.list_suggestions(discovered, status="pending")[0]
        review.approve(discovered, first.id, reviewer="ana", note="checked")

        row = next(
            item
            for item in bi.build_table(discovered, "fact_suggestion")
            if item["suggestion_id"] == first.id
        )
        assert row["status"] == "approved"
        assert row["is_approved"] == 1
        assert row["is_pending"] == 0
        assert row["reviewer"] == "ana"


class TestDailySeries:
    def test_days_with_no_traffic_are_omitted(self, discovered) -> None:
        # Reporting an untravelled day as 100/100 health would put a flattering
        # spike in every trend chart.
        rows = bi.build_table(discovered, "fact_daily")
        assert rows
        assert all(row["total_searches"] > 0 for row in rows)

    def test_daily_searches_sum_to_the_total(self, discovered) -> None:
        rows = bi.build_table(discovered, "fact_daily")
        total = compute_overview(discovered).total_searches
        assert sum(row["total_searches"] for row in rows) == total

    def test_dates_are_plain_and_sorted(self, discovered) -> None:
        dates = [row["date"] for row in bi.build_table(discovered, "fact_daily")]
        assert dates == sorted(dates)
        assert all(len(date) == 10 for date in dates)

    def test_an_empty_store_yields_no_rows_rather_than_failing(
        self, configured: Path, tmp_path: Path
    ) -> None:
        from searchiq.ingest.loader import load
        from searchiq.store.db import connect

        empty = tmp_path / "empty.sql"
        empty.write_text("-- nothing here\n", encoding="utf-8")
        load(db_path=configured, dump_path=empty)

        with connect(configured) as connection:
            assert bi.build_table(connection, "fact_daily") == []


class TestCsvExport:
    @pytest.fixture
    def exported(self, discovered, tmp_path: Path) -> Path:
        out = tmp_path / "bi"
        bi.export_bi(discovered, out)
        return out

    def test_writes_one_file_per_table(self, exported: Path) -> None:
        for table in bi.TABLES:
            assert (exported / f"{table.name}.csv").is_file()

    def test_writes_a_manifest_and_a_readme(self, exported: Path) -> None:
        manifest = json.loads((exported / "manifest.json").read_text(encoding="utf-8"))
        assert {entry["name"] for entry in manifest["tables"]} == set(bi.table_names())
        assert "Power BI" in (exported / "README.md").read_text(encoding="utf-8")

    def test_csv_is_utf8_with_a_bom(self, exported: Path) -> None:
        # Without the BOM, Excel and older Power BI builds read the file in the
        # system code page and every Arabic query comes out as mojibake — which
        # looks like a data problem and is really an encoding default.
        assert (exported / "dim_query.csv").read_bytes().startswith(b"\xef\xbb\xbf")

    def test_arabic_survives_the_round_trip(self, exported: Path) -> None:
        with (exported / "dim_query.csv").open(encoding="utf-8-sig", newline="") as handle:
            queries = {row["norm_query"] for row in csv.DictReader(handle)}
        assert "حليب" in queries

    def test_a_pbids_is_written_only_when_a_url_is_given(
        self, discovered, tmp_path: Path
    ) -> None:
        without = tmp_path / "a"
        bi.export_bi(discovered, without)
        assert not (without / "search-pulse.pbids").exists()

        with_url = tmp_path / "b"
        report = bi.export_bi(discovered, with_url, base_url="http://127.0.0.1:8000/")
        document = json.loads(report.pbids_path.read_text(encoding="utf-8"))
        # The trailing slash must not survive into the URL.
        assert (
            document["connections"][0]["details"]["address"]["url"]
            == "http://127.0.0.1:8000/api/bi/tables"
        )


class TestBiEndpoints:
    def test_lists_the_tables_with_loadable_urls(self, client: TestClient) -> None:
        body = client.get("/api/bi/tables").json()
        assert {entry["name"] for entry in body["tables"]} == set(bi.table_names())
        for entry in body["tables"]:
            assert client.get(entry["url"]).status_code == 200

    def test_a_table_is_a_bare_array_of_records(self, client: TestClient) -> None:
        # Power Query turns an array of flat records straight into a table. An
        # envelope adds a navigation step every person connecting has to get
        # right.
        body = client.get("/api/bi/dim_query").json()
        assert isinstance(body, list)
        assert isinstance(body[0], dict)

    def test_matches_what_the_csv_export_writes(
        self, client: TestClient, discovered_store: Path
    ) -> None:
        from searchiq.store.db import connect

        with connect(discovered_store) as connection:
            expected = bi.build_table(connection, "dim_query")
        assert client.get("/api/bi/dim_query").json() == expected

    def test_an_unknown_table_is_a_404_that_names_the_real_ones(
        self, client: TestClient
    ) -> None:
        response = client.get("/api/bi/fact_nonsense")
        assert response.status_code == 404
        assert "dim_query" in response.json()["detail"]

    def test_the_daily_window_is_bounded(self, client: TestClient) -> None:
        assert client.get("/api/bi/fact_daily?days=0").status_code == 422
        assert client.get("/api/bi/fact_daily?days=5000").status_code == 422
