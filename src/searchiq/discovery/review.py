"""The review queue.

Discovery is automated; adoption is not. Nothing here writes to the production
search configuration. A proposal enters as pending, a person accepts or rejects
it, and the accepted set leaves as an export file someone deploys deliberately.

Two properties make that boundary worth trusting. Re-running discovery updates
confidence on proposals still awaiting review and leaves anything already ruled
on exactly as the reviewer left it, so a rejected proposal stays rejected however
often the evidence is recomputed. And every proposal carries the numbers its
confidence came from, so a reviewer can disagree with the score.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from searchiq.analytics.catalog import CatalogIndex
from searchiq.discovery import misspellings, synonyms
from searchiq.discovery.misspellings import Candidate

# Statuses a reviewer can move a proposal into. `pending` is the undo: a
# decision taken by mistake has to be reversible, or reviewers hesitate over the
# ones they are unsure about and the queue stops moving. Reopening restores the
# proposal to the state discovery left it in, so the next run re-scores it.
DECISIONS = ("approved", "rejected", "pending")


@dataclass
class Suggestion:
    """A stored proposal, as a reviewer sees it."""

    id: int
    kind: str
    source_term: str
    target_term: str
    lang: str
    confidence: float
    rationale: str
    evidence: dict[str, Any]
    status: str
    created_at: str
    updated_at: str
    reviewer: str | None
    review_note: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Suggestion:
        return cls(
            id=row["id"],
            kind=row["kind"],
            source_term=row["source_term"],
            target_term=row["target_term"],
            lang=row["lang"],
            confidence=row["confidence"],
            rationale=row["rationale"],
            evidence=json.loads(row["evidence"]),
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            reviewer=row["reviewer"],
            review_note=row["review_note"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "source_term": self.source_term,
            "target_term": self.target_term,
            "lang": self.lang,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "evidence": self.evidence,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reviewer": self.reviewer,
            "review_note": self.review_note,
        }


@dataclass
class RefreshReport:
    """What a discovery run changed in the queue."""

    proposed: int = 0
    updated: int = 0
    unchanged_by_decision: int = 0
    withdrawn: int = 0

    def as_lines(self) -> list[str]:
        return [
            f"  {self.proposed} new proposal(s)",
            f"  {self.updated} pending proposal(s) re-scored",
            f"  {self.withdrawn} pending proposal(s) withdrawn, no longer supported",
            f"  {self.unchanged_by_decision} already reviewed, left untouched",
        ]


def refresh(
    connection: sqlite3.Connection, *, catalog: CatalogIndex | None = None
) -> RefreshReport:
    """Run every discovery method and merge the results into the queue."""
    catalog = catalog or CatalogIndex.for_connection(connection)
    candidates: list[Candidate] = [
        *misspellings.discover(connection, catalog=catalog),
        *synonyms.discover(connection, catalog=catalog),
    ]

    report = RefreshReport()
    now = _now()
    still_supported = {
        (candidate.kind, candidate.source_term, candidate.target_term)
        for candidate in candidates
    }

    for candidate in candidates:
        existing = connection.execute(
            "SELECT id, status FROM suggestion "
            "WHERE kind = ? AND source_term = ? AND target_term = ?",
            (candidate.kind, candidate.source_term, candidate.target_term),
        ).fetchone()

        if existing is None:
            connection.execute(
                "INSERT INTO suggestion "
                "(kind, source_term, target_term, lang, confidence, rationale, "
                " evidence, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,'pending',?,?)",
                (
                    candidate.kind,
                    candidate.source_term,
                    candidate.target_term,
                    candidate.lang,
                    candidate.confidence,
                    candidate.rationale,
                    json.dumps(candidate.evidence, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            report.proposed += 1
        elif existing["status"] == "pending":
            connection.execute(
                "UPDATE suggestion SET confidence = ?, rationale = ?, evidence = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    candidate.confidence,
                    candidate.rationale,
                    json.dumps(candidate.evidence, ensure_ascii=False),
                    now,
                    existing["id"],
                ),
            )
            report.updated += 1
        else:
            # Already approved or rejected. A person has ruled on this, and a
            # recomputed score is not grounds for overturning them.
            report.unchanged_by_decision += 1

    report.withdrawn = _withdraw_unsupported(connection, still_supported)
    return report


def _withdraw_unsupported(
    connection: sqlite3.Connection, still_supported: set[tuple[str, str, str]]
) -> int:
    """Drop pending proposals this run no longer produces.

    Evidence is computed from the loaded data, so re-loading different data can
    leave a proposal standing that nothing supports any more — a rule about a
    catalogue that is no longer there. Leaving it in the queue would invite a
    reviewer to approve it on a rationale that has quietly stopped being true,
    which is exactly the silent adoption this queue exists to prevent.

    Only *pending* rows are withdrawn. An approved or rejected proposal is a
    record of a human decision, and this system does not delete those.
    """
    pending = connection.execute(
        "SELECT id, kind, source_term, target_term FROM suggestion WHERE status = 'pending'"
    ).fetchall()
    stale = [
        (row["id"],)
        for row in pending
        if (row["kind"], row["source_term"], row["target_term"]) not in still_supported
    ]
    if stale:
        connection.executemany("DELETE FROM suggestion WHERE id = ?", stale)
    return len(stale)


def list_suggestions(
    connection: sqlite3.Connection,
    *,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> list[Suggestion]:
    """Read the queue, strongest proposals first."""
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    rows = connection.execute(
        f"SELECT * FROM suggestion {where} "  # noqa: S608 - clauses are literals
        "ORDER BY status = 'pending' DESC, confidence DESC, id LIMIT ?",
        params,
    ).fetchall()
    return [Suggestion.from_row(row) for row in rows]


def decide(
    connection: sqlite3.Connection,
    suggestion_id: int,
    *,
    decision: str,
    reviewer: str = "unknown",
    note: str | None = None,
) -> Suggestion:
    """Record a reviewer's decision on one proposal."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, got {decision!r}")

    cursor = connection.execute(
        "UPDATE suggestion SET status = ?, reviewer = ?, review_note = ?, updated_at = ? "
        "WHERE id = ?",
        (decision, reviewer, note, _now(), suggestion_id),
    )
    if cursor.rowcount == 0:
        raise KeyError(f"no suggestion with id {suggestion_id}")

    row = connection.execute(
        "SELECT * FROM suggestion WHERE id = ?", (suggestion_id,)
    ).fetchone()
    return Suggestion.from_row(row)


