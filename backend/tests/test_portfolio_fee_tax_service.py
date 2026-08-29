"""
backend/tests/test_portfolio_fee_tax_service.py
===============================================
Comprehensive test suite for Owner-Bound Persisted Observed Fee/Tax Evidence Query Service (Phase 14C).

Tests:
1. Constructor & Dependency Validation (Repository, Clock)
2. Parameter Validation (Portfolio ID, as_of_recorded_at)
3. Current Clock Resolution (UTC normalization, single call, fail-closed on invalid)
4. As-Of Exact Representation Binding (+03:00 preservation, zero clock calls)
5. Repository Response Integrity (Missing portfolio, Wrong type, ID mismatch, Invalid tx collection)
6. Domain Scenarios (Empty history, Multi-account, Multi-currency, PIT future events, PIT reversals)
7. Final Red-Team Integration (Section 66)
8. Fail-Closed Error Propagation (Repository exceptions, Lower-layer projection errors)
9. Static Purity & Architectural Invariants (No owner args, single lookup, no calculation duplication)
"""

from __future__ import annotations

import ast
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
import inspect
from typing import Any, Callable, Dict, List, Optional, Sequence
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import Currency, PortfolioMode, TransactionType
from backend.engine.private.portfolio.fee_tax import (
    FeeTaxProjectionError,
    ObservedFeeTaxAggregateState,
    ObservedFeeTaxAggregation,
)
from backend.engine.private.portfolio.fee_tax_service import (
    PortfolioFeeTaxQueryError,
    PortfolioFeeTaxQueryService,
    PortfolioFeeTaxRepositoryPort,
)
import backend.engine.private.portfolio.fee_tax_service as fee_tax_service_module
from backend.engine.private.portfolio.models import Portfolio, PortfolioTransaction
from backend.engine.private.portfolio.projection import PortfolioProjectionError


class NullOffsetTZ(tzinfo):
    """Custom tzinfo implementation returning None for utcoffset (non-None tzinfo but not aware)."""
    def utcoffset(self, dt: Optional[datetime]) -> Optional[timedelta]:
        return None

    def dst(self, dt: Optional[datetime]) -> Optional[timedelta]:
        return None

    def tzname(self, dt: Optional[datetime]) -> str:
        return "NULL"


# ─────────────────────────────────────────────────────────────────────────────
# Test Repository Double
# ─────────────────────────────────────────────────────────────────────────────

class StrictTestPortfolioRepository:
    """
    Strict repository test double exposing ONLY get_portfolio and list_transactions.
    Records all calls and parameters for invariant verification.
    """

    def __init__(
        self,
        portfolios: Optional[Dict[UUID, Portfolio]] = None,
        transactions: Optional[Dict[UUID, List[PortfolioTransaction]]] = None,
        get_portfolio_error: Optional[Exception] = None,
        list_transactions_error: Optional[Exception] = None,
    ) -> None:
        self._portfolios: Dict[UUID, Portfolio] = portfolios or {}
        self._transactions: Dict[UUID, List[PortfolioTransaction]] = transactions or {}
        self._get_portfolio_error: Optional[Exception] = get_portfolio_error
        self._list_transactions_error: Optional[Exception] = list_transactions_error

        self.get_portfolio_calls: List[Any] = []
        self.list_transactions_calls: List[Any] = []

    def get_portfolio(self, portfolio_id: UUID) -> Optional[Portfolio]:
        self.get_portfolio_calls.append(portfolio_id)
        if self._get_portfolio_error is not None:
            raise self._get_portfolio_error
        return self._portfolios.get(portfolio_id)

    def list_transactions(self, portfolio_id: UUID) -> List[PortfolioTransaction]:
        self.list_transactions_calls.append(portfolio_id)
        if self._list_transactions_error is not None:
            raise self._list_transactions_error
        return list(self._transactions.get(portfolio_id, []))


# ─────────────────────────────────────────────────────────────────────────────
# Helper Factories
# ─────────────────────────────────────────────────────────────────────────────

def _make_portfolio(
    portfolio_id: Optional[UUID] = None,
    mode: PortfolioMode = PortfolioMode.MY_PORTFOLIO,
    base_currency: Currency = Currency.USD,
) -> Portfolio:
    return Portfolio(
        id=portfolio_id or uuid4(),
        owner_id=str(uuid4()),
        name="Test Portfolio",
        base_currency=base_currency,
        mode=mode,
        created_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
    )


