"""
backend/engine/private/backtest_replay_plan.py
==============================================
Pure ordered historical replay plan (Phase 26B): an immutable, caller-ordered tuple of explicit Phase 26A1 replay points. It answers only which explicit
points belong to a replay and in what knowledge-time order.

Rules: the tuple is stored by identity (exact `tuple`, no list, generator, set or subclass; no copy), non-empty, every member an exact
`PrivateBacktestReplayPoint`; adjacent `knowledge_cutoff_utc` instants must strictly increase (comparison by UTC instant through the closed point, so
10:00+03:00 and 07:00 UTC are the same frontier and a duplicate); one `AsOfMode` for the whole plan (taken from the first point, a mid-run switch is rejected).
The caller's order is the authority: nothing is sorted, deduplicated or repaired. `evaluation_date` is not order authority (it may repeat, decrease or lie after
the cutoff's calendar date) and is never compared across points. No dates or frontiers are generated, so there is no calendar, scheduler recurrence or
market-data dependency, and no execution, input resolution, performance, clock, randomness, hash or I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.engine.private.backtest_replay_point import PrivateBacktestReplayPoint
from backend.engine.private.domain import AsOfMode

_ERR_TUPLE = "points must be an exact tuple"
_ERR_EMPTY = "a replay plan needs at least one replay point"
_ERR_MEMBER = "every point must be an exact PrivateBacktestReplayPoint instance"
_ERR_MODE = "every point of a plan must carry the same AsOfMode"
_ERR_ORDER = "knowledge_cutoff_utc must strictly increase from one point to the next"


@dataclass(frozen=True)
class PrivateBacktestReplayPlan:
    """Caller-ordered replay points with strictly increasing UTC knowledge frontiers and a single PIT perspective."""
    points: tuple[PrivateBacktestReplayPoint, ...]

    def __post_init__(self) -> None:
        if type(self.points) is not tuple:
            raise TypeError(_ERR_TUPLE)
        if len(self.points) < 1:
            raise ValueError(_ERR_EMPTY)
        previous = None
        for point in self.points:
            if type(point) is not PrivateBacktestReplayPoint:
                raise TypeError(_ERR_MEMBER)
            if previous is not None:
                if point.as_of_mode is not self.points[0].as_of_mode:
                    raise ValueError(_ERR_MODE)
                if not previous.knowledge_cutoff_utc < point.knowledge_cutoff_utc:
                    raise ValueError(_ERR_ORDER)
            previous = point

    @property
    def as_of_mode(self) -> AsOfMode:
        return self.points[0].as_of_mode

    @property
    def first_point(self) -> PrivateBacktestReplayPoint:
        return self.points[0]

    @property
    def last_point(self) -> PrivateBacktestReplayPoint:
        return self.points[-1]


def build_private_backtest_replay_plan(*, points: tuple[PrivateBacktestReplayPoint, ...]) -> PrivateBacktestReplayPlan:
    """Validate and wrap the caller's explicit, already ordered replay points."""
    return PrivateBacktestReplayPlan(points=points)
