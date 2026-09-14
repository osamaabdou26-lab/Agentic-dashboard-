"""Read the source tables from a live MySQL server instead of a dump file.

The dump reader is the default because it needs no server and no credentials.
This path exists for a deployment that already runs the production database and
wants the analytics store refreshed from it on a schedule.

Credentials come from the environment only (`SEARCHIQ_MYSQL_*`). Nothing here
prompts for, logs, or persists a password.
"""

from __future__ import annotations

from collections.abc import Iterator

from searchiq.config import MySQLSettings, settings
from searchiq.ingest.dump_reader import Row
from searchiq.ingest.loader import SOURCE_TABLES

# Columns are selected explicitly and in the same order the dump emits them, so
# both sources hand the loader identically shaped rows.
_SELECTS = {
    "catalog_category": (
        "SELECT id, parent_category_id, created_at, integration_id, updated_at "
        "FROM catalog_category"
    ),
    "catalog_categoryname": "SELECT id, name, language, category_id FROM catalog_categoryname",
    "catalog_product": (
        "SELECT id, created_at, sku, image, updated_at, url_key, default_price, "
        "default_special_price FROM catalog_product"
    ),
    "catalog_product_categories": (
        "SELECT id, product_id, category_id FROM catalog_product_categories"
    ),
    "catalog_productname": (
        "SELECT id, name, language, created_at, product_id FROM catalog_productname"
    ),
    "recommendations_querycount": "SELECT id, query, count FROM recommendations_querycount",
    "recommendations_searches": (
        "SELECT id, query, ar_results, en_results, num_results, created_at "
        "FROM recommendations_searches"
    ),
}


def iter_mysql_rows(
    mysql: MySQLSettings | None = None, tables: set[str] | None = None
) -> Iterator[tuple[str, Row]]:
    """Yield `(table_name, row)` for each requested table, streamed server-side.

    Raises `RuntimeError` with an actionable message if the optional driver is
    missing, rather than failing at import time and breaking the dump path too.
    """
    try:
        import mysql.connector  # noqa: PLC0415 - optional dependency, imported on use
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise RuntimeError(
            "The live-MySQL source needs the optional driver. "
            'Install it with: pip install -e ".[mysql]"'
        ) from exc

    mysql_settings = mysql or settings().mysql
    tables = tables or SOURCE_TABLES

    connection = mysql.connector.connect(
        host=mysql_settings.host,
        port=mysql_settings.port,
        user=mysql_settings.user,
        password=mysql_settings.password,
        database=mysql_settings.database,
        charset="utf8mb4",
    )
    try:
        for table in sorted(tables):
            statement = _SELECTS.get(table)
            if statement is None:
                continue
            # A buffered cursor would pull all 125k products into memory at
            # once; an unbuffered one streams them row by row.
            cursor = connection.cursor(buffered=False)
            try:
                cursor.execute(statement)
                for record in cursor:
                    yield table, [None if v is None else str(v) for v in record]
            finally:
                cursor.close()
    finally:
        connection.close()
