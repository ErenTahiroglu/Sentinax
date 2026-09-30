"""
backend/engine/private/macro/us_macro_evidence.py
=================================================
PIT-safe monthly U.S. macro evidence transforms (Phase 17G). Pure calculation over Phase 17C facts and Phase 17D
curve points; evidence only (no score, regime, weight, MacroState or portfolio action).

Architectural Invariants:
    - ONE snapshot at ONE explicit `mode` / `as_of`. There is deliberately no function returning a series of past
      evidence states: earlier months inside the supplied histories are normalization evidence, not historical
      decision authority. A backtest must rebuild the snapshot at every historical cutoff.
    - Inputs are exact tuples of exact `MacroStateInputFact` (each bound to its canonical key) and exact
      `USTreasuryCurveSlopePoint`, strictly increasing by `effective_date` (validated, never sorted), carrying the
      caller's `mode` and `as_of`. Any `effective_date` after `as_of.date()` is rejected, never filtered.
    - A calendar month is `date(year, month, 1)`. Per series the last actual object of a month is its monthly sample
      (no average / interpolation / fill). An explicit UNAVAILABLE that is latest wins; there is no backward search.
    - Each component exposes its own real `reference_month`; nothing is forward-filled to a common month.
    - Growth is 3m/3m SAAR on US_INDUSTRIAL_PRODUCTION: 100 * ((recent 3m sum / previous 3m sum) ** 4 - 1), using six
      consecutive calendar-month slots. Real yield, broad dollar and STLFSI4 are raw levels; the curve primary is
      10Y-3M with 10Y-2Y as an un-normalized diagnostic (both taken from Phase 17D as supplied).
    - Robust normalization (growth, real yield, broad dollar, 10Y-3M only): the 120 immediately preceding calendar
      months, current month excluded, all required, no partial fallback. median, MAD, 1.4826 * MAD and
      z = (current - median) / scaled MAD; MAD == 0 gives z = None. No winsorization or clipping.
    - Analytics use a fresh explicit Decimal context per public build (prec 50, ROUND_HALF_EVEN, MIN_EMIN / MAX_EMAX), independent of
      and never touching the ambient context. No float, no output rounding. Overflow becomes a static ValueError.
"""

from __future__ import annotations

import decimal
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from backend.engine.private.domain import AsOfMode
from backend.engine.private.macro.state_inputs import MacroStateInputFact
from backend.engine.private.macro.us_treasury_curve import USTreasuryCurveSlopePoint

_KEY_IP = "US_INDUSTRIAL_PRODUCTION"
_KEY_REAL = "US_TREASURY_REAL_10Y_YIELD"
_KEY_USD = "US_BROAD_DOLLAR_INDEX"
_KEY_STRESS = "US_FINANCIAL_STRESS_INDEX"

_WINDOW_MONTHS = 120
_GROWTH_SLOTS = 6
_MAD_SCALE = Decimal("1.4826")

_ERR_MODE_TYPE = "mode must be an exact AsOfMode instance"
_ERR_AS_OF_TYPE = "as_of must be an exact timezone-aware datetime instance"
_ERR_MEMBER = "every history member must be an exact MacroStateInputFact"
_ERR_POINT = "every history member must be an exact USTreasuryCurveSlopePoint"
_ERR_IP_POSITIVE = "industrial production index must be positive"
_ERR_IP_MONTH = "industrial_production history must have at most one fact per calendar month"
_ERR_RANGE = "US macro evidence exceeds supported Decimal analytics range"


@dataclass(frozen=True)
class RobustNormalizationEvidence:
    """Median / MAD statistics of the 120 preceding calendar months and the uncapped robust z (None if MAD == 0)."""
    window_start_month: date
    window_end_month: date
    median: Decimal
    mad: Decimal
    scaled_mad: Decimal
    z: Decimal | None


@dataclass(frozen=True)
class GrowthMomentumEvidence:
    """3m/3m SAAR industrial-production growth (percentage points); six calendar-month slots, oldest first."""
    reference_month: date
    source_facts: tuple[MacroStateInputFact | None, ...]
    raw_3m3m_saar: Decimal | None
    normalization: RobustNormalizationEvidence | None


@dataclass(frozen=True)
class NormalizedMacroLevelEvidence:
    """Raw latest level of one series (real yield or broad dollar) with its robust normalization."""
    reference_month: date
    source_fact: MacroStateInputFact
    raw_value: Decimal | None
    normalization: RobustNormalizationEvidence | None


@dataclass(frozen=True)
class YieldCurveEvidence:
    """Primary 10Y-3M level (normalized) and 10Y-2Y diagnostic, exactly as supplied by Phase 17D."""
    reference_month: date
    source_point: USTreasuryCurveSlopePoint
    primary_10y_3m: Decimal | None
    diagnostic_10y_2y: Decimal | None
    normalization: RobustNormalizationEvidence | None


@dataclass(frozen=True)
class FinancialStressEvidence:
    """Raw STLFSI4 level; the series is already an official standardized index and is not normalized again."""
    reference_month: date
    source_fact: MacroStateInputFact
    raw_value: Decimal | None