def _make_tx(
    portfolio_id: UUID,
    account_id: UUID,
    transaction_type: TransactionType,
    recorded_at: Optional[datetime] = None,
    effective_date: Optional[date] = None,
    cash_amount: Optional[Decimal] = None,
    cash_currency: Optional[Currency] = None,
    instrument_id: Optional[UUID] = None,
    quantity: Optional[Decimal] = None,
    unit_price: Optional[Decimal] = None,
    trade_currency: Optional[Currency] = None,
    reverses_transaction_id: Optional[UUID] = None,
) -> PortfolioTransaction:
    rec_at = recorded_at or datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    eff_date = effective_date or date(2026, 6, 1)

    if transaction_type == TransactionType.FEE:
        c_amount = cash_amount if cash_amount is not None else Decimal("10.00")
        c_curr = cash_currency or Currency.USD
        return PortfolioTransaction(
            id=uuid4(),
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=transaction_type,
            recorded_at=rec_at,
            effective_date=eff_date,
            cash_amount=c_amount,
            cash_currency=c_curr,
            instrument_id=instrument_id,
        )

    if transaction_type == TransactionType.TAX_WITHHOLDING:
        c_amount = cash_amount if cash_amount is not None else Decimal("15.00")
        c_curr = cash_currency or Currency.TRY
        return PortfolioTransaction(
            id=uuid4(),
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=transaction_type,
            recorded_at=rec_at,
            effective_date=eff_date,
            cash_amount=c_amount,
            cash_currency=c_curr,
            instrument_id=instrument_id,
        )

    if transaction_type == TransactionType.CASH_DEPOSIT:
        c_amount = cash_amount if cash_amount is not None else Decimal("1000.00")
        c_curr = cash_currency or Currency.USD
        return PortfolioTransaction(
            id=uuid4(),
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=transaction_type,
            recorded_at=rec_at,
            effective_date=eff_date,
            cash_amount=c_amount,
            cash_currency=c_curr,
        )

    if transaction_type == TransactionType.BUY:
        qty = quantity if quantity is not None else Decimal("10")
        price = unit_price if unit_price is not None else Decimal("100.00")
        t_curr = trade_currency or Currency.USD
        inst_id = instrument_id or uuid4()
        return PortfolioTransaction(
            id=uuid4(),
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=transaction_type,
            recorded_at=rec_at,
            effective_date=eff_date,
            instrument_id=inst_id,
            quantity=qty,
            unit_price=price,
            trade_currency=t_curr,
        )

    if transaction_type == TransactionType.DIVIDEND:
        c_amount = cash_amount if cash_amount is not None else Decimal("50.00")
        c_curr = cash_currency or Currency.USD
        inst_id = instrument_id or uuid4()
        return PortfolioTransaction(
            id=uuid4(),
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=transaction_type,
            recorded_at=rec_at,
            effective_date=eff_date,
            cash_amount=c_amount,
            cash_currency=c_curr,
            instrument_id=inst_id,
        )

    if transaction_type == TransactionType.REVERSAL:
        return PortfolioTransaction(
            id=uuid4(),
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=transaction_type,
            recorded_at=rec_at,
            effective_date=eff_date,
            reverses_transaction_id=reverses_transaction_id or uuid4(),
        )

    raise ValueError(f"Unsupported transaction type in helper: {transaction_type}")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Constructor & Dependency Validation Tests (Section 33)
# ─────────────────────────────────────────────────────────────────────────────

def test_constructor_valid_repository_and_default_clock() -> None:
    repo = StrictTestPortfolioRepository()
    svc = PortfolioFeeTaxQueryService(repo)
    assert svc._repository is repo
    assert callable(svc._clock)


def test_constructor_rejects_none_repository() -> None:
    with pytest.raises(PortfolioFeeTaxQueryError, match="repository must not be None"):
        PortfolioFeeTaxQueryService(None)  # type: ignore[arg-type]


def test_constructor_rejects_missing_get_portfolio() -> None:
    class MissingGetPortfolio:
        def list_transactions(self, portfolio_id: UUID) -> List[PortfolioTransaction]:
            return []

    with pytest.raises(PortfolioFeeTaxQueryError, match="repository must provide a callable get_portfolio"):
        PortfolioFeeTaxQueryService(MissingGetPortfolio())  # type: ignore[arg-type]


def test_constructor_rejects_non_callable_get_portfolio() -> None:
    class NonCallableGetPortfolio:
        get_portfolio = "not a method"

        def list_transactions(self, portfolio_id: UUID) -> List[PortfolioTransaction]:
            return []

    with pytest.raises(PortfolioFeeTaxQueryError, match="repository must provide a callable get_portfolio"):
        PortfolioFeeTaxQueryService(NonCallableGetPortfolio())  # type: ignore[arg-type]


