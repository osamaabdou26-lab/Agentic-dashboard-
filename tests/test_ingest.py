"""The dump reader and the loader.

These two decide what every later number is computed from, so the cases that
matter are the ones where a naive parser quietly gets it wrong: an escaped quote
inside a product name, a newline inside a result list, and SQL `NULL` sitting
next to the literal string `'NULL'`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from searchiq.ingest.dump_reader import iter_table_rows
from searchiq.ingest.loader import SOURCE_TABLES, load
from searchiq.store.db import connect, read_meta


def _write(tmp_path: Path, sql: str) -> Path:
    path = tmp_path / "dump.sql"
    path.write_text(sql, encoding="utf-8")
    return path


class TestDumpReader:
    def test_reads_multiple_rows_from_one_extended_insert(self, dump_path: Path) -> None:
        rows = [
            row
            for table, row in iter_table_rows(dump_path, {"recommendations_searches"})
            if table == "recommendations_searches"
        ]
        assert len(rows) == 10

    def test_skips_tables_outside_the_whitelist(self, dump_path: Path) -> None:
        tables = {table for table, _ in iter_table_rows(dump_path, SOURCE_TABLES)}
        assert "recommendations_productembedding" not in tables

    def test_unquoted_null_becomes_none(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "INSERT INTO `catalog_category` VALUES (1,NULL,'a','b','c');\n")
        (_, row), = iter_table_rows(path, {"catalog_category"})
        assert row[1] is None

    def test_quoted_null_stays_a_string(self, tmp_path: Path) -> None:
        # A product genuinely named "NULL" must not vanish into a SQL NULL.
        path = _write(tmp_path, "INSERT INTO `catalog_category` VALUES (1,'NULL','a','b','c');\n")
        (_, row), = iter_table_rows(path, {"catalog_category"})
        assert row[1] == "NULL"

    @pytest.mark.parametrize(
        ("escaped", "expected"),
        [
            (r"Chef\'s Choice", "Chef's Choice"),
            (r"line one\nline two", "line one\nline two"),
            (r"back\\slash", "back\\slash"),
            (r"tab\there", "tab\there"),
        ],
    )
    def test_escape_sequences(self, tmp_path: Path, escaped: str, expected: str) -> None:
        path = _write(
            tmp_path,
            f"INSERT INTO `catalog_category` VALUES (1,'{escaped}','a','b','c');\n",
        )
        (_, row), = iter_table_rows(path, {"catalog_category"})
        assert row[1] == expected

    def test_structural_characters_inside_a_string_are_data(self, tmp_path: Path) -> None:
        # Commas and parentheses inside a quoted name must not split the row.
        path = _write(
            tmp_path,
            "INSERT INTO `catalog_category` VALUES (1,'Juhayna (0.5% Fat), 1 L','a','b','c');\n",
        )
        rows = list(iter_table_rows(path, {"catalog_category"}))
        assert len(rows) == 1
        assert rows[0][1][1] == "Juhayna (0.5% Fat), 1 L"

    def test_statement_spanning_several_lines(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "INSERT INTO `catalog_category` VALUES (1,'first','a','b','c'),\n"
            "(2,'second','a','b','c');\n",
        )
        rows = list(iter_table_rows(path, {"catalog_category"}))
        assert [row[0] for _, row in rows] == ["1", "2"]

    def test_arabic_survives_the_round_trip(self, dump_path: Path) -> None:
        queries = {
            row[1]
            for table, row in iter_table_rows(dump_path, {"recommendations_searches"})
        }
        assert "حليبن" in queries

    def test_a_missing_file_names_the_setting_to_fix(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="SEARCHIQ_DUMP_PATH"):
            list(iter_table_rows(tmp_path / "absent.sql", {"catalog_product"}))


class TestLoader:
    def test_loads_every_source_table(self, connection) -> None:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("product", "product_name", "category", "search_event", "query_count")
        }
        assert counts == {
            "product": 10,
            "product_name": 20,
            "category": 5,
            "search_event": 10,
            "query_count": 5,
        }

    def test_normalises_queries_at_load_time(self, connection) -> None:
        stored = connection.execute(
            "SELECT norm_query FROM search_event WHERE raw_query = 'فراولة'"
        ).fetchone()[0]
        assert stored == "فراوله"

    def test_derives_sessions_from_inter_search_gaps(self, connection) -> None:
        # Every fixture search falls inside one 30-minute window.
        sessions = connection.execute(
            "SELECT COUNT(DISTINCT session_id) FROM search_event"
        ).fetchone()[0]
        assert sessions == 1

    def test_a_long_gap_starts_a_new_session(self, tmp_path: Path, monkeypatch) -> None:
        from searchiq.config import settings

        monkeypatch.setenv("SEARCHIQ_DB", str(tmp_path / "gap.db"))
        monkeypatch.setenv("SEARCHIQ_SESSION_GAP_SECONDS", "60")
        settings.cache_clear()

        path = _write(
            tmp_path,
            "INSERT INTO `recommendations_searches` VALUES "
            "(1,'a','x','x',1,'2025-03-03 09:00:00.000000'),"
            "(2,'b','x','x',1,'2025-03-03 11:00:00.000000');\n",
        )
        load(db_path=tmp_path / "gap.db", dump_path=path)
        with connect(tmp_path / "gap.db") as conn:
            sessions = conn.execute(
                "SELECT COUNT(DISTINCT session_id) FROM search_event"
            ).fetchone()[0]
        settings.cache_clear()
        assert sessions == 2

    def test_builds_the_catalogue_vocabulary(self, connection) -> None:
        count = connection.execute(
            "SELECT product_count FROM catalog_term WHERE term = 'حليب' AND lang = 'ar'"
        ).fetchone()[0]
        # Four fixture products name milk in Arabic.
        assert count == 4

    def test_indexes_stemmed_forms_too(self, connection) -> None:
        # `الشيف` is merchandised with the definite article; a shopper searching
        # `شيف` must still find it.
        assert connection.execute(
            "SELECT 1 FROM catalog_term WHERE term = 'شيف'"
        ).fetchone() is not None

    def test_records_provenance(self, connection) -> None:
        meta = read_meta(connection)
        assert meta["rows.search_event"] == "10"
        assert "loaded_at" in meta
        assert meta["source"].startswith("mysqldump:")

    def test_result_signature_ignores_ordering(self, connection) -> None:
        # The two `حليبن` searches returned the same products and must share a
        # signature, or the instability metric would fire on identical answers.
        signatures = {
            row[0]
            for row in connection.execute(
                "SELECT result_signature FROM search_event WHERE norm_query = 'حليبن'"
            )
        }
        assert len(signatures) == 1

    def test_reload_preserves_reviewer_decisions(self, store: Path, dump_path: Path) -> None:
        from searchiq.discovery import refresh, review

        with connect(store) as conn:
            refresh(conn)
            pending = review.list_suggestions(conn, status="pending")
            review.reject(conn, pending[0].id, reviewer="tester", note="not a real word")
            rejected_id = pending[0].id

        # A fresh extract must not silently resurrect something a human declined.
        load(db_path=store, dump_path=dump_path)

        with connect(store) as conn:
            row = conn.execute(
                "SELECT status, reviewer FROM suggestion WHERE id = ?", (rejected_id,)
            ).fetchone()
        assert row["status"] == "rejected"
        assert row["reviewer"] == "tester"

    def test_foreign_keys_hold_after_load(self, connection) -> None:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
