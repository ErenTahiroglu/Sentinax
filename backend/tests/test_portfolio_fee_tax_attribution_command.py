"""
backend/tests/test_portfolio_fee_tax_attribution_command.py
===========================================================
Comprehensive unit tests for Phase 14M / 14M.1 / 14N:
Owner-Bound Explicit Fee/Tax Allocation & Reversal Command Service
with Retry-Safe Command Idempotency & First-Commit-Wins Authority.
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
    _allocation_event_matches_command,
    _reversal_event_matches_command,
)
from backend.engine.private.portfolio.fee_tax_attribution_persistence import (
    FeeTaxAttributionEventType,
    FeeTaxAttributionPersistenceError,
    FeeTaxAttributionPersistenceEvent,
    build_allocation_persistence_event,
    build_attribution_reversal_persistence_event,
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
        self.get_event_calls: List[Tuple[UUID, UUID]] = []
        self.append_calls: List[FeeTaxAttributionPersistenceEvent] = []
        self.append_override: Optional[Callable[[FeeTaxAttributionPersistenceEvent], FeeTaxAttributionPersistenceEvent]] = None
        self.get_event_override: Optional[Callable[[UUID, UUID], Optional[FeeTaxAttributionPersistenceEvent]]] = None

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

    def get_fee_tax_attribution_event(
        self,
        portfolio_id: UUID,
        event_id: UUID,
    ) -> Optional[FeeTaxAttributionPersistenceEvent]:
        self.get_event_calls.append((portfolio_id, event_id))
        if self.get_event_override is not None:
            return self.get_event_override(portfolio_id, event_id)
        for e in self.attribution_events.get(portfolio_id, []):
            if e.id == event_id:
                return e
        return None

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


def _make_reversal_event(
    portfolio_id: UUID,
    account_id: UUID,
    reverses_event_id: UUID,
    event_id: Optional[UUID] = None,
    recorded_at: Optional[datetime] = None,
) -> FeeTaxAttributionPersistenceEvent:
    return FeeTaxAttributionPersistenceEvent(
        id=event_id or uuid4(),
        portfolio_id=portfolio_id,
        account_id=account_id,
        event_type=FeeTaxAttributionEventType.REVERSAL,
        charge_transaction_id=None,
        target_transaction_id=None,
        allocated_amount=None,
        reverses_attribution_event_id=reverses_event_id,
        recorded_at=recorded_at or datetime(2026, 8, 29, 11, 30, 0, tzinfo=timezone.utc),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test Suites
# ─────────────────────────────────────────────────────────────────────────────

class TestConstructorValidation:
    """Constructor dependency validation (Phase 14M / 14M.1 / 14N)."""

    def test_rejects_none_repository(self):
        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="repository must not be None"):
            PortfolioFeeTaxAttributionCommandService(None)  # type: ignore

    @pytest.mark.parametrize("missing_method", [
        "get_portfolio",
        "list_transactions",
        "list_fee_tax_attribution_events",
        "get_fee_tax_attribution_event",
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

    def test_no_event_id_factory_in_init_signature(self):
        sig = inspect.signature(PortfolioFeeTaxAttributionCommandService.__init__)
        assert "event_id_factory" not in sig.parameters
        assert set(sig.parameters.keys()) == {"self", "repository", "clock"}


class TestPublicParameterStrictness:
    """Public parameter strictness & validation ordering."""

    def test_allocate_signature_exact_parameters(self):
        sig = inspect.signature(PortfolioFeeTaxAttributionCommandService.allocate)
        params = list(sig.parameters.keys())
        assert params == [
            "self",
            "command_id",
            "portfolio_id",
            "charge_transaction_id",
            "target_transaction_id",
            "allocated_amount",
        ]

    def test_reverse_allocation_signature_exact_parameters(self):
        sig = inspect.signature(PortfolioFeeTaxAttributionCommandService.reverse_allocation)
        params = list(sig.parameters.keys())
        assert params == [
            "self",
            "command_id",
            "portfolio_id",
            "allocation_event_id",
        ]

    @pytest.mark.parametrize("bad_id", [
        None,
        True,
        False,
        "550e8400-e29b-41d4-a716-446655440000",
        123,
        b"\x00" * 16,
    ])
    def test_rejects_invalid_command_id_in_allocate(self, bad_id: Any):
        repo = StrictCommandTestRepository()
        service = PortfolioFeeTaxAttributionCommandService(repo)
        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="command_id must be a non-bool UUID instance"):
            service.allocate(bad_id, uuid4(), uuid4(), uuid4(), Decimal("10.000"))

        assert len(repo.get_portfolio_calls) == 0
        assert len(repo.get_event_calls) == 0
        assert len(repo.append_calls) == 0

    @pytest.mark.parametrize("bad_id", [
        None,
        True,
        False,
        "550e8400-e29b-41d4-a716-446655440000",
        123,
        b"\x00" * 16,
    ])
    def test_rejects_invalid_command_id_in_reverse_allocation(self, bad_id: Any):
        repo = StrictCommandTestRepository()
        service = PortfolioFeeTaxAttributionCommandService(repo)
        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="command_id must be a non-bool UUID instance"):
            service.reverse_allocation(bad_id, uuid4(), uuid4())

        assert len(repo.get_portfolio_calls) == 0
        assert len(repo.get_event_calls) == 0
        assert len(repo.append_calls) == 0

    @pytest.mark.parametrize("field_idx,field_name", [
        (1, "portfolio_id"),
        (2, "charge_transaction_id"),
        (3, "target_transaction_id"),
    ])
    @pytest.mark.parametrize("bad_id", [
        None,
        True,
        False,
        "550e8400-e29b-41d4-a716-446655440000",
        123,
        b"\x00" * 16,
    ])
    def test_rejects_invalid_uuid_arguments_in_allocate(self, field_idx: int, field_name: str, bad_id: Any):
        repo = StrictCommandTestRepository()
        service = PortfolioFeeTaxAttributionCommandService(repo)
        args = [uuid4(), uuid4(), uuid4(), uuid4()]
        args[field_idx] = bad_id

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match=f"{field_name} must be a non-bool UUID instance"):
            service.allocate(args[0], args[1], args[2], args[3], Decimal("10.000"))

        assert len(repo.get_portfolio_calls) == 0
        assert len(repo.get_event_calls) == 0
        assert len(repo.append_calls) == 0

    @pytest.mark.parametrize("field_idx,field_name", [
        (1, "portfolio_id"),
        (2, "allocation_event_id"),
    ])
    @pytest.mark.parametrize("bad_id", [
        None,
        True,
        False,
        "550e8400-e29b-41d4-a716-446655440000",
        123,
        b"\x00" * 16,
    ])
    def test_rejects_invalid_uuid_arguments_in_reverse_allocation(self, field_idx: int, field_name: str, bad_id: Any):
        repo = StrictCommandTestRepository()
        service = PortfolioFeeTaxAttributionCommandService(repo)
        args = [uuid4(), uuid4(), uuid4()]
        args[field_idx] = bad_id

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match=f"{field_name} must be a non-bool UUID instance"):
            service.reverse_allocation(args[0], args[1], args[2])

        assert len(repo.get_portfolio_calls) == 0
        assert len(repo.get_event_calls) == 0
        assert len(repo.append_calls) == 0

    def test_rejects_self_attribution_immediately(self):
        repo = StrictCommandTestRepository()
        service = PortfolioFeeTaxAttributionCommandService(repo)
        same_id = uuid4()

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="Self-attribution rejected"):
            service.allocate(uuid4(), uuid4(), same_id, same_id, Decimal("10.000"))

        assert len(repo.get_portfolio_calls) == 0
        assert len(repo.get_event_calls) == 0
        assert len(repo.append_calls) == 0

    def test_rejects_self_reversal_immediately(self):
        repo = StrictCommandTestRepository()
        service = PortfolioFeeTaxAttributionCommandService(repo)
        same_id = uuid4()

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="Self-reversal rejected"):
            service.reverse_allocation(same_id, uuid4(), same_id)

        assert len(repo.get_portfolio_calls) == 0
        assert len(repo.get_event_calls) == 0
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
            service.allocate(uuid4(), uuid4(), uuid4(), uuid4(), bad_amount)  # type: ignore

        assert len(repo.get_portfolio_calls) == 0
        assert len(repo.get_event_calls) == 0
        assert len(repo.append_calls) == 0

    @pytest.mark.parametrize("non_positive_or_nonfinite", [
        Decimal("0"),
        Decimal("-0.001"),
        Decimal("-10.000"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        Decimal("sNaN"),
    ])
    def test_rejects_non_positive_or_non_finite_decimal(self, non_positive_or_nonfinite: Decimal):
        repo = StrictCommandTestRepository()
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="allocated_amount must be"):
            service.allocate(uuid4(), uuid4(), uuid4(), uuid4(), non_positive_or_nonfinite)

        assert len(repo.get_portfolio_calls) == 0
        assert len(repo.get_event_calls) == 0
        assert len(repo.append_calls) == 0


class TestPureIdempotencyMatchingHelper:
    """Contract tests for _allocation_event_matches_command and _reversal_event_matches_command."""

    def test_allocation_exact_match_success(self):
        cmd_id = uuid4()
        p_id = uuid4()
        c_id = uuid4()
        t_id = uuid4()
        amount = Decimal("6.000")
        event = _make_allocation_event(p_id, uuid4(), c_id, t_id, amount, event_id=cmd_id)

        assert _allocation_event_matches_command(
            event,
            command_id=cmd_id,
            portfolio_id=p_id,
            charge_transaction_id=c_id,
            target_transaction_id=t_id,
            allocated_amount=amount,
        ) is True

    def test_reversal_exact_match_success(self):
        cmd_id = uuid4()
        p_id = uuid4()
        alloc_id = uuid4()
        event = _make_reversal_event(p_id, uuid4(), alloc_id, event_id=cmd_id)

        assert _reversal_event_matches_command(
            event,
            command_id=cmd_id,
            portfolio_id=p_id,
            allocation_event_id=alloc_id,
        ) is True

    def test_allocation_decimal_representation_drift_fails_match(self):
        cmd_id = uuid4()
        p_id = uuid4()
        c_id = uuid4()
        t_id = uuid4()
        event = _make_allocation_event(p_id, uuid4(), c_id, t_id, Decimal("6.000"), event_id=cmd_id)

        assert _allocation_event_matches_command(
            event,
            command_id=cmd_id,
            portfolio_id=p_id,
            charge_transaction_id=c_id,
            target_transaction_id=t_id,
            allocated_amount=Decimal("6"),
        ) is False

    def test_reversal_helper_rejects_allocation_event(self):
        cmd_id = uuid4()
        p_id = uuid4()
        event = _make_allocation_event(p_id, uuid4(), uuid4(), uuid4(), Decimal("6.000"), event_id=cmd_id)
        assert _reversal_event_matches_command(
            event,
            command_id=cmd_id,
            portfolio_id=p_id,
            allocation_event_id=uuid4(),
        ) is False

    def test_allocation_helper_rejects_reversal_event(self):
        cmd_id = uuid4()
        p_id = uuid4()
        event = _make_reversal_event(p_id, uuid4(), uuid4(), event_id=cmd_id)
        assert _allocation_event_matches_command(
            event,
            command_id=cmd_id,
            portfolio_id=p_id,
            charge_transaction_id=uuid4(),
            target_transaction_id=uuid4(),
            allocated_amount=Decimal("6.000"),
        ) is False


class TestSequentialExactRetryIdempotency:
    """Sequential exact retry and first-commit-wins behavior for allocate and reverse_allocation."""

    def test_allocation_sequential_replay(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("10"), unit_price=Decimal("150.000"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
        )

        t1 = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
        clock_calls = [t1]
        service = PortfolioFeeTaxAttributionCommandService(repo, clock=lambda: clock_calls.pop(0))

        cmd_id = uuid4()
        amount = Decimal("6.000")

        # 1. First execution
        first_event = service.allocate(cmd_id, p.id, c_tx.id, t_tx.id, amount)
        assert first_event.id == cmd_id
        assert first_event.recorded_at == t1
        assert len(repo.append_calls) == 1
        assert len(repo.get_portfolio_calls) == 1

        # 2. Sequential retry with exact same command
        retry_event = service.allocate(cmd_id, p.id, c_tx.id, t_tx.id, amount)

        assert retry_event == first_event
        assert retry_event.recorded_at == t1
        assert len(repo.append_calls) == 1
        assert len(repo.get_portfolio_calls) == 1
        assert len(repo.list_transactions_calls) == 1
        assert len(repo.list_fee_tax_attribution_events_calls) == 1
        assert len(repo.get_event_calls) == 2

    def test_reversal_sequential_replay(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("10"), unit_price=Decimal("150.000"))

        alloc_id = uuid4()
        t0 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id, recorded_at=t0)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event]},
        )

        t1 = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
        clock_calls = [t1]
        service = PortfolioFeeTaxAttributionCommandService(repo, clock=lambda: clock_calls.pop(0))

        cmd_id = uuid4()

        # 1. First execution
        first_reversal = service.reverse_allocation(cmd_id, p.id, alloc_id)
        assert first_reversal.id == cmd_id
        assert first_reversal.event_type == FeeTaxAttributionEventType.REVERSAL
        assert first_reversal.reverses_attribution_event_id == alloc_id
        assert first_reversal.recorded_at == t1
        assert len(repo.append_calls) == 1

        # 2. Sequential retry
        retry_reversal = service.reverse_allocation(cmd_id, p.id, alloc_id)
        assert retry_reversal == first_reversal
        assert retry_reversal.recorded_at == t1
        assert len(repo.append_calls) == 1
        assert len(repo.get_portfolio_calls) == 1
        assert len(repo.list_transactions_calls) == 1
        assert len(repo.get_event_calls) == 2

    def test_reversal_pre_read_occurs_before_clock(self):
        p = _make_portfolio()
        cmd_id = uuid4()
        alloc_id = uuid4()
        t1 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        existing = _make_reversal_event(p.id, uuid4(), alloc_id, event_id=cmd_id, recorded_at=t1)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            attribution_events={p.id: [existing]},
        )

        def exploding_clock():
            raise AssertionError("Clock must not be called during idempotent replay!")

        service = PortfolioFeeTaxAttributionCommandService(repo, clock=exploding_clock)
        replayed = service.reverse_allocation(cmd_id, p.id, alloc_id)
        assert replayed == existing
        assert replayed.recorded_at == t1


class TestRetryAfterReversals:
    """Replay after subsequent attribution/ledger reversals."""

    def test_allocation_retry_after_attribution_reversal_returns_original_event(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("10"), unit_price=Decimal("150.000"))

        cmd_id = uuid4()
        t1 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=cmd_id, recorded_at=t1)
        t2 = datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc)
        rev_event = _make_reversal_event(p.id, a_id, cmd_id, recorded_at=t2)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event, rev_event]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        replayed = service.allocate(cmd_id, p.id, c_tx.id, t_tx.id, Decimal("6.000"))
        assert replayed == alloc_event
        assert replayed.recorded_at == t1
        assert len(repo.append_calls) == 0

    def test_reversal_retry_after_subsequent_ledger_state_changes(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("10"), unit_price=Decimal("150.000"))

        alloc_id = uuid4()
        cmd_id = uuid4()
        t1 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        rev_event = _make_reversal_event(p.id, a_id, alloc_id, event_id=cmd_id, recorded_at=t1)

        # Later ledger change
        later_tx = _make_tx(p.id, a_id, TransactionType.CASH_DEPOSIT, cash_amount=Decimal("500.000"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx, later_tx]},
            attribution_events={p.id: [rev_event]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        replayed = service.reverse_allocation(cmd_id, p.id, alloc_id)
        assert replayed == rev_event
        assert replayed.recorded_at == t1
        assert len(repo.append_calls) == 0
        assert len(repo.get_portfolio_calls) == 0


class TestConflictingCommandIdReuse:
    """Same command ID with different semantics fails closed."""

    def test_reversal_command_id_points_to_allocation_fails_closed(self):
        p = _make_portfolio()
        cmd_id = uuid4()
        c_id = uuid4()
        t_id = uuid4()
        existing_alloc = _make_allocation_event(p.id, uuid4(), c_id, t_id, Decimal("6.000"), event_id=cmd_id)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            attribution_events={p.id: [existing_alloc]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="Command ID conflict"):
            service.reverse_allocation(cmd_id, p.id, uuid4())

        assert len(repo.append_calls) == 0

    def test_reversal_command_id_different_target_fails_closed(self):
        p = _make_portfolio()
        cmd_id = uuid4()
        alloc1_id = uuid4()
        alloc2_id = uuid4()
        existing_rev = _make_reversal_event(p.id, uuid4(), alloc1_id, event_id=cmd_id)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            attribution_events={p.id: [existing_rev]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="Command ID conflict"):
            service.reverse_allocation(cmd_id, p.id, alloc2_id)

        assert len(repo.append_calls) == 0


class TestReversalDomainScenariosAndPreflight:
    """Phase 14N domain preflight and execution scenarios for reverse_allocation."""

    def test_active_allocation_reversal_success(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))

        alloc_id = uuid4()
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id, recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event]},
        )

        t_rev = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
        service = PortfolioFeeTaxAttributionCommandService(repo, clock=lambda: t_rev)

        cmd_id = uuid4()
        rev = service.reverse_allocation(cmd_id, p.id, alloc_id)

        assert rev.id == cmd_id
        assert rev.portfolio_id == p.id
        assert rev.account_id == a_id
        assert rev.event_type == FeeTaxAttributionEventType.REVERSAL
        assert rev.charge_transaction_id is None
        assert rev.target_transaction_id is None
        assert rev.allocated_amount is None
        assert rev.reverses_attribution_event_id == alloc_id
        assert rev.recorded_at == t_rev

    def test_unknown_allocation_event_rejected(self):
        p = _make_portfolio()
        repo = StrictCommandTestRepository(portfolios={p.id: p})
        service = PortfolioFeeTaxAttributionCommandService(repo)

        missing_alloc_id = uuid4()
        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="not found in persisted attribution history"):
            service.reverse_allocation(uuid4(), p.id, missing_alloc_id)

        assert len(repo.append_calls) == 0

    def test_reversal_of_reversal_rejected(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))

        alloc_id = uuid4()
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id, recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))
        rev_id = uuid4()
        rev_event = _make_reversal_event(p.id, a_id, alloc_id, event_id=rev_id, recorded_at=datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event, rev_event]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        # Attempt to reverse the reversal event
        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="not found in persisted attribution history"):
            service.reverse_allocation(uuid4(), p.id, rev_id)

        assert len(repo.append_calls) == 0

    def test_future_allocation_rejected_at_pit(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))

        alloc_id = uuid4()
        # Allocation recorded at 15:00
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id, recorded_at=datetime(2026, 8, 29, 15, 0, 0, tzinfo=timezone.utc))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event]},
        )
        # Reversal command clock at 12:00 (before allocation)
        service = PortfolioFeeTaxAttributionCommandService(repo, clock=lambda: datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc))

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="not found in persisted attribution history"):
            service.reverse_allocation(uuid4(), p.id, alloc_id)

        assert len(repo.append_calls) == 0

    def test_already_reversed_by_another_command_fails_before_append(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))

        alloc_id = uuid4()
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id, recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))
        existing_rev_id = uuid4()
        existing_rev = _make_reversal_event(p.id, a_id, alloc_id, event_id=existing_rev_id, recorded_at=datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event, existing_rev]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        # New reversal command R2 attempting to reverse A1
        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="is not active at PIT cutoff"):
            service.reverse_allocation(uuid4(), p.id, alloc_id)

        assert len(repo.append_calls) == 0

    def test_multi_allocation_active_index_correspondence(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_1 = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))
        buy_1 = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))
        buy_2 = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))

        alloc_1 = _make_allocation_event(p.id, a_id, fee_1.id, buy_1.id, Decimal("3.000"), recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))
        alloc_2 = _make_allocation_event(p.id, a_id, fee_1.id, buy_2.id, Decimal("4.000"), recorded_at=datetime(2026, 8, 29, 10, 1, 0, tzinfo=timezone.utc))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_1, buy_1, buy_2]},
            attribution_events={p.id: [alloc_1, alloc_2]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        cmd_id = uuid4()
        # Reverse second allocation alloc_2
        rev = service.reverse_allocation(cmd_id, p.id, alloc_2.id)
        assert rev.id == cmd_id
        assert rev.reverses_attribution_event_id == alloc_2.id


class TestReversalConcurrentRaceRecovery:
    """Phase 14N concurrent race recovery for reverse_allocation."""

    def test_concurrent_same_reversal_pk_conflict_recovery(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))

        alloc_id = uuid4()
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id, recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))

        cmd_id = uuid4()
        t1 = datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 29, 11, 0, 1, tzinfo=timezone.utc)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event]},
        )

        committed_by_tx1 = _make_reversal_event(p.id, a_id, alloc_id, event_id=cmd_id, recorded_at=t1)

        def concurrent_append_conflict(event: FeeTaxAttributionPersistenceEvent):
            repo.attribution_events.setdefault(p.id, []).append(committed_by_tx1)
            raise RuntimeError("duplicate key value violates unique constraint 23505")

        repo.append_override = concurrent_append_conflict
        service = PortfolioFeeTaxAttributionCommandService(repo, clock=lambda: t2)

        result = service.reverse_allocation(cmd_id, p.id, alloc_id)
        assert result == committed_by_tx1
        assert result.recorded_at == t1

    def test_concurrent_same_reversal_single_reversal_trigger_recovery(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))

        alloc_id = uuid4()
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id, recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))

        cmd_id = uuid4()
        t1 = datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 29, 11, 0, 2, tzinfo=timezone.utc)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event]},
        )

        committed_by_tx1 = _make_reversal_event(p.id, a_id, alloc_id, event_id=cmd_id, recorded_at=t1)

        def concurrent_trigger_conflict(event: FeeTaxAttributionPersistenceEvent):
            repo.attribution_events.setdefault(p.id, []).append(committed_by_tx1)
            raise RuntimeError("uq_fee_tax_attribution_single_reversal violation")

        repo.append_override = concurrent_trigger_conflict
        service = PortfolioFeeTaxAttributionCommandService(repo, clock=lambda: t2)

        result = service.reverse_allocation(cmd_id, p.id, alloc_id)
        assert result == committed_by_tx1
        assert result.recorded_at == t1

    def test_different_command_ids_race_fails_closed(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))

        alloc_id = uuid4()
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id, recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))

        c1_id = uuid4()
        c2_id = uuid4()
        t1 = datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event]},
        )

        committed_c1 = _make_reversal_event(p.id, a_id, alloc_id, event_id=c1_id, recorded_at=t1)
        db_single_rev_exc = RuntimeError("uq_fee_tax_attribution_single_reversal")

        def append_loser_c2(event: FeeTaxAttributionPersistenceEvent):
            repo.attribution_events.setdefault(p.id, []).append(committed_c1)
            raise db_single_rev_exc

        repo.append_override = append_loser_c2
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(RuntimeError) as exc_info:
            service.reverse_allocation(c2_id, p.id, alloc_id)

        assert exc_info.value is db_single_rev_exc


class TestReversalPersistenceAndReadbackAuthority:
    """Readback validation and fail-closed defenses for reverse_allocation."""

    def test_reversal_returned_wrong_event_type_fails_closed(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))
        alloc_id = uuid4()
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event]},
        )

        def bad_append(event: FeeTaxAttributionPersistenceEvent) -> Any:
            return _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=event.id)

        repo.append_override = bad_append
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="wrong event type"):
            service.reverse_allocation(uuid4(), p.id, alloc_id)

    def test_reversal_returned_mismatched_target_fails_closed(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))
        alloc_id = uuid4()
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event]},
        )

        def bad_append(event: FeeTaxAttributionPersistenceEvent) -> Any:
            mutated = deepcopy(event)
            object.__setattr__(mutated, "reverses_attribution_event_id", uuid4())
            return mutated

        repo.append_override = bad_append
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="contents do not match"):
            service.reverse_allocation(uuid4(), p.id, alloc_id)

    def test_reversal_returned_wrong_id_fails_closed(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))
        alloc_id = uuid4()
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event]},
        )

        def bad_append(event: FeeTaxAttributionPersistenceEvent) -> Any:
            mutated = deepcopy(event)
            object.__setattr__(mutated, "id", uuid4())
            return mutated

        repo.append_override = bad_append
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="mismatched event ID"):
            service.reverse_allocation(uuid4(), p.id, alloc_id)

    def test_reversal_returned_account_mismatch_fails_closed(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))
        alloc_id = uuid4()
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event]},
        )

        def bad_append(event: FeeTaxAttributionPersistenceEvent) -> Any:
            mutated = deepcopy(event)
            object.__setattr__(mutated, "account_id", uuid4())
            return mutated

        repo.append_override = bad_append
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="contents do not match"):
            service.reverse_allocation(uuid4(), p.id, alloc_id)

    def test_reversal_returned_same_instant_different_offset_accepted(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))
        alloc_id = uuid4()
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event]},
        )

        def timezone_shift_append(event: FeeTaxAttributionPersistenceEvent) -> Any:
            mutated = deepcopy(event)
            shifted = event.recorded_at.astimezone(timezone(timedelta(hours=3)))
            object.__setattr__(mutated, "recorded_at", shifted)
            return mutated

        repo.append_override = timezone_shift_append
        service = PortfolioFeeTaxAttributionCommandService(repo)

        event = service.reverse_allocation(uuid4(), p.id, alloc_id)
        assert event is not None

    def test_reversal_returned_different_physical_instant_fails_closed(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))
        alloc_id = uuid4()
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event]},
        )

        def bad_append(event: FeeTaxAttributionPersistenceEvent) -> Any:
            mutated = deepcopy(event)
            drifted = event.recorded_at + timedelta(seconds=1)
            object.__setattr__(mutated, "recorded_at", drifted)
            return mutated

        repo.append_override = bad_append
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="physical timestamp does not match"):
            service.reverse_allocation(uuid4(), p.id, alloc_id)

    def test_reversal_returned_nonnull_economic_fields_fails_closed(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))
        alloc_id = uuid4()
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event]},
        )

        def bad_append(event: FeeTaxAttributionPersistenceEvent) -> Any:
            mutated = deepcopy(event)
            object.__setattr__(mutated, "allocated_amount", Decimal("1.000"))
            return mutated

        repo.append_override = bad_append
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="contents do not match"):
            service.reverse_allocation(uuid4(), p.id, alloc_id)

    def test_reversal_returned_portfolio_mismatch_fails_closed(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))
        alloc_id = uuid4()
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event]},
        )

        def bad_append(event: FeeTaxAttributionPersistenceEvent) -> Any:
            mutated = deepcopy(event)
            object.__setattr__(mutated, "portfolio_id", uuid4())
            return mutated

        repo.append_override = bad_append
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="contents do not match"):
            service.reverse_allocation(uuid4(), p.id, alloc_id)

    def test_reversal_append_called_once_and_no_recovery_get_on_success(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))
        alloc_id = uuid4()
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        cmd_id = uuid4()
        rev = service.reverse_allocation(cmd_id, p.id, alloc_id)
        assert rev is not None

        # Call counts: pre-read = 1, append = 1
        assert len(repo.get_event_calls) == 1
        assert len(repo.append_calls) == 1
        assert len(repo.get_portfolio_calls) == 1

    def test_reversal_clock_utc_normalization(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))
        alloc_id = uuid4()
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event]},
        )

        t_custom = datetime(2026, 8, 29, 15, 30, 0, tzinfo=timezone(timedelta(hours=3)))
        t_utc = t_custom.astimezone(timezone.utc)
        service = PortfolioFeeTaxAttributionCommandService(repo, clock=lambda: t_custom)

        cmd_id = uuid4()
        rev = service.reverse_allocation(cmd_id, p.id, alloc_id)
        assert rev.recorded_at == t_utc

    def test_reversal_active_ledger_charge_required_indirectly(self):
        from backend.engine.private.portfolio.fee_tax_attribution_binding import FeeTaxAttributionBindingError
        p = _make_portfolio()
        a_id = uuid4()
        t1 = datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        t3 = datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc)

        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=t1)
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=t1)
        alloc_id = uuid4()
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id, recorded_at=t2)
        # Charge reversed on ledger at t3
        charge_rev = _make_tx(p.id, a_id, TransactionType.REVERSAL, reverses_transaction_id=c_tx.id, recorded_at=t3)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx, charge_rev]},
            attribution_events={p.id: [alloc_event]},
        )
        # Reversal command at t3+1 hour
        service = PortfolioFeeTaxAttributionCommandService(repo, clock=lambda: datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc))

        with pytest.raises(FeeTaxAttributionBindingError, match="is not an active FEE or TAX_WITHHOLDING at PIT"):
            service.reverse_allocation(uuid4(), p.id, alloc_id)

        assert len(repo.append_calls) == 0

    def test_reversal_active_ledger_target_required_indirectly(self):
        from backend.engine.private.portfolio.fee_tax_attribution_binding import FeeTaxAttributionBindingError
        p = _make_portfolio()
        a_id = uuid4()
        t1 = datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        t3 = datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc)

        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=t1)
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=t1)
        alloc_id = uuid4()
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id, recorded_at=t2)
        # Target reversed on ledger at t3
        target_rev = _make_tx(p.id, a_id, TransactionType.REVERSAL, reverses_transaction_id=t_tx.id, recorded_at=t3)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx, target_rev]},
            attribution_events={p.id: [alloc_event]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo, clock=lambda: datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc))

        with pytest.raises(FeeTaxAttributionBindingError, match="is not an active transaction at PIT"):
            service.reverse_allocation(uuid4(), p.id, alloc_id)

        assert len(repo.append_calls) == 0

    def test_reversal_append_error_with_no_command_event_propagates(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))
        alloc_id = uuid4()
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event]},
        )

        sentinel_exc = ConnectionError("PostgreSQL write failed")

        def bad_append(event: FeeTaxAttributionPersistenceEvent):
            raise sentinel_exc

        repo.append_override = bad_append
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(ConnectionError) as exc_info:
            service.reverse_allocation(uuid4(), p.id, alloc_id)

        assert exc_info.value is sentinel_exc

    def test_reversal_recovery_get_failure_propagates_original_error(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))
        alloc_id = uuid4()
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event]},
        )

        orig_exc = RuntimeError("Append error")
        get_exc = RuntimeError("Secondary get error")

        def bad_append(event: FeeTaxAttributionPersistenceEvent):
            raise orig_exc

        def bad_get(p_id: UUID, e_id: UUID):
            raise get_exc

        repo.append_override = bad_append
        repo.get_event_override = lambda p_id, e_id: None if len(repo.append_calls) == 0 else bad_get(p_id, e_id)
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(RuntimeError) as exc_info:
            service.reverse_allocation(uuid4(), p.id, alloc_id)

        assert exc_info.value is orig_exc




class TestStaticPurityAndInvariants:
    """Static inspection of module source code for Phase 14M / 14M.1 / 14N purity invariants."""

    def test_public_methods_contain_no_owner_arguments(self):
        for method_name in ("allocate", "reverse_allocation"):
            method = getattr(PortfolioFeeTaxAttributionCommandService, method_name)
            sig = inspect.signature(method)
            assert "owner_id" not in sig.parameters
            assert "user_id" not in sig.parameters

    def test_zero_uuid_generation_in_command_module(self):
        import backend.engine.private.portfolio.fee_tax_attribution_command as mod
        src = inspect.getsource(mod)
        assert "uuid4(" not in src
        assert "uuid5(" not in src
        assert "event_id_factory" not in src

    def test_command_id_passed_as_event_id_in_builders(self):
        import backend.engine.private.portfolio.fee_tax_attribution_command as mod
        src = inspect.getsource(mod)
        assert "event_id=cmd_id" in src or "event_id = cmd_id" in src

    def test_no_manual_reversal_instantiation(self):
        import backend.engine.private.portfolio.fee_tax_attribution_command as mod
        src = inspect.getsource(mod)
        assert "build_attribution_reversal_persistence_event(" in src

    def test_static_source_code_purity(self):
        import backend.engine.private.portfolio.fee_tax_attribution_command as mod
        src = inspect.getsource(mod)
        assert ".table(" not in src
        assert ".rpc(" not in src
        assert "from supabase" not in src
        assert "import postgrest" not in src
        assert "postgrest" not in src.lower()
        assert "calc_tax" not in src
        assert "cost_basis" not in src
        assert "fx_rate" not in src
