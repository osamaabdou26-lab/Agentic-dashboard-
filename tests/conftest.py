"""Shared fixtures.

The tests never touch the 828 MB production dump. Instead they build a small
one that reproduces the structural features that matter — extended inserts,
backslash escapes, embedded newlines in result lists, SQL `NULL` next to the
literal string `'NULL'`, Arabic text, and the real misspelling patterns found in
the source data — and run the genuine ETL over it.

That keeps the suite fast and hermetic while still exercising the same code path
production uses, rather than a stubbed-out substitute for it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# Products are chosen so that each correction target clears the minimum
# catalogue support the discovery layer requires, and so that milk and
# strawberry each appear under several spellings, as they do in the real log.
_PRODUCTS = [
    (1, "400001", "bekhero-milk", 42.0, "لبن بخيره كامل الدسم - 1 لتر", "Bekhero Full Cream Milk - 1 L", 1),
    (2, "400002", "juhayna-skimmed", 38.5, "حليب جهينه خالي الدسم - 1 لتر", "Juhayna Skimmed Milk - 1 L", 1),
    (3, "400003", "healthy-milk", 30.0, "حليب هيلثى كامل الدسم، 850 مل", "Healthy Fresh Milk Full Cream - 850 Ml", 1),
    (4, "400004", "nestle-condensed", 55.0, "حليب مكثّف محلّى من نستله", "Nestle Sweetened Condensed Milk", 1),
    (5, "400005", "mero-jam", 62.0, "مربى فراولة من ميرو - 420 جم", "Mero Strawberry Jam - 420 Gr", 2),
    (6, "400006", "givrex-strawberry", 89.0, "فراولة مجمدة من جيفريكس - 400 جم", "Givrex Frozen Strawberries - 400 Gr", 3),
    (7, "400007", "dina-strawberry-milk", 15.0, "حليب بالفراولة من دينا - 250 مل", "Dina Farms Strawberry Milk - 250 Ml", 1),
    (8, "400008", "mazaq-tea-latte", 6.0, "شاي بلبن 2 في 1 من مذاق - 19 جم", "Mazaq Tea Latte 2In1 - 19 Gr", 4),
    (9, "400009", "halwani-burger", 180.0, "برجر بيف من حلواني - 1 كيلو", "Halwani Beef Burger - 1 Kg", 3),
    # A deliberately awkward name: an apostrophe that must survive escaping, and
    # the literal four-character string "NULL", which must not become SQL NULL.
    (10, "400010", "chefs-choice", 25.0, "زبدة الشيف NULL", "Chef's Choice NULL Butter", 5),
]

_CATEGORIES = [
    (1, None, "منتجات الألبان", "Milk & Dairy"),
    (2, None, "مربى", "Jam"),
    (3, None, "أطعمة مجمدة", "Frozen Food"),
    (4, None, "مشروبات", "Beverages"),
    (5, None, "زبدة", "Butter"),
]

# (id, query, arabic results, english results, count, timestamp)
# # Encodes the failure patterns the analytics layer has to detect:
# - `حليب`  works: milk products come back
# - `حليبن` is a one-letter typo that returns tea and a beef burger
# - `فراوله` works, `فراولت` is a typo returning nothing relevant
# - `لبن` immediately followed by `حليب` is a shopper rewording
# - `pas` fired twice a second apart is machine-like traffic
_SEARCHES = [
    (1, "لبن", [1, 8], "2025-03-03 09:00:00"),
    (2, "حليب", [2, 3, 4], "2025-03-03 09:00:20"),
    (3, "حليبن", [8, 9], "2025-03-03 09:01:00"),
    (4, "حليبن", [8, 9], "2025-03-03 09:02:00"),
    (5, "فراولة", [5, 6, 7], "2025-03-03 09:05:00"),
    (6, "فراولت", [9], "2025-03-03 09:05:30"),
    (7, "فراولة", [5, 6, 7], "2025-03-03 09:06:00"),
    (8, "pas", [8], "2025-03-03 09:10:00"),
    (9, "pas", [8], "2025-03-03 09:10:01"),
    (10, "حليب", [2, 3, 4], "2025-03-03 09:20:00"),
]

_QUERY_COUNTS = [("حليب", 12), ("حليبن", 4), ("فراولة", 9), ("فراولت", 2), ("لبن", 7)]


def _escape(value: str) -> str:
    """Escape a Python string the way `mysqldump` would."""
    return value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")


def _build_dump() -> str:
    """Render the fixture as a mysqldump-format SQL file."""
    products = ",".join(
        f"({pid},'2025-01-01 00:00:00.000000','{sku}',NULL,"
        f"'2025-01-02 00:00:00.000000','{url_key}',{price},NULL)"
        for pid, sku, url_key, price, _, _, _ in _PRODUCTS
    )
    names = ",".join(
        f"({i},'{_escape(ar)}','Arabic','2025-01-01 00:00:00.000000',{pid}),"
        f"({i + 100},'{_escape(en)}','English','2025-01-01 00:00:00.000000',{pid})"
        for i, (pid, _, _, _, ar, en, _) in enumerate(_PRODUCTS, start=1)
    )
    categories = ",".join(
        f"({cid},{'NULL' if parent is None else parent},"
        f"'2025-01-01 00:00:00.000000','cat{cid}','2025-01-01 00:00:00.000000')"
        for cid, parent, _, _ in _CATEGORIES
    )
    category_names = ",".join(
        f"({i},'{_escape(ar)}','Arabic',{cid}),({i + 100},'{_escape(en)}','English',{cid})"
        for i, (cid, _, ar, en) in enumerate(_CATEGORIES, start=1)
    )
    product_categories = ",".join(
        f"({i},{pid},{cat})" for i, (pid, _, _, _, _, _, cat) in enumerate(_PRODUCTS, start=1)
    )

    by_id = {p[0]: p for p in _PRODUCTS}
    searches = ",".join(
        "({id},'{q}','{ar}','{en}',{n},'{ts}.000000')".format(
            id=sid,
            q=_escape(query),
            ar=_escape("\n".join(by_id[p][4] for p in results)),
            en=_escape("\n".join(by_id[p][5] for p in results)),
            n=len(results),
            ts=timestamp,
        )
        for sid, query, results, timestamp in _SEARCHES
    )
    counts = ",".join(
        f"({i},'{_escape(query)}',{count})"
        for i, (query, count) in enumerate(_QUERY_COUNTS, start=1)
    )

    return "\n".join(
        [
            "-- MySQL dump 10.13  Distrib 8.0.39, for Win64 (x86_64)",
            "/*!40101 SET NAMES utf8mb4 */;",
            "DROP TABLE IF EXISTS `catalog_category`;",
            f"INSERT INTO `catalog_category` VALUES {categories};",
            f"INSERT INTO `catalog_categoryname` VALUES {category_names};",
            f"INSERT INTO `catalog_product` VALUES {products};",
            f"INSERT INTO `catalog_product_categories` VALUES {product_categories};",
            f"INSERT INTO `catalog_productname` VALUES {names};",
            # A table outside the whitelist: the reader must skip it entirely.
            "INSERT INTO `recommendations_productembedding` VALUES (1,1,'ignored','blob','ar');",
            f"INSERT INTO `recommendations_querycount` VALUES {counts};",
            f"INSERT INTO `recommendations_searches` VALUES {searches};",
            "-- Dump completed",
            "",
        ]
    )


@pytest.fixture
def dump_path(tmp_path: Path) -> Path:
    """A small but structurally faithful mysqldump file."""
    path = tmp_path / "fixture.sql"
    path.write_text(_build_dump(), encoding="utf-8")
    return path


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every setting at a temporary store and clear the settings cache.

    `settings()` is cached for the process, so a test that changed the
    environment without clearing it would silently read another test's
    configuration.
    """
    from searchiq.config import settings

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SEARCHIQ_DB", str(db_path))
    monkeypatch.setenv("SEARCHIQ_RESULT_CAP", "5")
    monkeypatch.setenv("SEARCHIQ_SESSION_GAP_SECONDS", "1800")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    settings.cache_clear()
    yield db_path
    settings.cache_clear()


@pytest.fixture
def store(configured: Path, dump_path: Path) -> Path:
    """A fully loaded analytics store, built by the real ETL."""
    from searchiq.ingest.loader import load

    load(db_path=configured, dump_path=dump_path)
    return configured


@pytest.fixture
def connection(store: Path):
    """An open connection to the loaded store."""
    from searchiq.store.db import connect

    with connect(store) as conn:
        yield conn


@pytest.fixture
def discovered(connection: sqlite3.Connection):
    """A store whose review queue has been populated by discovery."""
    from searchiq.discovery import refresh

    refresh(connection)
    return connection


@pytest.fixture
def discovered_store(store: Path) -> Path:
    """A store whose queue is populated, with no connection left open.

    The API opens its own connection per request, so a fixture that held a
    write transaction would deadlock against it rather than test it.
    """
    from searchiq.discovery import refresh
    from searchiq.store.db import connect

    with connect(store) as conn:
        refresh(conn)
    return store
