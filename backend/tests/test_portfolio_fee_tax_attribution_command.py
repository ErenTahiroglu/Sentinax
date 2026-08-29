"""
backend/tests/test_portfolio_fee_tax_attribution_command.py
===========================================================
Comprehensive unit tests for Phase 14M / 14M.1: Owner-Bound Explicit Fee/Tax Allocation Command Service
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
    """Constructor dependency validation (Phase 14M / 14M.1)."""

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

    @pytest.mark.parametrize("bad_id", [
        None,
        True,
        False,
        "550e8400-e29b-41d4-a716-446655440000",
        123,
        b"\x00" * 16,
    ])
    def test_rejects_invalid_command_id(self, bad_id: Any):
        repo = StrictCommandTestRepository()
        service = PortfolioFeeTaxAttributionCommandService(repo)
        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="command_id must be a non-bool UUID instance"):
            service.allocate(bad_id, uuid4(), uuid4(), uuid4(), Decimal("10.000"))

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
    def test_rejects_invalid_uuid_arguments(self, field_idx: int, field_name: str, bad_id: Any):
        repo = StrictCommandTestRepository()
        service = PortfolioFeeTaxAttributionCommandService(repo)
        args = [uuid4(), uuid4(), uuid4(), uuid4()]
        args[field_idx] = bad_id

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match=f"{field_name} must be a non-bool UUID instance"):
            service.allocate(args[0], args[1], args[2], args[3], Decimal("10.000"))

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
    """Item 12, 13, 67: Contract tests for _allocation_event_matches_command."""

    def test_exact_match_success(self):
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

    def test_decimal_representation_drift_fails_match(self):
        cmd_id = uuid4()
        p_id = uuid4()
        c_id = uuid4()
        t_id = uuid4()
        event = _make_allocation_event(p_id, uuid4(), c_id, t_id, Decimal("6.000"), event_id=cmd_id)

        # Decimal("6") is numerically equal but has different as_tuple()
        assert _allocation_event_matches_command(
            event,
            command_id=cmd_id,
            portfolio_id=p_id,
            charge_transaction_id=c_id,
            target_transaction_id=t_id,
            allocated_amount=Decimal("6"),
        ) is False

    def test_mismatched_command_id_fails_match(self):
        event = _make_allocation_event(uuid4(), uuid4(), uuid4(), uuid4(), Decimal("6.000"), event_id=uuid4())
        assert _allocation_event_matches_command(
            event,
            command_id=uuid4(),
            portfolio_id=event.portfolio_id,
            charge_transaction_id=event.charge_transaction_id,  # type: ignore
            target_transaction_id=event.target_transaction_id,  # type: ignore
            allocated_amount=Decimal("6.000"),
        ) is False

    def test_mismatched_target_fails_match(self):
        cmd_id = uuid4()
        event = _make_allocation_event(uuid4(), uuid4(), uuid4(), uuid4(), Decimal("6.000"), event_id=cmd_id)
        assert _allocation_event_matches_command(
            event,
            command_id=cmd_id,
            portfolio_id=event.portfolio_id,
            charge_transaction_id=event.charge_transaction_id,  # type: ignore
            target_transaction_id=uuid4(),
            allocated_amount=Decimal("6.000"),
        ) is False

    def test_reversal_event_fails_match(self):
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
    """Items 46, 54, 57, 70: Sequential exact retry and first-commit-wins behavior."""

    def test_first_invocation_commits_and_second_invocation_replays(self):
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
        # Clock must NOT be called; if called it will raise IndexError
        retry_event = service.allocate(cmd_id, p.id, c_tx.id, t_tx.id, amount)

        assert retry_event == first_event
        assert retry_event.recorded_at == t1
        # No extra append, no extra semantic query
        assert len(repo.append_calls) == 1
        assert len(repo.get_portfolio_calls) == 1
        assert len(repo.list_transactions_calls) == 1
        assert len(repo.list_fee_tax_attribution_events_calls) == 1
        assert len(repo.get_event_calls) == 2  # 1 for first attempt pre-read, 1 for retry pre-read

    def test_pre_read_occurs_before_clock(self):
        p = _make_portfolio()
        cmd_id = uuid4()
        c_id = uuid4()
        t_id = uuid4()
        amount = Decimal("6.000")
        t1 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        existing = _make_allocation_event(p.id, uuid4(), c_id, t_id, amount, event_id=cmd_id, recorded_at=t1)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            attribution_events={p.id: [existing]},
        )

        def exploding_clock():
            raise AssertionError("Clock must not be called during idempotent replay!")

        service = PortfolioFeeTaxAttributionCommandService(repo, clock=exploding_clock)
        replayed = service.allocate(cmd_id, p.id, c_id, t_id, amount)
        assert replayed == existing
        assert replayed.recorded_at == t1

    def test_pre_read_error_propagates_unchanged(self):
        p = _make_portfolio()
        repo = StrictCommandTestRepository(portfolios={p.id: p})
        sentinel_exc = RuntimeError("Database GET connection failure")

        def bad_get(p_id: UUID, e_id: UUID):
            raise sentinel_exc

        repo.get_event_override = bad_get
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(RuntimeError) as exc_info:
            service.allocate(uuid4(), p.id, uuid4(), uuid4(), Decimal("5.000"))

        assert exc_info.value is sentinel_exc
        assert len(repo.append_calls) == 0


class TestRetryAfterReversals:
    """Items 47, 48, 49, 85: Replay after subsequent attribution/ledger reversals."""

    def test_retry_after_attribution_reversal_returns_original_event(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("10"), unit_price=Decimal("150.000"))

        cmd_id = uuid4()
        t1 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=cmd_id, recorded_at=t1)
        # Later reversal of cmd_id
        t2 = datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc)
        rev_event = _make_reversal_event(p.id, a_id, cmd_id, recorded_at=t2)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc_event, rev_event]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        # Retry original command
        replayed = service.allocate(cmd_id, p.id, c_tx.id, t_tx.id, Decimal("6.000"))
        assert replayed == alloc_event
        assert replayed.recorded_at == t1
        assert len(repo.append_calls) == 0
        assert len(repo.get_portfolio_calls) == 0

    def test_retry_after_target_ledger_reversal_returns_original_event(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("10"), unit_price=Decimal("150.000"))
        target_rev = _make_tx(p.id, a_id, TransactionType.REVERSAL, reverses_transaction_id=t_tx.id)

        cmd_id = uuid4()
        t1 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=cmd_id, recorded_at=t1)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx, target_rev]},
            attribution_events={p.id: [alloc_event]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        replayed = service.allocate(cmd_id, p.id, c_tx.id, t_tx.id, Decimal("6.000"))
        assert replayed == alloc_event
        assert replayed.recorded_at == t1
        assert len(repo.append_calls) == 0

    def test_retry_after_charge_ledger_reversal_returns_original_event(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        charge_rev = _make_tx(p.id, a_id, TransactionType.REVERSAL, reverses_transaction_id=c_tx.id)
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("10"), unit_price=Decimal("150.000"))

        cmd_id = uuid4()
        t1 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        alloc_event = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=cmd_id, recorded_at=t1)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, charge_rev, t_tx]},
            attribution_events={p.id: [alloc_event]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        replayed = service.allocate(cmd_id, p.id, c_tx.id, t_tx.id, Decimal("6.000"))
        assert replayed == alloc_event
        assert replayed.recorded_at == t1
        assert len(repo.append_calls) == 0


class TestConflictingCommandIdReuse:
    """Items 50, 51, 52, 53, 68, 69, 83, 84: Same command ID with different semantics fails closed."""

    def test_same_command_id_different_target_fails_closed(self):
        p = _make_portfolio()
        cmd_id = uuid4()
        c_id = uuid4()
        t1_id = uuid4()
        t2_id = uuid4()
        existing = _make_allocation_event(p.id, uuid4(), c_id, t1_id, Decimal("6.000"), event_id=cmd_id)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            attribution_events={p.id: [existing]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="Command ID conflict"):
            service.allocate(cmd_id, p.id, c_id, t2_id, Decimal("6.000"))

        assert len(repo.append_calls) == 0
        assert len(repo.get_portfolio_calls) == 0

    def test_same_command_id_different_charge_fails_closed(self):
        p = _make_portfolio()
        cmd_id = uuid4()
        c1_id = uuid4()
        c2_id = uuid4()
        t_id = uuid4()
        existing = _make_allocation_event(p.id, uuid4(), c1_id, t_id, Decimal("6.000"), event_id=cmd_id)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            attribution_events={p.id: [existing]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="Command ID conflict"):
            service.allocate(cmd_id, p.id, c2_id, t_id, Decimal("6.000"))

        assert len(repo.append_calls) == 0

    def test_same_command_id_different_amount_fails_closed(self):
        p = _make_portfolio()
        cmd_id = uuid4()
        c_id = uuid4()
        t_id = uuid4()
        existing = _make_allocation_event(p.id, uuid4(), c_id, t_id, Decimal("6.000"), event_id=cmd_id)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            attribution_events={p.id: [existing]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="Command ID conflict"):
            service.allocate(cmd_id, p.id, c_id, t_id, Decimal("7.000"))

        assert len(repo.append_calls) == 0

    def test_same_command_id_decimal_representation_drift_fails_closed(self):
        p = _make_portfolio()
        cmd_id = uuid4()
        c_id = uuid4()
        t_id = uuid4()
        existing = _make_allocation_event(p.id, uuid4(), c_id, t_id, Decimal("6.000"), event_id=cmd_id)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            attribution_events={p.id: [existing]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        # Decimal("6") is numerically equal but has different representation
        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="Command ID conflict"):
            service.allocate(cmd_id, p.id, c_id, t_id, Decimal("6"))

        assert len(repo.append_calls) == 0

    def test_existing_reversal_under_command_id_fails_closed(self):
        p = _make_portfolio()
        cmd_id = uuid4()
        c_id = uuid4()
        t_id = uuid4()
        existing = _make_reversal_event(p.id, uuid4(), uuid4(), event_id=cmd_id)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            attribution_events={p.id: [existing]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="Command ID conflict"):
            service.allocate(cmd_id, p.id, c_id, t_id, Decimal("6.000"))

        assert len(repo.append_calls) == 0

    def test_existing_wrong_portfolio_fails_closed(self):
        p = _make_portfolio()
        cmd_id = uuid4()
        c_id = uuid4()
        t_id = uuid4()
        other_portfolio_id = uuid4()
        existing = _make_allocation_event(other_portfolio_id, uuid4(), c_id, t_id, Decimal("6.000"), event_id=cmd_id)

        repo = StrictCommandTestRepository(portfolios={p.id: p})
        repo.get_event_override = lambda p_id, e_id: existing if e_id == cmd_id else None
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="Command ID conflict"):
            service.allocate(cmd_id, p.id, c_id, t_id, Decimal("6.000"))

        assert len(repo.append_calls) == 0


class TestConcurrentRaceRecovery:
    """Items 26, 27, 28, 59, 60, 61, 62, 63, 64, 65, 66: Post-error idempotency recovery."""

    def test_concurrent_same_command_pk_conflict_recovery(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("10"), unit_price=Decimal("150.000"))

        cmd_id = uuid4()
        amount = Decimal("6.000")
        t1 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 29, 10, 0, 1, tzinfo=timezone.utc)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
        )

        committed_by_tx1 = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, amount, event_id=cmd_id, recorded_at=t1)

        def concurrent_append_conflict(event: FeeTaxAttributionPersistenceEvent):
            # Simulate TX1 committing right as TX2 tries to insert
            repo.attribution_events.setdefault(p.id, []).append(committed_by_tx1)
            raise RuntimeError("duplicate key value violates unique constraint 23505")

        repo.append_override = concurrent_append_conflict
        service = PortfolioFeeTaxAttributionCommandService(repo, clock=lambda: t2)

        # TX2 executes allocate
        result = service.allocate(cmd_id, p.id, c_tx.id, t_tx.id, amount)

        assert result == committed_by_tx1
        assert result.recorded_at == t1  # First commit timestamp preserved

    def test_concurrent_same_command_trigger_conflict_recovery(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("10"), unit_price=Decimal("150.000"))

        cmd_id = uuid4()
        amount = Decimal("6.000")
        t1 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 29, 10, 0, 2, tzinfo=timezone.utc)

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
        )

        committed_by_tx1 = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, amount, event_id=cmd_id, recorded_at=t1)

        def concurrent_trigger_conflict(event: FeeTaxAttributionPersistenceEvent):
            repo.attribution_events.setdefault(p.id, []).append(committed_by_tx1)
            raise RuntimeError("Database trigger error: active duplicate pair already exists")

        repo.append_override = concurrent_trigger_conflict
        service = PortfolioFeeTaxAttributionCommandService(repo, clock=lambda: t2)

        result = service.allocate(cmd_id, p.id, c_tx.id, t_tx.id, amount)
        assert result == committed_by_tx1
        assert result.recorded_at == t1

    def test_unrelated_overallocation_error_propagates(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("10"), unit_price=Decimal("150.000"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
        )

        overallocation_exc = RuntimeError("Database trigger: cumulative allocation exceeds charge capacity")

        def append_error(event: FeeTaxAttributionPersistenceEvent):
            raise overallocation_exc

        repo.append_override = append_error
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(RuntimeError) as exc_info:
            service.allocate(uuid4(), p.id, c_tx.id, t_tx.id, Decimal("6.000"))

        assert exc_info.value is overallocation_exc

    def test_unrelated_target_reversal_error_propagates(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("10"), unit_price=Decimal("150.000"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
        )

        target_rev_exc = RuntimeError("Database trigger: target transaction has been reversed")

        def append_error(event: FeeTaxAttributionPersistenceEvent):
            raise target_rev_exc

        repo.append_override = append_error
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(RuntimeError) as exc_info:
            service.allocate(uuid4(), p.id, c_tx.id, t_tx.id, Decimal("6.000"))

        assert exc_info.value is target_rev_exc

    def test_database_outage_propagates(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("10"), unit_price=Decimal("150.000"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
        )

        outage_exc = ConnectionError("PostgreSQL cluster network unreachable")

        def append_error(event: FeeTaxAttributionPersistenceEvent):
            raise outage_exc

        repo.append_override = append_error
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(ConnectionError) as exc_info:
            service.allocate(uuid4(), p.id, c_tx.id, t_tx.id, Decimal("6.000"))

        assert exc_info.value is outage_exc

    def test_post_error_lookup_failure_propagates_original_error(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("10"), unit_price=Decimal("150.000"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
        )

        orig_exc = RuntimeError("Original append network error")
        get_exc = RuntimeError("Secondary get error")

        def bad_append(event: FeeTaxAttributionPersistenceEvent):
            raise orig_exc

        def bad_get(p_id: UUID, e_id: UUID):
            raise get_exc

        repo.append_override = bad_append
        repo.get_event_override = lambda p_id, e_id: None if len(repo.append_calls) == 0 else bad_get(p_id, e_id)
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(RuntimeError) as exc_info:
            service.allocate(uuid4(), p.id, c_tx.id, t_tx.id, Decimal("6.000"))

        assert exc_info.value is orig_exc

    def test_post_error_conflicting_event_fails_closed(self):
        p = _make_portfolio()
        a_id = uuid4()
        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("10"), unit_price=Decimal("150.000"))

        cmd_id = uuid4()
        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
        )

        # Conflicting event committed under same ID
        conflicting_event = _make_allocation_event(p.id, a_id, c_tx.id, uuid4(), Decimal("6.000"), event_id=cmd_id)

        def append_conflict(event: FeeTaxAttributionPersistenceEvent):
            repo.attribution_events.setdefault(p.id, []).append(conflicting_event)
            raise RuntimeError("23505 duplicate key")

        repo.append_override = append_conflict
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="Command ID conflict"):
            service.allocate(cmd_id, p.id, c_tx.id, t_tx.id, Decimal("6.000"))


class TestDomainScenariosAndPreflight:
    """Preflight domain validation for genuinely new commands."""

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

        cmd_id = uuid4()
        event = service.allocate(cmd_id, p.id, fee_tx.id, buy_tx.id, Decimal("6.000"))

        assert event.id == cmd_id
        assert event.portfolio_id == p.id
        assert event.account_id == a_id
        assert event.event_type == FeeTaxAttributionEventType.ALLOCATION
        assert event.charge_transaction_id == fee_tx.id
        assert event.target_transaction_id == buy_tx.id
        assert event.allocated_amount == Decimal("6.000")
        assert event.reverses_attribution_event_id is None

    def test_partial_existing_allocation_success(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))
        buy_1 = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))
        buy_2 = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))

        # Existing active: Fee -> Buy 1 = 3.000
        existing_alloc = _make_allocation_event(p.id, a_id, fee_tx.id, buy_1.id, Decimal("3.000"), recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_1, buy_2]},
            attribution_events={p.id: [existing_alloc]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        cmd_id = uuid4()
        # Allocate Fee -> Buy 2 = 7.000 (total = 10.000 <= 10.000)
        event = service.allocate(cmd_id, p.id, fee_tx.id, buy_2.id, Decimal("7.000"))
        assert event.id == cmd_id
        assert event.allocated_amount == Decimal("7.000")

    def test_cumulative_over_allocation_preflight_rejection(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))
        buy_1 = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))
        buy_2 = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc))

        # Existing active: Fee -> Buy 1 = 6.000
        existing_alloc = _make_allocation_event(p.id, a_id, fee_tx.id, buy_1.id, Decimal("6.000"), recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_1, buy_2]},
            attribution_events={p.id: [existing_alloc]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        # Allocate Fee -> Buy 2 = 5.000 (total = 11.000 > 10.000) -> Preflight fails before append
        with pytest.raises(FeeTaxAttributionError, match="Over-allocation detected for charge"):
            service.allocate(uuid4(), p.id, fee_tx.id, buy_2.id, Decimal("5.000"))

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

        # Duplicate pair C -> X with a NEW command ID
        with pytest.raises(FeeTaxAttributionError, match="Duplicate attribution intent detected"):
            service.allocate(uuid4(), p.id, fee_tx.id, buy_x.id, Decimal("2.000"))

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
            service.allocate(uuid4(), p.id, buy_1.id, buy_2.id, Decimal("5.000"))

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
            service.allocate(uuid4(), p.id, fee_1.id, fee_2.id, Decimal("5.000"))

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
            service.allocate(uuid4(), p.id, fee_tx.id, buy_tx.id, Decimal("5.000"))

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

        with pytest.raises(FeeTaxAttributionError, match="not found in active transactions at PIT cutoff"):
            service.allocate(uuid4(), p.id, fee_tx.id, buy_tx.id, Decimal("5.000"))

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

        with pytest.raises(FeeTaxAttributionError, match="Cross-account attribution rejected"):
            service.allocate(uuid4(), p.id, fee_tx.id, buy_tx.id, Decimal("5.000"))

        assert len(repo.append_calls) == 0

    def test_large_exact_decimal_representation_preserved(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("123456789.123456789"))
        buy_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("1"), unit_price=Decimal("100"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        cmd_id = uuid4()
        exact_amount = Decimal("98765432.123456789")
        event = service.allocate(cmd_id, p.id, fee_tx.id, buy_tx.id, exact_amount)

        assert event.allocated_amount == exact_amount
        assert event.allocated_amount.as_tuple() == exact_amount.as_tuple()


class TestClockResolutionAndUTC:
    """Clock resolution, awareness validation, and UTC normalization."""

    def test_single_clock_call_per_new_command(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
        )

        t_custom = datetime(2026, 8, 29, 15, 30, 0, tzinfo=timezone(timedelta(hours=3)))
        t_utc = t_custom.astimezone(timezone.utc)
        clock_calls = [t_custom]

        def tracking_clock() -> datetime:
            return clock_calls.pop(0)

        service = PortfolioFeeTaxAttributionCommandService(repo, clock=tracking_clock)
        event = service.allocate(uuid4(), p.id, fee_tx.id, buy_tx.id, Decimal("5.000"))

        assert len(clock_calls) == 0  # Proof of exactly one invocation
        assert event.recorded_at == t_utc

    @pytest.mark.parametrize("bad_clock_return", [
        None,
        True,
        False,
        "2026-08-29T12:00:00Z",
        1234567890,
        datetime(2026, 8, 29, 12, 0, 0),  # Naive datetime
    ])
    def test_invalid_clock_return_fails_closed(self, bad_clock_return: Any):
        repo = StrictCommandTestRepository()
        service = PortfolioFeeTaxAttributionCommandService(repo, clock=lambda: bad_clock_return)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="Clock"):
            service.allocate(uuid4(), uuid4(), uuid4(), uuid4(), Decimal("5.000"))

        assert len(repo.append_calls) == 0


class TestPersistenceAndReadbackAuthority:
    """Readback validation, fail-closed drift defense, and zero post-write requery."""

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

        event = service.allocate(uuid4(), p.id, fee_tx.id, buy_tx.id, Decimal("6.000"))
        assert event is not None

        # Verify call counts: exactly 1 get_event (pre-read), 1 get_portfolio, 1 list_transactions, 1 list_events, 1 append
        assert len(repo.get_event_calls) == 1
        assert len(repo.get_portfolio_calls) == 1
        assert len(repo.list_transactions_calls) == 1
        assert len(repo.list_fee_tax_attribution_events_calls) == 1
        assert len(repo.append_calls) == 1

    def test_returned_wrong_event_type_fails_closed(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
        )

        def bad_append(event: FeeTaxAttributionPersistenceEvent) -> Any:
            return _make_reversal_event(p.id, a_id, uuid4(), event_id=event.id)

        repo.append_override = bad_append
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="wrong event type"):
            service.allocate(uuid4(), p.id, fee_tx.id, buy_tx.id, Decimal("6.000"))


    def test_returned_mismatched_id_fails_closed(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
        )

        def bad_append(event: FeeTaxAttributionPersistenceEvent) -> Any:
            mutated = deepcopy(event)
            object.__setattr__(mutated, "id", uuid4())
            return mutated

        repo.append_override = bad_append
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="mismatched event ID"):
            service.allocate(uuid4(), p.id, fee_tx.id, buy_tx.id, Decimal("6.000"))

    def test_returned_decimal_drift_fails_closed(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
        )

        def bad_append(event: FeeTaxAttributionPersistenceEvent) -> Any:
            mutated = deepcopy(event)
            object.__setattr__(mutated, "allocated_amount", Decimal("6"))
            return mutated

        repo.append_override = bad_append
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="economic contents do not match"):
            service.allocate(uuid4(), p.id, fee_tx.id, buy_tx.id, Decimal("6.000"))

    def test_returned_same_instant_different_offset_accepted(self):
        p = _make_portfolio()
        a_id = uuid4()
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StrictCommandTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
        )

        def timezone_shift_append(event: FeeTaxAttributionPersistenceEvent) -> Any:
            mutated = deepcopy(event)
            shifted = event.recorded_at.astimezone(timezone(timedelta(hours=3)))
            object.__setattr__(mutated, "recorded_at", shifted)
            return mutated

        repo.append_override = timezone_shift_append
        service = PortfolioFeeTaxAttributionCommandService(repo)

        event = service.allocate(uuid4(), p.id, fee_tx.id, buy_tx.id, Decimal("6.000"))
        assert event is not None


class TestStaticPurityAndInvariants:
    """Static inspection of module source code for Phase 14M / 14M.1 purity invariants."""

    def test_public_methods_contain_no_owner_arguments(self):
        sig = inspect.signature(PortfolioFeeTaxAttributionCommandService.allocate)
        assert "owner_id" not in sig.parameters
        assert "user_id" not in sig.parameters

    def test_no_reversal_command_in_module(self):
        assert not hasattr(PortfolioFeeTaxAttributionCommandService, "reverse")
        assert not hasattr(PortfolioFeeTaxAttributionCommandService, "reverse_attribution")
        assert not hasattr(PortfolioFeeTaxAttributionCommandService, "reverse_allocation")

    def test_zero_uuid_generation_in_command_module(self):
        import backend.engine.private.portfolio.fee_tax_attribution_command as mod
        src = inspect.getsource(mod)
        assert "uuid4(" not in src
        assert "uuid5(" not in src
        assert "event_id_factory" not in src

    def test_command_id_passed_as_event_id(self):
        import backend.engine.private.portfolio.fee_tax_attribution_command as mod
        src = inspect.getsource(mod)
        assert "event_id=cmd_id" in src or "event_id = cmd_id" in src

    def test_idempotency_pre_read_called_before_clock(self):
        import backend.engine.private.portfolio.fee_tax_attribution_command as mod
        src = inspect.getsource(mod)
        idx_get = src.find("get_fee_tax_attribution_event")
        idx_clock = src.find("_resolve_command_clock")
        assert idx_get != -1
        assert idx_clock != -1
        assert idx_get < idx_clock

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