def test_constructor_rejects_missing_list_transactions() -> None:
    class MissingListTransactions:
        def get_portfolio(self, portfolio_id: UUID) -> Optional[Portfolio]:
            return None

    with pytest.raises(PortfolioFeeTaxQueryError, match="repository must provide a callable list_transactions"):
        PortfolioFeeTaxQueryService(MissingListTransactions())  # type: ignore[arg-type]


def test_constructor_rejects_non_callable_list_transactions() -> None:
    class NonCallableListTransactions:
        def get_portfolio(self, portfolio_id: UUID) -> Optional[Portfolio]:
            return None
        list_transactions = 123

    with pytest.raises(PortfolioFeeTaxQueryError, match="repository must provide a callable list_transactions"):
        PortfolioFeeTaxQueryService(NonCallableListTransactions())  # type: ignore[arg-type]


def test_constructor_valid_custom_clock() -> None:
    repo = StrictTestPortfolioRepository()
    fixed_time = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    svc = PortfolioFeeTaxQueryService(repo, clock=lambda: fixed_time)
    assert svc._clock() == fixed_time


@pytest.mark.parametrize("invalid_clock", [
    123,
    "clock_str",
    True,
    False,
    object(),
])
def test_constructor_rejects_non_callable_clock(invalid_clock: Any) -> None:
    repo = StrictTestPortfolioRepository()
    with pytest.raises(PortfolioFeeTaxQueryError, match="clock must be a callable returning datetime"):
        PortfolioFeeTaxQueryService(repo, clock=invalid_clock)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Portfolio ID Validation Tests (Section 34)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("invalid_id", [
    None,
    True,
    False,
    "c8a1e8e2-6bf2-411a-8c76-2f08960824b2",
    12345,
    3.14,
    [],
    {},
])
def test_portfolio_id_validation_rejects_invalid_types(invalid_id: Any) -> None:
    repo = StrictTestPortfolioRepository()
    svc = PortfolioFeeTaxQueryService(repo)

    with pytest.raises(PortfolioFeeTaxQueryError, match="portfolio_id must be a UUID instance"):
        svc.get_current_aggregation(invalid_id)

    with pytest.raises(PortfolioFeeTaxQueryError, match="portfolio_id must be a UUID instance"):
        svc.get_aggregation_as_of(invalid_id, datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc))

    # Assert no repository methods were called
    assert len(repo.get_portfolio_calls) == 0
    assert len(repo.list_transactions_calls) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. Current Clock Resolution Tests (Section 35-37)
# ─────────────────────────────────────────────────────────────────────────────

def test_current_clock_invoked_exactly_once_and_normalized_to_utc() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"), cash_currency=Currency.USD)

    repo = StrictTestPortfolioRepository(
        portfolios={portfolio.id: portfolio},
        transactions={portfolio.id: [fee_tx]},
    )

    clock_calls = 0
    # Returns UTC+3 instant: 2026-06-01 15:00:00+03:00 -> normalized to UTC: 2026-06-01 12:00:00+00:00
    def clock_fn() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return datetime(2026, 6, 1, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))

    svc = PortfolioFeeTaxQueryService(repo, clock=clock_fn)
    agg = svc.get_current_aggregation(portfolio.id)

    assert clock_calls == 1
    assert agg.as_of_recorded_at == datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert agg.as_of_recorded_at.tzinfo == timezone.utc


@pytest.mark.parametrize("invalid_clock_return", [
    None,
    "2026-06-01T12:00:00Z",
    True,
    False,
    123456,
])
def test_current_clock_rejects_non_datetime_return(invalid_clock_return: Any) -> None:
    portfolio = _make_portfolio()
    repo = StrictTestPortfolioRepository(portfolios={portfolio.id: portfolio})
    svc = PortfolioFeeTaxQueryService(repo, clock=lambda: invalid_clock_return)

    with pytest.raises(PortfolioFeeTaxQueryError, match="clock must return a datetime instance"):
        svc.get_current_aggregation(portfolio.id)

    assert len(repo.get_portfolio_calls) == 0


def test_current_clock_rejects_naive_datetime() -> None:
    portfolio = _make_portfolio()
    repo = StrictTestPortfolioRepository(portfolios={portfolio.id: portfolio})
    svc = PortfolioFeeTaxQueryService(repo, clock=lambda: datetime(2026, 6, 1, 12, 0, 0))

    with pytest.raises(PortfolioFeeTaxQueryError, match="clock return value must be timezone-aware"):
        svc.get_current_aggregation(portfolio.id)

    assert len(repo.get_portfolio_calls) == 0


