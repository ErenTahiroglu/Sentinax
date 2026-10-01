"""
backend/engine/private/game_changer_event.py
============================================
Source-neutral Game Changer event evidence with PIT knowledge binding (Phase 23A).

This module is evidence only. It represents one external material event with canonical scope, source provenance, knowledge
times, an optional economic effective date and explicit revision lineage, and it can prove whether that event was temporally
admissible at one explicit analysis cutoff. It fabricates no interpretation: event_type is a structural label, not materiality,
not sentiment and not a recommendation; no event type is inherently good or bad.

Explicitly out of scope (later checkpoints or never): no sentiment or polarity, no materiality / urgency / thesis-impact
assessment (23B), no quarantine or allocation consequence (23C), no provider / KAP ingestion, scraping or polling (23D), no LLM
or prompt dependency (a later LLM may only perform structured extraction and may never set weights, sales or orders), no
scheduler, no portfolio, rebalance or allocation coupling, no tax, no execution. No clock is read here.

Three times with distinct meanings:

    published_at     proven source-publication instant, or None when unproven (never fabricated from any other date)
    observed_at      when Sentinax / the upstream evidence pipeline actually observed the event (caller supplied)
    effective_date   known economic / legal effective date, or None; NOT the publication date, may lie in the future

A future effective_date does not make an event look-ahead: if it was published by the cutoff, the future date was public knowledge.

PIT binding (exact UTC-instant comparison, equality allowed, no tolerance):

    SOURCE_AS_OF   availability = published_at, else observed_at (fallback); admissible iff availability <= knowledge_cutoff
    SYSTEM_AS_OF   admissible iff observed_at <= knowledge_cutoff and (published_at is None or published_at <= knowledge_cutoff)

so a publicly knowable event that Sentinax had not yet observed is accepted under SOURCE_AS_OF and rejected under SYSTEM_AS_OF.

Identity and provenance are explicit caller authority. `source_key` is a strict lowercase identifier; `source_event_key` is the
provider-native immutable identifier kept exactly (case preserved, trimmed, printable, 1..128 characters); `source_tier` is never
derived from the source key; instrument ids are canonical UUIDs resolved upstream (nothing is inferred from text, symbol, name
or ISIN). INSTRUMENT scope requires at least one id; SYSTEMIC scope requires none; MACRO_SHOCK is the only SYSTEMIC type, so an
unresolved company event can never be silently systemic. `content_sha256` is an opaque upstream content-provenance reference
(64 lowercase hex): nothing is hashed or stored here and it proves no authenticity. Revision lineage (ORIGINAL / UPDATE /
CORRECTION / WITHDRAWAL plus `revises_source_event_key`) is recorded exactly as supplied: the chain is not resolved or chased and
no earlier event is superseded here. The core is source-neutral: no source-specific flags.

Architectural Invariants:
    - Pure domain module: standard library, `AnalysisPITContext`, `AsOfMode` and `SourceTier`. No network, filesystem, database,
      clock, randomness, float arithmetic, LLM or persistence. Exact concrete types: subclasses are rejected.
    - `GameChangerEventPITBinding` retains the event and context by identity and re-validates temporal admissibility on construction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from uuid import UUID

from backend.engine.private.analysis_pit import AnalysisPITContext
from backend.engine.private.domain import AsOfMode, SourceTier

_SOURCE_KEY = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")

_ERR_SOURCE_KEY = "source_key must be a canonical lowercase identifier of at most 64 characters"
_ERR_EVENT_KEY = "event keys must be non-blank, already trimmed, printable strings of 1 to 128 characters"
_ERR_STR_TYPE = "source_key, source_event_key and revises_source_event_key must be exact str instances"
_ERR_TIER = "source_tier must be an exact SourceTier instance"
_ERR_SCOPE = "scope must be an exact GameChangerEventScope instance"
_ERR_EVENT_TYPE = "event_type must be an exact GameChangerEventType instance"
_ERR_REVISION_KIND = "revision_kind must be an exact GameChangerRevisionKind instance"
_ERR_IDS_TYPE = "instrument_ids must be a tuple of exact UUID instances"
_ERR_IDS_VALUE = "instrument_ids must be unique and in canonical ascending UUID string order"
_ERR_SCOPE_IDS = "INSTRUMENT scope requires at least one instrument id and SYSTEMIC scope requires none"
_ERR_SCOPE_TYPE = "MACRO_SHOCK requires SYSTEMIC scope and every other event type requires INSTRUMENT scope"
_ERR_EFFECTIVE = "effective_date must be None or an exact date instance"
_ERR_DATETIME = "published_at and observed_at must be timezone-aware datetime instances"
_ERR_PUBLISHED = "published_at must not be later than observed_at"
_ERR_SHA = "content_sha256 must be exactly 64 lowercase hexadecimal characters"
_ERR_LINEAGE = "ORIGINAL events must not revise another event; UPDATE, CORRECTION and WITHDRAWAL must revise a different event"
_ERR_EVENT_TYPE_INSTANCE = "event must be an exact GameChangerEvent instance"
_ERR_CONTEXT_TYPE = "context must be an exact AnalysisPITContext instance"
_ERR_NOT_ADMISSIBLE = "event is not temporally admissible at the knowledge cutoff"


class GameChangerEventScope(Enum):
    INSTRUMENT = "instrument"
    SYSTEMIC = "systemic"


class GameChangerEventType(Enum):
    """Structural, source-neutral taxonomy. Not severity, sentiment, thesis relevance or a recommendation."""
    FINANCIAL_REPORT = "financial_report"
    GUIDANCE = "guidance"
    CAPITAL_ALLOCATION = "capital_allocation"
    FINANCING_LIQUIDITY = "financing_liquidity"
    M_AND_A = "m_and_a"
    OPERATIONS = "operations"
    MANAGEMENT_GOVERNANCE = "management_governance"
    LEGAL_REGULATORY = "legal_regulatory"
    OWNERSHIP_CONTROL = "ownership_control"
    CORPORATE_ACTION = "corporate_action"
    MACRO_SHOCK = "macro_shock"
    OTHER = "other"


class GameChangerRevisionKind(Enum):
    ORIGINAL = "original"
    UPDATE = "update"
    CORRECTION = "correction"
    WITHDRAWAL = "withdrawal"


def _check_event_key(value: object) -> None:
    if type(value) is not str:
        raise TypeError(_ERR_STR_TYPE)
    if not 1 <= len(value) <= 128 or value != value.strip() or not value.isprintable():
        raise ValueError(_ERR_EVENT_KEY)


def _check_aware(value: object) -> None:
    if type(value) is not datetime or value.tzinfo is None:
        raise TypeError(_ERR_DATETIME)
    try:
        if value.utcoffset() is None:
            raise TypeError(_ERR_DATETIME)
        value.astimezone(timezone.utc)
    except TypeError:
        raise TypeError(_ERR_DATETIME) from None
    except Exception:
        raise TypeError(_ERR_DATETIME) from None


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class GameChangerEvent:
    """Immutable source-neutral event evidence; no interpretation, severity or consequence is attached."""
    source_key: str
    source_event_key: str
    source_tier: SourceTier
    scope: GameChangerEventScope
    event_type: GameChangerEventType
    instrument_ids: tuple[UUID, ...]
    effective_date: date | None
    published_at: datetime | None
    observed_at: datetime
    content_sha256: str
    revision_kind: GameChangerRevisionKind
    revises_source_event_key: str | None

    def __post_init__(self) -> None:
        if type(self.source_key) is not str:
            raise TypeError(_ERR_STR_TYPE)
        if _SOURCE_KEY.fullmatch(self.source_key) is None:
            raise ValueError(_ERR_SOURCE_KEY)
        _check_event_key(self.source_event_key)
        if type(self.source_tier) is not SourceTier:
            raise TypeError(_ERR_TIER)
        if type(self.scope) is not GameChangerEventScope:
            raise TypeError(_ERR_SCOPE)
        if type(self.event_type) is not GameChangerEventType:
            raise TypeError(_ERR_EVENT_TYPE)
        if type(self.instrument_ids) is not tuple or any(type(i) is not UUID for i in self.instrument_ids):
            raise TypeError(_ERR_IDS_TYPE)
        keys = [str(i) for i in self.instrument_ids]
        if len(set(keys)) != len(keys) or keys != sorted(keys):
            raise ValueError(_ERR_IDS_VALUE)
        if (self.scope is GameChangerEventScope.INSTRUMENT) != bool(self.instrument_ids):
            raise ValueError(_ERR_SCOPE_IDS)
        if (self.event_type is GameChangerEventType.MACRO_SHOCK) != (self.scope is GameChangerEventScope.SYSTEMIC):
            raise ValueError(_ERR_SCOPE_TYPE)
        if self.effective_date is not None and type(self.effective_date) is not date:
            raise TypeError(_ERR_EFFECTIVE)
        _check_aware(self.observed_at)
        if self.published_at is not None:
            _check_aware(self.published_at)
            if _utc(self.published_at) > _utc(self.observed_at):
                raise ValueError(_ERR_PUBLISHED)
        if type(self.content_sha256) is not str:
            raise TypeError(_ERR_SHA)
        if _SHA256.fullmatch(self.content_sha256) is None:
            raise ValueError(_ERR_SHA)
        if type(self.revision_kind) is not GameChangerRevisionKind:
            raise TypeError(_ERR_REVISION_KIND)
        if self.revises_source_event_key is not None:
            _check_event_key(self.revises_source_event_key)
        if self.revision_kind is GameChangerRevisionKind.ORIGINAL:
            if self.revises_source_event_key is not None:
                raise ValueError(_ERR_LINEAGE)
        elif self.revises_source_event_key is None or self.revises_source_event_key == self.source_event_key:
            raise ValueError(_ERR_LINEAGE)


def _admissible(event: GameChangerEvent, context: AnalysisPITContext) -> bool:
    cutoff = context.knowledge_cutoff_utc
    observed = _utc(event.observed_at)
    published = None if event.published_at is None else _utc(event.published_at)
    if context.mode is AsOfMode.SOURCE_AS_OF:
        return (published if published is not None else observed) <= cutoff
    if context.mode is AsOfMode.SYSTEM_AS_OF:
        return observed <= cutoff and (published is None or published <= cutoff)
    return False


@dataclass(frozen=True)
class GameChangerEventPITBinding:
    """An event proven temporally admissible at one explicit analysis cutoff; retains both inputs by identity."""
    event: GameChangerEvent
    context: AnalysisPITContext

    def __post_init__(self) -> None:
        if type(self.event) is not GameChangerEvent:
            raise TypeError(_ERR_EVENT_TYPE_INSTANCE)
        if type(self.context) is not AnalysisPITContext:
            raise TypeError(_ERR_CONTEXT_TYPE)
        if not _admissible(self.event, self.context):
            raise ValueError(_ERR_NOT_ADMISSIBLE)


def bind_game_changer_event_pit(
    *,
    event: GameChangerEvent,
    context: AnalysisPITContext,
) -> GameChangerEventPITBinding:
    """Bind one event to one analysis context, failing closed when it was not knowable at the cutoff."""
    if type(event) is not GameChangerEvent:
        raise TypeError(_ERR_EVENT_TYPE_INSTANCE)
    if type(context) is not AnalysisPITContext:
        raise TypeError(_ERR_CONTEXT_TYPE)
    return GameChangerEventPITBinding(event=event, context=context)
