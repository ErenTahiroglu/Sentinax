"""
backend/engine/private/identity.py
====================================
Instrument Identity, Symbology & Corporate Action Resolution Service.

Core Principles:
    - Ticker and company names are NOT primary keys.
    - Every master instrument has a pure, immutable UUID `internal_instrument_id`.
    - Symbology is point-in-time aware with half-open [valid_from, valid_to) interval semantics.
    - Overlapping validity intervals for the same (provider, provider_symbol) are strictly rejected.
    - Action-specific corporate action semantics (splits, dividends, symbol changes) isolate
      mathematical fields to prevent erroneous adjustments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from backend.engine.private.domain import (
    AssetClass,
    CorporateActionType,
    Currency,
    InstrumentStatus,
    InstrumentType,
)


@dataclass
class InstrumentRecord:
    """
    Master financial instrument definition with pure UUID-based identity.
    """
    canonical_name: str
    asset_class: AssetClass
    instrument_type: InstrumentType
    internal_instrument_id: UUID = field(default_factory=uuid4)
    currency: Currency = Currency.TRY
    mic: str = "XIST"
    isin: Optional[str] = None
    cik: Optional[str] = None
    status: InstrumentStatus = InstrumentStatus.ACTIVE
    valid_from: date = field(default_factory=date.today)
    valid_to: Optional[date] = None
    id: UUID = field(default_factory=uuid4)

    def is_valid_on(self, as_of_date: date) -> bool:
        if as_of_date < self.valid_from:
            return False
        if self.valid_to and as_of_date >= self.valid_to: # [valid_from, valid_to)
            return False
        return True

    def to_record_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "internal_instrument_id": str(self.internal_instrument_id),
            "canonical_name": self.canonical_name,
            "asset_class": self.asset_class.value,
            "instrument_type": self.instrument_type.value,
            "currency": self.currency.value,
            "mic": self.mic,
            "isin": self.isin,
            "cik": self.cik,
            "status": self.status.value,
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
        }


@dataclass
class ProviderAliasRecord:
    """
    Mapping between an external provider's ticker symbol and the internal instrument ID.
    Enforces half-open [valid_from, valid_to) boundary semantics.
    """
    instrument_id: UUID
    provider: str
    provider_symbol: str
    valid_from: date
    valid_to: Optional[date] = None
    is_primary: bool = True
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if self.valid_to is not None and self.valid_from >= self.valid_to:
            raise ValueError(
                f"Invalid date range [{self.valid_from}, {self.valid_to}): "
                f"valid_from must be strictly before valid_to."
            )

    def is_valid_on(self, as_of_date: date) -> bool:
        """Evaluates half-open range [valid_from, valid_to)."""
        if as_of_date < self.valid_from:
            return False
        if self.valid_to and as_of_date >= self.valid_to:
            return False
        return True

    def overlaps_with(self, other: ProviderAliasRecord) -> bool:
        """Checks if two aliases for the same (provider, symbol) overlap in [from, to)."""
        if self.provider.lower() != other.provider.lower() or \
           self.provider_symbol.upper() != other.provider_symbol.upper():
            return False

        end_self = self.valid_to or date.max
        end_other = other.valid_to or date.max

        # Overlap in [start, end) exists if max(start_a, start_b) < min(end_a, end_b)
        return max(self.valid_from, other.valid_from) < min(end_self, end_other)

    def to_record_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "instrument_id": str(self.instrument_id),
            "provider": self.provider,
            "provider_symbol": self.provider_symbol,
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "is_primary": self.is_primary,
        }


@dataclass
class CorporateActionRecord:
    """
    Action-specific corporate action and reference event model.
    Isolates fields to prevent mathematical misapplication (e.g. split factor on dividend).
    """
    instrument_id: UUID
    action_type: CorporateActionType
    effective_date: date
    announced_date: Optional[date] = None
    record_date: Optional[date] = None
    ex_date: Optional[date] = None
    old_symbol: Optional[str] = None
    new_symbol: Optional[str] = None
    split_factor: Optional[float] = None
    cash_amount: Optional[float] = None
    currency: Optional[Currency] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if self.action_type == CorporateActionType.SPLIT:
            if self.split_factor is None or self.split_factor <= 0:
                raise ValueError("SPLIT corporate action requires split_factor > 0.")
            if self.cash_amount is not None:
                raise ValueError("SPLIT corporate action must not have cash_amount.")

        elif self.action_type == CorporateActionType.DIVIDEND:
            if self.cash_amount is None or self.cash_amount < 0:
                raise ValueError("DIVIDEND corporate action requires cash_amount >= 0.")
            if self.split_factor is not None:
                raise ValueError("DIVIDEND corporate action must not have split_factor.")

        elif self.action_type in (CorporateActionType.SYMBOL_CHANGE, CorporateActionType.FUND_CODE_CHANGE):
            if not self.old_symbol or not self.new_symbol:
                raise ValueError(f"{self.action_type.name} requires both old_symbol and new_symbol.")
            if self.split_factor is not None or self.cash_amount is not None:
                raise ValueError(f"{self.action_type.name} must not contain split_factor or cash_amount.")

    def to_record_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "instrument_id": str(self.instrument_id),
            "action_type": self.action_type.value,
            "effective_date": self.effective_date.isoformat(),
            "announced_date": self.announced_date.isoformat() if self.announced_date else None,
            "record_date": self.record_date.isoformat() if self.record_date else None,
            "ex_date": self.ex_date.isoformat() if self.ex_date else None,
            "old_symbol": self.old_symbol,
            "new_symbol": self.new_symbol,
            "split_factor": self.split_factor,
            "cash_amount": self.cash_amount,
            "currency": self.currency.value if self.currency else None,
            "metadata": self.metadata,
        }


class InstrumentResolverService:
    """
    In-memory / repository-backed symbology & identity resolver.
    Guarantees that historical series are never severed by ticker renames.
    """

    def __init__(self) -> None:
        self._instruments_by_id: Dict[UUID, InstrumentRecord] = {}
        self._instruments_by_internal_id: Dict[UUID, InstrumentRecord] = {}
        self._aliases: List[ProviderAliasRecord] = []
        self._corporate_actions: List[CorporateActionRecord] = []

    def register_instrument(self, instrument: InstrumentRecord) -> None:
        """Register or update a master instrument."""
        self._instruments_by_id[instrument.id] = instrument
        self._instruments_by_internal_id[instrument.internal_instrument_id] = instrument

    def register_alias(self, alias: ProviderAliasRecord) -> None:
        """
        Registers a provider symbol mapping with strict overlap validation.
        Raises ValueError if an existing alias for the same (provider, symbol) overlaps.
        """
        for existing in self._aliases:
            if existing.overlaps_with(alias):
                raise ValueError(
                    f"Overlapping alias detected for {alias.provider}:{alias.provider_symbol}! "
                    f"Existing [{existing.valid_from}, {existing.valid_to}) overlaps with "
                    f"New [{alias.valid_from}, {alias.valid_to})."
                )
        self._aliases.append(alias)

    def register_corporate_action(self, action: CorporateActionRecord) -> None:
        """Register a corporate action or reference event."""
        self._corporate_actions.append(action)

    def get_instrument_by_id(self, instrument_id: UUID) -> Optional[InstrumentRecord]:
        return self._instruments_by_id.get(instrument_id)

    def get_instrument_by_internal_id(self, internal_id: UUID) -> Optional[InstrumentRecord]:
        return self._instruments_by_internal_id.get(internal_id)

    def resolve_provider_symbol_to_internal_id(
        self,
        provider: str,
        provider_symbol: str,
        as_of_date: Optional[date] = None,
    ) -> Optional[UUID]:
        """
        Resolves an external provider symbol to the canonical internal_instrument_id (UUID).
        Point-in-time aware with half-open [valid_from, valid_to) range.
        """
        query_date = as_of_date or date.today()
        matching_aliases = [
            a for a in self._aliases
            if a.provider.lower() == provider.lower()
            and a.provider_symbol.upper() == provider_symbol.upper()
            and a.is_valid_on(query_date)
        ]

        if not matching_aliases:
            return None

        matching_aliases.sort(key=lambda a: (a.is_primary, a.valid_from), reverse=True)
        selected_alias = matching_aliases[0]
        instrument = self._instruments_by_id.get(selected_alias.instrument_id)
        return instrument.internal_instrument_id if instrument else None

    def resolve_internal_id_to_provider_symbol(
        self,
        internal_instrument_id: UUID,
        provider: str,
        as_of_date: Optional[date] = None,
    ) -> Optional[str]:
        """
        Resolves canonical internal_instrument_id (UUID) to the specific provider symbol
        that was in effect on `as_of_date`.
        """
        instrument = self._instruments_by_internal_id.get(internal_instrument_id)
        if not instrument:
            return None

        query_date = as_of_date or date.today()
        matching_aliases = [
            a for a in self._aliases
            if a.instrument_id == instrument.id
            and a.provider.lower() == provider.lower()
            and a.is_valid_on(query_date)
        ]

        if not matching_aliases:
            return None

        matching_aliases.sort(key=lambda a: (a.is_primary, a.valid_from), reverse=True)
        return matching_aliases[0].provider_symbol

    def get_historical_aliases_for_instrument(
        self,
        internal_instrument_id: UUID,
        provider: Optional[str] = None,
    ) -> List[ProviderAliasRecord]:
        """Returns all provider aliases over time for an instrument."""
        instrument = self._instruments_by_internal_id.get(internal_instrument_id)
        if not instrument:
            return []

        aliases = [a for a in self._aliases if a.instrument_id == instrument.id]
        if provider:
            aliases = [a for a in aliases if a.provider.lower() == provider.lower()]
        return sorted(aliases, key=lambda a: a.valid_from)

    def get_corporate_actions_between(
        self,
        internal_instrument_id: UUID,
        start_date: date,
        end_date: date,
        action_type: Optional[CorporateActionType] = None,
    ) -> List[CorporateActionRecord]:
        """Returns all corporate actions for an instrument within a date window."""
        instrument = self._instruments_by_internal_id.get(internal_instrument_id)
        if not instrument:
            return []

        actions = [
            ca for ca in self._corporate_actions
            if ca.instrument_id == instrument.id
            and start_date <= ca.effective_date <= end_date
        ]
        if action_type:
            actions = [ca for ca in actions if ca.action_type == action_type]
        return sorted(actions, key=lambda a: a.effective_date)

    def get_cumulative_split_factor(
        self,
        internal_instrument_id: UUID,
        from_date: date,
        to_date: date,
    ) -> float:
        """
        Calculates cumulative split adjustment factor between two dates.
        (Conceptually similar to LEAN FactorFile split multiplier).
        """
        splits = self.get_corporate_actions_between(
            internal_instrument_id,
            from_date,
            to_date,
            action_type=CorporateActionType.SPLIT,
        )
        factor = 1.0
        for s in splits:
            if s.split_factor and s.split_factor > 0:
                factor *= s.split_factor
        return factor