def test_current_clock_rejects_null_offset_tz() -> None:
    portfolio = _make_portfolio()
    repo = StrictTestPortfolioRepository(portfolios={portfolio.id: portfolio})
    svc = PortfolioFeeTaxQueryService(repo, clock=lambda: datetime(2026, 6, 1, 12, 0, 0, tzinfo=NullOffsetTZ()))

    with pytest.raises(PortfolioFeeTaxQueryError, match="clock return value must be timezone-aware"):
        svc.get_current_aggregation(portfolio.id)

    assert len(repo.get_portfolio_calls) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 4. As-Of Representation & Clock Invariants (Section 38-40)
# ─────────────────────────────────────────────────────────────────────────────

def test_as_of_does_not_call_clock() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE)

    repo = StrictTestPortfolioRepository(
        portfolios={portfolio.id: portfolio},
        transactions={portfolio.id: [fee_tx]},
    )

    def exploding_clock() -> datetime:
        raise AssertionError("Clock must not be called during get_aggregation_as_of")

    svc = PortfolioFeeTaxQueryService(repo, clock=exploding_clock)
    cutoff = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    agg = svc.get_aggregation_as_of(portfolio.id, cutoff)

    assert agg.as_of_recorded_at == cutoff


def test_as_of_exact_representation_preservation() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE)

    repo = StrictTestPortfolioRepository(
        portfolios={portfolio.id: portfolio},
        transactions={portfolio.id: [fee_tx]},
    )

    svc = PortfolioFeeTaxQueryService(repo)
    plus_three_cutoff = datetime(2026, 8, 1, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))

    agg = svc.get_aggregation_as_of(portfolio.id, plus_three_cutoff)

    # Exact representation preserved across aggregation, projection, and ledger view
    assert agg.as_of_recorded_at == plus_three_cutoff
    assert agg.as_of_recorded_at.tzinfo == timezone(timedelta(hours=3))
    assert agg.observed_projection.as_of_recorded_at == plus_three_cutoff
    assert agg.observed_projection.ledger_view.as_of_recorded_at == plus_three_cutoff


@pytest.mark.parametrize("invalid_cutoff", [
    None,
    True,
    False,
    "2026-08-01T15:00:00Z",
    12345,
    datetime(2026, 8, 1, 15, 0, 0),  # naive
    datetime(2026, 8, 1, 15, 0, 0, tzinfo=NullOffsetTZ()),  # non-null tzinfo with None utcoffset
])
def test_as_of_rejects_invalid_cutoff(invalid_cutoff: Any) -> None:
    portfolio = _make_portfolio()
    repo = StrictTestPortfolioRepository(portfolios={portfolio.id: portfolio})
    svc = PortfolioFeeTaxQueryService(repo)

    with pytest.raises(PortfolioFeeTaxQueryError, match="as_of_recorded_at must be"):
        svc.get_aggregation_as_of(portfolio.id, invalid_cutoff)

    assert len(repo.get_portfolio_calls) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 5. Repository Response Integrity Tests (Section 41-44)
# ─────────────────────────────────────────────────────────────────────────────

def test_missing_portfolio_raises_error() -> None:
    repo = StrictTestPortfolioRepository(portfolios={})
    svc = PortfolioFeeTaxQueryService(repo)
    missing_id = uuid4()

    with pytest.raises(PortfolioFeeTaxQueryError, match="does not exist under the bound owner"):
        svc.get_current_aggregation(missing_id)

    assert repo.get_portfolio_calls == [missing_id]
    assert len(repo.list_transactions_calls) == 0


def test_wrong_portfolio_type_fails_closed() -> None:
    pid = uuid4()
    class FakeBadRepo:
        def get_portfolio(self, portfolio_id: UUID) -> Any:
            return {"id": portfolio_id, "name": "Fake"}

        def list_transactions(self, portfolio_id: UUID) -> List[PortfolioTransaction]:
            return []

    svc = PortfolioFeeTaxQueryService(FakeBadRepo())  # type: ignore[arg-type]

    with pytest.raises(PortfolioFeeTaxQueryError, match="Repository returned invalid portfolio object"):
        svc.get_current_aggregation(pid)


def test_wrong_portfolio_id_fails_closed() -> None:
    requested_id = uuid4()
    different_id = uuid4()
    portfolio_b = _make_portfolio(portfolio_id=different_id)

    repo = StrictTestPortfolioRepository(portfolios={requested_id: portfolio_b})
    svc = PortfolioFeeTaxQueryService(repo)

    with pytest.raises(PortfolioFeeTaxQueryError, match="for requested portfolio"):
        svc.get_current_aggregation(requested_id)

    assert len(repo.list_transactions_calls) == 0


