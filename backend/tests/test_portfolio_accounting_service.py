"""
backend/tests/test_portfolio_accounting_service.py
==================================================
Tests for Phase 12C.5: Owner-Bound Persisted Accounting Snapshot Query Service.

Zero network calls (pytest-socket enforced).
Pure domain execution with real ledger & accounting projections and strict repository double.

Test Matrix:
    1. Basic Queries & Repository Flow (Empty portfolio, PIT / Current, Not Found, Call counts)
    2. Clock Validation & Resolution (Invoked once, UTC / +03 normalized, Microseconds, Invalid clocks, PIT bypass)
    3. PIT Representation & Input Validation (UTC / Non-UTC preservation, Naive / NullOffset rejections, Portfolio ID validation)
    4. Economic Integration (Deposit + BUY, BUY + SELL, Reversed BUY, Reversed SELL)
    5. Point-in-Time & Temporal Knowledge Integration (Late import exclusion, Reversal cutoff, Future-recorded current exclusion)
    6. Shuffled Order, Multi-Account, Multi-Currency, Mode & Archived Portfolios
    7. Fail-Closed Error Propagation (Repository get/list errors, Position/Cash/Ledger projection errors)
    8. Public Signature Inspection (No owner_id, user_id, mode, or account_id parameter exposure)
"""

from __future__ import annotations

from datetime import date, datetime, timezone, tzinfo, timedelta
from decimal import Decimal
import inspect
import random
from typing import Any, Callable, Dict, List, Optional
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import Currency, PortfolioMode, TransactionType
from backend.engine.private.portfolio.accounting import (
    PortfolioAccountingError,
    PortfolioAccountingSnapshot,
)
from backend.engine.private.portfolio.accounting_service import (
    PortfolioAccountingQueryError,
    PortfolioAccountingQueryService,
)
from backend.engine.private.portfolio.cash import CashProjectionError
from backend.engine.private.portfolio.models import Portfolio, PortfolioTransaction
from backend.engine.private.portfolio.positions import PositionProjectionError
from backend.engine.private.portfolio.projection import PortfolioProjectionError


class NullOffsetTZ(tzinfo):
    """Custom tzinfo implementation returning None for utcoffset (non-None tzinfo but not aware)."""
    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None

    def tzname(self, dt):
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

    def get_portfolio(self, portfolio_id: UUID | str) -> Optional[Portfolio]:
        self.get_portfolio_calls.append(portfolio_id)
        if self._get_portfolio_error is not None:
            raise self._get_portfolio_error
        p_id = portfolio_id if isinstance(portfolio_id, UUID) else UUID(str(portfolio_id))
        return self._portfolios.get(p_id)

    def list_transactions(self, portfolio_id: UUID | str) -> List[PortfolioTransaction]:
        self.list_transactions_calls.append(portfolio_id)
        if self._list_transactions_error is not None:
            raise self._list_transactions_error
        p_id = portfolio_id if isinstance(portfolio_id, UUID) else UUID(str(portfolio_id))
        txs = self._transactions.get(p_id, [])
        return list(txs)


# ─────────────────────────────────────────────────────────────────────────────
# Helper Factories
# ─────────────────────────────────────────────────────────────────────────────

def _make_portfolio(
    mode: PortfolioMode = PortfolioMode.MY_PORTFOLIO,
    owner_id: Optional[UUID] = None,
    id: Optional[UUID] = None,
    archived_at: Optional[datetime] = None,
) -> Portfolio:
    return Portfolio(
        owner_id=owner_id or uuid4(),
        name="Service Test Portfolio",
        base_currency=Currency.USD,
        mode=mode,
        id=id or uuid4(),
        created_at=datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc),
        archived_at=archived_at,
    )


