"""
backend/engine/private/backtest_user_view_history.py
====================================================
Pure immutable historical revision envelope for Phase 20 user views (Phase 26C2D1). The closed `UserReturnView` is a source-neutral mathematical object: it carries
no timestamp, no lifecycle identity and positional loadings only. It must therefore never be replayed into an earlier historical analysis merely because its
economic contents are valid. This module adds the provenance a later point-in-time resolver needs, and nothing else.

One revision carries a stable caller-supplied lifecycle `view_id` (never generated, never derived), an explicit `revision` number, the lifecycle `action`, the
`available_at` knowledge-availability instant (when Sentinax may treat this exact revision as known; not an economic or effective time), an exact canonical
instrument basis and the exact `UserReturnView` (stored by identity, never copied or normalized). Loadings are positional, so an UPSERT must name the instrument
basis they refer to: exact UUIDs in strictly ascending UUID-string order, as many as there are loadings. A WITHDRAW carries no view and no instrument basis.

Only one envelope is validated here. Cross-revision rules, comparison against a replay cutoff, revision-family grouping, view-set or posterior construction and any
completeness or readiness policy belong to later boundaries. Nothing is persisted. No clock, randomness, hash, I/O or sequence handling beyond validating the supplied tuple.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from uuid import UUID

from backend.engine.private.allocation_user_views import UserReturnView

_ERR_VIEW_ID = "view_id must be an exact UUID instance"
_ERR_REVISION_TYPE = "revision must be an exact int (bool is not accepted)"
_ERR_REVISION_RANGE = "revision must be >= 1"
_ERR_ACTION = "action must be an exact PrivateBacktestUserViewRevisionAction member"
_ERR_AVAILABLE_AT = "available_at must be an exact timezone-aware datetime with a valid UTC offset"
_ERR_VIEW = "an UPSERT requires an exact UserReturnView"
_ERR_IDS_TYPE = "instrument_ids must be an exact tuple of exact UUID instances"
_ERR_IDS_EMPTY = "an UPSERT requires a non-empty instrument basis"
_ERR_IDS_ORDER = "instrument_ids must be unique and in strictly ascending UUID-string order"
_ERR_DIMENSION = "instrument_ids length must equal the number of view loadings"
_ERR_WITHDRAW = "a WITHDRAW carries no view and no instrument basis"


class PrivateBacktestUserViewRevisionAction(Enum):
    UPSERT = "upsert"
    WITHDRAW = "withdraw"


@dataclass(frozen=True)
class PrivateBacktestUserViewRevision:
    """One historical user-view revision: lifecycle identity, revision number, action, instrument basis, exact view and knowledge-availability instant."""
    view_id: UUID
    revision: int
    action: PrivateBacktestUserViewRevisionAction
    instrument_ids: tuple[UUID, ...]
    view: UserReturnView | None
    available_at: datetime

    def __post_init__(self) -> None:
        if type(self.view_id) is not UUID:
            raise TypeError(_ERR_VIEW_ID)
        if type(self.revision) is not int:
            raise TypeError(_ERR_REVISION_TYPE)
        if self.revision < 1:
            raise ValueError(_ERR_REVISION_RANGE)
        if type(self.action) is not PrivateBacktestUserViewRevisionAction:
            raise TypeError(_ERR_ACTION)
        if type(self.available_at) is not datetime or self.available_at.tzinfo is None:
            raise TypeError(_ERR_AVAILABLE_AT)
        try:
            offset = self.available_at.utcoffset()
            if type(offset) is not timedelta or not (-timedelta(hours=24) < offset < timedelta(hours=24)):
                raise TypeError(_ERR_AVAILABLE_AT)
            self.available_at.astimezone(timezone.utc)
        except Exception as exc:
            raise TypeError(_ERR_AVAILABLE_AT) from exc

        if self.action is PrivateBacktestUserViewRevisionAction.WITHDRAW:
            if self.view is not None or self.instrument_ids != ():
                raise ValueError(_ERR_WITHDRAW)
            return

        if type(self.view) is not UserReturnView:
            raise TypeError(_ERR_VIEW)
        if type(self.instrument_ids) is not tuple or any(type(member) is not UUID for member in self.instrument_ids):
            raise TypeError(_ERR_IDS_TYPE)
        if not self.instrument_ids:
            raise ValueError(_ERR_IDS_EMPTY)
        for earlier, later in zip(self.instrument_ids, self.instrument_ids[1:]):
            if not str(earlier) < str(later):
                raise ValueError(_ERR_IDS_ORDER)
        if len(self.instrument_ids) != len(self.view.loadings):
            raise ValueError(_ERR_DIMENSION)
