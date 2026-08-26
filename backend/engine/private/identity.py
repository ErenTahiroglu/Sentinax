"""
backend/engine/private/identity.py
====================================
Instrument Identity, Symbology & Corporate Action Resolution Service.

Core Principles:
    - `id` (UUID) is the SINGLE canonical instrument identity across Sentinax.
    - No duplicate or redundant internal ID fields.
    - Missing market identity or currency is NEVER silently defaulted to XIST/TRY.
    - Provider aliases enforce deterministic case-insensitive normalization (e.g. Yahoo/yahoo, META/meta).
    - Symbology is point-in-time aware with half-open [valid_from, valid_to) interval semantics.
    - Overlapping validity intervals for the same (normalized_provider, normalized_symbol) are strictly rejected.
    - Corporate actions enforce strict field exclusivity to eliminate mathematical misapplication.
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
    Master financial instrument definition with a single canonical UUID identity (`id`).
    Currency is explicitly required. MIC is explicit (None for non-exchange assets).
    """
    canonical_name: str
    asset_class: AssetClass
    instrument_type: InstrumentType
    currency: Currency
    mic: Optional[str] = None
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
    Mapping between an external provider's ticker symbol and the canonical instrument ID (`id`).
    Enforces half-open [valid_from, valid_to) boundary semantics and normalized lookup keys.
    """
    instrument_id: UUID
    provider: str
    provider_symbol: str
    valid_from: date
    valid_to: Optional[date] = None
    is_primary: bool = True
    id: UUID = field(default_factory=uuid4)
    normalized_provider: str = field(init=False)
    normalized_symbol: str = field(init=False)

    def __post_init__(self) -> None:
        self.provider = self.provider.strip()
        self.provider_symbol = self.provider_symbol.strip()
        self.normalized_provider = self.provider.lower()
        self.normalized_symbol = self.provider_symbol.upper()

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
        if self.normalized_provider != other.normalized_provider or \
           self.normalized_symbol != other.normalized_symbol:
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
            "normalized_provider": self.normalized_provider,
            "normalized_symbol": self.normalized_symbol,
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "is_primary": self.is_primary,
        }


@dataclass
class CorporateActionRecord:
    """
    Action-specific corporate action and reference event model.
    Enforces strict field exclusivity to prevent mathematical misapplication.
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
            if self.old_symbol is not None or self.new_symbol is not None:
                raise ValueError("SPLIT corporate action must not contain old_symbol or new_symbol.")

        elif self.action_type == CorporateActionType.DIVIDEND:
            if self.cash_amount is None or self.cash_amount < 0:
                raise ValueError("DIVIDEND corporate action requires cash_amount >= 0.")
            if self.currency is None:
                raise ValueError("DIVIDEND corporate action requires currency.")
            if self.split_factor is not None:
                raise ValueError("DIVIDEND corporate action must not have split_factor.")
            if self.old_symbol is not None or self.new_symbol is not None:
                raise ValueError("DIVIDEND corporate action must not contain old_symbol or new_symbol.")

        elif self.action_type in (CorporateActionType.SYMBOL_CHANGE, CorporateActionType.FUND_CODE_CHANGE):
            if not self.old_symbol or not self.new_symbol:
                raise ValueError(f"{self.action_type.name} requires both old_symbol and new_symbol.")
            if self.split_factor is not None or self.cash_amount is not None:
                raise ValueError(f"{self.action_type.name} must not contain split_factor or cash_amount.")

        elif self.action_type == CorporateActionType.MERGER:
            if self.split_factor is not None or self.cash_amount is not None:
                raise ValueError("MERGER corporate action must not contain split_factor or cash_amount.")

        elif self.action_type == CorporateActionType.DELISTING:
            if self.split_factor is not None or self.cash_amount is not None:
                raise ValueError("DELISTING corporate action must not contain split_factor or cash_amount.")
            if self.old_symbol is not None or self.new_symbol is not None:
                raise ValueError("DELISTING corporate action must not contain old_symbol or new_symbol.")

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
        self._aliases: List[ProviderAliasRecord] = []
        self._corporate_actions: List[CorporateActionRecord] = []

    def register_instrument(self, instrument: InstrumentRecord) -> None:
        """Register or update a master instrument."""
        self._instruments_by_id[instrument.id] = instrument

    def register_alias(self, alias: ProviderAliasRecord) -> None:
        """
        Registers a provider symbol mapping with strict overlap validation.
        Raises ValueError if an existing alias for the same (normalized_provider, normalized_symbol) overlaps.
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

    def resolve_provider_symbol_to_instrument_id(
        self,
        provider: str,
        provider_symbol: str,
        as_of_date: Optional[date] = None,
    ) -> Optional[UUID]:
        """
        Resolves an external provider symbol to the single canonical instrument UUID (`id`).
        Point-in-time aware with half-open [valid_from, valid_to) range and case normalization.
        """
        norm_prov = provider.strip().lower()
        norm_sym = provider_symbol.strip().upper()
        query_date = as_of_date or date.today()

        matching_aliases = [
            a for a in self._aliases
            if a.normalized_provider == norm_prov
            and a.normalized_symbol == norm_sym
            and a.is_valid_on(query_date)
        ]

        if not matching_aliases:
            return None

        matching_aliases.sort(key=lambda a: (a.is_primary, a.valid_from), reverse=True)
        selected_alias = matching_aliases[0]
        return selected_alias.instrument_id

    def resolve_instrument_id_to_provider_symbol(
        self,
        instrument_id: UUID,
        provider: str,
        as_of_date: Optional[date] = None,
    ) -> Optional[str]:
        """
        Resolves canonical instrument UUID (`id`) to the specific provider symbol
        that was in effect on `as_of_date`.
        """
        norm_prov = provider.strip().lower()
        query_date = as_of_date or date.today()

        matching_aliases = [
            a for a in self._aliases
            if a.instrument_id == instrument_id
            and a.normalized_provider == norm_prov
            and a.is_valid_on(query_date)
        ]

        if not matching_aliases:
            return None

        matching_aliases.sort(key=lambda a: (a.is_primary, a.valid_from), reverse=True)
        return matching_aliases[0].provider_symbol

    def get_historical_aliases_for_instrument(
        self,
        instrument_id: UUID,
        provider: Optional[str] = None,
    ) -> List[ProviderAliasRecord]:
        """Returns all provider aliases over time for an instrument."""
        aliases = [a for a in self._aliases if a.instrument_id == instrument_id]
        if provider:
            norm_prov = provider.strip().lower()
            aliases = [a for a in aliases if a.normalized_provider == norm_prov]
        return sorted(aliases, key=lambda a: a.valid_from)

    def get_corporate_actions_between(
        self,
        instrument_id: UUID,
        start_date: date,
        end_date: date,
        action_type: Optional[CorporateActionType] = None,
    ) -> List[CorporateActionRecord]:
        """Returns all corporate actions for an instrument within a date window."""
        actions = [
            ca for ca in self._corporate_actions
            if ca.instrument_id == instrument_id
            and start_date <= ca.effective_date <= end_date
        ]
        if action_type:
            actions = [ca for ca in actions if ca.action_type == action_type]
        return sorted(actions, key=lambda a: a.effective_date)

    def get_cumulative_split_factor(
        self,
        instrument_id: UUID,
        from_date: date,
        to_date: date,
    ) -> float:
        """
        Calculates cumulative split adjustment factor between two dates.
        """
        splits = self.get_corporate_actions_between(
            instrument_id,
            from_date,
            to_date,
            action_type=CorporateActionType.SPLIT,
        )
        factor = 1.0
        for s in splits:
            if s.split_factor and s.split_factor > 0:
                factor *= s.split_factor
        return factor

    def resolve_instruments_by_cik(self, cik: str) -> List[InstrumentRecord]:
        """
        Resolves all canonical investable security instruments associated with an SEC issuer CIK.
        Returns 0..N InstrumentRecord instances (e.g. multiple share classes or ADRs).
        """
        if not cik or not isinstance(cik, str):
            return []
        norm_cik = cik.strip().zfill(10)
        return [
            inst for inst in self._instruments.values()
            if inst.cik and inst.cik.strip().zfill(10) == norm_cik
        ]


def resolve_instruments_for_sec_cik(
    cik: str,
    instruments: List[InstrumentRecord],
) -> List[InstrumentRecord]:
    """
    Read-side resolution helper mapping an SEC issuer CIK to 0..N investable InstrumentRecord instances.
    """
    if not cik or not isinstance(cik, str):
        return []
    norm_cik = cik.strip().zfill(10)
    return [
        inst for inst in instruments
        if inst.cik and inst.cik.strip().zfill(10) == norm_cik
    ]

