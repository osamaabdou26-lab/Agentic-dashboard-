"""Build a static, publishable copy of the dashboard.

Netlify and its equivalents serve files; they do not run Python. So publishing
the dashboard means answering every request it makes ahead of time and writing
those answers to disk as JSON, then pointing the same front-end at the files
instead of at the API.

What survives the trip: every view, every figure, the query drill-downs, the
review queue and the digest. What cannot: approving a proposal, asking the agent
a question, and re-running discovery, because those need a process to talk to.
The exported page says so rather than offering controls that quietly do nothing.

The front-end is the same `web/` directory the live app serves. It switches
behaviour on one flag written into `config.js`, so there is no second copy of
the dashboard to keep in step with the first.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from searchiq import __version__
from searchiq.analytics.catalog import CatalogIndex
from searchiq.analytics.metrics import compute_overview, compute_query_quality
from searchiq.analytics.terms import compute_term_drivers
from searchiq.api.app import WEB_ROOT, _sample_results
from searchiq.config import settings
from searchiq.discovery import review

# Digest periods baked into the export. A static site cannot compute a new one
# on demand, so the choices the UI offers all have to exist as files.
DIGEST_PERIODS = (7, 14, 30)

NETLIFY_TOML = """\
# Netlify reads this automatically. The site is plain files, so there is no
# build step to run — the publish directory is the whole of it.
[build]
  publish = "."
  command = ""

[[headers]]
  for = "/*"
  [headers.values]
    X-Content-Type-Options = "nosniff"
    Referrer-Policy = "strict-origin-when-cross-origin"

# The data files change on every export; the shell rarely does.
[[headers]]
  for = "/data/*"
  [headers.values]
    Cache-Control = "public, max-age=300"
"""

README = """\
# Search Pulse — published snapshot

A static copy of the Search Pulse dashboard. Every figure here was computed from
the analytics store at export time and written to `data/*.json`; the page reads
those files instead of calling an API.

## Publishing it

Drag this whole folder onto https://app.netlify.com/drop — no account needed.
Any static host works the same way (GitHub Pages, Cloudflare Pages, Vercel).

## What works, and what does not

Working: the overview, every query and its drill-down, the terms rollup, the
review queue as a read-only list, and the digest.

Not working, because each needs a running server: approving or rejecting a
proposal, asking the agent a question, re-running discovery, and changing the
reporting period. The page states this rather than showing dead controls.

Run `searchiq serve` locally for the interactive version.

## Refreshing it

    searchiq export-site --output site

Then re-upload. The snapshot does not update on its own.
"""


@dataclass
class ExportReport:
    output: Path
    files: int = 0
    bytes_written: int = 0
    queries_detailed: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_lines(self) -> list[str]:
        lines = [
            f"  {self.files} files, {self.bytes_written / 1024:.0f} KB",
            f"  {self.queries_detailed} query drill-downs baked in",
            f"  output: {self.output}",
        ]
        lines += [f"  warning: {warning}" for warning in self.warnings]
        return lines


def export_site(
    connection: sqlite3.Connection, output: Path, *, generated_at: str | None = None
) -> ExportReport:
    """Write a self-contained copy of the dashboard to `output`."""
    output = Path(output)
    data_dir = output / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    report = ExportReport(output=output)
    catalog = CatalogIndex.for_connection(connection)

    def write(relative: str, payload: Any) -> None:
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        path.write_text(text, encoding="utf-8")
        report.files += 1
        report.bytes_written += len(text.encode("utf-8"))

    overview = compute_overview(connection, catalog=catalog)
    qualities = compute_query_quality(connection, catalog=catalog)

    drivers = compute_term_drivers(connection, catalog=catalog)

    write("data/overview.json", overview.to_dict())
    write("data/terms.json", [driver.to_dict() for driver in drivers])
    write("data/queries-all.json", [q.to_dict() for q in qualities])
    write(
        "data/queries-problems.json",
        [q.to_dict() for q in qualities if q.is_problem],
    )

    # Drill-downs are keyed by normalised query rather than written as one file
    # per query: Arabic terms make poor file names on some systems, and the
    # whole map is only a few hundred kilobytes.
    details: dict[str, Any] = {}
    for quality in qualities:
        detail = quality.to_dict()
        detail["sample_results"] = _sample_results(connection, quality.norm_query)
        details[quality.norm_query] = detail
    write("data/query-details.json", details)
    report.queries_detailed = len(details)

    for status in ("pending", "approved", "rejected"):
        write(
            f"data/suggestions-{status}.json",
            [s.to_dict() for s in review.list_suggestions(connection, status=status, limit=500)],
        )
    write("data/rules-export.json", review.export_approved(connection))

    for days in DIGEST_PERIODS:
        write(f"data/digest-{days}.json", _digest_payload(connection, days))

    write(
        "data/status.json",
        {
            "ready": True,
            "version": __version__,
            "meta": _meta(connection),
            "result_cap": settings().result_cap,
            "agent_mode": "static",
            "model": None,
        },
    )

    _copy_front_end(output, report)
    _write_config(output, report, generated_at=generated_at)

    (output / "netlify.toml").write_text(NETLIFY_TOML, encoding="utf-8")
    (output / "README.md").write_text(README, encoding="utf-8")
    report.files += 2

    if not overview.total_searches:
        report.warnings.append("the store holds no searches; the export will look empty")

    return report


def _digest_payload(connection: sqlite3.Connection, days: int) -> dict[str, Any]:
    """Generate a digest without storing it; an export must not mutate the store."""
    from searchiq.agent.digest import generate_digest

    return generate_digest(connection, days=days, store=False, use_model=False).to_dict()


def _meta(connection: sqlite3.Connection) -> dict[str, str]:
    from searchiq.store.db import read_meta

    return read_meta(connection)


def _copy_front_end(output: Path, report: ExportReport) -> None:
    """Copy the same front-end the live app serves, into `static/`.

    The paths in `index.html` are absolute (`/static/app.js`), which is exactly
    what a site root needs, so nothing has to be rewritten.
    """
    target = output / "static"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "styles.css", "app.js"):
        source = WEB_ROOT / name
        if not source.is_file():
            report.warnings.append(f"missing front-end file: {source}")
            continue
        shutil.copy2(source, target / name)
        report.files += 1
        report.bytes_written += source.stat().st_size

    # A static host serves the root, so the page itself belongs there too.
    shutil.copy2(WEB_ROOT / "index.html", output / "index.html")
    report.files += 1


def _write_config(output: Path, report: ExportReport, *, generated_at: str | None) -> None:
    """Write the one flag that puts the front-end into read-only mode."""
    from datetime import datetime

    stamp = generated_at or datetime.now().astimezone().isoformat(timespec="seconds")
    config = (
        "// Written by `searchiq export-site`. Its presence is what tells the\n"
        "// dashboard to read data/*.json instead of calling an API.\n"
        "window.SEARCHPULSE_STATIC = true;\n"
        f"window.SEARCHPULSE_EXPORTED_AT = {json.dumps(stamp)};\n"
    )
    (output / "static" / "config.js").write_text(config, encoding="utf-8")
    report.files += 1

    # The page loads config.js before app.js so the flag is set in time.
    index = output / "index.html"
    html = index.read_text(encoding="utf-8")
    if "config.js" not in html:
        html = html.replace(
            '<script src="/static/app.js"></script>',
            '<script src="/static/config.js"></script>\n'
            '  <script src="/static/app.js"></script>',
        )
        index.write_text(html, encoding="utf-8")
        (output / "static" / "index.html").write_text(html, encoding="utf-8")
