"""A streaming reader for `mysqldump` output.

The source dataset ships as an 828 MB SQL dump. Only about 12 MB of it — the
search log and the catalogue names — is relevant to search quality; the rest is
vector embeddings, stock levels, and price history.

Reading the dump directly rather than restoring it into MySQL means the project
has no database server to install, no 828 MB import to wait through, and no
credentials to handle. A live-MySQL path exists alongside this one
(`ingest.mysql_source`) for deployments that have the server anyway.

The file is streamed, never read whole: statements are accumulated one at a time
and rows are yielded as they are parsed, so memory stays flat regardless of dump
size.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

# `INSERT INTO `table` VALUES ` — the only statement kind carrying data.
_INSERT_RE = re.compile(r"^INSERT INTO `([^`]+)` VALUES ", re.IGNORECASE)

# MySQL's string escapes. `\%` and `\_` deliberately keep their backslash:
# MySQL only strips it in LIKE patterns, not in string literals.
_ESCAPES = {
    "0": "\0",
    "'": "'",
    '"': '"',
    "b": "\b",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "Z": "\x1a",
    "\\": "\\",
    "%": "\\%",
    "_": "\\_",
}

Row = list[str | None]


def iter_table_rows(
    dump_path: Path | str, tables: set[str]
) -> Iterator[tuple[str, Row]]:
    """Yield `(table_name, row)` for every row of every table in `tables`.

    Values arrive as strings, or `None` for SQL NULL; callers cast as needed.
    Statements for tables outside `tables` are skipped without being parsed,
    which is what keeps a full pass over the dump cheap.
    """
    dump_path = Path(dump_path)
    if not dump_path.is_file():
        raise FileNotFoundError(
            f"SQL dump not found at {dump_path}. Set SEARCHIQ_DUMP_PATH to its "
            "location, or run `searchiq sample-data` to generate a mock one."
        )

    # errors="replace": a handful of rows in the source dump carry bytes that
    # are not valid UTF-8. Replacing them keeps the pass going and confines the
    # damage to the affected product name, rather than aborting the load.
    with dump_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        for statement in _iter_insert_statements(handle):
            match = _INSERT_RE.match(statement)
            if match is None:
                continue
            table = match.group(1)
            if table not in tables:
                continue
            payload = statement[match.end():].rstrip().rstrip(";")
            for row in _parse_values(payload):
                yield table, row


def _iter_insert_statements(handle: Iterator[str]) -> Iterator[str]:
    """Yield complete `INSERT` statements, joining any that span several lines.

    `mysqldump` writes one extended INSERT per line, but a statement is only
    guaranteed to end at an unquoted `;`. Tracking quote state across lines makes
    the reader correct for both layouts.
    """
    buffer: list[str] = []
    in_string = False

    for line in handle:
        if not buffer:
            if not line.startswith("INSERT INTO "):
                continue
            buffer.append(line)
        else:
            buffer.append(line)

        in_string = _ends_inside_string(line, in_string)
        if not in_string and line.rstrip().endswith(";"):
            yield "".join(buffer)
            buffer = []

    if buffer:
        yield "".join(buffer)


def _ends_inside_string(text: str, in_string: bool) -> bool:
    """Return whether `text` leaves the parser inside a quoted literal."""
    escaped = False
    for char in text:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "'":
            in_string = not in_string
    return in_string


def _parse_values(payload: str) -> Iterator[Row]:
    """Parse a `(...),(...),(...)` VALUES payload into rows.

    A single character-by-character pass handles quoting and escaping together:
    a `(`, `)` or `,` inside a quoted literal is data, not structure, and a
    quote preceded by a backslash does not close the literal.
    """
    row: Row = []
    field: list[str] = []
    quoted = False       # this field was written as a quoted literal
    in_string = False    # currently inside that literal
    escaped = False
    depth = 0

    for char in payload:
        if in_string:
            if escaped:
                field.append(_ESCAPES.get(char, char))
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "'":
                in_string = False
            else:
                field.append(char)
            continue

        if char == "'":
            in_string = True
            quoted = True
        elif char == "(":
            depth += 1
            if depth == 1:
                row, field, quoted = [], [], False
        elif char == ")":
            depth -= 1
            if depth == 0:
                row.append(_finish_field(field, quoted))
                yield row
                row, field, quoted = [], [], False
        elif char == "," and depth == 1:
            row.append(_finish_field(field, quoted))
            field, quoted = [], False
        elif depth >= 1:
            field.append(char)
        # Characters at depth 0 outside a literal are the commas and whitespace
        # between row groups — structure, not data.


def _finish_field(field: list[str], quoted: bool) -> str | None:
    """Close a field, mapping the bare token `NULL` to `None`.

    `quoted` distinguishes SQL `NULL` from the four-character string `'NULL'`,
    which is a legitimate product name fragment.
    """
    value = "".join(field)
    if not quoted and value.strip().upper() == "NULL":
        return None
    return value if quoted else value.strip()
