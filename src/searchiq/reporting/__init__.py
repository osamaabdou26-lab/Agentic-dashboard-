"""Reporting surfaces for tools outside this application.

Today that means Power BI, but nothing here is Power BI specific: the tables are
flat, typed and named, which is what every BI tool wants.
"""

from searchiq.reporting.bi import (
    TABLES,
    BiExportReport,
    build_table,
    export_bi,
    table_names,
    write_pbids,
)

__all__ = [
    "TABLES",
    "BiExportReport",
    "build_table",
    "export_bi",
    "table_names",
    "write_pbids",
]
