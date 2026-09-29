"""
backend/engine/private/fund_expected_shortfall.py
=================================================
Horizon-specific historical empirical VaR and Expected Shortfall over TEFAS rolling returns for the Private
Investment Decision Engine (Phase 16J).

Architectural Invariants:
    - Pure domain module. Zero network, filesystem, database, ambient clock, randomness, hashing, float
      arithmetic, pandas/numpy/scipy, persistence, or provider/resolver calls. Phase 16D
      (`TefasFundRollingReturnSeries`) is the sole input authority and is retained by identity; the horizon is
      the source's horizon (12M / 36M / 60M) and is never annualized or converted.
    - `confidence_level` is mandatory (no default): an exact finite `Decimal` fraction with 0 < c < 1.
    - Loss is `L = -R` via the context-independent `copy_negate`; it is NEVER floored at zero, so a profitable
      window has a negative loss. A zero return yields a plain zero loss (no negative zero).
    - Empirical VaR is the nearest-rank quantile: losses ascending, `k = ceil(alpha * N)` by exact integer
      arithmetic from `Decimal.as_integer_ratio()` (no float, no ambient rounding, no interpolation),
      `VaR = L(k)`.
    - Expected Shortfall is the exact mean of ALL losses `>= VaR` (ties at the threshold included), so the tail
      may exceed `(1 - alpha) * N` observations. Sum and mean use an explicit fresh 50-digit ROUND_HALF_EVEN
      context with maximum exponent range; only `decimal.Overflow` is translated (static range error).
    - An empty rolling series is unavailable: VaR and ES are None (never zero). A non-empty series always has
      finite VaR and ES, both at most 1 (total-loss window), with no finite lower bound, and ES >= VaR.
    - The constructor recomputes VaR and ES through the same private helper as the builder, rejecting forgery.
    - Rolling windows overlap: this is a descriptive empirical tail diagnostic, not an independent-sample estimator,
      confidence interval, forecast, or maximum possible loss.
    - No parametric distribution, bootstrap/randomness, stress scenarios, volatility, annualization, portfolio
      aggregation, optimizer integration, minimum-sample policy, ranking, score, or recommendation.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from decimal import Decimal

from backend.engine.private.fund_rolling_returns import TefasFundRollingReturnSeries

_ERR_SOURCE_TYPE = "source must be an exact TefasFundRollingReturnSeries instance"
_ERR_CONFIDENCE_TYPE = "confidence_level must be an exact Decimal instance"
_ERR_CONFIDENCE_RANGE = "confidence_level must be a finite Decimal strictly between 0 and 1"
_ERR_VALUE_TYPE = "value_at_risk and expected_shortfall must be exact Decimal instances"
_ERR_EMPTY = "value_at_risk and expected_shortfall must both be None for an empty rolling series"
_ERR_VALUE_RANGE = "value_at_risk and expected_shortfall must be finite and at most 1"
_ERR_ORDER = "expected_shortfall must not be below value_at_risk"
_ERR_MATCH = "historical VaR and expected shortfall must match the canonical empirical calculation exactly"
_ERR_RANGE = "TEFAS historical expected shortfall exceeds supported Decimal analytics range"


def _es_context() -> decimal.Context:
    return decimal.Context(
        prec=50,
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=decimal.MIN_EMIN,
        Emax=decimal.MAX_EMAX,
    )


def _require_confidence(confidence_level: object) -> None:
    if type(confidence_level) is not Decimal:
        raise TypeError(_ERR_CONFIDENCE_TYPE)
    if not confidence_level.is_finite() or not (Decimal(0) < confidence_level < Decimal(1)):
        raise ValueError(_ERR_CONFIDENCE_RANGE)


def _loss(simple_return: Decimal) -> Decimal:
    loss = simple_return.copy_negate()
    return Decimal(0) if loss.is_zero() else loss  # exact; avoids a negative-zero loss


def _canonical(
    source: TefasFundRollingReturnSeries, confidence_level: Decimal
) -> tuple[Decimal, Decimal] | tuple[None, None]:
    """Single canonical VaR / ES calculation shared by the builder and the constructor verification."""
    if not source.points:
        return (None, None)
    losses = sorted(_loss(p.simple_return) for p in source.points)
    numerator, denominator = confidence_level.as_integer_ratio()
    rank = (numerator * len(losses) + denominator - 1) // denominator  # exact ceil(alpha * N)
    value_at_risk = losses[rank - 1]
    tail = [loss for loss in losses if loss >= value_at_risk]
    ctx = _es_context()
    try:
        total = Decimal(0)
        for loss in tail:
            total = ctx.add(total, loss)
        expected_shortfall = ctx.divide(total, Decimal(len(tail)))
    except decimal.Overflow:
        raise ValueError(_ERR_RANGE) from None
    return (value_at_risk, expected_shortfall)


@dataclass(frozen=True)
class TefasFundHistoricalExpectedShortfall:
    """Empirical historical VaR and ES of one fund's rolling-return sample at one explicit confidence level."""
    source: TefasFundRollingReturnSeries
    confidence_level: Decimal
    value_at_risk: Decimal | None
    expected_shortfall: Decimal | None

    def __post_init__(self) -> None:
        if type(self.source) is not TefasFundRollingReturnSeries:
            raise TypeError(_ERR_SOURCE_TYPE)
        _require_confidence(self.confidence_level)
        for value in (self.value_at_risk, self.expected_shortfall):
            if value is not None and type(value) is not Decimal:
                raise TypeError(_ERR_VALUE_TYPE)
        if not self.source.points:
            if self.value_at_risk is not None or self.expected_shortfall is not None:
                raise ValueError(_ERR_EMPTY)
            return
        if self.value_at_risk is None or self.expected_shortfall is None:
            raise TypeError(_ERR_VALUE_TYPE)
        for value in (self.value_at_risk, self.expected_shortfall):
            if not value.is_finite() or not value <= Decimal(1):
                raise ValueError(_ERR_VALUE_RANGE)
        if self.expected_shortfall < self.value_at_risk:
            raise ValueError(_ERR_ORDER)
        if (self.value_at_risk, self.expected_shortfall) != _canonical(self.source, self.confidence_level):
            raise ValueError(_ERR_MATCH)

    @property
    def is_available(self) -> bool:
        return self.expected_shortfall is not None

    @property
    def observation_count(self) -> int:
        return len(self.source.points)

    @property
    def tail_count(self) -> int:
        if self.value_at_risk is None:
            return 0
        return sum(1 for p in self.source.points if _loss(p.simple_return) >= self.value_at_risk)

    @property
    def horizon(self) -> object:  # exact Horizon; type not imported to keep private imports minimal
        return self.source.horizon


def calculate_tefas_fund_historical_expected_shortfall(
    *,
    rolling_series: TefasFundRollingReturnSeries,
    confidence_level: Decimal,
) -> TefasFundHistoricalExpectedShortfall:
    """Empirical nearest-rank VaR and tail-mean Expected Shortfall over a Phase 16D rolling-return series."""
    if type(rolling_series) is not TefasFundRollingReturnSeries:
        raise TypeError(_ERR_SOURCE_TYPE)
    _require_confidence(confidence_level)
    value_at_risk, expected_shortfall = _canonical(rolling_series, confidence_level)
    return TefasFundHistoricalExpectedShortfall(
        source=rolling_series,
        confidence_level=confidence_level,
        value_at_risk=value_at_risk,
        expected_shortfall=expected_shortfall,
    )
