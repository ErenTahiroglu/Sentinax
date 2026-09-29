"""
backend/engine/private/fund_price_series.py
===========================================
PIT-safe TEFAS fund price series foundation for the Private Investment Decision Engine (Phase 16A).

Purpose:
    The single authoritative series contract that later fund analytics must consume, instead of raw TEFAS
    responses, latest rows, unsorted observations, future snapshots, or silently backfilled observations.

Architectural Invariants:
    - Pure domain module. Zero network, filesystem, database, ambient clock, UUID generation, randomness, hashing,
      float conversion, pandas/numpy/polars, persistence, or provider calls.
    - Temporal authority is delegated entirely to `PointInTimeMarketDataResolver.resolve_tefas_fund_price`: this
      module never filters snapshots, picks a "latest" snapshot, deduplicates, or re-implements no-resurrection,
      future isolation, conflict handling, or resolution-key logic. SYSTEM_AS_OF future isolation and
      no-resurrection are inherited from the resolver.
    - Explicit gaps: every requested trade date becomes exactly one `TefasFundPricePoint` (resolver status SELECTED)
      or exactly one `TefasFundPriceGap` (any other status). A gap never means zero, previous/next price, weekend,
      or holiday. No interpolation, forward-fill, backward-fill, or calendar inference: the caller supplies the
      strictly ascending `trade_dates`, and nothing is sorted or normalized.
    - `SOURCE_AS_OF` remains unavailable (the resolver returns UNAVAILABLE_SOURCE_AS_OF) and becomes gaps; it is
      never downgraded to SYSTEM_AS_OF or CURRENT_REPORTED. SYSTEM_AS_OF requires an exact timezone-aware
      `as_of`; CURRENT_REPORTED requires `as_of is None`.
    - Exact Decimal unit prices (finite, > 0). All points in one series share one Currency (no FX conversion);
      otherwise the series fails closed.
    - The series retains no snapshot collection and no mutable source observation. `provider_symbol` is
      diagnostic only, never economic identity.
    - No returns, volatility, drawdown, VaR/CVaR, Sharpe/Sortino, rankings, scores, current-metrics fields, or
      recommendations.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from backend.engine.private.domain import Currency, DataConfidenceLevel
from backend.engine.private.market_data.models import (
    MarketDataResolutionMode,
    MarketDataResolutionStatus,
    TefasFundPriceQueryKey,
)
from backend.engine.private.market_data.resolver import PointInTimeMarketDataResolver
from backend.engine.private.market_data.tefas_models import (
    TefasFundPriceObservation,
    TefasFundPriceSnapshot,
)

_ERR_DATES = "trade_dates must be a non-empty tuple of strictly ascending exact dates"


def _is_aware_datetime(value: object) -> bool:
    if type(value) is not datetime or value.tzinfo is None:
        return False
    try:
        return type(value.utcoffset()) is timedelta
    except Exception:
        return False


def _validate_temporal_context(mode: MarketDataResolutionMode, as_of: datetime | None) -> None:
    """
    Single source of the canonical temporal contract, shared by the series constructor and the builder:
    SYSTEM_AS_OF requires an exact timezone-aware `as_of`; CURRENT_REPORTED requires `as_of is None`;
    SOURCE_AS_OF allows None or an exact timezone-aware datetime (the resolver reports it unavailable).
    """
    if mode is MarketDataResolutionMode.SYSTEM_AS_OF:
        if not _is_aware_datetime(as_of):
            raise TypeError("as_of must be an exact timezone-aware datetime for SYSTEM_AS_OF")
    elif mode is MarketDataResolutionMode.CURRENT_REPORTED:
        if as_of is not None:
            raise ValueError("as_of must be None for CURRENT_REPORTED")
    elif as_of is not None and not _is_aware_datetime(as_of):
        raise TypeError("as_of must be None or an exact timezone-aware datetime")


def _require_ascending_dates(dates: object, message: str) -> None:
    if type(dates) is not tuple or len(dates) == 0:
        raise TypeError(message)
    previous = None
    for item in dates:
        if type(item) is not date:
            raise TypeError(message)
        if previous is not None and not previous < item:
            raise ValueError(message)
        previous = item


@dataclass(frozen=True)
class TefasFundPricePoint:
    """One authoritative SELECTED TEFAS unit price for a requested trade date."""
    trade_date: date
    unit_price: Decimal
    currency: Currency
    confidence: DataConfidenceLevel
    snapshot_retrieved_at: datetime
    resolution_key: str

    def __post_init__(self) -> None:
        if type(self.trade_date) is not date:
            raise TypeError("trade_date must be an exact date instance")
        if type(self.unit_price) is not Decimal:
            raise TypeError("unit_price must be an exact Decimal instance")
        if not self.unit_price.is_finite() or self.unit_price <= Decimal("0"):
            raise ValueError("unit_price must be a finite positive Decimal")
        if type(self.currency) is not Currency:
            raise TypeError("currency must be an exact Currency instance")
        if type(self.confidence) is not DataConfidenceLevel:
            raise TypeError("confidence must be an exact DataConfidenceLevel instance")
        if not _is_aware_datetime(self.snapshot_retrieved_at):
            raise TypeError("snapshot_retrieved_at must be an exact timezone-aware datetime")
        if type(self.resolution_key) is not str or len(self.resolution_key) == 0:
            raise TypeError("resolution_key must be a non-empty str")


@dataclass(frozen=True)
class TefasFundPriceGap:
    """A requested trade date without an authoritative SELECTED observation (never a zero or a fill)."""
    trade_date: date
    status: MarketDataResolutionStatus

    def __post_init__(self) -> None:
        if type(self.trade_date) is not date:
            raise TypeError("trade_date must be an exact date instance")
        if type(self.status) is not MarketDataResolutionStatus:
            raise TypeError("status must be an exact MarketDataResolutionStatus instance")
        if self.status is MarketDataResolutionStatus.SELECTED:
            raise ValueError("gap status must not be SELECTED")


@dataclass(frozen=True)
class TefasFundPriceSeries:
    """Explicit, gap-preserving PIT TEFAS fund price series for one canonical instrument."""
    instrument_id: UUID
    mode: MarketDataResolutionMode
    as_of: datetime | None
    requested_dates: tuple[date, ...]
    points: tuple[TefasFundPricePoint, ...]
    gaps: tuple[TefasFundPriceGap, ...]

    def __post_init__(self) -> None:
        if type(self.instrument_id) is not UUID:
            raise TypeError("instrument_id must be an exact UUID instance")
        if type(self.mode) is not MarketDataResolutionMode:
            raise TypeError("mode must be an exact MarketDataResolutionMode instance")
        _validate_temporal_context(self.mode, self.as_of)
        _require_ascending_dates(
            self.requested_dates,
            "requested_dates must be a non-empty tuple of strictly ascending exact dates",
        )
        if type(self.points) is not tuple or any(type(p) is not TefasFundPricePoint for p in self.points):
            raise TypeError("points must be a tuple of exact TefasFundPricePoint instances")
        if type(self.gaps) is not tuple or any(type(g) is not TefasFundPriceGap for g in self.gaps):
            raise TypeError("gaps must be a tuple of exact TefasFundPriceGap instances")

        point_dates = [p.trade_date for p in self.points]
        gap_dates = [g.trade_date for g in self.gaps]
        requested = list(self.requested_dates)
        if (
            len(point_dates) + len(gap_dates) != len(requested)
            or sorted(point_dates + gap_dates) != requested
            or point_dates != sorted(set(point_dates))
            or gap_dates != sorted(set(gap_dates))
        ):
            raise ValueError("points and gaps must partition requested_dates exactly")

        if len({p.currency for p in self.points}) > 1:
            raise ValueError("TEFAS fund price series contains inconsistent currencies")

    @property
    def is_complete(self) -> bool:
        return len(self.gaps) == 0


def _point_from_selected(result, instrument_id: UUID, trade_date: date) -> TefasFundPricePoint:
    observation = result.selected_observation
    if (
        type(observation) is not TefasFundPriceObservation
        or observation.instrument_id != instrument_id
        or observation.trade_date != trade_date
    ):
        raise ValueError("TEFAS resolver returned an observation that does not match the requested key")
    return TefasFundPricePoint(
        trade_date=trade_date,
        unit_price=observation.unit_price,
        currency=observation.currency,
        confidence=observation.confidence_level,
        snapshot_retrieved_at=result.snapshot_retrieved_at,
        resolution_key=result.resolution_key,
    )


def build_tefas_fund_price_series(
    *,
    instrument_id: UUID,
    trade_dates: tuple[date, ...],
    snapshots: Sequence[TefasFundPriceSnapshot],
    mode: MarketDataResolutionMode,
    as_of: datetime | None,
    provider_symbol: str | None = None,
) -> TefasFundPriceSeries:
    """
    Build a PIT-safe series by resolving each requested date through the authoritative TEFAS resolver.
    """
    if type(instrument_id) is not UUID:
        raise TypeError("instrument_id must be an exact UUID instance")
    _require_ascending_dates(trade_dates, _ERR_DATES)
    if type(mode) is not MarketDataResolutionMode:
        raise TypeError("mode must be an exact MarketDataResolutionMode instance")
    if (
        not isinstance(snapshots, Sequence)
        or isinstance(snapshots, (str, bytes, bytearray))
        or any(type(s) is not TefasFundPriceSnapshot for s in snapshots)
    ):
        raise TypeError("snapshots must be a sequence of exact TefasFundPriceSnapshot instances")
    if provider_symbol is not None and type(provider_symbol) is not str:
        raise TypeError("provider_symbol must be None or a str")

    _validate_temporal_context(mode, as_of)

    points: list[TefasFundPricePoint] = []
    gaps: list[TefasFundPriceGap] = []
    for trade_date in trade_dates:
        query_key = TefasFundPriceQueryKey(
            instrument_id=instrument_id,
            trade_date=trade_date,
            provider_symbol=provider_symbol,
        )
        result = PointInTimeMarketDataResolver.resolve_tefas_fund_price(
            query_key,
            snapshots,
            mode=mode,
            as_of=as_of,
        )
        if result.status is MarketDataResolutionStatus.SELECTED:
            points.append(_point_from_selected(result, instrument_id, trade_date))
        else:
            gaps.append(TefasFundPriceGap(trade_date=trade_date, status=result.status))

    return TefasFundPriceSeries(
        instrument_id=instrument_id,
        mode=mode,
        as_of=as_of,
        requested_dates=trade_dates,
        points=tuple(points),
        gaps=tuple(gaps),
    )