@pytest.mark.parametrize("invalid_txs", [
    None,
    "string_not_list",
    b"bytes_not_list",
    {"tx": 1},
    12345,
])
def test_invalid_transaction_collection_fails_closed(invalid_txs: Any) -> None:
    portfolio = _make_portfolio()
    class BadTxRepo:
        def get_portfolio(self, portfolio_id: UUID) -> Optional[Portfolio]:
            return portfolio

        def list_transactions(self, portfolio_id: UUID) -> Any:
            return invalid_txs

    svc = PortfolioFeeTaxQueryService(BadTxRepo())  # type: ignore[arg-type]

    with pytest.raises(PortfolioFeeTaxQueryError, match="Repository returned invalid transaction collection"):
        svc.get_current_aggregation(portfolio.id)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Domain Query Scenarios (Section 45-52)
# ─────────────────────────────────────────────────────────────────────────────

def test_empty_valid_history() -> None:
    portfolio = _make_portfolio()
    repo = StrictTestPortfolioRepository(
        portfolios={portfolio.id: portfolio},
        transactions={portfolio.id: []},
    )
    svc = PortfolioFeeTaxQueryService(repo)
    agg = svc.get_current_aggregation(portfolio.id)

    assert isinstance(agg, ObservedFeeTaxAggregation)
    assert agg.states == ()
    assert agg.state_count == 0


def test_basic_persisted_evidence_filters_only_fee_and_tax() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 11, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    t4 = datetime(2026, 6, 1, 13, 0, 0, tzinfo=timezone.utc)

    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY, recorded_at=t1)
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, recorded_at=t2, cash_amount=Decimal("12.50"), cash_currency=Currency.USD)
    tax_tx = _make_tx(portfolio.id, account_id, TransactionType.TAX_WITHHOLDING, recorded_at=t3, cash_amount=Decimal("25.00"), cash_currency=Currency.TRY)
    div_tx = _make_tx(portfolio.id, account_id, TransactionType.DIVIDEND, recorded_at=t4)

    repo = StrictTestPortfolioRepository(
        portfolios={portfolio.id: portfolio},
        transactions={portfolio.id: [buy_tx, fee_tx, tax_tx, div_tx]},
    )

    svc = PortfolioFeeTaxQueryService(repo)
    agg = svc.get_current_aggregation(portfolio.id)

    assert agg.state_count == 2
    assert agg.account_ids == (account_id,)

    # USD fee state
    s_usd = agg.states[0]
    assert s_usd.currency == Currency.USD
    assert s_usd.fee_amount == Decimal("12.50")
    assert s_usd.tax_withholding_amount == Decimal("0")

    # TRY tax state
    s_try = agg.states[1]
    assert s_try.currency == Currency.TRY
    assert s_try.fee_amount == Decimal("0")
    assert s_try.tax_withholding_amount == Decimal("25.00")


def test_multi_account_query_loads_all_accounts() -> None:
    portfolio = _make_portfolio()
    account_a = uuid4()
    account_b = uuid4()
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 11, 0, 0, tzinfo=timezone.utc)

    fee_a = _make_tx(portfolio.id, account_a, TransactionType.FEE, recorded_at=t1, cash_amount=Decimal("10.00"), cash_currency=Currency.USD)
    tax_b = _make_tx(portfolio.id, account_b, TransactionType.TAX_WITHHOLDING, recorded_at=t2, cash_amount=Decimal("5.00"), cash_currency=Currency.USD)

    repo = StrictTestPortfolioRepository(
        portfolios={portfolio.id: portfolio},
        transactions={portfolio.id: [fee_a, tax_b]},
    )

    svc = PortfolioFeeTaxQueryService(repo)
    agg = svc.get_current_aggregation(portfolio.id)

    assert agg.state_count == 2
    assert agg.account_ids == (account_a, account_b)
    assert agg.states[0].account_id == account_a and agg.states[0].fee_amount == Decimal("10.00")
    assert agg.states[1].account_id == account_b and agg.states[1].tax_withholding_amount == Decimal("5.00")


