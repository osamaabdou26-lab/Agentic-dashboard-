"""Command-line entry point.

One command per thing an operator actually does: build the store, look for
problems, run discovery, review proposals, ask a question, write the digest,
serve the dashboard.

Output is plain text written for a terminal. Anything meant to be piped
elsewhere (the rules export, the digest) can be written straight to a file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from searchiq import __version__
from searchiq.config import settings
from searchiq.store.db import connect, read_meta
from searchiq.text.normalize import strip_isolates


def echo(*parts: object) -> None:
    """Print to the console, without the bidi marks a terminal cannot lay out."""
    print(strip_isolates(" ".join(str(part) for part in parts)))


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return 1

    # Windows consoles still default to a legacy code page; Arabic query terms
    # are unreadable without this and the process would die on an encode error.
    _force_utf8()

    try:
        return int(args.handler(args) or 0)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


def _cmd_etl(args: argparse.Namespace) -> int:
    from searchiq.ingest.loader import SOURCE_TABLES, load

    if args.source == "mysql":
        from searchiq.ingest.mysql_source import iter_mysql_rows

        config = settings().mysql
        echo(f"Reading {config.database} on {config.host}:{config.port} as {config.user}…")
        report = load(
            rows=iter_mysql_rows(tables=SOURCE_TABLES),
            source_label=f"mysql: {config.user}@{config.host}/{config.database}",
        )
    else:
        dump = Path(args.dump) if args.dump else settings().dump_path
        echo(f"Reading {dump}…")
        echo("This streams the whole dump once and takes about half a minute.")
        report = load(dump_path=dump)

    echo("\nLoaded:")
    echo("\n".join(report.as_lines()))
    echo(f"\nStore written to {settings().db_path}")
    return 0


def _cmd_sample_data(args: argparse.Namespace) -> int:
    from searchiq.ingest.loader import load
    from searchiq.ingest.sample_data import generate_sample_dump

    output = Path(args.output) if args.output else settings().sample_dump_path
    echo(f"Generating mock query logs and catalogue into {output}…")
    report = generate_sample_dump(
        output,
        days=args.days,
        sessions_per_day=args.sessions_per_day,
        seed=args.seed,
    )
    echo("\n".join(report.as_lines()))

    if args.no_load:
        echo(f"\nLoad it with: searchiq etl --dump {output}")
        return 0

    echo("\nBuilding the analytics store from it…")
    load(dump_path=output, source_label=f"generated sample: {output}")
    echo(f"Store written to {settings().db_path}")
    echo("\nNext: searchiq discover, then searchiq serve")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from searchiq.analytics.metrics import compute_overview, compute_query_quality
    from searchiq.analytics.terms import compute_term_drivers

    with connect(read_only=True) as connection:
        overview = compute_overview(connection, since=args.since, until=args.until)
        if not overview.total_searches:
            echo("No searches in the loaded log for this period.")
            return 0
        problems = [
            q
            for q in compute_query_quality(connection, since=args.since, until=args.until)
            if q.is_problem
        ]
        drivers = compute_term_drivers(
            connection, since=args.since, until=args.until, limit=args.limit
        )

    _heading(f"Search health: {overview.health_score}/100")
    echo(
        f"{overview.total_searches:,} searches, {overview.distinct_queries} distinct "
        f"queries, {overview.sessions} sessions"
    )
    echo(f"{overview.period_start} to {overview.period_end}\n")
    echo(f"  irrelevant results   {overview.lexical_miss_rate:>6.1%}")
    echo(f"  zero results         {overview.zero_result_rate:>6.1%}")
    echo(f"  under-filled         {overview.under_filled_rate:>6.1%}")
    echo(f"  retrieval gaps       {overview.retrieval_gap_queries:>6}")
    if overview.dissatisfaction_rate is not None:
        echo(
            f"  retried or reworded  {overview.dissatisfaction_rate:>6.1%}  "
            f"({overview.engagement_source})"
        )
    echo(f"  machine-like traffic {overview.automated_traffic_rate:>6.1%}")

    _heading(f"Failing queries ({len(problems)})")
    for position, query in enumerate(problems[: args.limit], start=1):
        echo(
            f"{position:>2}. {query.display_query}   "
            f"({query.searches} searches, severity {query.severity:.2f})"
        )
        for reason in query.reasons:
            echo(f"      - {reason}")

    _heading("Terms driving failures")
    for driver in drivers:
        echo(f"  {driver.term}  [{driver.verdict}]")
        echo(f"      {driver.headline}")
    return 0


def _cmd_discover(args: argparse.Namespace) -> int:
    from searchiq.discovery import refresh

    with connect() as connection:
        report = refresh(connection)
    echo("Discovery complete:")
    echo("\n".join(report.as_lines()))
    echo("\nReview them with: searchiq suggestions")
    return 0


def _cmd_suggestions(args: argparse.Namespace) -> int:
    from searchiq.discovery import list_suggestions

    with connect(read_only=True) as connection:
        rows = list_suggestions(
            connection, status=args.status, kind=args.kind, limit=args.limit
        )
    if not rows:
        echo("Nothing matches. Run `searchiq discover` first.")
        return 0

    for suggestion in rows:
        echo(
            f"[{suggestion.id:>3}] {suggestion.kind:<14} "
            f"{suggestion.source_term} -> {suggestion.target_term}   "
            f"confidence {suggestion.confidence:.2f}   ({suggestion.status})"
        )
        echo(f"      {suggestion.rationale}")
    echo(f"\n{len(rows)} shown. Approve with: searchiq approve <id>")
    return 0


def _cmd_decide(args: argparse.Namespace) -> int:
    from searchiq.discovery import review

    with connect() as connection:
        try:
            updated = review.decide(
                connection,
                args.id,
                decision=args.decision,
                reviewer=args.reviewer,
                note=args.note,
            )
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    echo(
        f"{updated.kind} {updated.source_term} -> {updated.target_term} "
        f"is now {updated.status}."
    )
    if updated.status == "approved":
        echo("It joins the next export. Nothing is live until you deploy that file.")
    elif updated.status == "pending":
        echo("It is back in the review queue and out of the export.")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    from searchiq.discovery import export_approved

    with connect(read_only=True) as connection:
        document = export_approved(connection)

    payload = json.dumps(document, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
        echo(f"Wrote {document['approved_count']} approved rule(s) to {args.output}")
        echo("Deploying this file to live search remains a manual step.")
    else:
        echo(payload)
    return 0


def _cmd_ask(args: argparse.Namespace) -> int:
    from searchiq.agent import ask

    with connect(read_only=True) as connection:
        answer = ask(connection, " ".join(args.question))

    echo(answer.answer)
    if args.trace and answer.tool_calls:
        _heading("Tools called")
        for call in answer.tool_calls:
            echo(f"  {call.name}({json.dumps(call.arguments, ensure_ascii=False)})")
    echo(
        f"\n[{answer.source}"
        + (f", {answer.model}" if answer.model else ", no API key configured")
        + "]"
    )
    return 0


def _cmd_tools(args: argparse.Namespace) -> int:
    """Print the tool registry the agent is bound to.

    The agent's guarantee is that it answers only through these, so being able
    to read the list is part of the guarantee rather than a debugging extra.
    """
    from searchiq.agent import tools as toolkit

    config = settings()
    mode = f"model {config.model}" if config.agent_is_live else "offline planner"
    echo(f"{len(toolkit.TOOLS)} tool(s) bound to the agent ({mode}):\n")
    for tool in toolkit.TOOLS:
        parameters = ", ".join(sorted(tool.input_schema.get("properties", {}))) or "—"
        required = set(tool.input_schema.get("required", []))
        echo(f"  {tool.name}")
        echo(f"      {tool.description}")
        echo(
            f"      arguments: {parameters}"
            + (f"   (required: {', '.join(sorted(required))})" if required else "")
        )
        echo("")
    echo("Every answer names the tools it called; add --trace to `searchiq ask`.")
    return 0


def _cmd_digest(args: argparse.Namespace) -> int:
    from searchiq.agent import generate_digest

    with connect() as connection:
        digest = generate_digest(connection, days=args.days, end=args.end)

    if args.output:
        Path(args.output).write_text(digest.body_md, encoding="utf-8")
        echo(f"Wrote digest to {args.output} (narrative: {digest.narrative_source})")
    else:
        echo(digest.body_md)
    return 0


def _cmd_export_bi(args: argparse.Namespace) -> int:
    from searchiq.reporting import export_bi

    output = Path(args.output)
    with connect(read_only=True) as connection:
        report = export_bi(
            connection, output, days=args.days, base_url=args.base_url
        )

    echo(f"Wrote {len(report.tables)} table(s) for Power BI:")
    echo("\n".join(report.as_lines()))
    echo("")
    echo("Load them with Get Data > Folder, then Combine & Load.")
    if not args.base_url:
        echo("For a live connection instead, pass --base-url http://127.0.0.1:8000")
    echo(f"Modelling notes are in {output / 'README.md'}")
    return 0


def _cmd_export_site(args: argparse.Namespace) -> int:
    from searchiq.export_site import export_site

    output = Path(args.output)
    with connect(read_only=True) as connection:
        report = export_site(connection, output)

    echo(f"Wrote a publishable snapshot to {output}")
    for line in report.as_lines():
        echo(line)
    echo("")
    echo("Publish it by dragging that folder onto https://app.netlify.com/drop")
    echo("The review queue and the agent are read-only in a snapshot; run")
    echo("`searchiq serve` for the interactive version.")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    if not settings().db_path.exists():
        print("No analytics store yet. Run `searchiq etl` first.", file=sys.stderr)
        return 2
    echo(f"Dashboard on http://{args.host}:{args.port}")
    uvicorn.run("searchiq.api.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    config = settings()
    echo(f"searchiq {__version__}")
    missing_store = "" if config.db_path.exists() else " (missing)"
    missing_dump = "" if config.dump_path.exists() else " (missing)"
    echo(f"  store        {config.db_path}{missing_store}")
    echo(f"  dump         {config.dump_path}{missing_dump}")
    echo(f"  result cap   {config.result_cap}")
    agent = (
        f"model {config.model}"
        if config.agent_is_live
        else "offline planner (no ANTHROPIC_API_KEY)"
    )
    echo(f"  agent        {agent}")
    if config.db_path.exists():
        with connect(read_only=True) as connection:
            meta = read_meta(connection)
        echo("\nLast load:")
        for key in sorted(meta):
            echo(f"  {key:<28} {meta[key]}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="searchiq",
        description="Search-quality insights, discovery, and agent for the grocery catalogue.",
    )
    parser.add_argument("--version", action="version", version=f"searchiq {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    etl = subparsers.add_parser("etl", help="build the analytics store from the source data")
    etl.add_argument(
        "--source",
        choices=("dump", "mysql"),
        default="dump",
        help="read the mysqldump file (default) or a live MySQL server",
    )
    etl.add_argument("--dump", help="path to the .sql dump (overrides SEARCHIQ_DUMP_PATH)")
    etl.set_defaults(handler=_cmd_etl)

    sample = subparsers.add_parser(
        "sample-data",
        help="generate mock query logs and a catalogue, then load them",
        description=(
            "Write a mock mysqldump file containing zero-result queries, "
            "misspellings, partial queries and low-engagement terms, then build "
            "the analytics store from it through the ordinary ETL path. Use this "
            "when you have no source database to hand."
        ),
    )
    sample.add_argument("-o", "--output", help="where to write the .sql file")
    sample.add_argument(
        "--days", type=int, default=28, help="length of the generated log (default: 28)"
    )
    sample.add_argument(
        "--sessions-per-day",
        type=int,
        default=6,
        help="shopper journeys per day (default: 6)",
    )
    sample.add_argument(
        "--seed", type=int, default=20250109, help="generation seed, for reproducibility"
    )
    sample.add_argument(
        "--no-load",
        action="store_true",
        help="only write the file; do not build the store from it",
    )
    sample.set_defaults(handler=_cmd_sample_data)

    report = subparsers.add_parser("report", help="print a search-quality summary")
    _add_period_args(report)
    report.add_argument("--limit", type=int, default=10, help="how many rows per section")
    report.set_defaults(handler=_cmd_report)

    discover = subparsers.add_parser(
        "discover", help="find synonyms and misspellings, and queue them for review"
    )
    discover.set_defaults(handler=_cmd_discover)

    suggestions = subparsers.add_parser("suggestions", help="show the review queue")
    suggestions.add_argument("--status", choices=("pending", "approved", "rejected"))
    suggestions.add_argument("--kind", choices=("misspelling", "synonym", "partial_query"))
    suggestions.add_argument("--limit", type=int, default=50)
    suggestions.set_defaults(handler=_cmd_suggestions)

    # `reopen` is the undo, and maps to the `pending` status rather than to a
    # past participle of its own verb.
    for verb, status in (("approve", "approved"), ("reject", "rejected"),
                         ("reopen", "pending")):
        decide = subparsers.add_parser(
            verb,
            help=(
                "return a decided proposal to the queue"
                if verb == "reopen"
                else f"{verb} a proposal by id"
            ),
        )
        decide.add_argument("id", type=int)
        decide.add_argument("--reviewer", default="cli")
        decide.add_argument("--note")
        decide.set_defaults(handler=_cmd_decide, decision=status)

    export = subparsers.add_parser(
        "export", help="write approved rules as a deployable JSON document"
    )
    export.add_argument("-o", "--output", help="file to write (default: stdout)")
    export.set_defaults(handler=_cmd_export)

    ask = subparsers.add_parser("ask", help="ask a question about search performance")
    ask.add_argument("question", nargs="+")
    ask.add_argument("--trace", action="store_true", help="show which tools were called")
    ask.set_defaults(handler=_cmd_ask)

    tools = subparsers.add_parser(
        "tools", help="list the analytics tools the agent is bound to"
    )
    tools.set_defaults(handler=_cmd_tools)

    digest = subparsers.add_parser("digest", help="generate the period digest")
    digest.add_argument("--days", type=int, default=7)
    digest.add_argument("--end", help="period end (default: the newest search in the log)")
    digest.add_argument("-o", "--output", help="file to write (default: stdout)")
    digest.set_defaults(handler=_cmd_digest)

    export_bi = subparsers.add_parser(
        "export-bi",
        help="write flat CSV tables for Power BI",
        description=(
            "Write the analytics as flat CSV tables a BI tool can load without "
            "any Power Query reshaping. Every figure is read from the same "
            "functions the dashboard uses, so a report built on these cannot "
            "disagree with the dashboard."
        ),
    )
    export_bi.add_argument(
        "-o", "--output", default="bi", help="output directory (default: bi)"
    )
    export_bi.add_argument(
        "--days",
        type=int,
        default=90,
        help="days of daily rollup to build (default: 90)",
    )
    export_bi.add_argument(
        "--base-url",
        help=(
            "a running instance, e.g. http://127.0.0.1:8000. Adds a .pbids file "
            "that opens Power BI already connected to the live API."
        ),
    )
    export_bi.set_defaults(handler=_cmd_export_bi)

    export_site = subparsers.add_parser(
        "export-site", help="write a static, publishable copy of the dashboard"
    )
    export_site.add_argument(
        "-o", "--output", default="site", help="output directory (default: site)"
    )
    export_site.set_defaults(handler=_cmd_export_site)

    serve = subparsers.add_parser("serve", help="run the dashboard and API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    serve.set_defaults(handler=_cmd_serve)

    status = subparsers.add_parser("status", help="show configuration and load provenance")
    status.set_defaults(handler=_cmd_status)

    return parser


def _add_period_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--since", help="inclusive start, YYYY-MM-DD")
    parser.add_argument("--until", help="inclusive end, YYYY-MM-DD")


def _heading(text: str) -> None:
    echo(f"\n{text}\n{'-' * len(text)}")


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