@dataclass(frozen=True)
class USMacroEvidenceSnapshot:
    """Evidence valid only for its single explicit mode/as_of. A component is None only if it has no source object."""
    mode: AsOfMode
    as_of: datetime
    growth: GrowthMomentumEvidence | None
    real_yield: NormalizedMacroLevelEvidence | None
    yield_curve: YieldCurveEvidence | None
    broad_dollar: NormalizedMacroLevelEvidence | None
    financial_stress: FinancialStressEvidence | None


def _evidence_context() -> decimal.Context:
    """A fresh, isolated analytics context (one per public build; signal flags are sticky, so never shared)."""
    return decimal.Context(
        prec=50,
        rounding=decimal.ROUND_HALF_EVEN,
        Emin=decimal.MIN_EMIN,
        Emax=decimal.MAX_EMAX,
    )


def _month_of(day: date) -> date:
    return date(day.year, day.month, 1)


def _shift_month(month: date, offset: int) -> date:
    index = month.year * 12 + (month.month - 1) + offset
    return date(index // 12, index % 12 + 1, 1)


def _validate_facts(
    name: str, history: object, key: str, mode: AsOfMode, as_of: datetime,
) -> tuple[MacroStateInputFact, ...]:
    if type(history) is not tuple:
        raise TypeError(f"{name} history must be an exact tuple")
    previous: date | None = None
    for fact in history:
        if type(fact) is not MacroStateInputFact:
            raise TypeError(_ERR_MEMBER)
        if fact.canonical_key != key:
            raise ValueError(f"{name} history canonical_key must be {key}")
        if fact.mode is not mode:
            raise ValueError(f"{name} history fact mode does not match the requested mode")
        if fact.as_of != as_of:
            raise ValueError(f"{name} history fact as_of does not match the requested as_of")
        if previous is not None and fact.effective_date <= previous:
            raise ValueError(f"{name} history must have strictly increasing effective_date")
        if fact.effective_date > as_of.date():
            raise ValueError(f"{name} history effective_date is after as_of")
        previous = fact.effective_date
    return history


def _validate_points(
    history: object, mode: AsOfMode, as_of: datetime,
) -> tuple[USTreasuryCurveSlopePoint, ...]:
    if type(history) is not tuple:
        raise TypeError("treasury_curve history must be an exact tuple")
    previous: date | None = None
    for point in history:
        if type(point) is not USTreasuryCurveSlopePoint:
            raise TypeError(_ERR_POINT)
        if point.mode is not mode:
            raise ValueError("treasury_curve history point mode does not match the requested mode")
        if point.as_of != as_of:
            raise ValueError("treasury_curve history point as_of does not match the requested as_of")
        if previous is not None and point.effective_date <= previous:
            raise ValueError("treasury_curve history must have strictly increasing effective_date")
        if point.effective_date > as_of.date():
            raise ValueError("treasury_curve history effective_date is after as_of")
        previous = point.effective_date
    return history


def _last_per_month(items: tuple) -> dict[date, object]:
    """Last actual object of each calendar month (inputs are strictly increasing by effective_date)."""
    monthly: dict[date, object] = {}
    for item in items:
        monthly[_month_of(item.effective_date)] = item
    return monthly


def _median(sorted_values: list[Decimal], ctx: decimal.Context) -> Decimal:
    count = len(sorted_values)
    middle = count // 2
    if count % 2 == 1:
        return sorted_values[middle]
    return ctx.divide(ctx.add(sorted_values[middle - 1], sorted_values[middle]), Decimal(2))


def _normalize(
    current: Decimal | None, raw_by_month: dict[date, Decimal | None], reference_month: date, ctx: decimal.Context,
) -> RobustNormalizationEvidence | None:
    """Robust normalization over the 120 calendar months before `reference_month` (current excluded)."""
    if current is None:
        return None
    start = _shift_month(reference_month, -_WINDOW_MONTHS)
    window: list[Decimal] = []
    for offset in range(-_WINDOW_MONTHS, 0):
        value = raw_by_month.get(_shift_month(reference_month, offset))
        if value is None:
            return None
        window.append(value)
    median = _median(sorted(window), ctx)
    mad = _median(sorted(ctx.abs(ctx.subtract(value, median)) for value in window), ctx)
    scaled_mad = ctx.multiply(_MAD_SCALE, mad)
    z = None if mad == 0 else ctx.divide(ctx.subtract(current, median), scaled_mad)
    return RobustNormalizationEvidence(
        window_start_month=start,
        window_end_month=_shift_month(reference_month, -1),
        median=median,
        mad=mad,
        scaled_mad=scaled_mad,
        z=z,
    )


def _growth_raw(
    facts_by_month: dict[date, MacroStateInputFact], month: date, ctx: decimal.Context,
) -> Decimal | None:
    values: list[Decimal] = []
    for offset in range(-(_GROWTH_SLOTS - 1), 1):
        fact = facts_by_month.get(_shift_month(month, offset))
        if fact is None or fact.value is None:
            return None
        values.append(fact.value)
    prior_sum = ctx.add(ctx.add(values[0], values[1]), values[2])
    recent_sum = ctx.add(ctx.add(values[3], values[4]), values[5])
    ratio = ctx.divide(recent_sum, prior_sum)
    return ctx.multiply(Decimal(100), ctx.subtract(ctx.power(ratio, Decimal(4)), Decimal(1)))


def _growth_evidence(
    ip_history: tuple[MacroStateInputFact, ...], ctx: decimal.Context,
) -> GrowthMomentumEvidence | None:
    if not ip_history:
        return None
    by_month: dict[date, MacroStateInputFact] = {}
    for fact in ip_history:
        month = _month_of(fact.effective_date)
        if month in by_month:
            raise ValueError(_ERR_IP_MONTH)
        if fact.value is not None and not fact.value > 0:
            raise ValueError(_ERR_IP_POSITIVE)
        by_month[month] = fact
    reference_month = _month_of(ip_history[-1].effective_date)
    slots = tuple(by_month.get(_shift_month(reference_month, offset)) for offset in range(-(_GROWTH_SLOTS - 1), 1))
    raw = _growth_raw(by_month, reference_month, ctx)
    normalization = None
    if raw is not None:
        raw_by_month = {
            _shift_month(reference_month, offset): _growth_raw(by_month, _shift_month(reference_month, offset), ctx)
            for offset in range(-_WINDOW_MONTHS, 0)
        }
        normalization = _normalize(raw, raw_by_month, reference_month, ctx)
    return GrowthMomentumEvidence(
        reference_month=reference_month, source_facts=slots, raw_3m3m_saar=raw, normalization=normalization,
    )


def _level_evidence(
    history: tuple[MacroStateInputFact, ...], ctx: decimal.Context,
) -> NormalizedMacroLevelEvidence | None:
    if not history:
        return None
    monthly = _last_per_month(history)
    raw_by_month = {month: fact.value for month, fact in monthly.items()}
    source = history[-1]
    reference_month = _month_of(source.effective_date)
    return NormalizedMacroLevelEvidence(
        reference_month=reference_month,
        source_fact=source,
        raw_value=source.value,
        normalization=_normalize(source.value, raw_by_month, reference_month, ctx),
    )


def _curve_evidence(
    history: tuple[USTreasuryCurveSlopePoint, ...], ctx: decimal.Context,
) -> YieldCurveEvidence | None:
    if not history:
        return None
    monthly = _last_per_month(history)
    raw_by_month = {month: point.slope_10y_3m for month, point in monthly.items()}
    source = history[-1]
    reference_month = _month_of(source.effective_date)
    return YieldCurveEvidence(
        reference_month=reference_month,
        source_point=source,
        primary_10y_3m=source.slope_10y_3m,
        diagnostic_10y_2y=source.slope_10y_2y,
        normalization=_normalize(source.slope_10y_3m, raw_by_month, reference_month, ctx),
    )


def _stress_evidence(history: tuple[MacroStateInputFact, ...]) -> FinancialStressEvidence | None:
    if not history:
        return None
    source = history[-1]
    return FinancialStressEvidence(
        reference_month=_month_of(source.effective_date), source_fact=source, raw_value=source.value,
    )


def build_us_macro_evidence_snapshot(
    *,
    industrial_production_history: tuple[MacroStateInputFact, ...],
    real_10y_yield_history: tuple[MacroStateInputFact, ...],
    broad_dollar_history: tuple[MacroStateInputFact, ...],
    financial_stress_history: tuple[MacroStateInputFact, ...],
    treasury_curve_history: tuple[USTreasuryCurveSlopePoint, ...],
    mode: AsOfMode,
    as_of: datetime,
) -> USMacroEvidenceSnapshot:
    ctx = _evidence_context()
    if type(mode) is not AsOfMode:
        raise TypeError(_ERR_MODE_TYPE)
    if type(as_of) is not datetime or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise TypeError(_ERR_AS_OF_TYPE)

    ip = _validate_facts("industrial_production", industrial_production_history, _KEY_IP, mode, as_of)
    real = _validate_facts("real_10y_yield", real_10y_yield_history, _KEY_REAL, mode, as_of)
    usd = _validate_facts("broad_dollar", broad_dollar_history, _KEY_USD, mode, as_of)
    stress = _validate_facts("financial_stress", financial_stress_history, _KEY_STRESS, mode, as_of)
    curve = _validate_points(treasury_curve_history, mode, as_of)

    try:
        return USMacroEvidenceSnapshot(
            mode=mode,
            as_of=as_of,
            growth=_growth_evidence(ip, ctx),
            real_yield=_level_evidence(real, ctx),
            yield_curve=_curve_evidence(curve, ctx),
            broad_dollar=_level_evidence(usd, ctx),
            financial_stress=_stress_evidence(stress),
        )
    except decimal.Overflow:
        raise ValueError(_ERR_RANGE) from None