def test_multi_currency_query_retains_native_currencies() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 11, 0, 0, tzinfo=timezone.utc)

    fee_usd = _make_tx(portfolio.id, account_id, TransactionType.FEE, recorded_at=t1, cash_amount=Decimal("10.00"), cash_currency=Currency.USD)
    fee_try = _make_tx(portfolio.id, account_id, TransactionType.FEE, recorded_at=t2, cash_amount=Decimal("100.00"), cash_currency=Currency.TRY)

    repo = StrictTestPortfolioRepository(
        portfolios={portfolio.id: portfolio},
        transactions={portfolio.id: [fee_usd, fee_try]},
    )

    svc = PortfolioFeeTaxQueryService(repo)
    agg = svc.get_current_aggregation(portfolio.id)

    assert agg.state_count == 2
    assert agg.states[0].currency == Currency.USD and agg.states[0].fee_amount == Decimal("10.00")
    assert agg.states[1].currency == Currency.TRY and agg.states[1].fee_amount == Decimal("100.00")


def test_pit_future_event_exclusion() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 6, 1, 14, 0, 0, tzinfo=timezone.utc)

    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, recorded_at=t1, cash_amount=Decimal("10.00"), cash_currency=Currency.USD)
    tax_tx = _make_tx(portfolio.id, account_id, TransactionType.TAX_WITHHOLDING, recorded_at=t3, cash_amount=Decimal("5.00"), cash_currency=Currency.USD)

    repo = StrictTestPortfolioRepository(
        portfolios={portfolio.id: portfolio},
        transactions={portfolio.id: [fee_tx, tax_tx]},
    )

    svc = PortfolioFeeTaxQueryService(repo)

    # Query as-of T2 (before T3)
    agg_t2 = svc.get_aggregation_as_of(portfolio.id, t2)
    assert agg_t2.state_count == 1
    assert agg_t2.states[0].fee_amount == Decimal("10.00")
    assert agg_t2.states[0].tax_withholding_amount == Decimal("0")

    # Query as-of T3 (on T3)
    agg_t3 = svc.get_aggregation_as_of(portfolio.id, t3)
    assert agg_t3.state_count == 1
    assert agg_t3.states[0].fee_amount == Decimal("10.00")
    assert agg_t3.states[0].tax_withholding_amount == Decimal("5.00")


def test_pit_fee_reversal_lifecycle() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 6, 1, 14, 0, 0, tzinfo=timezone.utc)

    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, recorded_at=t1, cash_amount=Decimal("50.00"), cash_currency=Currency.USD)
    rev_tx = _make_tx(portfolio.id, account_id, TransactionType.REVERSAL, recorded_at=t3, reverses_transaction_id=fee_tx.id)

    repo = StrictTestPortfolioRepository(
        portfolios={portfolio.id: portfolio},
        transactions={portfolio.id: [fee_tx, rev_tx]},
    )

    svc = PortfolioFeeTaxQueryService(repo)

    # At T2: Fee present
    agg_t2 = svc.get_aggregation_as_of(portfolio.id, t2)
    assert agg_t2.state_count == 1
    assert agg_t2.states[0].fee_amount == Decimal("50.00")

    # At T3: Fee reversed / absent
    agg_t3 = svc.get_aggregation_as_of(portfolio.id, t3)
    assert agg_t3.state_count == 0
    assert agg_t3.states == ()


def test_pit_tax_reversal_lifecycle() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 6, 1, 14, 0, 0, tzinfo=timezone.utc)

    tax_tx = _make_tx(portfolio.id, account_id, TransactionType.TAX_WITHHOLDING, recorded_at=t1, cash_amount=Decimal("30.00"), cash_currency=Currency.TRY)
    rev_tx = _make_tx(portfolio.id, account_id, TransactionType.REVERSAL, recorded_at=t3, reverses_transaction_id=tax_tx.id)

    repo = StrictTestPortfolioRepository(
        portfolios={portfolio.id: portfolio},
        transactions={portfolio.id: [tax_tx, rev_tx]},
    )

    svc = PortfolioFeeTaxQueryService(repo)

    # At T2: Tax present
    agg_t2 = svc.get_aggregation_as_of(portfolio.id, t2)
    assert agg_t2.state_count == 1
    assert agg_t2.states[0].tax_withholding_amount == Decimal("30.00")

    # At T3: Tax reversed / absent
    agg_t3 = svc.get_aggregation_as_of(portfolio.id, t3)
    assert agg_t3.state_count == 0
    assert agg_t3.states == ()