def _make_tx(
    portfolio_id: UUID,
    account_id: UUID,
    tx_type: TransactionType = TransactionType.CASH_DEPOSIT,
    effective_date: Optional[date] = None,
    recorded_at: Optional[datetime] = None,
    executed_at: Optional[datetime] = None,
    id: Optional[UUID] = None,
    reverses_tx_id: Optional[UUID] = None,
    instrument_id: Optional[UUID] = None,
    quantity: Optional[Decimal] = None,
    unit_price: Optional[Decimal] = None,
    trade_currency: Optional[Currency] = None,
    cash_amount: Optional[Decimal] = None,
    cash_currency: Optional[Currency] = None,
    from_amount: Optional[Decimal] = None,
    from_currency: Optional[Currency] = None,
    to_amount: Optional[Decimal] = None,
    to_currency: Optional[Currency] = None,
    ext_source: Optional[str] = None,
    ext_ref: Optional[str] = None,
) -> PortfolioTransaction:
    rec = recorded_at or datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
    eff = effective_date or date(2026, 8, 10)

    if tx_type == TransactionType.BUY:
        return PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.BUY,
            instrument_id=instrument_id or uuid4(),
            effective_date=eff,
            recorded_at=rec,
            executed_at=executed_at,
            quantity=quantity or Decimal("10"),
            unit_price=unit_price or Decimal("150.00"),
            trade_currency=trade_currency or Currency.USD,
            external_source=ext_source,
            external_reference=ext_ref,
            id=id or uuid4(),
        )
    elif tx_type == TransactionType.SELL:
        return PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.SELL,
            instrument_id=instrument_id or uuid4(),
            effective_date=eff,
            recorded_at=rec,
            executed_at=executed_at,
            quantity=quantity or Decimal("5"),
            unit_price=unit_price or Decimal("160.00"),
            trade_currency=trade_currency or Currency.USD,
            external_source=ext_source,
            external_reference=ext_ref,
            id=id or uuid4(),
        )
    elif tx_type in (
        TransactionType.CASH_DEPOSIT,
        TransactionType.CASH_WITHDRAWAL,
        TransactionType.DIVIDEND,
        TransactionType.INTEREST,
        TransactionType.FEE,
        TransactionType.TAX_WITHHOLDING,
    ):
        return PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=tx_type,
            instrument_id=instrument_id if tx_type == TransactionType.DIVIDEND else None,
            effective_date=eff,
            recorded_at=rec,
            executed_at=executed_at,
            cash_amount=cash_amount or Decimal("100.00"),
            cash_currency=cash_currency or Currency.USD,
            external_source=ext_source,
            external_reference=ext_ref,
            id=id or uuid4(),
        )
    elif tx_type == TransactionType.FX_CONVERSION:
        return PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.FX_CONVERSION,
            effective_date=eff,
            recorded_at=rec,
            executed_at=executed_at,
            from_amount=from_amount or Decimal("100.00"),
            from_currency=from_currency or Currency.USD,
            to_amount=to_amount or Decimal("3400.00"),
            to_currency=to_currency or Currency.TRY,
            external_source=ext_source,
            external_reference=ext_ref,
            id=id or uuid4(),
        )
    elif tx_type == TransactionType.REVERSAL:
        return PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.REVERSAL,
            effective_date=eff,
            recorded_at=rec,
            reverses_transaction_id=reverses_tx_id,
            external_source=ext_source,
            external_reference=ext_ref,
            id=id or uuid4(),
        )
    else:
        raise NotImplementedError(f"Factory not configured for {tx_type}")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Basic Queries & Repository Flow
# ─────────────────────────────────────────────────────────────────────────────

