"""
backend/engine/private/identity.py
====================================
Instrument Identity, Symbology & Corporate Action Resolution Service.

Core Principles:
    - Ticker is NOT the primary key.
    - Every instrument is tracked by a stable `internal_instrument_id`.
    - Symbology is point-in-time aware: ticker renames (e.g. FB -> META)
      resolve to the same internal instrument without breaking historical time series.
    - Corporate actions (splits, dividends, symbol changes) maintain time-series integrity
      (conceptually referencing the LEAN MapFile/FactorFile pattern without engine dependencies).
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
    Master financial instrument definition.
    """
    internal_instrument_id: str
    asset_class: AssetClass
    instrument_type: InstrumentType
    currency: Currency = Currency.TRY
    mic: str = "XIST"
    isin: Optional[str] = None
    cik: Optional[str] = None
    status: InstrumentStatus = InstrumentStatus.ACTIVE
    name: Optional[str] = None
    valid_from: date = field(default_factory=date.today)
    valid_to: Optional[date] = None
    id: UUID = field(default_factory=uuid4)

    def is_valid_on(self, as_of_date: date) -> bool:
        if as_of_date < self.valid_from:
            return False
        if self.valid_to and as_of_date > self.valid_to:
            return False
        return True

    def to_record_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "internal_instrument_id": self.internal_instrument_id,
            "asset_class": self.asset_class.value,
            "instrument_type": self.instrument_type.value,
            "currency": self.currency.value,
            "mic": self.mic,
            "isin": self.isin,
            "cik": self.cik,
            "status": self.status.value,
            "name": self.name,
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
        }


@dataclass
class ProviderAliasRecord:
    """
    Mapping between an external provider's ticker symbol and the internal instrument ID over time.
    """
    instrument_id: UUID
    provider: str
    provider_symbol: str
    valid_from: date
    valid_to: Optional[date] = None
    is_primary: bool = True
    id: UUID = field(default_factory=uuid4)

    def is_valid_on(self, as_of_date: date) -> bool:
        if as_of_date < self.valid_from:
            return False
        if self.valid_to and as_of_date > self.valid_to:
            return False
        return True

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
    Corporate action or reference event (split, dividend, rename, merger).
    """
    instrument_id: UUID
    action_type: CorporateActionType
    effective_date: date
    announced_date: Optional[date] = None
    record_date: Optional[date] = None
    ex_date: Optional[date] = None
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    factor: float = 1.0
    amount: Optional[float] = None
    currency: Optional[Currency] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)

    def to_record_dict(self) -> Dict[str, Any]:
        return {
            "id": str(self.id),
            "instrument_id": str(self.instrument_id),
            "action_type": self.action_type.value,
            "effective_date": self.effective_date.isoformat(),
            "announced_date": self.announced_date.isoformat() if self.announced_date else None,
            "record_date": self.record_date.isoformat() if self.record_date else None,
            "ex_date": self.ex_date.isoformat() if self.ex_date else None,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "factor": self.factor,
            "amount": self.amount,
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
        self._instruments_by_internal_id: Dict[str, InstrumentRecord] = {}
        self._aliases: List[ProviderAliasRecord] = []
        self._corporate_actions: List[CorporateActionRecord] = []

    def register_instrument(self, instrument: InstrumentRecord) -> None:
        """Register or update a master instrument."""
        self._instruments_by_id[instrument.id] = instrument
        self._instruments_by_internal_id[instrument.internal_instrument_id] = instrument

    def register_alias(self, alias: ProviderAliasRecord) -> None:
        """Register a provider symbol mapping."""
        self._aliases.append(alias)

    def register_corporate_action(self, action: CorporateActionRecord) -> None:
        """Register a corporate action or reference event."""
        self._corporate_actions.append(action)

    def get_instrument_by_id(self, instrument_id: UUID) -> Optional[InstrumentRecord]:
        return self._instruments_by_id.get(instrument_id)

    def get_instrument_by_internal_id(self, internal_id: str) -> Optional[InstrumentRecord]:
        return self._instruments_by_internal_id.get(internal_id)

    def resolve_provider_symbol_to_internal_id(
        self,
        provider: str,
        provider_symbol: str,
        as_of_date: Optional[date] = None,
    ) -> Optional[str]:
        """
        Resolves an external provider symbol to the canonical internal_instrument_id.
        Point-in-time aware: evaluates which alias was active on `as_of_date`.
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

        # Sort by primary flag and latest valid_from
        matching_aliases.sort(key=lambda a: (a.is_primary, a.valid_from), reverse=True)
        selected_alias = matching_aliases[0]
        instrument = self._instruments_by_id.get(selected_alias.instrument_id)
        return instrument.internal_instrument_id if instrument else None

    def resolve_internal_id_to_provider_symbol(
        self,
        internal_instrument_id: str,
        provider: str,
        as_of_date: Optional[date] = None,
    ) -> Optional[str]:
        """
        Resolves canonical internal_instrument_id to the specific provider symbol
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
        internal_instrument_id: str,
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
        internal_instrument_id: str,
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
        internal_instrument_id: str,
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
            if s.factor > 0:
                factor *= s.factor
        return factor