def test_current_query_future_event_exclusion() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    t_now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    t_future = datetime(2026, 6, 1, 15, 0, 0, tzinfo=timezone.utc)

    fee_past = _make_tx(portfolio.id, account_id, TransactionType.FEE, recorded_at=t_now, cash_amount=Decimal("10.00"), cash_currency=Currency.USD)
    fee_future = _make_tx(portfolio.id, account_id, TransactionType.FEE, recorded_at=t_future, cash_amount=Decimal("20.00"), cash_currency=Currency.USD)

    repo = StrictTestPortfolioRepository(
        portfolios={portfolio.id: portfolio},
        transactions={portfolio.id: [fee_past, fee_future]},
    )

    svc = PortfolioFeeTaxQueryService(repo, clock=lambda: t_now)
    agg = svc.get_current_aggregation(portfolio.id)

    assert agg.state_count == 1
    assert agg.states[0].fee_amount == Decimal("10.00")


# ─────────────────────────────────────────────────────────────────────────────
# 7. Final Red-Team Integration (Section 66)
# ─────────────────────────────────────────────────────────────────────────────

def test_final_red_team_pipeline_integration() -> None:
    """
    Final Red-Team integration (Section 66):
    Portfolio
    ├── Account A / USD FEE (1.20 @ T1)
    ├── Account A / TRY FEE (10.00 @ T2)
    ├── Account B / USD TAX_WITHHOLDING (5.00 @ T3)
    └── Future FEE (100.00 @ T5)
    Plus a reversed fee: FEE (50.00 @ T1) reversed at T4.
    """
    portfolio = _make_portfolio()
    account_a = uuid4()
    account_b = uuid4()

    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 11, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    t4 = datetime(2026, 6, 1, 13, 0, 0, tzinfo=timezone.utc)
    t_cutoff = datetime(2026, 6, 1, 14, 0, 0, tzinfo=timezone.utc)
    t5_future = datetime(2026, 6, 1, 16, 0, 0, tzinfo=timezone.utc)

    tx_a_usd = _make_tx(portfolio.id, account_a, TransactionType.FEE, recorded_at=t1, cash_amount=Decimal("1.20"), cash_currency=Currency.USD)
    tx_a_try = _make_tx(portfolio.id, account_a, TransactionType.FEE, recorded_at=t2, cash_amount=Decimal("10.00"), cash_currency=Currency.TRY)
    tx_b_usd = _make_tx(portfolio.id, account_b, TransactionType.TAX_WITHHOLDING, recorded_at=t3, cash_amount=Decimal("5.00"), cash_currency=Currency.USD)

    # Reversed fee: 50.00 TRY at T1 reversed at T4
    rev_target = _make_tx(portfolio.id, account_a, TransactionType.FEE, recorded_at=t1, cash_amount=Decimal("50.00"), cash_currency=Currency.TRY)
    reversal_tx = _make_tx(portfolio.id, account_a, TransactionType.REVERSAL, recorded_at=t4, reverses_transaction_id=rev_target.id)

    # Future fee
    future_fee = _make_tx(portfolio.id, account_a, TransactionType.FEE, recorded_at=t5_future, cash_amount=Decimal("100.00"), cash_currency=Currency.USD)

    all_txs = [tx_a_usd, tx_a_try, tx_b_usd, rev_target, reversal_tx, future_fee]

    repo = StrictTestPortfolioRepository(
        portfolios={portfolio.id: portfolio},
        transactions={portfolio.id: all_txs},
    )

    svc = PortfolioFeeTaxQueryService(repo)

    # 1. Query as-of T_cutoff in UTC+3 representation
    plus_three_cutoff = datetime(2026, 6, 1, 17, 0, 0, tzinfo=timezone(timedelta(hours=3)))
    assert plus_three_cutoff == t_cutoff  # Same physical instant

    agg = svc.get_aggregation_as_of(portfolio.id, plus_three_cutoff)

    # Exact representation preserved
    assert agg.as_of_recorded_at == plus_three_cutoff
    assert agg.as_of_recorded_at.tzinfo == timezone(timedelta(hours=3))

    # Required states in first-seen order: A/USD, A/TRY, B/USD
    assert agg.state_count == 3
    assert agg.states[0].account_id == account_a and agg.states[0].currency == Currency.USD
    assert agg.states[1].account_id == account_a and agg.states[1].currency == Currency.TRY
    assert agg.states[2].account_id == account_b and agg.states[2].currency == Currency.USD

    # A/USD: 1.20 fee, 0 tax
    assert agg.states[0].fee_amount == Decimal("1.20")
    assert agg.states[0].tax_withholding_amount == Decimal("0")

    # A/TRY: 10.00 fee (50.00 reversed fee is excluded because reversal at T4 is <= cutoff)
    assert agg.states[1].fee_amount == Decimal("10.00")
    assert agg.states[1].tax_withholding_amount == Decimal("0")

    # B/USD: 0 fee, 5.00 tax
    assert agg.states[2].fee_amount == Decimal("0")
    assert agg.states[2].tax_withholding_amount == Decimal("5.00")

    # 2. Before reversal cutoff (T2): 50.00 TRY fee must be present
    agg_before_rev = svc.get_aggregation_as_of(portfolio.id, t2)
    s_try_before_rev = [s for s in agg_before_rev.states if s.currency == Currency.TRY][0]
    assert s_try_before_rev.fee_amount == Decimal("60.00")  # 10.00 + 50.00