class TestBasicQueriesAndRepositoryFlow:
    """Verifies baseline service queries, repository call counts, and not-found behaviors."""

    def test_existing_empty_portfolio_explicit_pit(self):
        """A: Empty portfolio with explicit PIT produces valid empty snapshot."""
        port = _make_portfolio()
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: []})
        service = PortfolioAccountingQueryService(repo)

        cutoff = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
        snap = service.get_snapshot_as_of(port.id, cutoff)

        assert isinstance(snap, PortfolioAccountingSnapshot)
        assert snap.portfolio_id == port.id
        assert snap.as_of_recorded_at == cutoff
        assert snap.positions.positions == ()
        assert snap.cash.balances == ()

    def test_existing_empty_portfolio_current(self):
        """B: Empty portfolio current snapshot produces valid snapshot with captured UTC cutoff."""
        port = _make_portfolio()
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: []})

        fixed_now = datetime(2026, 8, 28, 14, 30, 0, 123456, tzinfo=timezone.utc)
        service = PortfolioAccountingQueryService(repo, clock=lambda: fixed_now)

        snap = service.get_current_snapshot(port.id)

        assert snap.portfolio_id == port.id
        assert snap.as_of_recorded_at == fixed_now
        assert snap.positions.positions == ()
        assert snap.cash.balances == ()

    def test_portfolio_not_found_raises_query_error(self):
        """C: Non-existent portfolio raises PortfolioAccountingQueryError; no fallback."""
        repo = StrictTestPortfolioRepository(portfolios={}, transactions={})
        service = PortfolioAccountingQueryService(repo)

        missing_id = uuid4()
        with pytest.raises(PortfolioAccountingQueryError, match=f"Portfolio {missing_id} does not exist"):
            service.get_current_snapshot(missing_id)

    def test_get_portfolio_called_once(self):
        """D: get_portfolio is called exactly once per query."""
        port = _make_portfolio()
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: []})
        service = PortfolioAccountingQueryService(repo)

        service.get_current_snapshot(port.id)
        assert len(repo.get_portfolio_calls) == 1
        assert repo.get_portfolio_calls[0] == port.id

    def test_list_transactions_called_once_after_portfolio_found(self):
        """E: list_transactions is called exactly once with portfolio.id."""
        port = _make_portfolio()
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: []})
        service = PortfolioAccountingQueryService(repo)

        service.get_current_snapshot(port.id)
        assert len(repo.list_transactions_calls) == 1
        assert repo.list_transactions_calls[0] == port.id

    def test_list_transactions_not_called_when_portfolio_absent(self):
        """F: list_transactions is NOT called when portfolio is not found."""
        repo = StrictTestPortfolioRepository(portfolios={}, transactions={})
        service = PortfolioAccountingQueryService(repo)

        with pytest.raises(PortfolioAccountingQueryError):
            service.get_current_snapshot(uuid4())

        assert len(repo.list_transactions_calls) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 2. Clock Validation & Resolution
# ─────────────────────────────────────────────────────────────────────────────

class TestClockValidationAndResolution:
    """Verifies clock execution, normalization to UTC, and fail-closed guards."""

    def test_clock_called_exactly_once_per_current_query(self):
        """G: Clock is invoked exactly once per get_current_snapshot call."""
        port = _make_portfolio()
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: []})

        clock_count = 0
        def counting_clock():
            nonlocal clock_count
            clock_count += 1
            return datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

        service = PortfolioAccountingQueryService(repo, clock=counting_clock)
        service.get_current_snapshot(port.id)

        assert clock_count == 1

    def test_utc_clock_accepted(self):
        """H: UTC aware clock is accepted and preserved."""
        port = _make_portfolio()
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: []})

        utc_time = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)
        service = PortfolioAccountingQueryService(repo, clock=lambda: utc_time)

        snap = service.get_current_snapshot(port.id)
        assert snap.as_of_recorded_at == utc_time

    def test_plus3_clock_accepted_and_normalized_to_utc(self):
        """I: +03:00 clock is accepted and normalized to equivalent UTC datetime for current cutoff."""
        port = _make_portfolio()
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: []})

        plus3_time = datetime(2026, 8, 28, 15, 30, 0, tzinfo=timezone(timedelta(hours=3)))
        expected_utc = datetime(2026, 8, 28, 12, 30, 0, tzinfo=timezone.utc)

        service = PortfolioAccountingQueryService(repo, clock=lambda: plus3_time)
        snap = service.get_current_snapshot(port.id)

        assert snap.as_of_recorded_at == expected_utc
        assert snap.as_of_recorded_at.tzinfo == timezone.utc

    def test_microseconds_preserved(self):
        """J: Sub-second microsecond precision is strictly preserved from clock."""
        port = _make_portfolio()
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: []})

        precise_time = datetime(2026, 8, 28, 12, 0, 0, 987654, tzinfo=timezone.utc)
        service = PortfolioAccountingQueryService(repo, clock=lambda: precise_time)

        snap = service.get_current_snapshot(port.id)
        assert snap.as_of_recorded_at.microsecond == 987654

    def test_naive_clock_rejected(self):
        """K: Naive clock return value raises PortfolioAccountingQueryError."""
        port = _make_portfolio()
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: []})

        service = PortfolioAccountingQueryService(repo, clock=lambda: datetime(2026, 8, 28, 12, 0, 0))  # Naive
        with pytest.raises(PortfolioAccountingQueryError, match="clock return value must be timezone-aware"):
            service.get_current_snapshot(port.id)

    def test_null_offset_clock_rejected(self):
        """L: Clock returning datetime with NullOffsetTZ is rejected."""
        port = _make_portfolio()
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: []})

        service = PortfolioAccountingQueryService(repo, clock=lambda: datetime(2026, 8, 28, 12, 0, 0, tzinfo=NullOffsetTZ()))
        with pytest.raises(PortfolioAccountingQueryError, match="clock return value must be timezone-aware"):
            service.get_current_snapshot(port.id)

    def test_invalid_clock_type_rejected(self):
        """M: Bool, string, int, float clock return values are rejected."""
        port = _make_portfolio()
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: []})

        for invalid in (True, False, "2026-08-28T12:00:00Z", 1724846400, 1724846400.5, None):
            service = PortfolioAccountingQueryService(repo, clock=lambda: invalid)  # type: ignore
            with pytest.raises(PortfolioAccountingQueryError, match="clock must return a datetime"):
                service.get_current_snapshot(port.id)

    def test_pit_query_does_not_invoke_clock(self):
        """N: get_snapshot_as_of does not invoke the clock callable."""
        port = _make_portfolio()
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: []})

        def bomb_clock():
            raise AssertionError("Clock must not be called for PIT query")

        service = PortfolioAccountingQueryService(repo, clock=bomb_clock)
        cutoff = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        snap = service.get_snapshot_as_of(port.id, cutoff)
        assert snap.as_of_recorded_at == cutoff


