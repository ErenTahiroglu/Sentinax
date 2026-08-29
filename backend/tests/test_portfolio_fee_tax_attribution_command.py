"""
backend/tests/test_portfolio_fee_tax_attribution_command.py
===========================================================
Comprehensive unit tests for Phase 14M: Owner-Bound Explicit Fee/Tax Allocation Command Service.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import inspect
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import Currency, PortfolioMode, TransactionType
from backend.engine.private.portfolio.fee_tax_attribution import (
    FeeTaxAttributionError,
    FeeTaxAttributionIntent,
    build_observed_fee_tax_attribution_set,
)
from backend.engine.private.portfolio.fee_tax_attribution_command import (
    PortfolioFeeTaxAttributionCommandError,
    PortfolioFeeTaxAttributionCommandRepositoryPort,
    PortfolioFeeTaxAttributionCommandService,
)
from backend.engine.private.portfolio.fee_tax_attribution_persistence import (
    FeeTaxAttributionEventType,
    FeeTaxAttributionPersistenceError,
    FeeTaxAttributionPersistenceEvent,
    build_allocation_persistence_event,
)
from backend.engine.private.portfolio.models import (
    Portfolio,
    PortfolioAccount,
    PortfolioTransaction,
)


# ─────────────────────────────────────────────────────────────────────────────
# Strict Fake Test Repository for Command Port
# ─────────────────────────────────────────────────────────────────────────────

class StrictCommandTestRepository:
    def __init__(
        self,
        portfolios: Optional[Dict[UUID, Portfolio]] = None,
        transactions: Optional[Dict[UUID, List[PortfolioTransaction]]] = None,
        attribution_events: Optional[Dict[UUID, List[FeeTaxAttributionPersistenceEvent]]] = None,
    ) -> None:
        self.portfolios: Dict[UUID, Portfolio] = portfolios or {}
        self.transactions: Dict[UUID, List[PortfolioTransaction]] = transactions or {}
        self.attribution_events: Dict[UUID, List[FeeTaxAttributionPersistenceEvent]] = (
            attribution_events or {}
        )
        self.get_portfolio_calls: List[UUID] = []
        self.list_transactions_calls: List[UUID] = []
        self.list_fee_tax_attribution_events_calls: List[Tuple[UUID, Optional[UUID], Optional[datetime]]] = []
        self.append_calls: List[FeeTaxAttributionPersistenceEvent] = []
        self.append_override: Optional[Callable[[FeeTaxAttributionPersistenceEvent], FeeTaxAttributionPersistenceEvent]] = None

    def get_portfolio(self, portfolio_id: UUID) -> Optional[Portfolio]:
        self.get_portfolio_calls.append(portfolio_id)
        return self.portfolios.get(portfolio_id)

    def list_transactions(self, portfolio_id: UUID) -> Sequence[PortfolioTransaction]:
        self.list_transactions_calls.append(portfolio_id)
        return list(self.transactions.get(portfolio_id, []))

    def list_fee_tax_attribution_events(
        self,
        portfolio_id: UUID,
        account_id: Optional[UUID] = None,
        as_of_recorded_at: Optional[datetime] = None,
    ) -> Sequence[FeeTaxAttributionPersistenceEvent]:
        self.list_fee_tax_attribution_events_calls.append((portfolio_id, account_id, as_of_recorded_at))
        events = list(self.attribution_events.get(portfolio_id, []))
        if account_id is not None:
            events = [e for e in events if e.account_id == account_id]
        if as_of_recorded_at is not None:
            cutoff_utc = as_of_recorded_at.astimezone(timezone.utc)
            events = [e for e in events if e.recorded_at.astimezone(timezone.utc) <= cutoff_utc]
        return events

    def append_fee_tax_attribution_event(
        self,
        event: FeeTaxAttributionPersistenceEvent,
    ) -> FeeTaxAttributionPersistenceEvent:
        self.append_calls.append(event)
        if self.append_override is not None:
            return self.append_override(event)

        # In-memory storage & readback simulation
        p_events = self.attribution_events.setdefault(event.portfolio_id, [])
        p_events.append(event)
        return event


# ─────────────────────────────────────────────────────────────────────────────
# Helper Factories
# ─────────────────────────────────────────────────────────────────────────────

def _make_portfolio(portfolio_id: Optional[UUID] = None) -> Portfolio:
    p_id = portfolio_id or uuid4()
    return Portfolio(
        id=p_id,
        owner_id=uuid4(),
        name="Test Portfolio",
        base_currency=Currency.USD,
        mode=PortfolioMode.MY_PORTFOLIO,
        created_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
    )


def _make_tx(
    portfolio_id: UUID,
    account_id: UUID,
    tx_type: TransactionType,
    *,
    tx_id: Optional[UUID] = None,
    cash_amount: Optional[Decimal] = None,
    cash_currency: Optional[Currency] = None,
    quantity: Optional[Decimal] = None,
    unit_price: Optional[Decimal] = None,
    trade_currency: Optional[Currency] = None,
    effective_date: date = date(2026, 8, 29),
    recorded_at: Optional[datetime] = None,
    reverses_transaction_id: Optional[UUID] = None,
) -> PortfolioTransaction:
    is_trade = tx_type in (TransactionType.BUY, TransactionType.SELL)
    is_cash_tx = tx_type in (
        TransactionType.FEE,
        TransactionType.TAX_WITHHOLDING,
        TransactionType.DIVIDEND,
        TransactionType.INTEREST,
        TransactionType.CASH_DEPOSIT,
        TransactionType.CASH_WITHDRAWAL,
    )
    is_reversal = tx_type == TransactionType.REVERSAL
    return PortfolioTransaction(
        id=tx_id or uuid4(),
        portfolio_id=portfolio_id,
        account_id=account_id,
        transaction_type=tx_type,
        effective_date=effective_date,
        recorded_at=recorded_at or datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc),
        instrument_id="AAPL" if tx_type in (TransactionType.BUY, TransactionType.SELL, TransactionType.DIVIDEND) else None,
        quantity=quantity if is_trade else None,
        unit_price=unit_price if is_trade else None,
        trade_currency=(trade_currency or Currency.USD) if is_trade else None,
        cash_amount=cash_amount if is_cash_tx else None,
        cash_currency=(cash_currency or Currency.USD) if is_cash_tx else None,
        reverses_transaction_id=reverses_transaction_id if is_reversal else None,
    )



def _make_allocation_event(
    portfolio_id: UUID,
    account_id: UUID,
    charge_id: UUID,
    target_id: UUID,
    allocated_amount: Decimal = Decimal("6.000"),
    event_id: Optional[UUID] = None,
    recorded_at: Optional[datetime] = None,
) -> FeeTaxAttributionPersistenceEvent:
    return FeeTaxAttributionPersistenceEvent(
        id=event_id or uuid4(),
        portfolio_id=portfolio_id,
        account_id=account_id,
        event_type=FeeTaxAttributionEventType.ALLOCATION,
        charge_transaction_id=charge_id,
        target_transaction_id=target_id,
        allocated_amount=allocated_amount,
        reverses_attribution_event_id=None,
        recorded_at=recorded_at or datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test Suites
# ─────────────────────────────────────────────────────────────────────────────

class TestConstructorValidation:
    """Items 48, 49, 50: Constructor dependency validation."""

    def test_rejects_none_repository(self):
        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="repository must not be None"):
            PortfolioFeeTaxAttributionCommandService(None)  # type: ignore

    @pytest.mark.parametrize("missing_method", [
        "get_portfolio",
        "list_transactions",
        "list_fee_tax_attribution_events",
        "append_fee_tax_attribution_event",
    ])
    def test_rejects_missing_or_non_callable_repository_methods(self, missing_method: str):
        repo = StrictCommandTestRepository()
        setattr(repo, missing_method, None)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="repository must provide a callable"):
            PortfolioFeeTaxAttributionCommandService(repo)


    @pytest.mark.parametrize("bad_clock", [
        True,
        False,
        "clock",
        123,
        [],
    ])
    def test_rejects_invalid_clock_dependency(self, bad_clock: Any):
        repo = StrictCommandTestRepository()
        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="clock must be a callable"):
            PortfolioFeeTaxAttributionCommandService(repo, clock=bad_clock)

    @pytest.mark.parametrize("bad_factory", [
        True,
        False,
        "uuid",
        123,
        {},
    ])
    def test_rejects_invalid_event_id_factory_dependency(self, bad_factory: Any):
        repo = StrictCommandTestRepository()
        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="event_id_factory must be a callable"):
            PortfolioFeeTaxAttributionCommandService(repo, event_id_factory=bad_factory)


class TestPublicParameterStrictness:
    """Items 51, 52, 53, 54: Public argument type, finite, positive, and self-attribution strictness."""

    @pytest.mark.parametrize("bad_uuid", [
        None,
        True,
        False,
        "550e8400-e29b-41d4-a716-446655440000",
        123,
        b"\x00" * 16,
    ])
    def test_rejects_invalid_uuid_arguments(self, bad_uuid: Any):
        repo = StrictCommandTestRepository()
        service = PortfolioFeeTaxAttributionCommandService(repo)
        valid_u = uuid4()
        valid_amt = Decimal("10.000")

        # portfolio_id
        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="portfolio_id must be a non-bool UUID instance"):
            service.allocate(bad_uuid, valid_u, valid_u, valid_amt)

        # charge_transaction_id
        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="charge_transaction_id must be a non-bool UUID instance"):
            service.allocate(valid_u, bad_uuid, valid_u, valid_amt)

        # target_transaction_id
        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="target_transaction_id must be a non-bool UUID instance"):
            service.allocate(valid_u, valid_u, bad_uuid, valid_amt)

        assert len(repo.append_calls) == 0

    def test_rejects_self_attribution_immediately(self):
        repo = StrictCommandTestRepository()
        clock_calls = 0

        def counting_clock():
            nonlocal clock_calls
            clock_calls += 1
            return datetime.now(timezone.utc)

        service = PortfolioFeeTaxAttributionCommandService(repo, clock=counting_clock)
        same_id = uuid4()

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="Self-attribution rejected"):
            service.allocate(uuid4(), same_id, same_id, Decimal("5.000"))

        assert clock_calls == 0
        assert len(repo.append_calls) == 0

    @pytest.mark.parametrize("bad_amount", [
        None,
        True,
        False,
        6,
        6.0,
        "6.000",
    ])
    def test_rejects_non_decimal_allocated_amount(self, bad_amount: Any):
        repo = StrictCommandTestRepository()
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="allocated_amount must be a Decimal instance"):
            service.allocate(uuid4(), uuid4(), uuid4(), bad_amount)

        assert len(repo.append_calls) == 0

    @pytest.mark.parametrize("non_positive_or_nonfinite", [
        Decimal("0"),
        Decimal("0.000"),
        Decimal("-1"),
        Decimal("-0.001"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ])
    def test_rejects_non_positive_or_non_finite_decimal(self, non_positive_or_nonfinite: Decimal):
        repo = StrictCommandTestRepository()
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError):
            service.allocate(uuid4(), uuid4(), uuid4(), non_positive_or_nonfinite)

        assert len(repo.append_calls) == 0


class TestClockResolutionAndUTC:
    """Items 55, 56, 57: Clock invocation and UTC normalization."""

    def test_single_clock_call_per_command(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
        )

        clock_calls = 0
        fixed_time = datetime(2026, 8, 29, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))

        def test_clock():
            nonlocal clock_calls
            clock_calls += 1
            return fixed_time

        service = PortfolioFeeTaxAttributionCommandService(repo, clock=test_clock)
        persisted = service.allocate(p.id, fee_tx.id, buy_tx.id, Decimal("6.000"))

        assert clock_calls == 1
        expected_utc = fixed_time.astimezone(timezone.utc)
        assert persisted.recorded_at == expected_utc
        # Verify as-of cutoff passed to list_fee_tax_attribution_events
        assert repo.list_fee_tax_attribution_events_calls[0][2] == expected_utc

    @pytest.mark.parametrize("bad_clock_return", [
        None,
        True,
        False,
        "2026-08-29T12:00:00Z",
        1234567890,
        datetime(2026, 8, 29, 12, 0, 0),  # naive
    ])
    def test_invalid_clock_return_fails_closed(self, bad_clock_return: Any):
        repo = StrictCommandTestRepository()
        factory_calls = 0

        def counting_factory():
            nonlocal factory_calls
            factory_calls += 1
            return uuid4()

        service = PortfolioFeeTaxAttributionCommandService(
            repo,
            clock=lambda: bad_clock_return,
            event_id_factory=counting_factory,
        )

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="Clock"):
            service.allocate(uuid4(), uuid4(), uuid4(), Decimal("5.000"))

        assert factory_calls == 0
        assert len(repo.get_portfolio_calls) == 0
        assert len(repo.append_calls) == 0


class TestEventIdGeneration:
    """Items 58, 59: Event ID factory invocation and authority."""

    def test_single_factory_call_preserves_event_id(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
        )

        fixed_event_id = uuid4()
        factory_calls = 0

        def test_factory():
            nonlocal factory_calls
            factory_calls += 1
            return fixed_event_id

        service = PortfolioFeeTaxAttributionCommandService(repo, event_id_factory=test_factory)
        persisted = service.allocate(p.id, fee_tx.id, buy_tx.id, Decimal("6.000"))

        assert factory_calls == 1
        assert persisted.id == fixed_event_id
        assert repo.append_calls[0].id == fixed_event_id

    @pytest.mark.parametrize("bad_id_return", [
        None,
        True,
        False,
        "550e8400-e29b-41d4-a716-446655440000",
        123,
    ])
    def test_invalid_event_id_factory_return_fails_closed(self, bad_id_return: Any):
        repo = StrictCommandTestRepository()
        service = PortfolioFeeTaxAttributionCommandService(
            repo,
            event_id_factory=lambda: bad_id_return,
        )

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="Event ID factory"):
            service.allocate(uuid4(), uuid4(), uuid4(), Decimal("5.000"))

        assert len(repo.get_portfolio_calls) == 0
        assert len(repo.append_calls) == 0


class TestDomainScenariosAndPreflight:
    """Items 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71: Domain execution & preflight validations."""

    def test_empty_existing_attribution_state_success(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        persisted = service.allocate(p.id, fee_tx.id, buy_tx.id, Decimal("6.000"))

        assert persisted.event_type == FeeTaxAttributionEventType.ALLOCATION
        assert persisted.charge_transaction_id == fee_tx.id
        assert persisted.target_transaction_id == buy_tx.id
        assert persisted.allocated_amount.as_tuple() == Decimal("6.000").as_tuple()
        assert len(repo.append_calls) == 1

    def test_partial_existing_allocation_success(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))
        buy_x = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))
        buy_y = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))

        # Existing allocation: C -> X = 4.000
        existing_alloc = _make_allocation_event(p.id, a_id, fee_tx.id, buy_x.id, Decimal("4.000"), recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_x, buy_y]},
            attribution_events={p.id: [existing_alloc]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        persisted = service.allocate(p.id, fee_tx.id, buy_y.id, Decimal("6.000"))

        assert persisted.charge_transaction_id == fee_tx.id
        assert persisted.target_transaction_id == buy_y.id
        assert persisted.allocated_amount == Decimal("6.000")

    def test_cumulative_over_allocation_preflight_rejection(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))
        buy_x = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))
        buy_y = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))

        # Existing allocation: C -> X = 6.000
        existing_alloc = _make_allocation_event(p.id, a_id, fee_tx.id, buy_x.id, Decimal("6.000"), recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_x, buy_y]},
            attribution_events={p.id: [existing_alloc]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        # 6.000 + 5.000 = 11.000 > 10.000 charge -> Phase 14D FeeTaxAttributionError
        with pytest.raises(FeeTaxAttributionError, match="exceeds charge amount"):
            service.allocate(p.id, fee_tx.id, buy_y.id, Decimal("5.000"))

        assert len(repo.append_calls) == 0

    def test_active_duplicate_pair_preflight_rejection(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))
        buy_x = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))

        # Existing active: C -> X = 3.000
        existing_alloc = _make_allocation_event(p.id, a_id, fee_tx.id, buy_x.id, Decimal("3.000"), recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_x]},
            attribution_events={p.id: [existing_alloc]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        # Duplicate pair C -> X
        with pytest.raises(FeeTaxAttributionError, match="Duplicate attribution intent detected"):
            service.allocate(p.id, fee_tx.id, buy_x.id, Decimal("2.000"))

        assert len(repo.append_calls) == 0

    def test_invalid_charge_type_rejection(self):
        p = _make_portfolio()
        a_id = uuid4()
        buy_1 = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))
        buy_2 = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [buy_1, buy_2]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(FeeTaxAttributionError, match="not found in observed active charge events"):
            service.allocate(p.id, buy_1.id, buy_2.id, Decimal("5.000"))

        assert len(repo.append_calls) == 0

    def test_invalid_target_type_rejection(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_1 = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        fee_2 = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("20.000"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_1, fee_2]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(FeeTaxAttributionError, match="cannot be of type FEE"):
            service.allocate(p.id, fee_1.id, fee_2.id, Decimal("5.000"))

        assert len(repo.append_calls) == 0

    def test_inactive_charge_rejection(self):
        p = _make_portfolio()
        a_id = uuid4()
        t1 = datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)

        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=t1)
        buy_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=t1)
        fee_rev = _make_tx(p.id, a_id, TransactionType.REVERSAL, reverses_transaction_id=fee_tx.id, recorded_at=t2)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx, fee_rev]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(FeeTaxAttributionError, match="not found in observed active charge events"):
            service.allocate(p.id, fee_tx.id, buy_tx.id, Decimal("5.000"))

        assert len(repo.append_calls) == 0


    def test_inactive_target_rejection(self):
        p = _make_portfolio()
        a_id = uuid4()
        t1 = datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)

        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=t1)
        buy_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=t1)
        buy_rev = _make_tx(p.id, a_id, TransactionType.REVERSAL, reverses_transaction_id=buy_tx.id, recorded_at=t2)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx, buy_rev]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(FeeTaxAttributionError, match="not found in active transactions"):
            service.allocate(p.id, fee_tx.id, buy_tx.id, Decimal("5.000"))

        assert len(repo.append_calls) == 0

    def test_cross_account_rejection(self):
        p = _make_portfolio()
        a1_id = uuid4()
        a2_id = uuid4()

        fee_tx = _make_tx(p.id, a1_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = _make_tx(p.id, a2_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(FeeTaxAttributionError, match="Cross-account attribution"):
            service.allocate(p.id, fee_tx.id, buy_tx.id, Decimal("5.000"))

        assert len(repo.append_calls) == 0

    def test_large_exact_decimal_representation_preserved(self):
        p = _make_portfolio()
        a_id = uuid4()
        large_amt = Decimal("12345678901234567890.123400")
        charge_amt = Decimal("20000000000000000000.000000")

        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=charge_amt)
        buy_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        persisted = service.allocate(p.id, fee_tx.id, buy_tx.id, large_amt)
        assert persisted.allocated_amount.as_tuple() == large_amt.as_tuple()


class TestPersistenceAndReadbackAuthority:
    """Items 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85, 86, 87: Append and returned authority."""

    def test_append_called_once_and_no_post_write_requery(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        service.allocate(p.id, fee_tx.id, buy_tx.id, Decimal("6.000"))

        assert len(repo.append_calls) == 1
        assert len(repo.get_portfolio_calls) == 1
        assert len(repo.list_transactions_calls) == 1
        assert len(repo.list_fee_tax_attribution_events_calls) == 1

    def test_repository_append_error_propagates_unchanged(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
        )
        custom_err = RuntimeError("Simulated database connection crash")

        def exploding_append(e):
            raise custom_err

        repo.append_override = exploding_append
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(RuntimeError) as exc_info:
            service.allocate(p.id, fee_tx.id, buy_tx.id, Decimal("6.000"))

        assert exc_info.value is custom_err

    def test_database_concurrent_error_propagates_unchanged(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
        )
        trigger_err = ValueError("PostgreSQL trigger: Cumulative active allocation exceeds charge capacity")

        def trigger_append(e):
            raise trigger_err

        repo.append_override = trigger_append
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(ValueError) as exc_info:
            service.allocate(p.id, fee_tx.id, buy_tx.id, Decimal("6.000"))

        assert exc_info.value is trigger_err

    def test_returned_wrong_event_type_fails_closed(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
        )

        def malicious_append(e):
            return FeeTaxAttributionPersistenceEvent(
                id=e.id,
                portfolio_id=e.portfolio_id,
                account_id=e.account_id,
                event_type=FeeTaxAttributionEventType.REVERSAL,  # Wrong event type!
                charge_transaction_id=None,
                target_transaction_id=None,
                allocated_amount=None,
                reverses_attribution_event_id=uuid4(),
                recorded_at=e.recorded_at,
            )

        repo.append_override = malicious_append
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="Repository returned wrong event type"):
            service.allocate(p.id, fee_tx.id, buy_tx.id, Decimal("6.000"))

    def test_returned_mismatched_id_fails_closed(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
        )

        def malicious_append(e):
            return FeeTaxAttributionPersistenceEvent(
                id=uuid4(),  # Different ID!
                portfolio_id=e.portfolio_id,
                account_id=e.account_id,
                event_type=e.event_type,
                charge_transaction_id=e.charge_transaction_id,
                target_transaction_id=e.target_transaction_id,
                allocated_amount=e.allocated_amount,
                reverses_attribution_event_id=None,
                recorded_at=e.recorded_at,
            )

        repo.append_override = malicious_append
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="Repository returned mismatched event ID"):
            service.allocate(p.id, fee_tx.id, buy_tx.id, Decimal("6.000"))

    def test_returned_decimal_drift_fails_closed(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
        )

        def malicious_append(e):
            return FeeTaxAttributionPersistenceEvent(
                id=e.id,
                portfolio_id=e.portfolio_id,
                account_id=e.account_id,
                event_type=e.event_type,
                charge_transaction_id=e.charge_transaction_id,
                target_transaction_id=e.target_transaction_id,
                allocated_amount=Decimal("6"),  # Drifted precision!
                reverses_attribution_event_id=None,
                recorded_at=e.recorded_at,
            )

        repo.append_override = malicious_append
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="Persisted event economic contents do not match"):
            service.allocate(p.id, fee_tx.id, buy_tx.id, Decimal("6.000"))

    def test_returned_same_instant_different_offset_accepted(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
        )

        def offset_append(e):
            # Same instant in UTC+3
            rec_offset = e.recorded_at.astimezone(timezone(timedelta(hours=3)))
            return FeeTaxAttributionPersistenceEvent(
                id=e.id,
                portfolio_id=e.portfolio_id,
                account_id=e.account_id,
                event_type=e.event_type,
                charge_transaction_id=e.charge_transaction_id,
                target_transaction_id=e.target_transaction_id,
                allocated_amount=e.allocated_amount,
                reverses_attribution_event_id=None,
                recorded_at=rec_offset,
            )

        repo.append_override = offset_append
        service = PortfolioFeeTaxAttributionCommandService(repo)

        persisted = service.allocate(p.id, fee_tx.id, buy_tx.id, Decimal("6.000"))
        assert persisted.allocated_amount.as_tuple() == Decimal("6.000").as_tuple()


class TestStaticPurityAndInvariants:
    """Items 88, 89, 90, 91: Static purity and behavioral invariant assertions."""

    def test_public_methods_contain_no_owner_arguments(self):
        service_init = inspect.signature(PortfolioFeeTaxAttributionCommandService.__init__)
        service_allocate = inspect.signature(PortfolioFeeTaxAttributionCommandService.allocate)

        for sig in (service_init, service_allocate):
            for param in ("owner_id", "user_id", "auth_user_id", "service_role"):
                assert param not in sig.parameters

    def test_no_reversal_command_in_module(self):
        methods = dir(PortfolioFeeTaxAttributionCommandService)
        reversal_candidates = [m for m in methods if "reverse" in m or "cancel" in m or "undo" in m]
        assert reversal_candidates == []

    def test_static_source_code_purity(self):
        import backend.engine.private.portfolio.fee_tax_attribution_command as mod
        source = inspect.getsource(mod)

        prohibited = [
            ".table(",
            ".rpc(",
            "Supabase",
            "PostgREST",
            "float(",
            "round(",
            "quantize(",
            "hashlib",
            "sha256",
        ]
        for token in prohibited:
            assert token not in source, f"Found prohibited pattern {token!r} in production source!"
