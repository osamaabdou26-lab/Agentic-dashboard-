"""Flat tables for Power BI and anything else that speaks CSV or JSON.

A BI tool wants rectangles: one row per thing, one column per field, no nesting,
and a date column it can build a timeline on. The analytics layer produces
nested objects with lists inside them, which Power BI can ingest but only after
a person clicks through a dozen "Expand" steps in Power Query — work that has to
be redone by hand every time a field is added.

So this module flattens, and does nothing else. **Every figure is read from the
same `analytics` functions the dashboard, the CLI and the agent read**; not one
number is recomputed here, and there is no SQL in this file that touches a
metric. That is the whole design constraint: the moment a BI report can disagree
with the dashboard about the health score, both become untrustworthy and someone
has to reconcile them by hand.

Two delivery routes, one definition:

* `searchiq export-bi` writes the tables as CSV files, for a scheduled refresh
  from a folder or a one-off import.
* `GET /api/bi/{table}` serves the same rows as JSON, for Power BI's Web
  connector against a running instance.

Both call `build_table`, so the two can never drift apart.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from searchiq import __version__
from searchiq.analytics.catalog import CatalogIndex
from searchiq.analytics.metrics import (
    PROBLEM_THRESHOLD,
    compute_overview,
    compute_query_quality,
)
from searchiq.analytics.terms import compute_term_drivers
from searchiq.config import settings
from searchiq.discovery import review

# How many days of daily rollup to build when the caller does not say. Long
# enough for a month-on-month view, short enough that the export stays quick on
# the full catalogue.
DEFAULT_DAILY_DAYS = 90

# Terms table depth. The dashboard shows 25; a BI model wants enough tail to
# aggregate over.
_TERM_LIMIT = 200


@dataclass(frozen=True)
class Table:
    """One flat table, described once for CSV, JSON and the documentation."""

    name: str
    grain: str  #: what one row is
    description: str
    builder: Callable[..., list[dict[str, Any]]]

    def to_manifest_entry(self, rows: int) -> dict[str, Any]:
        return {
            "name": self.name,
            "grain": self.grain,
            "description": self.description,
            "rows": rows,
            "file": f"{self.name}.csv",
        }


@dataclass
class BiExportReport:
    """What an export run wrote, for the CLI to echo."""

    directory: Path
    tables: dict[str, int] = field(default_factory=dict)
    pbids_path: Path | None = None

    def as_lines(self) -> list[str]:
        lines = [f"directory: {self.directory}"]
        lines += [
            f"  {name + '.csv':<28} {rows:>7,} rows"
            for name, rows in sorted(self.tables.items())
        ]
        if self.pbids_path:
            lines.append(f"  {self.pbids_path.name:<28} Power BI connection file")
        return lines


# ---------------------------------------------------------------------------
# Table builders
#
# Each returns a list of flat dicts. Booleans are emitted as 0/1 integers:
# Power Query types a column from its first rows, and Python's `True`/`False`
# arrive as the text "True"/"False" in CSV, which it then treats as strings.
# ---------------------------------------------------------------------------


def _searches(
    connection: sqlite3.Connection, *, catalog: CatalogIndex, **_: Any
) -> list[dict[str, Any]]:
    """The event grain: one row per logged search.

    This is the fact table. Everything else here could be derived from it *if*
    the derivation were re-implemented in DAX — which is exactly what should not
    happen, because the severity model is not a sum of columns. The per-query
    scores are joined on rather than recomputed, so a report built on this table
    and the dashboard cannot disagree.
    """
    quality_by_query = {
        item.norm_query: item
        for item in compute_query_quality(connection, catalog=catalog)
    }
    rows = connection.execute(
        "SELECT id, raw_query, norm_query, script, result_count, occurred_at, "
        "session_id, result_signature, clicked_rank, converted FROM search_event "
        "ORDER BY occurred_at"
    ).fetchall()

    result_cap = settings().result_cap
    out = []
    for row in rows:
        quality = quality_by_query.get(row["norm_query"])
        occurred = row["occurred_at"] or ""
        out.append(
            {
                "search_id": row["id"],
                "occurred_at": occurred,
                # A separate date column so Power BI can mark a date table and
                # build a proper time hierarchy without parsing the timestamp.
                "occurred_date": occurred[:10],
                "occurred_hour": int(occurred[11:13]) if len(occurred) >= 13 else None,
                "session_id": row["session_id"],
                "raw_query": row["raw_query"],
                "norm_query": row["norm_query"],
                "script": row["script"],
                "result_count": row["result_count"],
                "is_zero_result": int(row["result_count"] == 0),
                "is_under_filled": int(row["result_count"] < result_cap),
                "result_signature": row["result_signature"],
                "clicked_rank": row["clicked_rank"],
                "converted": row["converted"],
                # Joined from the query grain, never recomputed here.
                "query_severity": quality.severity if quality else None,
                "query_is_problem": int(quality.is_problem) if quality else 0,
                "query_verdict_retrieval_gap": (
                    int(quality.retrieval_gap) if quality else 0
                ),
                "intended_term": quality.intended_term if quality else None,
            }
        )
    return out


def _queries(
    connection: sqlite3.Connection, *, catalog: CatalogIndex, **_: Any
) -> list[dict[str, Any]]:
    """One row per distinct query, with every metric the dashboard shows."""
    return [
        {
            "norm_query": item.norm_query,
            "display_query": item.display_query,
            "script": item.script,
            "searches": item.searches,
            "sessions": item.sessions,
            "first_seen": item.first_seen,
            "first_seen_date": (item.first_seen or "")[:10],
            "last_seen": item.last_seen,
            "last_seen_date": (item.last_seen or "")[:10],
            "mean_results": item.mean_results,
            "min_results": item.min_results,
            "zero_result_rate": item.zero_result_rate,
            "under_fill": item.under_fill,
            "lexical_miss_rate": item.lexical_miss_rate,
            "incoherence": item.incoherence,
            "instability": item.instability,
            "dissatisfaction_rate": item.dissatisfaction_rate,
            "engagement_rate": item.engagement_rate,
            "rapid_repeat_rate": item.rapid_repeat_rate,
            "catalog_coverage": item.catalog_coverage,
            "retrieval_gap": int(item.retrieval_gap),
            "dominant_category": item.dominant_category,
            "intended_term": item.intended_term,
            "intended_coverage": item.intended_coverage,
            "severity": item.severity,
            "impact": item.impact,
            "is_problem": int(item.is_problem),
            # The reasons are a list. Power BI cannot pivot on a list, and the
            # count is the part a report actually charts; the prose is joined
            # for a tooltip.
            "reason_count": len(item.reasons),
            "reasons": " | ".join(item.reasons),
        }
        for item in compute_query_quality(connection, catalog=catalog)
    ]


def _terms(
    connection: sqlite3.Connection, *, catalog: CatalogIndex, **_: Any
) -> list[dict[str, Any]]:
    """One row per term, every spelling of a word folded onto it."""
    return [
        {
            "term": driver.term,
            "canonical_term": driver.canonical_term,
            "script": driver.script,
            "searches": driver.searches,
            "failing_searches": driver.failing_searches,
            "distinct_spellings": driver.distinct_spellings,
            "spellings": " | ".join(driver.spellings),
            "mean_severity": driver.mean_severity,
            "total_impact": driver.total_impact,
            "catalog_coverage": driver.catalog_coverage,
            "verdict": driver.verdict,
            "headline": driver.headline,
        }
        for driver in compute_term_drivers(
            connection, catalog=catalog, limit=_TERM_LIMIT
        )
    ]


def _daily(
    connection: sqlite3.Connection,
    *,
    catalog: CatalogIndex,
    days: int = DEFAULT_DAILY_DAYS,
    **_: Any,
) -> list[dict[str, Any]]:
    """One row per calendar day: the time series a BI report is usually after.

    Built by asking `compute_overview` for each day rather than by aggregating
    the event table, because the health score is a severity-weighted mean and
    does not survive being averaged a second time. A day with no traffic is
    omitted rather than reported as perfect health.

    The window ends at the newest search in the log, not at today, for the same
    reason the digest does: a historical extract should describe itself.
    """
    latest = connection.execute("SELECT MAX(occurred_at) FROM search_event").fetchone()[0]
    earliest = connection.execute("SELECT MIN(occurred_at) FROM search_event").fetchone()[0]
    if not latest or not earliest:
        return []

    last_day = _as_date(latest)
    first_day = max(_as_date(earliest), last_day - timedelta(days=days - 1))

    out = []
    day = first_day
    while day <= last_day:
        start = day.strftime("%Y-%m-%d 00:00:00")
        end = day.strftime("%Y-%m-%d 23:59:59")
        overview = compute_overview(connection, since=start, until=end, catalog=catalog)
        if overview.total_searches:
            out.append(
                {
                    "date": day.strftime("%Y-%m-%d"),
                    "total_searches": overview.total_searches,
                    "distinct_queries": overview.distinct_queries,
                    "sessions": overview.sessions,
                    "health_score": overview.health_score,
                    "problem_queries": overview.problem_queries,
                    "zero_result_rate": overview.zero_result_rate,
                    "under_filled_rate": overview.under_filled_rate,
                    "lexical_miss_rate": overview.lexical_miss_rate,
                    "retrieval_gap_queries": overview.retrieval_gap_queries,
                    "dissatisfaction_rate": overview.dissatisfaction_rate,
                    "engagement_rate": overview.engagement_rate,
                    "engagement_source": overview.engagement_source,
                    "automated_traffic_rate": overview.automated_traffic_rate,
                }
            )
        day += timedelta(days=1)
    return out


def _suggestions(connection: sqlite3.Connection, **_: Any) -> list[dict[str, Any]]:
    """The review queue, flat: what was proposed, who decided, and when.

    Reporting on this is how a team sees whether review is actually happening —
    how much is waiting, how long it waits, and how much of what discovery
    proposes a human agrees with.
    """
    return [
        {
            "suggestion_id": item.id,
            "kind": item.kind,
            "source_term": item.source_term,
            "target_term": item.target_term,
            "lang": item.lang,
            "confidence": item.confidence,
            "status": item.status,
            "is_pending": int(item.status == "pending"),
            "is_approved": int(item.status == "approved"),
            "is_rejected": int(item.status == "rejected"),
            "reviewer": item.reviewer,
            "review_note": item.review_note,
            "created_at": item.created_at,
            "created_date": (item.created_at or "")[:10],
            "updated_at": item.updated_at,
            "updated_date": (item.updated_at or "")[:10],
            "rationale": item.rationale,
        }
        for item in review.list_suggestions(connection, limit=100_000)
    ]


def _overview(
    connection: sqlite3.Connection, *, catalog: CatalogIndex, **_: Any
) -> list[dict[str, Any]]:
    """A single row of headline numbers, for card visuals.

    One row rather than a scalar because Power BI models tables, not values.
    """
    overview = compute_overview(connection, catalog=catalog)
    data = overview.to_dict()
    # The notes are guidance for a human reading the figures, and they belong
    # with the figures rather than in documentation nobody opens.
    data["notes"] = " | ".join(overview.notes)
    data["problem_threshold"] = PROBLEM_THRESHOLD
    data["result_cap"] = settings().result_cap
    data["searchiq_version"] = __version__
    data["exported_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return [data]


TABLES: tuple[Table, ...] = (
    Table(
        name="fact_search",
        grain="one logged search",
        description=(
            "The event fact table: every search with its timestamp, session, "
            "result count and the quality scores of the query it belongs to."
        ),
        builder=_searches,
    ),
    Table(
        name="dim_query",
        grain="one distinct query",
        description=(
            "Every distinct query with its full metric set — severity, impact, "
            "irrelevance, instability, catalogue coverage and diagnosis."
        ),
        builder=_queries,
    ),
    Table(
        name="dim_term",
        grain="one catalogue term",
        description=(
            "Terms with every spelling folded onto one row, each with the "
            "verdict naming who can fix it."
        ),
        builder=_terms,
    ),
    Table(
        name="fact_daily",
        grain="one calendar day with traffic",
        description=(
            "Daily search health: the time series for trend visuals. Built per "
            "day by the same function the dashboard uses, because the health "
            "score cannot be re-averaged."
        ),
        builder=_daily,
    ),
    Table(
        name="fact_suggestion",
        grain="one discovery proposal",
        description=(
            "The review queue: what was proposed, its confidence, and the human "
            "decision on it. Reporting on review throughput lives here."
        ),
        builder=_suggestions,
    ),
    Table(
        name="dim_overview",
        grain="the whole loaded period (one row)",
        description=(
            "Headline figures for card visuals, plus the measurement caveats "
            "that belong with them and the export timestamp."
        ),
        builder=_overview,
    ),
)

TABLES_BY_NAME: dict[str, Table] = {table.name: table for table in TABLES}


def table_names() -> list[str]:
    return [table.name for table in TABLES]


def build_table(
    connection: sqlite3.Connection,
    name: str,
    *,
    catalog: CatalogIndex | None = None,
    days: int = DEFAULT_DAILY_DAYS,
) -> list[dict[str, Any]]:
    """Build one table by name. Raises `KeyError` for an unknown one."""
    table = TABLES_BY_NAME.get(name)
    if table is None:
        raise KeyError(f"unknown table {name!r}; available: {table_names()}")
    catalog = catalog or CatalogIndex.for_connection(connection)
    return table.builder(connection, catalog=catalog, days=days)


def export_bi(
    connection: sqlite3.Connection,
    output_dir: Path | str,
    *,
    days: int = DEFAULT_DAILY_DAYS,
    base_url: str | None = None,
) -> BiExportReport:
    """Write every table as CSV, plus a manifest and a Power BI connection file.

    UTF-8 with a BOM, and CRLF line endings. Both are deliberate: Excel and
    older Power BI builds read a BOM-less UTF-8 CSV in the system code page, and
    Arabic product names come out as mojibake — which looks like a data problem
    and is really an encoding default.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog = CatalogIndex.for_connection(connection)

    report = BiExportReport(directory=output_dir)
    manifest_tables = []

    for table in TABLES:
        rows = table.builder(connection, catalog=catalog, days=days)
        _write_csv(output_dir / f"{table.name}.csv", rows)
        report.tables[table.name] = len(rows)
        manifest_tables.append(table.to_manifest_entry(len(rows)))

    (output_dir / "manifest.json").write_text(
        json.dumps(
            {
                "generated_by": f"searchiq {__version__}",
                "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "store": str(settings().db_path),
                "tables": manifest_tables,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (output_dir / "README.md").write_text(
        _readme(manifest_tables, base_url=base_url), encoding="utf-8"
    )

    if base_url:
        report.pbids_path = write_pbids(output_dir / "search-pulse.pbids", base_url)

    return report


def write_pbids(path: Path | str, base_url: str) -> Path:
    """Write a Power BI data-source file pointing at a running instance.

    Opening a `.pbids` starts Power BI Desktop already connected, which removes
    the step people most often get wrong: typing the URL by hand and landing on
    one table instead of the folder of them.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": "0.1",
                "connections": [
                    {
                        "details": {
                            "protocol": "http",
                            "address": {"url": f"{base_url.rstrip('/')}/api/bi/tables"},
                        },
                        "mode": "Import",
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    # A table with no rows still gets a file. Power BI fails a refresh on a
    # missing source, and an empty table is a truthful answer where a broken
    # refresh is not.
    fieldnames = list(rows[0]) if rows else ["(empty)"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, dialect="excel")
        writer.writeheader()
        writer.writerows(rows)


def _as_date(timestamp: str) -> datetime:
    return datetime.strptime(timestamp[:10], "%Y-%m-%d")


def _readme(tables: list[dict[str, Any]], *, base_url: str | None) -> str:
    rows = "\n".join(
        f"| `{table['name']}.csv` | {table['grain']} | {table['rows']:,} | "
        f"{table['description']} |"
        for table in tables
    )
    live = (
        f"""
## Connecting to the live API instead

These files are a snapshot. To refresh from a running instance, use **Get Data →
Web** against `{base_url.rstrip('/')}/api/bi/<table>`, or open the
`search-pulse.pbids` file in this folder, which starts Power BI already pointed
at the table list.
"""
        if base_url
        else """
## Connecting to the live API instead

These files are a snapshot. To refresh from a running instance, start the app
with `searchiq serve` and use **Get Data → Web** against
`http://127.0.0.1:8000/api/bi/<table>`. Re-run this export with
`--base-url http://127.0.0.1:8000` to get a `.pbids` file that does it for you.
"""
    )

    return f"""\
# Search Pulse — Power BI export

Flat tables, one CSV each, written by `searchiq export-bi`.

| File | One row is | Rows | What it holds |
|---|---|---:|---|
{rows}

## Loading them

**Get Data → Folder**, point at this directory, then **Combine & Load**. Power BI
reads the headers and types the columns; nothing needs expanding, because
nothing is nested.

## Modelling

Join `fact_search[norm_query]` to `dim_query[norm_query]` (many-to-one) and
`fact_search[occurred_date]` to your date table. `fact_daily` stands alone on
`date` — it is a pre-aggregated series, not a detail table, so do not sum it
against `fact_search` in the same visual.

Mark a date table before building any time intelligence. `fact_search` carries
`occurred_date` and `occurred_hour` ready for it.

## What not to recompute in DAX

`health_score`, `severity` and `impact` are not sums of columns. Severity blends
five independent signals and renormalises the weights whenever one of them could
not be measured for a given query; health is the severity-weighted mean across
*searches*, not across queries. Averaging `health_score` over rows will produce a
number that disagrees with the dashboard.

Aggregate the raw counts (`total_searches`, `problem_queries`, `is_zero_result`)
freely — those are additive. For the scored figures, use the value on the row at
the grain you are reporting.

## Encoding

UTF-8 with a BOM, so Arabic product and query names survive Excel and older
Power BI builds. Do not re-save these files as ANSI.
{live}"""