# ─────────────────────────────────────────────────────────────────────────────
# 3. PIT Representation & Input Validation
# ─────────────────────────────────────────────────────────────────────────────

class TestPITRepresentationAndInputValidation:
    """Verifies caller PIT representation preservation and boundary parameter validation."""

    def test_explicit_utc_cutoff_preserved(self):
        """O: Explicit UTC cutoff is preserved exactly without alteration."""
        port = _make_portfolio()
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: []})
        service = PortfolioAccountingQueryService(repo)

        cutoff = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
        snap = service.get_snapshot_as_of(port.id, cutoff)
        assert snap.as_of_recorded_at == cutoff

    def test_explicit_plus3_cutoff_preserved_exactly(self):
        """P: Explicit +03:00 cutoff is preserved with its exact representation, NOT normalized to UTC."""
        port = _make_portfolio()
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: []})
        service = PortfolioAccountingQueryService(repo)

        plus3_tz = timezone(timedelta(hours=3))
        cutoff = datetime(2026, 8, 10, 15, 0, 0, tzinfo=plus3_tz)
        snap = service.get_snapshot_as_of(port.id, cutoff)

        assert snap.as_of_recorded_at == cutoff
        assert snap.as_of_recorded_at.tzinfo == plus3_tz
        assert snap.ledger_view.as_of_recorded_at == cutoff
        assert snap.positions.as_of_recorded_at == cutoff
        assert snap.cash.as_of_recorded_at == cutoff

    def test_explicit_microseconds_preserved(self):
        """Q: Explicit microsecond timestamps in PIT queries are preserved."""
        port = _make_portfolio()
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: []})
        service = PortfolioAccountingQueryService(repo)

        cutoff = datetime(2026, 8, 10, 12, 0, 0, 654321, tzinfo=timezone.utc)
        snap = service.get_snapshot_as_of(port.id, cutoff)
        assert snap.as_of_recorded_at.microsecond == 654321

    def test_naive_explicit_cutoff_rejected(self):
        """R: Naive explicit cutoff is rejected with PortfolioAccountingQueryError."""
        port = _make_portfolio()
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: []})
        service = PortfolioAccountingQueryService(repo)

        with pytest.raises(PortfolioAccountingQueryError, match="as_of_recorded_at must be timezone-aware"):
            service.get_snapshot_as_of(port.id, datetime(2026, 8, 10, 12, 0, 0))

    def test_null_offset_explicit_cutoff_rejected(self):
        """S: NullOffsetTZ explicit cutoff is rejected with PortfolioAccountingQueryError."""
        port = _make_portfolio()
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: []})
        service = PortfolioAccountingQueryService(repo)

        with pytest.raises(PortfolioAccountingQueryError, match="as_of_recorded_at must be timezone-aware"):
            service.get_snapshot_as_of(port.id, datetime(2026, 8, 10, 12, 0, 0, tzinfo=NullOffsetTZ()))

    def test_malformed_cutoff_types_rejected(self):
        """T: Non-datetime cutoff types (bool, str, int) are rejected."""
        port = _make_portfolio()
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: []})
        service = PortfolioAccountingQueryService(repo)

        for invalid in (True, False, "2026-08-10T12:00:00Z", 1723291200, None):
            with pytest.raises(PortfolioAccountingQueryError, match="as_of_recorded_at must be a datetime"):
                service.get_snapshot_as_of(port.id, invalid)  # type: ignore

    def test_malformed_portfolio_id_rejected(self):
        """Portfolio ID validation rejects non-UUID types (bool, str, int, float, None)."""
        repo = StrictTestPortfolioRepository()
        service = PortfolioAccountingQueryService(repo)

        for invalid in (True, False, "b5b21356-32ed-4603-9be7-9f9bc97e011f", 12345, 123.45, None):
            with pytest.raises(PortfolioAccountingQueryError, match="portfolio_id must be a UUID"):
                service.get_current_snapshot(invalid)  # type: ignore

            with pytest.raises(PortfolioAccountingQueryError, match="portfolio_id must be a UUID"):
                service.get_snapshot_as_of(invalid, datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc))  # type: ignore

    def test_none_repository_in_constructor_rejected(self):
        """None repository in constructor raises TypeError."""
        with pytest.raises(TypeError, match="repository must not be None"):
            PortfolioAccountingQueryService(None)  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 4. Economic Integration
