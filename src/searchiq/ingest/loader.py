"""Build the analytics store from a source extract.

One pass over the source produces every table the rest of the system reads. The
load is a full rebuild rather than an incremental merge: the source is a
point-in-time extract, rebuilding takes about a minute, and a store that is
always reproducible from one command is worth more than an incremental path
nobody can verify.

Three things are derived here rather than at query time, because they are
expensive and never change once loaded:

* normalised query and product text  (see `searchiq.text.normalize`)
* session boundaries                 (the source log has no session column)
* the catalogue vocabulary           (the dictionary spelling checks run against)
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from searchiq.config import settings
from searchiq.ingest.dump_reader import Row, iter_table_rows
from searchiq.store.db import apply_schema, connect, write_meta
from searchiq.text.normalize import normalize, script_of, term_variants, tokenize

# Source tables the analytics store is built from. Everything else in the dump
# (embeddings, stock, prices, attributes, Django internals) is 98% of the bytes
# and none of the search-quality signal.
SOURCE_TABLES = {
    "recommendations_searches",
    "recommendations_querycount",
    "catalog_product",
    "catalog_productname",
    "catalog_category",
    "catalog_categoryname",
    "catalog_product_categories",
}

# The source writes language names, not ISO codes.
_LANG_CODES = {"arabic": "ar", "ar": "ar", "english": "en", "en": "en"}

_BATCH = 5_000


@dataclass
class LoadReport:
    """What a load run produced. Echoed to the console and stored in `meta`."""

    source: str = ""
    rows: Counter = field(default_factory=Counter)
    sessions: int = 0
    catalog_terms: int = 0
    orphans_removed: Counter = field(default_factory=Counter)
    skipped_rows: Counter = field(default_factory=Counter)

    def as_lines(self) -> list[str]:
        lines = [f"source: {self.source}"]
        lines += [
            f"  {table:<18} {count:>9,} rows" for table, count in sorted(self.rows.items())
        ]
        lines.append(f"  {'catalog_term':<18} {self.catalog_terms:>9,} rows")
        lines.append(f"  sessions derived: {self.sessions}")
        for table, count in sorted(self.orphans_removed.items()):
            if count:
                lines.append(f"  dropped {count:,} orphaned rows from {table}")
        for reason, count in sorted(self.skipped_rows.items()):
            lines.append(f"  skipped {count:,} rows: {reason}")
        return lines


def load(
    *,
    db_path: Path | None = None,
    dump_path: Path | None = None,
    rows: Iterator | None = None,
    source_label: str | None = None,
) -> LoadReport:
    """Rebuild the analytics store.

    `rows` lets a caller supply an already-open row stream. The live-MySQL
    source and the tests both use it. When omitted, the dump at `dump_path` is
    streamed instead.
    """
    config = settings()
    dump_path = dump_path or config.dump_path
    if rows is None:
        rows = iter_table_rows(dump_path, SOURCE_TABLES)
        source_label = source_label or f"mysqldump: {dump_path}"

    report = LoadReport(source=source_label or "caller-supplied rows")

    with connect(db_path) as connection:
        apply_schema(connection)

        # Bulk loading with referential checks deferred: the dump emits parent
        # tables before children, but relying on that ordering would make the
        # loader fragile. Orphans are found and removed afterwards instead.
        #
        # SQLite silently ignores this pragma inside a transaction, so the
        # implicit one opened by the schema script has to be closed first, and
        # the result is asserted rather than assumed.
        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0]:
            raise RuntimeError(
                "Could not disable foreign keys for the bulk load "
                "(a transaction was still open)."
            )

        _truncate(connection)
        searches = _ingest_stream(connection, rows, report)
        _load_search_events(connection, searches, report)
        _build_catalog_terms(connection, report)
        _remove_orphans(connection, report)

        connection.commit()
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_referential_integrity(connection)

        write_meta(connection, "source", report.source)
        write_meta(
            connection,
            "loaded_at",
            datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        write_meta(connection, "result_cap", config.result_cap)
        write_meta(connection, "session_gap_seconds", config.session_gap_seconds)
        for table, count in report.rows.items():
            write_meta(connection, f"rows.{table}", count)
        write_meta(connection, "rows.catalog_term", report.catalog_terms)
        write_meta(connection, "sessions", report.sessions)

    return report


def _truncate(connection: sqlite3.Connection) -> None:
    """Empty every loaded table, preserving reviewer decisions and digests.

    `suggestion` and `digest` are records this system owns. A reviewer who has
    approved fifty synonyms should not lose that work because the catalogue was
    re-extracted.
    """
    for table in (
        "product_category",
        "product_name",
        "category_name",
        "catalog_term",
        "search_event",
        "query_count",
        "product",
        "category",
    ):
        connection.execute(f"DELETE FROM {table}")  # fixed table list, not user input


def _ingest_stream(
    connection: sqlite3.Connection,
    rows: Iterator,
    report: LoadReport,
) -> list[Row]:
    """Insert catalogue rows as they stream past; collect search rows for later.

    Search events need a global time ordering before sessions can be assigned,
    so they are held back. The catalogue, which is the overwhelming majority of
    the rows, is written straight through in batches and never accumulates.
    """
    searches: list[Row] = []
    buffers: dict[str, list[tuple]] = {}

    inserts = {
        "product": (
            "INSERT OR REPLACE INTO product (id, sku, url_key, default_price) VALUES (?,?,?,?)"
        ),
        "product_name": (
            "INSERT OR REPLACE INTO product_name (product_id, lang, name, norm_name) "
            "VALUES (?,?,?,?)"
        ),
        "category": "INSERT OR REPLACE INTO category (id, parent_id) VALUES (?,?)",
        "category_name": (
            "INSERT OR REPLACE INTO category_name (category_id, lang, name, norm_name) "
            "VALUES (?,?,?,?)"
        ),
        "product_category": (
            "INSERT OR REPLACE INTO product_category (product_id, category_id) VALUES (?,?)"
        ),
        "query_count": (
            "INSERT OR REPLACE INTO query_count (query, norm_query, count) VALUES (?,?,?)"
        ),
    }

    def emit(table: str, values: tuple) -> None:
        buffer = buffers.setdefault(table, [])
        buffer.append(values)
        report.rows[table] += 1
        if len(buffer) >= _BATCH:
            connection.executemany(inserts[table], buffer)
            buffer.clear()

    for table, row in rows:
        try:
            if table == "recommendations_searches":
                searches.append(row)
            elif table == "recommendations_querycount":
                emit("query_count", (row[1], normalize(row[1]), int(row[2])))
            elif table == "catalog_product":
                emit("product", (int(row[0]), row[2], row[5], _as_float(row[6])))
            elif table == "catalog_productname":
                lang = _lang_code(row[2])
                if lang is None:
                    report.skipped_rows["unknown language in catalog_productname"] += 1
                    continue
                emit("product_name", (int(row[4]), lang, row[1], normalize(row[1] or "")))
            elif table == "catalog_category":
                emit("category", (int(row[0]), _as_int(row[1])))
            elif table == "catalog_categoryname":
                lang = _lang_code(row[2])
                if lang is None:
                    report.skipped_rows["unknown language in catalog_categoryname"] += 1
                    continue
                emit("category_name", (int(row[3]), lang, row[1], normalize(row[1] or "")))
            elif table == "catalog_product_categories":
                emit("product_category", (int(row[1]), int(row[2])))
        except (TypeError, ValueError, IndexError):
            report.skipped_rows[f"malformed row in {table}"] += 1

    for table, buffer in buffers.items():
        if buffer:
            connection.executemany(inserts[table], buffer)

    return searches


def _load_search_events(
    connection: sqlite3.Connection, searches: list[Row], report: LoadReport
) -> None:
    """Normalise, sessionise, and insert the search log.

    Sessionisation note: the source table records no session or user identifier,
    so sessions are inferred purely from the gap between consecutive searches.
    On a single-tenant log this reconstructs shopper journeys well; on
    multi-user traffic it would interleave them. That is why `session_id` is
    described as derived everywhere it surfaces, and why no metric depends on it
    alone.
    """
    config = settings()
    gap = timedelta(seconds=config.session_gap_seconds)

    parsed: list[tuple[datetime, Row]] = []
    for row in searches:
        occurred = _parse_timestamp(row[5])
        if occurred is None:
            report.skipped_rows["unparsable timestamp in recommendations_searches"] += 1
            continue
        parsed.append((occurred, row))
    parsed.sort(key=lambda item: item[0])

    records = []
    session_index = 0
    previous: datetime | None = None

    for occurred, row in parsed:
        if previous is None or occurred - previous > gap:
            session_index += 1
        previous = occurred

        raw_query = row[1] or ""
        results_ar = row[2] or ""
        results_en = row[3] or ""
        records.append(
            (
                int(row[0]),
                raw_query,
                normalize(raw_query),
                script_of(raw_query).value,
                int(row[4]),
                results_ar,
                results_en,
                _result_signature(results_en, results_ar),
                occurred.isoformat(sep=" ", timespec="seconds"),
                f"s{session_index:04d}",
            )
        )

    connection.executemany(
        "INSERT OR REPLACE INTO search_event "
        "(id, raw_query, norm_query, script, result_count, results_ar, results_en, "
        " result_signature, occurred_at, session_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        records,
    )
    report.rows["search_event"] = len(records)
    report.sessions = session_index


def _build_catalog_terms(connection: sqlite3.Connection, report: LoadReport) -> None:
    """Count how many products use each catalogue term, per language.

    This is the dictionary that keeps discovery honest. A spelling correction is
    only ever proposed towards a term the catalogue actually sells, and the
    product count doubles as the strength of that evidence: a term on 4,000
    products is a far safer correction target than one on a single product.
    """
    counts: Counter = Counter()
    for lang, norm_name in connection.execute("SELECT lang, norm_name FROM product_name"):
        # Each token is indexed under every form a shopper might type it in, so
        # a search for `فاكهه` finds a product merchandised as `كوكتيل الفاكهة`.
        terms: set[str] = set()
        for token in tokenize(norm_name):
            terms |= term_variants(token)
        for term in terms:
            counts[(term, lang)] += 1

    connection.executemany(
        "INSERT OR REPLACE INTO catalog_term (term, lang, product_count) VALUES (?,?,?)",
        [(term, lang, count) for (term, lang), count in counts.items()],
    )
    report.catalog_terms = len(counts)


def _remove_orphans(connection: sqlite3.Connection, report: LoadReport) -> None:
    """Delete child rows whose parent is missing, so the schema constraints hold.

    A dump can be internally inconsistent, for instance a product referenced by
    a category link but excluded from the product table. Reporting and removing
    those rows beats either failing the load or leaving the store in a state its
    own constraints would reject.
    """
    checks = (
        (
            "product_name",
            "DELETE FROM product_name WHERE product_id NOT IN (SELECT id FROM product)",
        ),
        (
            "category_name",
            "DELETE FROM category_name WHERE category_id NOT IN (SELECT id FROM category)",
        ),
        (
            "product_category",
            "DELETE FROM product_category "
            "WHERE product_id NOT IN (SELECT id FROM product) "
            "OR category_id NOT IN (SELECT id FROM category)",
        ),
        (
            "category",
            "UPDATE category SET parent_id = NULL WHERE parent_id IS NOT NULL "
            "AND parent_id NOT IN (SELECT id FROM category)",
        ),
    )
    for table, statement in checks:
        cursor = connection.execute(statement)
        if cursor.rowcount > 0:
            report.orphans_removed[table] = cursor.rowcount
            report.rows[table] = max(0, report.rows.get(table, 0) - cursor.rowcount)


def _assert_referential_integrity(connection: sqlite3.Connection) -> None:
    """Fail the load if any foreign key is still violated after cleanup.

    The bulk insert ran with constraint checking off, so this is the point where
    the store proves it is consistent. A violation here means `_remove_orphans`
    missed a relationship and the store should not be published.
    """
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        tables = sorted({row[0] for row in violations})
        raise RuntimeError(
            f"{len(violations)} foreign-key violations remain after load "
            f"in: {', '.join(tables)}"
        )


def _result_signature(results_en: str, results_ar: str) -> str:
    """Stable digest of a result set, ignoring order.

    Two searches for the same term that return the same products in a different
    order are the same retrieval outcome; two that return different products are
    not. Hashing the sorted titles makes "did the results change?" a cheap
    equality test (see `analytics.metrics`).
    """
    titles = sorted(
        line.strip() for line in (results_en or results_ar).splitlines() if line.strip()
    )
    return hashlib.sha1("\n".join(titles).encode("utf-8")).hexdigest()[:16]


def _lang_code(value: str | None) -> str | None:
    return _LANG_CODES.get((value or "").strip().casefold())


def _as_int(value: str | None) -> int | None:
    return None if value is None else int(value)


def _as_float(value: str | None) -> float | None:
    return None if value is None else float(value)


def _parse_timestamp(value: str | None) -> datetime | None:
    """Parse a MySQL `DATETIME(6)` value, with or without fractional seconds."""
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None