# ─────────────────────────────────────────────────────────────────────────────
# 8. Fail-Closed Error Propagation Tests (Section 53-54)
# ─────────────────────────────────────────────────────────────────────────────

def test_lower_layer_projection_error_propagates_unchanged() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()

    # Corrupt transaction history: Reversal referencing unknown target triggers PortfolioProjectionError
    orphan_rev = _make_tx(
        portfolio.id,
        account_id,
        TransactionType.REVERSAL,
        reverses_transaction_id=uuid4(),  # Unknown target
    )

    repo = StrictTestPortfolioRepository(
        portfolios={portfolio.id: portfolio},
        transactions={portfolio.id: [orphan_rev]},
    )

    svc = PortfolioFeeTaxQueryService(repo)

    # Must propagate PortfolioProjectionError, NOT wrapped into PortfolioFeeTaxQueryError
    with pytest.raises(PortfolioProjectionError, match="references unknown target transaction"):
        svc.get_current_aggregation(portfolio.id)


def test_repository_get_portfolio_error_propagates_unchanged() -> None:
    op_error = ConnectionError("DB connection dropped")
    repo = StrictTestPortfolioRepository(get_portfolio_error=op_error)
    svc = PortfolioFeeTaxQueryService(repo)

    with pytest.raises(ConnectionError, match="DB connection dropped"):
        svc.get_current_aggregation(uuid4())


def test_repository_list_transactions_error_propagates_unchanged() -> None:
    portfolio = _make_portfolio()
    op_error = RuntimeError("DB read timeout")
    repo = StrictTestPortfolioRepository(
        portfolios={portfolio.id: portfolio},
        list_transactions_error=op_error,
    )
    svc = PortfolioFeeTaxQueryService(repo)

    with pytest.raises(RuntimeError, match="DB read timeout"):
        svc.get_current_aggregation(portfolio.id)


# ─────────────────────────────────────────────────────────────────────────────
# 9. Architectural Invariants & Purity Verification (Section 55-61)
# ─────────────────────────────────────────────────────────────────────────────

def test_public_api_has_no_owner_or_user_arguments() -> None:
    sig_current = inspect.signature(PortfolioFeeTaxQueryService.get_current_aggregation)
    sig_as_of = inspect.signature(PortfolioFeeTaxQueryService.get_aggregation_as_of)

    assert list(sig_current.parameters.keys()) == ["self", "portfolio_id"]
    assert list(sig_as_of.parameters.keys()) == ["self", "portfolio_id", "as_of_recorded_at"]


def test_single_repository_lookups_per_query() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE)

    repo = StrictTestPortfolioRepository(
        portfolios={portfolio.id: portfolio},
        transactions={portfolio.id: [fee_tx]},
    )

    svc = PortfolioFeeTaxQueryService(repo)
    svc.get_current_aggregation(portfolio.id)

    assert repo.get_portfolio_calls == [portfolio.id]
    assert repo.list_transactions_calls == [portfolio.id]


def test_static_purity_ast_checks() -> None:
    src = inspect.getsource(fee_tax_service_module)
    tree = ast.parse(src)

    prohibited_names = {
        "float", "round", "quantize", "fsum",
        "uuid4", "uuid5", "hashlib", "sha256",
        "_exact_decimal_sum", "Decimal",
        "tax_due", "tax_rate", "tax_bracket",
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id not in prohibited_names, f"Prohibited identifier '{node.id}' in fee_tax_service.py"
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("rpc", "table", "execute", "append_transaction", "commit_import", "quantize")
            if node.attr in ("FEE", "TAX_WITHHOLDING"):
                pytest.fail("fee_tax_service.py must not reference TransactionType.FEE / TAX_WITHHOLDING directly")

        # Clock check: only datetime.now(timezone.utc) in default clock lambda
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "now":
                # Ensure it is datetime.now(timezone.utc)
                assert len(node.args) == 1
                arg = node.args[0]
                assert isinstance(arg, ast.Attribute) and arg.attr == "utc"