# ─────────────────────────────────────────────────────────────────────────────

class TestEconomicIntegration:
    """Verifies end-to-end accounting snapshot composition through real projection pipelines."""

    def test_deposit_and_buy(self):
        """U: Deposit 1000 USD + BUY 10 @ 20 USD produces position 10 and cash 800 USD."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        buy = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("10"), unit_price=Decimal("20.00"), recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))

        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: [dep, buy]})
        service = PortfolioAccountingQueryService(repo, clock=lambda: datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))

        snap = service.get_current_snapshot(port.id)

        assert len(snap.positions.open_positions) == 1
        assert snap.positions.open_positions[0].quantity == Decimal("10")
        assert len(snap.cash.balances) == 1
        assert snap.cash.balances[0].balance == Decimal("800.00")

    def test_deposit_buy_and_sell(self):
        """V: Deposit 1000 + BUY 10 @ 20 + SELL 4 @ 25 produces position 6 and cash 900 USD."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        buy = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("10"), unit_price=Decimal("20.00"), recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))
        sell = _make_tx(port.id, acc_id, tx_type=TransactionType.SELL, instrument_id=inst_id, quantity=Decimal("4"), unit_price=Decimal("25.00"), recorded_at=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc))

        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: [dep, buy, sell]})
        service = PortfolioAccountingQueryService(repo, clock=lambda: datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))

        snap = service.get_current_snapshot(port.id)

        assert len(snap.positions.open_positions) == 1
        assert snap.positions.open_positions[0].quantity == Decimal("6")
        assert len(snap.cash.balances) == 1
        assert snap.cash.balances[0].balance == Decimal("900.00")

    def test_reversed_buy_restores_positions_and_cash(self):
        """W: Reversal of BUY simultaneously removes security position and restores cash to 1000 USD."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        buy = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("10"), unit_price=Decimal("20.00"), recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))
        rev = _make_tx(port.id, acc_id, tx_type=TransactionType.REVERSAL, reverses_tx_id=buy.id, recorded_at=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc))

        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: [dep, buy, rev]})
        service = PortfolioAccountingQueryService(repo, clock=lambda: datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))

        snap = service.get_current_snapshot(port.id)

        assert snap.positions.open_positions == ()
        assert len(snap.cash.balances) == 1
        assert snap.cash.balances[0].balance == Decimal("1000.00")

    def test_reversed_sell_restores_positions_and_cash(self):
        """X: Reversal of SELL restores position to 10 and cash to 800 USD."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        buy = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("10"), unit_price=Decimal("20.00"), recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))
        sell = _make_tx(port.id, acc_id, tx_type=TransactionType.SELL, instrument_id=inst_id, quantity=Decimal("4"), unit_price=Decimal("25.00"), recorded_at=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc))
        rev_sell = _make_tx(port.id, acc_id, tx_type=TransactionType.REVERSAL, reverses_tx_id=sell.id, recorded_at=datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc))

        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: [dep, buy, sell, rev_sell]})
        service = PortfolioAccountingQueryService(repo, clock=lambda: datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))

        snap = service.get_current_snapshot(port.id)

        assert len(snap.positions.open_positions) == 1
        assert snap.positions.open_positions[0].quantity == Decimal("10")
        assert len(snap.cash.balances) == 1
        assert snap.cash.balances[0].balance == Decimal("800.00")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Point-in-Time & Temporal Knowledge Integration
