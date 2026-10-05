"""
backend/engine/private/backtest_user_view_resolution.py
=======================================================
Pure point-in-time resolution of Phase 26C2D1 user-view revisions at one analysis knowledge frontier (Phase 26C2D2). The only eligibility frontier is the analysis
context's knowledge cutoff: a revision is eligible iff its `available_at`, as a UTC instant, is not after the cutoff (equality is eligible). User views are
internal system knowledge, so SOURCE_AS_OF and SYSTEM_AS_OF apply the same rule; the evaluation date, horizon and every revision attribute other than `available_at`
play no part in eligibility.

Order is mandatory: the supplied envelopes are exact-type validated, future revisions are filtered out and discarded completely, and only then are duplicate, gap,
start and availability-order rules applied to the eligible subset. A future revision (duplicate, huge number, withdrawal, other instrument basis) can therefore never
change an earlier resolution. Caller order has no authority: eligible revisions are canonically ordered by `str(view_id)` then `revision`; the exact envelope objects
are preserved by identity.

Eligible `(view_id, revision)` identities must be unique. Under an explicit COMPLETE_AT_CUTOFF assertion every eligible family must start at revision 1 with an UPSERT,
be contiguous, and have non-decreasing availability instants by revision; the highest revision is the terminal one (revision number, never a timestamp or economic
value, is the lifecycle authority). A terminal WITHDRAW stays in `terminal_revisions` but is not active and revives nothing; a later UPSERT may reactivate. Under
INCOMPLETE_AT_CUTOFF the eligible set is preserved for audit only: no terminal and no active revision is exposed.

The coverage assertion is source-local user-view history coverage, never overall decision readiness, and the empty complete history (no views known) is a valid
resolved state. Direct construction revalidates the whole eligible-history graph and the resolution consistency through the same single derivation as the resolver; it
cannot prove that a caller's COMPLETE_AT_CUTOFF assertion is objectively exhaustive. Positional instrument bases are retained on the exact envelopes and are not bound
to any prior here. No view set, posterior, persistence, clock, randomness, hash or I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from enum import Enum
from typing import Tuple

from backend.engine.private.backtest_analysis_context import PrivateBacktestAnalysisContext
from backend.engine.private.backtest_user_view_history import (
    PrivateBacktestUserViewRevision,
    PrivateBacktestUserViewRevisionAction,
)

_ERR_CONTEXT = "analysis_context must be an exact PrivateBacktestAnalysisContext instance"
_ERR_REVISIONS = "revisions must be an exact tuple of exact PrivateBacktestUserViewRevision instances"
_ERR_COVERAGE = "coverage must be an exact PrivateBacktestUserViewHistoryCoverage member"
_ERR_STATUS_TYPE = "status must be an exact PrivateBacktestUserViewResolutionStatus member"
_ERR_FIELD_TUPLE = "eligible_revisions, terminal_revisions and active_revisions must be exact tuples of exact PrivateBacktestUserViewRevision instances"
_ERR_FUTURE = "an eligible revision is available after the knowledge cutoff"
_ERR_ORDER = "eligible_revisions must be in canonical order (str(view_id) then revision)"
_ERR_DUPLICATE = "eligible (view_id, revision) identities must be unique"
_ERR_START = "a complete eligible family must start at revision 1"
_ERR_GAP = "a complete eligible family must have contiguous revision numbers"
_ERR_FIRST_ACTION = "a complete eligible family must begin with an UPSERT"
_ERR_AVAILABILITY = "a complete eligible family must have non-decreasing availability instants by revision"
_ERR_STATUS = "status does not match the coverage and the eligible history"
_ERR_TERMINAL = "terminal_revisions must be exactly the canonical terminal revision objects of the eligible history"
_ERR_ACTIVE = "active_revisions must be exactly the canonical active revision objects of the eligible history"

_UPSERT = PrivateBacktestUserViewRevisionAction.UPSERT


class PrivateBacktestUserViewHistoryCoverage(Enum):
    COMPLETE_AT_CUTOFF = "complete_at_cutoff"
    INCOMPLETE_AT_CUTOFF = "incomplete_at_cutoff"


class PrivateBacktestUserViewResolutionStatus(Enum):
    RESOLVED = "resolved"
    INCOMPLETE_COVERAGE = "incomplete_coverage"


_Revisions = Tuple[PrivateBacktestUserViewRevision, ...]


def _is_revision_tuple(value: object) -> bool:
    return type(value) is tuple and all(type(item) is PrivateBacktestUserViewRevision for item in value)


def _order_key(revision: PrivateBacktestUserViewRevision) -> Tuple[str, int]:
    return (str(revision.view_id), revision.revision)


def _derive(
    eligible: _Revisions, coverage: PrivateBacktestUserViewHistoryCoverage,
) -> Tuple[PrivateBacktestUserViewResolutionStatus, _Revisions, _Revisions]:
    """The single canonical resolution of an already-filtered, canonically ordered eligible history."""
    for earlier, later in zip(eligible, eligible[1:]):
        if _order_key(earlier) == _order_key(later):
            raise ValueError(_ERR_DUPLICATE)
    if coverage is PrivateBacktestUserViewHistoryCoverage.INCOMPLETE_AT_CUTOFF:
        return PrivateBacktestUserViewResolutionStatus.INCOMPLETE_COVERAGE, (), ()

    terminal: list[PrivateBacktestUserViewRevision] = []
    previous: PrivateBacktestUserViewRevision | None = None
    for current in eligible:
        if previous is None or previous.view_id != current.view_id:
            if previous is not None:
                terminal.append(previous)
            if current.revision != 1:
                raise ValueError(_ERR_START)
            if current.action is not _UPSERT:
                raise ValueError(_ERR_FIRST_ACTION)
        else:
            if current.revision != previous.revision + 1:
                raise ValueError(_ERR_GAP)
            if current.available_at.astimezone(timezone.utc) < previous.available_at.astimezone(timezone.utc):
                raise ValueError(_ERR_AVAILABILITY)
        previous = current
    if previous is not None:
        terminal.append(previous)
    terminal_tuple = tuple(terminal)
    active_tuple = tuple(revision for revision in terminal_tuple if revision.action is _UPSERT)
    return PrivateBacktestUserViewResolutionStatus.RESOLVED, terminal_tuple, active_tuple


def _same_objects(left: _Revisions, right: _Revisions) -> bool:
    return len(left) == len(right) and all(a is b for a, b in zip(left, right))


@dataclass(frozen=True)
class PrivateBacktestUserViewResolution:
    """The user-view revision history knowable at one analysis knowledge cutoff, with its terminal and active revisions (exact envelope objects)."""
    analysis_context: PrivateBacktestAnalysisContext
    coverage: PrivateBacktestUserViewHistoryCoverage
    eligible_revisions: tuple[PrivateBacktestUserViewRevision, ...]
    status: PrivateBacktestUserViewResolutionStatus
    terminal_revisions: tuple[PrivateBacktestUserViewRevision, ...]
    active_revisions: tuple[PrivateBacktestUserViewRevision, ...]

    def __post_init__(self) -> None:
        if type(self.analysis_context) is not PrivateBacktestAnalysisContext:
            raise TypeError(_ERR_CONTEXT)
        if type(self.coverage) is not PrivateBacktestUserViewHistoryCoverage:
            raise TypeError(_ERR_COVERAGE)
        if type(self.status) is not PrivateBacktestUserViewResolutionStatus:
            raise TypeError(_ERR_STATUS_TYPE)
        if not (_is_revision_tuple(self.eligible_revisions) and _is_revision_tuple(self.terminal_revisions) and _is_revision_tuple(self.active_revisions)):
            raise TypeError(_ERR_FIELD_TUPLE)

        cutoff_utc = self.analysis_context.replay_point.knowledge_cutoff_utc
        for revision in self.eligible_revisions:
            if revision.available_at.astimezone(timezone.utc) > cutoff_utc:
                raise ValueError(_ERR_FUTURE)
        for earlier, later in zip(self.eligible_revisions, self.eligible_revisions[1:]):
            if _order_key(earlier) > _order_key(later):
                raise ValueError(_ERR_ORDER)

        status, terminal, active = _derive(self.eligible_revisions, self.coverage)
        if self.status is not status:
            raise ValueError(_ERR_STATUS)
        if not _same_objects(self.terminal_revisions, terminal):
            raise ValueError(_ERR_TERMINAL)
        if not _same_objects(self.active_revisions, active):
            raise ValueError(_ERR_ACTIVE)


def resolve_private_backtest_user_views(
    *,
    analysis_context: PrivateBacktestAnalysisContext,
    revisions: tuple[PrivateBacktestUserViewRevision, ...],
    coverage: PrivateBacktestUserViewHistoryCoverage,
) -> PrivateBacktestUserViewResolution:
    """Resolve the supplied revision history at the analysis knowledge cutoff: filter by knowledge time first, then validate and resolve the eligible subset."""
    if type(analysis_context) is not PrivateBacktestAnalysisContext:
        raise TypeError(_ERR_CONTEXT)
    if not _is_revision_tuple(revisions):
        raise TypeError(_ERR_REVISIONS)
    if type(coverage) is not PrivateBacktestUserViewHistoryCoverage:
        raise TypeError(_ERR_COVERAGE)

    cutoff_utc = analysis_context.replay_point.knowledge_cutoff_utc
    known = [revision for revision in revisions if revision.available_at.astimezone(timezone.utc) <= cutoff_utc]
    eligible = tuple(sorted(known, key=_order_key))
    status, terminal, active = _derive(eligible, coverage)
    return PrivateBacktestUserViewResolution(
        analysis_context=analysis_context, coverage=coverage, eligible_revisions=eligible, status=status, terminal_revisions=terminal, active_revisions=active,
    )
