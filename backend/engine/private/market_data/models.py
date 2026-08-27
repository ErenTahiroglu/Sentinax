"""
backend/engine/private/market_data/models.py
============================================
Data models, enums, query keys, and resolution result structures
for Point-in-Time Market Data Observation Resolution.

Invariants:
    - No float conversions: all monetary values and fineness are exact Decimals or strings.
    - Strict Point-in-Time separation: effective_date vs retrieved_at vs as_of.
    - Zero datetime.now() in resolution decision authority.
    - Full immutability and deterministic serialization.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Union
from uuid import UUID

from backend.engine.private.bist.models import BISTEODObservation
from backend.engine.private.domain import (
    Currency,
    DataConfidenceLevel,
)
from backend.engine.private.market_data.global_models import GlobalEODObservation
from backend.engine.private.market_data.tefas_models import TefasFundPriceObservation
from backend.engine.private.precious_metals.constants import (
    PreciousMetalPriceType,
    PreciousMetalType,
    PreciousMetalUnit,
)
from backend.engine.private.precious_metals.models import (
    PreciousMetalMarketObservation,
)


class MarketDataResolutionMode(Enum):
    """Point-in-time perspective for market data resolution."""
    CURRENT_REPORTED = "CURRENT_REPORTED"
    SYSTEM_AS_OF = "SYSTEM_AS_OF"
    SOURCE_AS_OF = "SOURCE_AS_OF"


class MarketDataResolutionStatus(Enum):
    """Deterministic outcome of a market observation resolution query."""
    SELECTED = "SELECTED"
    NO_SNAPSHOT = "NO_SNAPSHOT"
    NO_SNAPSHOT_AS_OF = "NO_SNAPSHOT_AS_OF"
    INVALID_SNAPSHOT = "INVALID_SNAPSHOT"
    INVALID_TEMPORAL_LINEAGE = "INVALID_TEMPORAL_LINEAGE"
    NO_ELIGIBLE_OBSERVATION = "NO_ELIGIBLE_OBSERVATION"
    OBSERVATION_CONFLICT = "OBSERVATION_CONFLICT"
    UNRESOLVED_IDENTITY = "UNRESOLVED_IDENTITY"
    SEMANTIC_KEY_AMBIGUOUS = "SEMANTIC_KEY_AMBIGUOUS"
    SNAPSHOT_CONFLICT = "SNAPSHOT_CONFLICT"
    UNAVAILABLE_SOURCE_AS_OF = "UNAVAILABLE_SOURCE_AS_OF"


@dataclass(frozen=True)
class BISTInstrumentQueryKey:
    """
    Query key for resolving a BIST Equity or Commodity Certificate (ALTIN.S1) EOD observation.
    Authority is canonical instrument_id; symbol is diagnostic/informational only.
    """
    instrument_id: UUID
    trade_date: date
    symbol: Optional[str] = None

    def to_string(self) -> str:
        sym = f"({self.symbol})" if self.symbol else ""
        return f"BIST_EOD:{self.instrument_id}{sym}@{self.trade_date.isoformat()}"


@dataclass(frozen=True)
class GlobalEODQueryKey:
    """
    Query key for resolving a Point-in-Time Global (US/European) EOD stock or ETF observation.
    Authority is (instrument_id, provider, trade_date); provider_symbol is diagnostic only.
    """
    instrument_id: UUID
    trade_date: date
    provider: str
    provider_symbol: Optional[str] = None

    def to_string(self) -> str:
        sym = f"({self.provider_symbol})" if self.provider_symbol else ""
        return f"GLOBAL_EOD:{self.provider}:{self.instrument_id}{sym}@{self.trade_date.isoformat()}"


@dataclass(frozen=True)
class TefasFundPriceQueryKey:
    """
    Query key for resolving a Point-in-Time TEFAS Turkish Investment Fund daily price observation.
    Authority is (instrument_id, trade_date); provider is fixed to 'TEFAS'; provider_symbol is diagnostic only.
    """
    instrument_id: UUID
    trade_date: date
    provider_symbol: Optional[str] = None

    def to_string(self) -> str:
        sym = f"({self.provider_symbol})" if self.provider_symbol else ""
        return f"TEFAS_FUND_PRICE:{self.instrument_id}{sym}@{self.trade_date.isoformat()}"


@dataclass(frozen=True)
class PreciousMetalSemanticKey:
    """
    Fully dimensioned semantic query key for resolving a Precious Metal market observation.
    Never a generic 'GOLD'; requires exact currency, unit, price type, purity, and settlement terms.
    """
    metal: PreciousMetalType
    effective_date: date
    price_currency: Currency
    quantity_unit: PreciousMetalUnit
    price_type: PreciousMetalPriceType
    price_quantity: Decimal = Decimal("1")
    fineness_per_mille: Optional[Decimal] = None
    settlement_term: Optional[str] = None
    value_date: Optional[date] = None
    raw_value_date_text: Optional[str] = None
    provider: str = "BIST_KMTP"
    originating_source: str = "BIST"

    def matches(self, obs: PreciousMetalMarketObservation) -> bool:
        """Evaluates whether a candidate observation matches all semantic dimensions."""
        if obs.metal != self.metal:
            return False
        if obs.effective_date != self.effective_date:
            return False
        if obs.price_currency != self.price_currency:
            return False
        if obs.quantity_unit != self.quantity_unit:
            return False
        if obs.price_type != self.price_type:
            return False
        if obs.price_quantity != self.price_quantity:
            return False
        if obs.fineness_per_mille != self.fineness_per_mille:
            return False
        if obs.settlement_term != self.settlement_term:
            return False
        if obs.value_date != self.value_date:
            return False
        if obs.raw_value_date_text != self.raw_value_date_text:
            return False
        if obs.provider != self.provider:
            return False
        if obs.originating_source != self.originating_source:
            return False
        return True

    def to_string(self) -> str:
        fin = f"fin={self.fineness_per_mille}‰" if self.fineness_per_mille is not None else "fin=None"
        settle = f"settle={self.settlement_term or self.raw_value_date_text or 'None'}"
        return (
            f"PM:{self.metal.value}:{self.price_type.value}:{self.price_currency.value}/"
            f"{self.quantity_unit.value}:{fin}:{settle}@{self.effective_date.isoformat()}"
        )


@dataclass
class MarketObservationResolutionResult:
    """
    Deterministic result of a Point-in-Time market observation resolution.
    References the authoritative selected observation without modifying or synthesizing values.
    """
    status: MarketDataResolutionStatus
    resolution_mode: MarketDataResolutionMode
    as_of: Optional[datetime]

    observation_type: str
    effective_date: Optional[date]

    selected_observation: Optional[
        Union[
            BISTEODObservation,
            PreciousMetalMarketObservation,
            GlobalEODObservation,
            TefasFundPriceObservation,
        ]
    ] = None
    selected_observation_id: Optional[UUID] = None

    snapshot_id: Optional[UUID] = None
    snapshot_hash: Optional[str] = None
    snapshot_retrieved_at: Optional[datetime] = None

    provider: str = ""
    originating_source: str = ""

    canonical_instrument_id: Optional[UUID] = None
    semantic_key: Optional[str] = None
    confidence: DataConfidenceLevel = DataConfidenceLevel.NONE
    is_stale_discovery: bool = False
    diagnostics: List[str] = field(default_factory=list)

    evaluation_snapshot_ids: List[str] = field(default_factory=list)
    resolution_key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serializes resolution result to audit-grade dictionary without float types."""
        obs_dict: Optional[Dict[str, Any]] = None
        if self.selected_observation is not None:
            obs_dict = self.selected_observation.to_dict()

        return {
            "status": self.status.value,
            "resolution_mode": self.resolution_mode.value,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "observation_type": self.observation_type,
            "effective_date": self.effective_date.isoformat() if self.effective_date else None,
            "selected_observation_id": str(self.selected_observation_id) if self.selected_observation_id else None,
            "selected_observation": obs_dict,
            "snapshot_id": str(self.snapshot_id) if self.snapshot_id else None,
            "snapshot_hash": self.snapshot_hash,
            "snapshot_retrieved_at": self.snapshot_retrieved_at.isoformat() if self.snapshot_retrieved_at else None,
            "provider": self.provider,
            "originating_source": self.originating_source,
            "canonical_instrument_id": str(self.canonical_instrument_id) if self.canonical_instrument_id else None,
            "semantic_key": self.semantic_key,
            "confidence": self.confidence.value,
            "is_stale_discovery": self.is_stale_discovery,
            "diagnostics": self.diagnostics,
            "evaluation_snapshot_ids": self.evaluation_snapshot_ids,
            "resolution_key": self.resolution_key,
        }