# ─────────────────────────────────────────────────────────────────────────────

class TestPointInTimeAndTemporalKnowledge:
    """Verifies point-in-time boundary isolation and late-import / future-recorded event defense."""

    def test_late_import_effective_before_cutoff_recorded_after_cutoff_excluded(self):
        """Y: Event with effective_date Aug 1 but recorded_at Aug 20 is excluded from Aug 10 snapshot."""
        port = _make_portfolio()
        acc_id = uuid4()

        late_dep = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.CASH_DEPOSIT,
            cash_amount=Decimal("500.00"),
            effective_date=date(2026, 8, 1),
            recorded_at=datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc),  # System knowledge cutoff is Aug 10
        )

        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: [late_dep]})
        service = PortfolioAccountingQueryService(repo)

        cutoff = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
        snap = service.get_snapshot_as_of(port.id, cutoff)

        # Ingested after Aug 10, so unknown to ledger at cutoff
        assert snap.cash.balances == ()

    def test_reversal_cutoff_timing(self):
        """Z, AA: Reversal recorded Aug 20 is inactive at Aug 10 snapshot and active at Aug 25 snapshot."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        buy = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("10"), unit_price=Decimal("20.00"), recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))
        sell = _make_tx(port.id, acc_id, tx_type=TransactionType.SELL, instrument_id=inst_id, quantity=Decimal("4"), unit_price=Decimal("25.00"), recorded_at=datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc))
        rev_sell = _make_tx(port.id, acc_id, tx_type=TransactionType.REVERSAL, reverses_tx_id=sell.id, recorded_at=datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc))

        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: [dep, buy, sell, rev_sell]})
        service = PortfolioAccountingQueryService(repo)

        # Z: As of Aug 10 (before reversal was recorded)
        snap_early = service.get_snapshot_as_of(port.id, datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc))
        assert snap_early.positions.open_positions[0].quantity == Decimal("6")
        assert snap_early.cash.balances[0].balance == Decimal("900.00")

        # AA: As of Aug 25 (after reversal was recorded)
        snap_late = service.get_snapshot_as_of(port.id, datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc))
        assert snap_late.positions.open_positions[0].quantity == Decimal("10")
        assert snap_late.cash.balances[0].balance == Decimal("800.00")

    def test_future_recorded_event_excluded_beyond_captured_current_clock(self):
        """AB: Row recorded at Aug 30 returned by repo is excluded by current snapshot with clock Aug 28."""
        port = _make_portfolio()
        acc_id = uuid4()

        dep_aug1 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        dep_aug30 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("500.00"), recorded_at=datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc))

        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: [dep_aug1, dep_aug30]})
        clock_aug28 = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        service = PortfolioAccountingQueryService(repo, clock=lambda: clock_aug28)

        snap = service.get_current_snapshot(port.id)

        # Only dep_aug1 is within the captured Aug 28 cutoff
        assert len(snap.cash.balances) == 1
        assert snap.cash.balances[0].balance == Decimal("1000.00")


# ─────────────────────────────────────────────────────────────────────────────
# 6. Shuffled Order, Multi-Account, Multi-Currency, Mode & Archived Portfolios
# ─────────────────────────────────────────────────────────────────────────────

class TestOrderAccountModeAndArchive:
    """Verifies isolation, multi-currency retention, mode preservation, and archive handling."""

    def test_shuffled_repository_order_deterministic(self):
        """AC: Arbitrarily shuffled transactions from repository produce identical deterministic snapshot."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        txs = [
            _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)),
            _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("10"), unit_price=Decimal("20.00"), recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)),
            _make_tx(port.id, acc_id, tx_type=TransactionType.SELL, instrument_id=inst_id, quantity=Decimal("4"), unit_price=Decimal("25.00"), recorded_at=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)),
        ]

        shuffled = list(txs)
        random.seed(42)
        random.shuffle(shuffled)

        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: shuffled})
        service = PortfolioAccountingQueryService(repo, clock=lambda: datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))

        snap = service.get_current_snapshot(port.id)
        assert snap.positions.open_positions[0].quantity == Decimal("6")
        assert snap.cash.balances[0].balance == Decimal("900.00")

    def test_multi_account_isolation(self):
        """AD: Same instrument across separate accounts retains independent position and cash balances."""
        port = _make_portfolio()
        acc_a = uuid4()
        acc_b = uuid4()
        inst_id = uuid4()

        txs = [
            _make_tx(port.id, acc_a, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("500.00")),
            _make_tx(port.id, acc_a, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("5"), unit_price=Decimal("50.00")),
            _make_tx(port.id, acc_b, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00")),
            _make_tx(port.id, acc_b, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("8"), unit_price=Decimal("50.00")),
        ]

        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: txs})
        service = PortfolioAccountingQueryService(repo, clock=lambda: datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))

        snap = service.get_current_snapshot(port.id)

        assert len(snap.positions.open_positions) == 2
        pos_a = next(p for p in snap.positions.open_positions if p.account_id == acc_a)
        pos_b = next(p for p in snap.positions.open_positions if p.account_id == acc_b)
        assert pos_a.quantity == Decimal("5")
        assert pos_b.quantity == Decimal("8")

        assert len(snap.cash.balances) == 2
        cash_a = next(c for c in snap.cash.balances if c.account_id == acc_a)
        cash_b = next(c for c in snap.cash.balances if c.account_id == acc_b)
        assert cash_a.balance == Decimal("250.00")
        assert cash_b.balance == Decimal("600.00")

    def test_multi_currency_retention(self):
        """AE: Multi-currency balances are retained natively without implicit conversion."""
        port = _make_portfolio()
        acc_id = uuid4()

        txs = [
            _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), cash_currency=Currency.USD),
            _make_tx(port.id, acc_id, tx_type=TransactionType.FX_CONVERSION, from_amount=Decimal("200.00"), from_currency=Currency.USD, to_amount=Decimal("7000.00"), to_currency=Currency.TRY),
        ]

        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: txs})
        service = PortfolioAccountingQueryService(repo, clock=lambda: datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))

        snap = service.get_current_snapshot(port.id)

        usd_b = next(c for c in snap.cash.balances if c.currency == Currency.USD)
        try_b = next(c for c in snap.cash.balances if c.currency == Currency.TRY)
        assert usd_b.balance == Decimal("800.00")
        assert try_b.balance == Decimal("7000.00")

    def test_my_portfolio_and_sandbox_mode_preservation(self):
        """AF, AG: MY_PORTFOLIO and SANDBOX modes are preserved strictly from persisted Portfolio."""
        for mode in (PortfolioMode.MY_PORTFOLIO, PortfolioMode.SANDBOX):
            port = _make_portfolio(mode=mode)
            repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: []})
            service = PortfolioAccountingQueryService(repo, clock=lambda: datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))

            snap = service.get_current_snapshot(port.id)
            assert snap.mode == mode
            assert snap.ledger_view.mode == mode
            assert snap.positions.mode == mode
            assert snap.cash.mode == mode

    def test_archived_portfolio_accepted(self):
        """AH: Archived portfolio returned by bound repository is accepted for query."""
        archive_time = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
        port = _make_portfolio(archived_at=archive_time)
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: []})
        service = PortfolioAccountingQueryService(repo, clock=lambda: datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))

        snap = service.get_current_snapshot(port.id)
        assert snap.portfolio_id == port.id


