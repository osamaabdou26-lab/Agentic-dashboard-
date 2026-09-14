"""What the shopper did next, which is the only engagement signal this data has.

The source database records no clicks, no add-to-cart and no orders, so real
engagement cannot be computed. What the log does carry is the next search, and
the inference is standard: someone who finds what they wanted stops searching,
someone who does not tries again.

A search followed by a repeat or a rewording did not satisfy. A search followed
by nothing is ambiguous and deliberately not counted either way — the log cannot
tell a shopper who checked out from one who gave up, and counting terminal
searches as successes would quietly flatter every metric.

search_event.clicked_rank and .converted already exist. Populate them and these
proxies become a cross-check instead of a substitute.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from searchiq.text.similarity import damerau_levenshtein, is_prefix_of

# A follow-up later than this is a new shopping intent, not a reaction to the
# previous result set. Five minutes is generous for a search box.
REFORMULATION_WINDOW_SECONDS = 300

# Two queries closer than this in edit distance are the same word typed twice.
_TYPO_DISTANCE = 2

# An identical search re-fired this fast, returning an identical result set, is
# not a shopper reconsidering. Nobody reads five products and decides to search
# again in under two seconds. Traffic like this is a test harness, a retry loop,
# or a held-down key — and counting it as dissatisfaction would inflate every
# engagement number. It is flagged rather than deleted: which queries attract
# automated traffic is itself worth knowing, and silently dropping rows would
# make the totals disagree with the source log.
RAPID_REPEAT_SECONDS = 2.0


class FollowUp(enum.StrEnum):
    """What the same shopper did after a given search."""

    NONE = "none"                    #: session ended here; outcome unknown
    REPEAT = "repeat"                #: searched the identical term again
    REFORMULATION = "reformulation"  #: reworded the query
    NEW_INTENT = "new_intent"        #: searched again, but too late to be a reaction


class Reformulation(enum.StrEnum):
    """How the query changed, when it changed."""

    TYPO_FIX = "typo_fix"            #: a near-identical spelling
    REFINEMENT = "refinement"        #: one query extends the other
    SWITCH = "switch"                #: a different word entirely


@dataclass(frozen=True)
class EventBehaviour:
    """The behavioural context of one search within its session."""

    event_id: int
    session_id: str
    follow_up: FollowUp
    reformulation: Reformulation | None
    next_norm_query: str | None
    seconds_to_next: float | None
    results_unchanged: bool

    @property
    def rapid_repeat(self) -> bool:
        """Whether this search was re-fired too fast to be a human reaction."""
        return (
            self.follow_up is FollowUp.REPEAT
            and self.results_unchanged
            and self.seconds_to_next is not None
            and self.seconds_to_next < RAPID_REPEAT_SECONDS
        )

    @property
    def dissatisfied(self) -> bool:
        """Whether the shopper visibly reacted to an unsatisfying result set.

        A repeat or a rewording inside the reaction window is the proxy. A
        session that simply ended is not counted either way, and machine-speed
        repeats are excluded so a retry loop cannot masquerade as frustration.
        """
        if self.rapid_repeat:
            return False
        return self.follow_up in (FollowUp.REPEAT, FollowUp.REFORMULATION)


@dataclass(frozen=True)
class SearchRow:
    """The fields of `search_event` that behavioural analysis needs."""

    event_id: int
    session_id: str
    norm_query: str
    occurred_at: datetime
    result_signature: str


def analyse_sessions(rows: Iterable[SearchRow]) -> dict[int, EventBehaviour]:
    """Classify every search by what the same session did next.

    Returns a mapping from event id to its behavioural context, so callers can
    join it onto quality metrics without re-walking the log.
    """
    by_session: dict[str, list[SearchRow]] = {}
    for row in rows:
        by_session.setdefault(row.session_id, []).append(row)

    behaviours: dict[int, EventBehaviour] = {}
    for session_id, events in by_session.items():
        ordered = sorted(events, key=lambda row: (row.occurred_at, row.event_id))
        for position, event in enumerate(ordered):
            following = ordered[position + 1] if position + 1 < len(ordered) else None
            behaviours[event.event_id] = _classify(session_id, event, following)
    return behaviours


def _classify(
    session_id: str, event: SearchRow, following: SearchRow | None
) -> EventBehaviour:
    if following is None:
        return EventBehaviour(
            event_id=event.event_id,
            session_id=session_id,
            follow_up=FollowUp.NONE,
            reformulation=None,
            next_norm_query=None,
            seconds_to_next=None,
            results_unchanged=False,
        )

    gap = (following.occurred_at - event.occurred_at).total_seconds()
    unchanged = following.result_signature == event.result_signature

    if gap > REFORMULATION_WINDOW_SECONDS:
        follow_up, reformulation = FollowUp.NEW_INTENT, None
    elif following.norm_query == event.norm_query:
        follow_up, reformulation = FollowUp.REPEAT, None
    else:
        follow_up = FollowUp.REFORMULATION
        reformulation = _classify_reformulation(event.norm_query, following.norm_query)

    return EventBehaviour(
        event_id=event.event_id,
        session_id=session_id,
        follow_up=follow_up,
        reformulation=reformulation,
        next_norm_query=following.norm_query,
        seconds_to_next=gap,
        results_unchanged=unchanged,
    )


def _classify_reformulation(before: str, after: str) -> Reformulation:
    """Describe how a shopper changed their query.

    The three cases call for three different fixes, which is why they are not
    lumped together: a typo fix wants a spelling dictionary, a refinement wants
    better autocomplete, and a switch to an unrelated word wants a synonym.
    """
    if is_prefix_of(before, after) or is_prefix_of(after, before):
        return Reformulation.REFINEMENT
    if damerau_levenshtein(before, after) <= _TYPO_DISTANCE:
        return Reformulation.TYPO_FIX
    return Reformulation.SWITCH


def dissatisfaction_rate(behaviours: Sequence[EventBehaviour]) -> float | None:
    """Share of searches that provoked a repeat or a rewording.

    Terminal searches are excluded from the denominator, not counted as
    successes: their outcome is unknown, and including them would quietly make
    every metric look better than the evidence supports.
    """
    scored = [
        b
        for b in behaviours
        if b.follow_up is not FollowUp.NONE and not b.rapid_repeat
    ]
    if not scored:
        return None
    return sum(1 for b in scored if b.dissatisfied) / len(scored)