def approve(
    connection: sqlite3.Connection,
    suggestion_id: int,
    *,
    reviewer: str = "unknown",
    note: str | None = None,
) -> Suggestion:
    """Accept a proposal. It joins the next export; nothing is applied yet."""
    return decide(
        connection, suggestion_id, decision="approved", reviewer=reviewer, note=note
    )


def reject(
    connection: sqlite3.Connection,
    suggestion_id: int,
    *,
    reviewer: str = "unknown",
    note: str | None = None,
) -> Suggestion:
    """Decline a proposal. It will not be re-proposed by later discovery runs."""
    return decide(
        connection, suggestion_id, decision="rejected", reviewer=reviewer, note=note
    )


def reopen(
    connection: sqlite3.Connection,
    suggestion_id: int,
    *,
    reviewer: str = "unknown",
    note: str | None = None,
) -> Suggestion:
    """Undo a decision, returning the proposal to the queue.

    An approved proposal that is reopened drops straight out of the export, so
    an accidental approval can be taken back before anything is deployed.
    """
    return decide(
        connection, suggestion_id, decision="pending", reviewer=reviewer, note=note
    )


def export_approved(connection: sqlite3.Connection) -> dict[str, Any]:
    """Render the approved proposals as a deployable configuration document.

    The shape mirrors how a search engine consumes these: spelling corrections
    map a typed term to its canonical form, synonyms are expansion groups, and
    partial queries are autocomplete hints rather than rewrites.

    This function only builds the document. Writing it anywhere that affects
    live search is a deliberate, separate act by an operator.
    """
    approved = list_suggestions(connection, status="approved", limit=10_000)

    corrections = {
        suggestion.source_term: suggestion.target_term
        for suggestion in approved
        if suggestion.kind == "misspelling"
    }
    autocomplete = {
        suggestion.source_term: suggestion.target_term
        for suggestion in approved
        if suggestion.kind == "partial_query"
    }

    # Synonyms are transitive: if a == b and b == c then all three should expand
    # to each other. Merging the pairs into connected groups is what the engine
    # actually wants, and it removes the ordering artefacts of pairwise
    # discovery.
    groups = _merge_into_groups(
        [
            (suggestion.source_term, suggestion.target_term)
            for suggestion in approved
            if suggestion.kind == "synonym"
        ]
    )

    return {
        "generated_at": _now(),
        "approved_count": len(approved),
        "spelling_corrections": corrections,
        "autocomplete_hints": autocomplete,
        "synonym_groups": groups,
    }


def _merge_into_groups(pairs: list[tuple[str, str]]) -> list[list[str]]:
    """Merge synonym pairs into connected groups (a small union-find)."""
    parent: dict[str, str] = {}

    def find(term: str) -> str:
        parent.setdefault(term, term)
        while parent[term] != term:
            parent[term] = parent[parent[term]]
            term = parent[term]
        return term

    for left, right in pairs:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    groups: dict[str, list[str]] = {}
    for term in parent:
        groups.setdefault(find(term), []).append(term)
    return sorted((sorted(members) for members in groups.values()), key=lambda g: g[0])


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
