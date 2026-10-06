"""
backend/engine/private/backtest_marked_holdings.py
==================================================
Historical marked holdings and raw cash projection (Phase 26D3A). From one COMPLETE Phase 26C2E bundle (exactly one PORTFOLIO_HISTORY slot plus MARKET_DATA slots) it derives, with
the CLOSED builders only, the reversal-aware position quantities and the raw account/currency cash balances of the SAME ledger view
(`input_bundle.portfolio_history.projection_binding.projection`), and marks every open instrument with a PIT-safe typed price obtained through the C2C2 boundary.

Valuation is explicit and conditional. `valuation_currency` is a fixed replay parameter (never inferred from the portfolio, an account, a transaction, a price or cash); it is not
claimed to have been historically persisted or used. This module is the first layer that chooses the price field: BIST_EOD `close`, GLOBAL_EOD `close` and TEFAS_FUND_PRICE `unit_price`.
Global `adj_close`, `previous_close`, `weighted_average`, TEFAS current metrics (their reported price stays diagnostic), precious-metal references (no portfolio instrument identity),
transaction prices and cost basis are never used. The valuation price must be an exact Decimal, finite and strictly positive (a valuation-policy rule stricter than the closed
resolver), its currency must be exactly the valuation currency (no FX, no conversion, no normalization) and its query date must be exactly the replay evaluation date (no previous-day,
nearest-day, last-known or CURRENT_REPORTED fallback).

Open quantities are summed across accounts per instrument with exact, context-independent integer arithmetic; zero (closed) positions need no price. Exactly one supported price snapshot
must exist for every open instrument and none for anything else (missing, duplicate across any surface, closed, unrelated, current-metrics and precious-metal evidence are all rejected).
Each market value is exactly quantity times the selected price: no rounding, quantization or decimal-place assumption, whatever the ambient Decimal precision. Marked positions are in
ascending `str(instrument_id)` order and keep the C2E market snapshot by object identity (a wrapper over an equal-valued clone is rejected). One private derivation serves the builder and
`__post_init__`, so a forged projection, quantity, value or position set is rejected.

The raw `CashBalanceProjection` is retained untouched and is NOT investable cash: nothing here classifies cash (no CashBucket purpose), converts a currency or sums balances, and no
`RebalanceCurrentState` is built. The ledger's cutoff stays the portfolio recorded cutoff; the market side stays governed by C2C1/C2C2; knowledge cutoff, recorded cutoff and evaluation
date are never collapsed. No target, composition, rebalance, Game Changer or candidate logic. Non-pure by dependency composition; no I/O, clock, randomness, hashing or persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from backend.engine.private.backtest_input_completeness import (
    PrivateBacktestInputBundle,
    PrivateBacktestInputCompletenessStatus,
    PrivateBacktestInputKind,
)
from backend.engine.private.backtest_market_data_resolution_snapshot import PrivateBacktestMarketDataKind
from backend.engine.private.backtest_market_data_selected_observation import (
    PrivateBacktestMarketDataSelectedObservation,
    reconstruct_private_backtest_selected_observation,
)
from backend.engine.private.domain import Currency
from backend.engine.private.portfolio.cash import CashBalanceProjection, build_cash_balance_projection
from backend.engine.private.portfolio.positions import PositionQuantityProjection, build_position_quantity_projection

_ERR_BUNDLE = "input_bundle must be an exact PrivateBacktestInputBundle instance"
_ERR_CURRENCY = "valuation_currency must be an exact Currency member"
_ERR_INCOMPLETE = "input_bundle must be COMPLETE with no missing requirement"
_ERR_SURFACE = "D3A manifest must declare only PORTFOLIO_HISTORY and MARKET_DATA requirements"
_ERR_KIND = "only BIST_EOD, GLOBAL_EOD and TEFAS_FUND_PRICE observations can mark a holding"
_ERR_DATE = "a valuation price must be for exactly the replay evaluation date"
_ERR_DUPLICATE = "more than one price snapshot for the same instrument"
_ERR_COVERAGE = "price snapshots must cover exactly the open holdings, one per instrument"
_ERR_PRICE = "valuation price must be an exact finite Decimal strictly greater than zero"
_ERR_PRICE_CURRENCY = "valuation price currency must be exactly the valuation currency"
_ERR_QUANTITY = "quantity must be an exact finite Decimal strictly greater than zero"
_ERR_AMOUNT = "market_value must be an exact finite Decimal"
_ERR_UUID = "instrument_id must be an exact UUID"
_ERR_OBSERVATION = "market_observation must be an exact PrivateBacktestMarketDataSelectedObservation instance"
_ERR_INSTRUMENT = "market observation must be the price of this very instrument"
_ERR_VALUE = "market_value must be exactly quantity times the selected valuation price"
_ERR_POSITION_TYPE = "position_projection must be an exact PositionQuantityProjection instance"
_ERR_CASH_TYPE = "cash_projection must be an exact CashBalanceProjection instance"
_ERR_MARKED_TYPE = "marked_positions must be an exact tuple of exact PrivateBacktestMarkedPosition instances"
_ERR_POSITION_MISMATCH = "position_projection must equal the closed projection of the bundle's ledger view"
_ERR_CASH_MISMATCH = "cash_projection must equal the closed projection of the bundle's ledger view"
_ERR_MARKED_MISMATCH = "marked_positions must be exactly the canonical marked open holdings, in order, over the bundle's own snapshots"

_ZERO = Decimal("0")
_MARKABLE_KINDS = frozenset({
    PrivateBacktestMarketDataKind.BIST_EOD, PrivateBacktestMarketDataKind.GLOBAL_EOD, PrivateBacktestMarketDataKind.TEFAS_FUND_PRICE,
})
_D3A_KINDS = frozenset({PrivateBacktestInputKind.PORTFOLIO_HISTORY, PrivateBacktestInputKind.MARKET_DATA})


# --- exact, ambient-context-independent Decimal arithmetic (integers only; no rounding or quantization) --------------------

def _as_integer(value: Decimal) -> tuple:
    sign, digits, exponent = value.as_tuple()
    magnitude = 0
    for digit in digits:
        magnitude = magnitude * 10 + digit
    return (-magnitude if sign else magnitude), exponent


def _from_integer(integer: int, exponent: int) -> Decimal:
    digits = tuple(int(character) for character in str(abs(integer)))
    return Decimal((1 if integer < 0 else 0, digits, exponent))


def _exact_sum(values: list) -> Decimal:
    if len(values) == 1:
        return values[0]
    parts = [_as_integer(value) for value in values]
    smallest = min(exponent for _integer, exponent in parts)
    return _from_integer(sum(integer * 10 ** (exponent - smallest) for integer, exponent in parts), smallest)


def _exact_product(left: Decimal, right: Decimal) -> Decimal:
    (left_integer, left_exponent), (right_integer, right_exponent) = _as_integer(left), _as_integer(right)
    return _from_integer(left_integer * right_integer, left_exponent + right_exponent)


def _is_positive_decimal(value: object) -> bool:
    return type(value) is Decimal and value.is_finite() and value > _ZERO


# --- the explicit valuation-field policy ---------------------------------------------------------------------------------------

def _valuation_price(kind: PrivateBacktestMarketDataKind, observation: object) -> Decimal:
    """The one place that chooses the valuation field: BIST close, Global close, TEFAS fund unit price."""
    if kind is PrivateBacktestMarketDataKind.BIST_EOD or kind is PrivateBacktestMarketDataKind.GLOBAL_EOD:
        price = observation.close
    elif kind is PrivateBacktestMarketDataKind.TEFAS_FUND_PRICE:
        price = observation.unit_price
    else:
        raise ValueError(_ERR_KIND)
    if not _is_positive_decimal(price):
        raise ValueError(_ERR_PRICE)
    return price


@dataclass(frozen=True)
class PrivateBacktestMarkedPosition:
    """One open instrument: its aggregated exact quantity, the typed C2C2 price observation and the exact market value; nothing is duplicated from the observation."""
    instrument_id: UUID
    quantity: Decimal
    market_observation: PrivateBacktestMarketDataSelectedObservation
    market_value: Decimal

    def __post_init__(self) -> None:
        if type(self.instrument_id) is not UUID:
            raise TypeError(_ERR_UUID)
        if type(self.quantity) is not Decimal:
            raise TypeError(_ERR_QUANTITY)
        if type(self.market_value) is not Decimal:
            raise TypeError(_ERR_AMOUNT)
        if type(self.market_observation) is not PrivateBacktestMarketDataSelectedObservation:
            raise TypeError(_ERR_OBSERVATION)
        if not _is_positive_decimal(self.quantity):
            raise ValueError(_ERR_QUANTITY)
        if not self.market_value.is_finite():
            raise ValueError(_ERR_AMOUNT)
        snapshot = self.market_observation.resolution_snapshot
        if snapshot.kind not in _MARKABLE_KINDS:
            raise ValueError(_ERR_KIND)
        if snapshot.query_key.instrument_id != self.instrument_id:
            raise ValueError(_ERR_INSTRUMENT)
        price = _valuation_price(snapshot.kind, self.market_observation.reconstruct())
        if self.market_value != _exact_product(self.quantity, price):
            raise ValueError(_ERR_VALUE)


def _derive(input_bundle: object, valuation_currency: object) -> tuple:
    """The single D3A derivation shared by the builder and `__post_init__`: (position projection, cash projection, ordered (instrument, quantity, snapshot, wrapper, value))."""
    if type(input_bundle) is not PrivateBacktestInputBundle:
        raise TypeError(_ERR_BUNDLE)
    if type(valuation_currency) is not Currency:
        raise TypeError(_ERR_CURRENCY)
    if input_bundle.status is not PrivateBacktestInputCompletenessStatus.COMPLETE or input_bundle.missing_requirements != ():
        raise ValueError(_ERR_INCOMPLETE)
    if not {requirement.kind for requirement in input_bundle.requirements} <= _D3A_KINDS:
        raise ValueError(_ERR_SURFACE)

    view = input_bundle.portfolio_history.projection_binding.projection
    position_projection = build_position_quantity_projection(view)
    cash_projection = build_cash_balance_projection(view)

    open_quantities: dict = {}
    for position in position_projection.open_positions:
        open_quantities.setdefault(position.instrument_id, []).append(position.quantity)

    evaluation_date = input_bundle.analysis_context.replay_point.evaluation_date
    snapshots: dict = {}
    for snapshot in input_bundle.market_data:
        if snapshot.kind not in _MARKABLE_KINDS:
            raise ValueError(_ERR_KIND)
        if snapshot.query_key.trade_date != evaluation_date:
            raise ValueError(_ERR_DATE)
        if snapshot.query_key.instrument_id in snapshots:
            raise ValueError(_ERR_DUPLICATE)
        snapshots[snapshot.query_key.instrument_id] = snapshot
    if set(snapshots) != set(open_quantities):
        raise ValueError(_ERR_COVERAGE)

    entries = []
    for instrument_id in sorted(open_quantities, key=str):
        snapshot = snapshots[instrument_id]
        wrapper = reconstruct_private_backtest_selected_observation(resolution_snapshot=snapshot)
        representative = wrapper.reconstruct()
        price = _valuation_price(snapshot.kind, representative)
        if representative.currency is not valuation_currency:
            raise ValueError(_ERR_PRICE_CURRENCY)
        quantity = _exact_sum(open_quantities[instrument_id])
        if not _is_positive_decimal(quantity):
            raise ValueError(_ERR_QUANTITY)
        entries.append((instrument_id, quantity, snapshot, wrapper, _exact_product(quantity, price)))
    return position_projection, cash_projection, tuple(entries)


@dataclass(frozen=True)
class PrivateBacktestMarkedHoldingsState:
    """The COMPLETE bundle, the explicit valuation currency, the closed position and RAW cash projections of its ledger view and the marked open holdings."""
    input_bundle: PrivateBacktestInputBundle
    valuation_currency: Currency
    position_projection: PositionQuantityProjection
    cash_projection: CashBalanceProjection
    marked_positions: tuple[PrivateBacktestMarkedPosition, ...]

    def __post_init__(self) -> None:
        expected_positions, expected_cash, entries = _derive(self.input_bundle, self.valuation_currency)
        if type(self.position_projection) is not PositionQuantityProjection:
            raise TypeError(_ERR_POSITION_TYPE)
        if type(self.cash_projection) is not CashBalanceProjection:
            raise TypeError(_ERR_CASH_TYPE)
        if type(self.marked_positions) is not tuple or not all(type(position) is PrivateBacktestMarkedPosition for position in self.marked_positions):
            raise TypeError(_ERR_MARKED_TYPE)
        if self.position_projection != expected_positions:
            raise ValueError(_ERR_POSITION_MISMATCH)
        if self.cash_projection != expected_cash:
            raise ValueError(_ERR_CASH_MISMATCH)
        if len(self.marked_positions) != len(entries):
            raise ValueError(_ERR_MARKED_MISMATCH)
        for position, (instrument_id, quantity, snapshot, _wrapper, value) in zip(self.marked_positions, entries):
            if (position.instrument_id != instrument_id or position.quantity != quantity or position.market_value != value
                    or position.market_observation.resolution_snapshot is not snapshot):
                raise ValueError(_ERR_MARKED_MISMATCH)


def build_private_backtest_marked_holdings_state(
    *,
    input_bundle: PrivateBacktestInputBundle,
    valuation_currency: Currency,
) -> PrivateBacktestMarkedHoldingsState:
    """Mark the historical open holdings of one COMPLETE bundle under an explicit valuation currency; raw cash is retained untouched."""
    position_projection, cash_projection, entries = _derive(input_bundle, valuation_currency)
    marked = tuple(
        PrivateBacktestMarkedPosition(instrument_id=instrument_id, quantity=quantity, market_observation=wrapper, market_value=value)
        for instrument_id, quantity, _snapshot, wrapper, value in entries
    )
    return PrivateBacktestMarkedHoldingsState(
        input_bundle=input_bundle, valuation_currency=valuation_currency, position_projection=position_projection,
        cash_projection=cash_projection, marked_positions=marked)