# ─────────────────────────────────────────────────────────────────────────────
# 7. Fail-Closed Error Propagation
# ─────────────────────────────────────────────────────────────────────────────

class TestFailClosedErrorPropagation:
    """Verifies that repository errors and domain projection errors propagate unchanged."""

    def test_repository_get_portfolio_error_propagates(self):
        """AI: Custom DB exception from get_portfolio propagates directly without wrapping."""
        db_err = RuntimeError("Database connection timed out during get_portfolio")
        repo = StrictTestPortfolioRepository(get_portfolio_error=db_err)
        service = PortfolioAccountingQueryService(repo)

        with pytest.raises(RuntimeError, match="Database connection timed out"):
            service.get_current_snapshot(uuid4())

    def test_repository_list_transactions_error_propagates(self):
        """AJ: Custom DB exception from list_transactions propagates directly without wrapping."""
        port = _make_portfolio()
        db_err = ConnectionError("PostgREST network drop during list_transactions")
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, list_transactions_error=db_err)
        service = PortfolioAccountingQueryService(repo)

        with pytest.raises(ConnectionError, match="PostgREST network drop"):
            service.get_current_snapshot(port.id)

    def test_negative_final_position_propagates_position_error(self):
        """AK: Oversell / negative position raises PositionProjectionError; no partial snapshot."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        # SELL without BUY -> negative position
        sell = _make_tx(port.id, acc_id, tx_type=TransactionType.SELL, instrument_id=inst_id, quantity=Decimal("1"), unit_price=Decimal("100.00"))
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: [sell]})
        service = PortfolioAccountingQueryService(repo)

        with pytest.raises(PositionProjectionError):
            service.get_current_snapshot(port.id)

    def test_negative_final_cash_propagates_cash_error(self):
        """AL: Unfunded trade / negative cash raises CashProjectionError; no partial snapshot."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        # BUY without deposit -> negative cash
        buy = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("1"), unit_price=Decimal("100.00"))
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: [buy]})
        service = PortfolioAccountingQueryService(repo)

        with pytest.raises(CashProjectionError):
            service.get_current_snapshot(port.id)

    def test_corrupt_ledger_history_propagates_ledger_error(self):
        """AM: Reversal targeting non-existent transaction raises PortfolioProjectionError."""
        port = _make_portfolio()
        acc_id = uuid4()

        orphan_rev = _make_tx(port.id, acc_id, tx_type=TransactionType.REVERSAL, reverses_tx_id=uuid4())
        repo = StrictTestPortfolioRepository(portfolios={port.id: port}, transactions={port.id: [orphan_rev]})
        service = PortfolioAccountingQueryService(repo)

        with pytest.raises(PortfolioProjectionError):
            service.get_current_snapshot(port.id)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Public Signature Inspection
# ─────────────────────────────────────────────────────────────────────────────

class TestPublicSignatureInspection:
    """Verifies that no owner_id, user_id, mode, or account_id parameter is exposed."""

    def test_service_method_signatures_do_not_expose_forbidden_parameters(self):
        """AN: get_current_snapshot and get_snapshot_as_of only accept portfolio_id and as_of_recorded_at."""
        forbidden_params = {"owner_id", "user_id", "mode", "account_id", "jwt", "claims"}

        current_sig = inspect.signature(PortfolioAccountingQueryService.get_current_snapshot)
        current_params = set(current_sig.parameters.keys())
        assert not (current_params & forbidden_params), f"Forbidden params in get_current_snapshot: {current_params & forbidden_params}"
        assert "portfolio_id" in current_params

        pit_sig = inspect.signature(PortfolioAccountingQueryService.get_snapshot_as_of)
        pit_params = set(pit_sig.parameters.keys())
        assert not (pit_params & forbidden_params), f"Forbidden params in get_snapshot_as_of: {pit_params & forbidden_params}"
        assert "portfolio_id" in pit_params
        assert "as_of_recorded_at" in pit_params
