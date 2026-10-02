"""
backend/engine/private/scheduler_work_dispatcher.py
===================================================
Typed work-dispatch composition (Phase 24D3B). It implements the 24D3A dispatcher port by exact route selection only: one admitted request goes to exactly
one injected handler chosen by the enum identity of `request.admission.trigger.work_kind`:

    SOURCE_DATA_REFRESH         -> source_data_refresh_handler
    PORTFOLIO_ANALYSIS_REFRESH  -> portfolio_analysis_refresh_handler
    PORTFOLIO_HEALTH_CHECK      -> portfolio_health_check_handler
    GAME_CHANGER_REVIEW         -> game_changer_review_handler

The scheduler trigger carries declarative intent only; it lacks the business inputs (provider and series, analysis configuration, portfolio projection and
bands, event revision-family resolution) that the existing engines need, so this module fabricates none of them and has no business workflow, provider,
allocation or Game Changer binding: those are application workflows owned by the injected handlers. GAME_CHANGER_REVIEW stays a review intent; nothing here
sells, trades, rebalances or quarantines.

Each handler receives a `PrivateSchedulerWorkContext`: the unchanged admission (same object), the claim key and `work_idempotency_key`, an explicit alias
of the admission's run hash (no second identity) that handlers with external effects may use as a stable logical occurrence key. It is deliberately
distinct from the claim key: the same run keeps its work_idempotency_key across a lease takeover while the claim key changes. 24D3A guarantees at most one
dispatch per invocation, not exactly-once effects.

Handler results must be the exact `PrivateSchedulerDispatchResult` and are returned by identity, unchanged (no failure-code rewrite, no fallback to another
handler). A wrong result type raises TypeError, which the 24D3A runtime catches as an ordinary Exception and persists as `dispatcher_exception`; the runtime's
`dispatcher_contract_error` stays reserved for a dispatcher that itself returns a wrong top-level object. Handler exceptions are not caught here (the runtime
owns sanitization); base exceptions are never caught. No database, clock, randomness, async, queue or loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from backend.engine.private.scheduler_run_admission import PrivateSchedulerRunAdmission
from backend.engine.private.scheduler_runtime import PrivateSchedulerDispatchRequest, PrivateSchedulerDispatchResult
from backend.engine.private.scheduler_trigger import PrivateSchedulerScope, PrivateSchedulerWorkKind

_HASH = re.compile(r"[0-9a-f]{64}")
_WK = PrivateSchedulerWorkKind

_ERR_ADMISSION = "admission must be an exact PrivateSchedulerRunAdmission instance"
_ERR_CLAIM_KEY_TYPE = "claim_key must be an exact str"
_ERR_CLAIM_KEY_VALUE = "claim_key must be 1..128 printable characters without surrounding whitespace"
_ERR_WORK_KEY = "work_idempotency_key must equal the admission run_idempotency_sha256"
_ERR_REQUEST = "request must be an exact PrivateSchedulerDispatchRequest instance"
_ERR_HANDLER = "handler must expose a callable handle"
_ERR_RESULT = "handler must return an exact PrivateSchedulerDispatchResult"
_ERR_ROUTE = "work_kind has no route"
_ERR_SCOPE = "work kind and scope/owner/portfolio authority are inconsistent"


@dataclass(frozen=True)
class PrivateSchedulerWorkContext:
    """What a work handler receives; owner, portfolio and work kind are reachable through the admission's trigger only."""
    admission: PrivateSchedulerRunAdmission
    claim_key: str
    work_idempotency_key: str

    def __post_init__(self) -> None:
        if type(self.admission) is not PrivateSchedulerRunAdmission:
            raise TypeError(_ERR_ADMISSION)
        if type(self.claim_key) is not str:
            raise TypeError(_ERR_CLAIM_KEY_TYPE)
        if not 1 <= len(self.claim_key) <= 128 or self.claim_key != self.claim_key.strip() or not self.claim_key.isprintable():
            raise ValueError(_ERR_CLAIM_KEY_VALUE)
        if type(self.work_idempotency_key) is not str or _HASH.fullmatch(self.work_idempotency_key) is None:
            raise ValueError(_ERR_WORK_KEY)
        if self.work_idempotency_key != self.admission.run_idempotency_sha256:
            raise ValueError(_ERR_WORK_KEY)


class PrivateSchedulerWorkHandler(Protocol):
    """One synchronous application work handler."""

    def handle(self, *, context: PrivateSchedulerWorkContext) -> PrivateSchedulerDispatchResult:
        ...


def _is_handler(handler: object) -> bool:
    return callable(getattr(handler, "handle", None))


def _check_scope(admission: PrivateSchedulerRunAdmission) -> None:
    trigger = admission.trigger
    if trigger.work_kind is _WK.SOURCE_DATA_REFRESH:
        valid = trigger.scope is PrivateSchedulerScope.SYSTEM and trigger.owner_id is None and trigger.portfolio_id is None
    else:
        valid = trigger.scope is PrivateSchedulerScope.PORTFOLIO and type(trigger.owner_id) is UUID and type(trigger.portfolio_id) is UUID
    if not valid:
        raise ValueError(_ERR_SCOPE)


class PrivateSchedulerTypedWorkDispatcher:
    """Satisfies the 24D3A work-dispatcher port: exact WorkKind route selection to four required injected handlers."""

    def __init__(
        self,
        *,
        source_data_refresh_handler: PrivateSchedulerWorkHandler,
        portfolio_analysis_refresh_handler: PrivateSchedulerWorkHandler,
        portfolio_health_check_handler: PrivateSchedulerWorkHandler,
        game_changer_review_handler: PrivateSchedulerWorkHandler,
    ) -> None:
        if not (_is_handler(source_data_refresh_handler) and _is_handler(portfolio_analysis_refresh_handler) and _is_handler(portfolio_health_check_handler)
                and _is_handler(game_changer_review_handler)):
            raise TypeError(_ERR_HANDLER)
        self._source_data_refresh_handler = source_data_refresh_handler
        self._portfolio_analysis_refresh_handler = portfolio_analysis_refresh_handler
        self._portfolio_health_check_handler = portfolio_health_check_handler
        self._game_changer_review_handler = game_changer_review_handler

    def _route(self, work_kind: PrivateSchedulerWorkKind) -> PrivateSchedulerWorkHandler:
        if work_kind is _WK.SOURCE_DATA_REFRESH:
            return self._source_data_refresh_handler
        if work_kind is _WK.PORTFOLIO_ANALYSIS_REFRESH:
            return self._portfolio_analysis_refresh_handler
        if work_kind is _WK.PORTFOLIO_HEALTH_CHECK:
            return self._portfolio_health_check_handler
        if work_kind is _WK.GAME_CHANGER_REVIEW:
            return self._game_changer_review_handler
        raise ValueError(_ERR_ROUTE)

    def dispatch(self, *, request: PrivateSchedulerDispatchRequest) -> PrivateSchedulerDispatchResult:
        """Route one request to exactly one handler, call it once and return its exact result unchanged."""
        if type(request) is not PrivateSchedulerDispatchRequest:
            raise TypeError(_ERR_REQUEST)
        admission = request.admission
        _check_scope(admission)
        handler = self._route(admission.trigger.work_kind)
        context = PrivateSchedulerWorkContext(admission=admission, claim_key=request.claim_key, work_idempotency_key=admission.run_idempotency_sha256)
        result = handler.handle(context=context)
        if type(result) is not PrivateSchedulerDispatchResult:
            raise TypeError(_ERR_RESULT)
        return result
