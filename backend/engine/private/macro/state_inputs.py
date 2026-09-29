"""
backend/engine/private/macro/state_inputs.py
============================================
PIT-safe, exact-Decimal analytical input authority for the future Phase 17 macro state (Phase 17A).

Scope: this module only defines the safe INPUT boundary. It calculates no GrowthImpulse / PolicyInflationState /
FinancialStress, no z-score, regime, technical indicator or tilt.

Architectural Invariants:
    - Pure domain module. Zero network, filesystem, database, environment, ambient clock, UUID generation,
      randomness, hashing, float arithmetic, numpy/pandas/scipy, persistence, provider, orchestrator, resolver or
      fund-module dependency. No arithmetic is performed at all.
    - `MacroStateInputFact` represents ONE already-selected persisted macro observation at an explicit knowledge
      cutoff. Legacy `MacroObservationRecord.value: float` is NOT accepted analytical input: `value` must be an
      exact `Decimal` (or None). There is no `Decimal(float)` / `Decimal(str(float))` rehabilitation; a rounded
      binary float cannot be turned back into the original economic value.
    - Registry authority: `canonical_key` must resolve through `MacroSeriesRegistry.get` to an ACTIVE, VERIFIED
      series (no aliasing, no normalization). Category, unit, frequency, geography and provider are derived
      read-only from that definition, never caller-supplied. `TR_POLICY_RATE` (unverified, inactive) fails closed
      and is never mapped to `TR_TCMB_AOFM`.
    - Missing is never zero: `None` means missing. COMPLETE requires a finite Decimal; UNAVAILABLE requires None.
      PARTIAL / DEGRADED / STALE describe data that exists, so they still require an observed Decimal (no
      fabricated zero). Confidence never creates or alters availability.
    - PIT (mirrors migration 006 `get_macro_observation_as_of`): SYSTEM_AS_OF requires ingested_at <= as_of and
      (published_at is None or published_at <= as_of); SOURCE_AS_OF requires
      coalesce(published_at, observed_at) <= as_of. In both modes a supersession at or before as_of makes the row
      ineligible (superseded_at must be None or strictly after as_of). CURRENT_REPORTED does not exist here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from backend.engine.private.domain import AsOfMode, DataConfidenceLevel, DataStatus, SourceTier
from backend.engine.private.macro.models import (
    ContractStatus,
    MacroCategory,
    MacroFrequency,
    MacroSeriesDefinition,
    MacroUnit,
)
from backend.engine.private.macro.registry import MacroSeriesRegistry

_ERR_KEY_TYPE = "canonical_key must be an exact str instance"
_ERR_KEY_EMPTY = "canonical_key must be non-empty"
_ERR_UNREGISTERED = "macro series is not registered"
_ERR_NOT_VERIFIED = "macro series must be active and VERIFIED"
_ERR_DATE_TYPE = "effective_date must be an exact date instance"
_ERR_VALUE_TYPE = "value must be an exact Decimal instance or None"
_ERR_VALUE_FINITE = "value must be a finite Decimal"
_ERR_STATUS_TYPE = "data_status must be an exact DataStatus instance"
_ERR_CONFIDENCE_TYPE = "confidence_level must be an exact DataConfidenceLevel instance"
_ERR_TIER_TYPE = "source_tier must be an exact SourceTier instance"
_ERR_MODE_TYPE = "mode must be an exact AsOfMode instance"
_ERR_DATETIME_REQUIRED = "{name} must be an exact timezone-aware datetime instance"
_ERR_DATETIME_OPTIONAL = "{name} must be None or an exact timezone-aware datetime instance"
_ERR_OBSERVATION_ID_TYPE = "observation_id must be an exact UUID instance"
_ERR_SNAPSHOT_ID_TYPE = "snapshot_id must be None or an exact UUID instance"
_ERR_COMPLETE_VALUE = "COMPLETE macro state input requires a finite Decimal value"
_ERR_UNAVAILABLE_VALUE = "UNAVAILABLE macro state input must not carry a value"
_ERR_NON_COMPLETE_VALUE = "non-COMPLETE macro state input still requires an observed Decimal value"
_ERR_SYSTEM_INGESTED = "SYSTEM_AS_OF requires ingested_at <= as_of"
_ERR_SYSTEM_PUBLISHED = "SYSTEM_AS_OF requires published_at <= as_of"
_ERR_SOURCE_KNOWN = "SOURCE_AS_OF requires the source-known timestamp <= as_of"
_ERR_SUPERSEDED = "a macro observation superseded at or before as_of is not eligible"


def _is_aware_datetime(value: object) -> bool:
    return type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None


def _verified_definition(canonical_key: str) -> MacroSeriesDefinition:
    definition = MacroSeriesRegistry.get(canonical_key)
    if definition is None:
        raise ValueError(_ERR_UNREGISTERED)
    if definition.is_active is not True or definition.contract_status is not ContractStatus.VERIFIED:
        raise ValueError(_ERR_NOT_VERIFIED)
    return definition


@dataclass(frozen=True)
class MacroStateInputFact:
    """One selected, PIT-eligible, exact-Decimal macro observation bound to a verified registry series."""
    canonical_key: str
    effective_date: date
    value: Decimal | None
    data_status: DataStatus
    confidence_level: DataConfidenceLevel
    source_tier: SourceTier
    mode: AsOfMode
    as_of: datetime
    published_at: datetime | None
    observed_at: datetime
    ingested_at: datetime
    superseded_at: datetime | None
    observation_id: UUID
    snapshot_id: UUID | None

    def __post_init__(self) -> None:
        if type(self.canonical_key) is not str:
            raise TypeError(_ERR_KEY_TYPE)
        if len(self.canonical_key) == 0:
            raise ValueError(_ERR_KEY_EMPTY)
        _verified_definition(self.canonical_key)
        if type(self.effective_date) is not date:
            raise TypeError(_ERR_DATE_TYPE)
        if self.value is not None:
            if type(self.value) is not Decimal:
                raise TypeError(_ERR_VALUE_TYPE)
            if not self.value.is_finite():
                raise ValueError(_ERR_VALUE_FINITE)
        if type(self.data_status) is not DataStatus:
            raise TypeError(_ERR_STATUS_TYPE)
        if type(self.confidence_level) is not DataConfidenceLevel:
            raise TypeError(_ERR_CONFIDENCE_TYPE)
        if type(self.source_tier) is not SourceTier:
            raise TypeError(_ERR_TIER_TYPE)
        if type(self.mode) is not AsOfMode:
            raise TypeError(_ERR_MODE_TYPE)
        for name in ("as_of", "observed_at", "ingested_at"):
            if not _is_aware_datetime(getattr(self, name)):
                raise TypeError(_ERR_DATETIME_REQUIRED.format(name=name))
        for name in ("published_at", "superseded_at"):
            value = getattr(self, name)
            if value is not None and not _is_aware_datetime(value):
                raise TypeError(_ERR_DATETIME_OPTIONAL.format(name=name))
        if type(self.observation_id) is not UUID:
            raise TypeError(_ERR_OBSERVATION_ID_TYPE)
        if self.snapshot_id is not None and type(self.snapshot_id) is not UUID:
            raise TypeError(_ERR_SNAPSHOT_ID_TYPE)

        if self.data_status is DataStatus.UNAVAILABLE:
            if self.value is not None:
                raise ValueError(_ERR_UNAVAILABLE_VALUE)
        elif self.value is None:
            raise ValueError(
                _ERR_COMPLETE_VALUE if self.data_status is DataStatus.COMPLETE else _ERR_NON_COMPLETE_VALUE
            )

        if self.mode is AsOfMode.SYSTEM_AS_OF:
            if not self.ingested_at <= self.as_of:
                raise ValueError(_ERR_SYSTEM_INGESTED)
            if self.published_at is not None and not self.published_at <= self.as_of:
                raise ValueError(_ERR_SYSTEM_PUBLISHED)
        else:
            source_known_at = self.published_at if self.published_at is not None else self.observed_at
            if not source_known_at <= self.as_of:
                raise ValueError(_ERR_SOURCE_KNOWN)
        if self.superseded_at is not None and not self.superseded_at > self.as_of:
            raise ValueError(_ERR_SUPERSEDED)

    @property
    def _definition(self) -> MacroSeriesDefinition:
        return _verified_definition(self.canonical_key)

    @property
    def category(self) -> MacroCategory:
        return self._definition.category

    @property
    def unit(self) -> MacroUnit:
        return self._definition.unit

    @property
    def frequency(self) -> MacroFrequency:
        return self._definition.frequency

    @property
    def geography(self) -> str:
        return self._definition.geography

    @property
    def provider(self) -> str:
        return self._definition.provider

    @property
    def is_available(self) -> bool:
        return self.data_status is not DataStatus.UNAVAILABLE and self.value is not None


def build_macro_state_input_fact(
    *,
    canonical_key: str,
    effective_date: date,
    value: Decimal | None,
    data_status: DataStatus,
    confidence_level: DataConfidenceLevel,
    source_tier: SourceTier,
    mode: AsOfMode,
    as_of: datetime,
    published_at: datetime | None,
    observed_at: datetime,
    ingested_at: datetime,
    superseded_at: datetime | None,
    observation_id: UUID,
    snapshot_id: UUID | None,
) -> MacroStateInputFact:
    """Build a macro state input fact; validation is identical to direct construction."""
    return MacroStateInputFact(
        canonical_key=canonical_key,
        effective_date=effective_date,
        value=value,
        data_status=data_status,
        confidence_level=confidence_level,
        source_tier=source_tier,
        mode=mode,
        as_of=as_of,
        published_at=published_at,
        observed_at=observed_at,
        ingested_at=ingested_at,
        superseded_at=superseded_at,
        observation_id=observation_id,
        snapshot_id=snapshot_id,
    )
