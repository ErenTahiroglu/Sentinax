"""
backend/tests/test_portfolio_fee_tax_phase14_end_to_end.py
==========================================================
Final End-to-End Fee/Tax Attribution Lifecycle Adversarial Closure Audit (Phase 14O).

This module implements the definitive cross-layer integration and closure audit for
the complete Phase 14 explicit fee/tax attribution evidence subsystem.

It tests the full lifecycle across:
- Ledger FEE / TAX_WITHHOLDING observation & PIT projection (Phase 14A/B)
- Exact account/currency aggregation & observed attribution sets (Phase 14C/D)
- Append-only persistence events & PostgREST transport normalization (Phase 14E/H)
- Persisted history projection & semantic ledger rebinding (Phase 14I/J)
- Owner-bound query service & retry-safe command services (Phase 14K/L/M/M.1/N)
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import inspect
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import Currency, PortfolioMode, TransactionType
from backend.engine.private.portfolio.fee_tax import (
    ObservedFeeTaxProjection,
    build_observed_fee_tax_projection,
)
from backend.engine.private.portfolio.fee_tax_attribution import (
    FeeTaxAttributionError,
    FeeTaxAttributionIntent,
    ObservedFeeTaxAttributionSet,
    ResolvedFeeTaxAttribution,
    build_observed_fee_tax_attribution_set,
)
from backend.engine.private.portfolio.fee_tax_attribution_binding import (
    FeeTaxAttributionBindingError,
    PersistedFeeTaxAttributionSemanticView,
    bind_persisted_fee_tax_attribution_history,
)
from backend.engine.private.portfolio.fee_tax_attribution_command import (
    PortfolioFeeTaxAttributionCommandError,
    PortfolioFeeTaxAttributionCommandRepositoryPort,
    PortfolioFeeTaxAttributionCommandService,
)
from backend.engine.private.portfolio.fee_tax_attribution_history import (
    PersistedFeeTaxAttributionHistoryView,
    build_persisted_fee_tax_attribution_history_view,
)
from backend.engine.private.portfolio.fee_tax_attribution_persistence import (
    FeeTaxAttributionEventType,
    FeeTaxAttributionPersistenceError,
    FeeTaxAttributionPersistenceEvent,
    build_allocation_persistence_event,
    build_attribution_reversal_persistence_event,
)
from backend.engine.private.portfolio.fee_tax_attribution_service import (
    PortfolioFeeTaxAttributionQueryService,
)
from backend.engine.private.portfolio.fee_tax_service import PortfolioFeeTaxQueryService
from backend.engine.private.portfolio.models import (
    Portfolio,
    PortfolioAccount,
    PortfolioTransaction,
)
from backend.engine.private.portfolio.projection import (
    LedgerProjectionView,
    build_ledger_projection_view,
)


# ─────────────────────────────────────────────────────────────────────────────
# Stateful Owner-Bound Lifecycle Test Repository
# ─────────────────────────────────────────────────────────────────────────────

class StatefulLifecycleTestRepository:
    """
    Stateful owner-bound in-memory repository implementing the exact command/query port.
    Stores and filters immutable PortfolioTransaction and FeeTaxAttributionPersistenceEvent records.
    """

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
        self.list_events_calls: List[Tuple[UUID, Optional[UUID], Optional[datetime]]] = []
        self.get_event_calls: List[Tuple[UUID, UUID]] = []
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
        self.list_events_calls.append((portfolio_id, account_id, as_of_recorded_at))
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
        name="Lifecycle Audit Portfolio",
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
        recorded_at=recorded_at or datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc),
        instrument_id="AAPL" if tx_type in (TransactionType.BUY, TransactionType.SELL, TransactionType.DIVIDEND) else None,
        quantity=quantity if is_trade else None,
        unit_price=unit_price if is_trade else None,
        trade_currency=(trade_currency or Currency.USD) if is_trade else None,
        cash_amount=cash_amount if is_cash_tx else None,
        cash_currency=(cash_currency or Currency.USD) if is_cash_tx else None,
        reverses_transaction_id=reverses_transaction_id if is_reversal else None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test Suites
# ─────────────────────────────────────────────────────────────────────────────

class TestPhase14EndToEndLifecycle:
    """Complete cross-layer end-to-end lifecycle audit (Scenarios A–Q)."""

    def test_complete_allocation_reversal_reallocation_lifecycle(self):
        """
        Covers Scenarios A, B, C, D, E, F, G, H:
        - Basic allocation (FEE C -> BUY X = 6.000)
        - Query & unallocated capacity = 4.000
        - Complete allocation (C -> SELL Y = 4.000), fully allocated
        - Over-allocation rejected (C -> target = 0.001)
        - Sequential retry returns exact event without clock/query/append
        - Reversal REV_CMD_1 of ALLOC_CMD_1
        - Capacity release after reversal (remaining = 6.000)
        - Reallocation with new command ID ALLOC_CMD_4 (C -> X = 6.000) succeeds
        - Old command ID replay after reversal returns original ALLOC_CMD_1 without re-activating
        - Reversal sequential retry returns original REV_CMD_1
        - Different second reversal of already-reversed ALLOC_CMD_1 rejected
        - Historical PIT queries across T1, T2, T3, T4
        """
        p = _make_portfolio()
        a_id = uuid4()

        # Authoritative ledger setup at T0 (09:00 UTC)
        t0 = datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc)
        fee_c = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=t0)
        buy_x = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("10"), unit_price=Decimal("150.000"), recorded_at=t0)
        sell_y = _make_tx(p.id, a_id, TransactionType.SELL, quantity=Decimal("5"), unit_price=Decimal("160.000"), recorded_at=t0)
        dep_z = _make_tx(p.id, a_id, TransactionType.CASH_DEPOSIT, cash_amount=Decimal("500.000"), recorded_at=t0)

        repo = StatefulLifecycleTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_c, buy_x, sell_y, dep_z]},
        )

        query_service = PortfolioFeeTaxAttributionQueryService(repo)

        # ── Step 1: SCENARIO A — Basic Allocation at T1 (10:00 UTC) ──
        t1 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        cmd_service_t1 = PortfolioFeeTaxAttributionCommandService(repo, clock=lambda: t1)

        alloc_cmd_1 = uuid4()
        event_1 = cmd_service_t1.allocate(alloc_cmd_1, p.id, fee_c.id, buy_x.id, Decimal("6.000"))

        assert event_1.id == alloc_cmd_1
        assert event_1.event_type == FeeTaxAttributionEventType.ALLOCATION
        assert event_1.charge_transaction_id == fee_c.id
        assert event_1.target_transaction_id == buy_x.id
        assert event_1.allocated_amount.as_tuple() == Decimal("6.000").as_tuple()
        assert event_1.recorded_at == t1

        # Query after allocation at T1
        view_t1 = query_service.get_attribution_view_as_of(p.id, t1)
        assert len(view_t1.persisted_history.allocation_events) == 1
        assert len(view_t1.persisted_history.active_allocation_events) == 1
        assert len(view_t1.attribution_set.attributions) == 1
        assert view_t1.attribution_set.unallocated_amount_for_charge(fee_c.id) == Decimal("4.000")
        assert not view_t1.attribution_set.is_fully_allocated(fee_c.id)

        # ── Step 2: SCENARIO B — Complete Allocation at T2 (11:00 UTC) ──
        t2 = datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc)
        cmd_service_t2 = PortfolioFeeTaxAttributionCommandService(repo, clock=lambda: t2)

        alloc_cmd_2 = uuid4()
        event_2 = cmd_service_t2.allocate(alloc_cmd_2, p.id, fee_c.id, sell_y.id, Decimal("4.000"))

        assert event_2.id == alloc_cmd_2
        assert event_2.recorded_at == t2

        # Query after second allocation at T2
        view_t2 = query_service.get_attribution_view_as_of(p.id, t2)
        assert len(view_t2.persisted_history.active_allocation_events) == 2
        assert len(view_t2.attribution_set.attributions) == 2
        assert view_t2.attribution_set.is_fully_allocated(fee_c.id)
        assert view_t2.attribution_set.unallocated_amount_for_charge(fee_c.id) == Decimal("0.000")

        # ── Step 3: SCENARIO C — Over-Allocation Rejected ──
        alloc_cmd_3 = uuid4()
        with pytest.raises(FeeTaxAttributionError, match="Over-allocation detected"):
            cmd_service_t2.allocate(alloc_cmd_3, p.id, fee_c.id, dep_z.id, Decimal("0.001"))

        assert repo.get_fee_tax_attribution_event(p.id, alloc_cmd_3) is None

        # ── Step 4: SCENARIO D — Sequential Allocation Retry ──
        prev_appends = len(repo.append_calls)
        prev_get_portfolio = len(repo.get_portfolio_calls)

        replayed_event_1 = cmd_service_t2.allocate(alloc_cmd_1, p.id, fee_c.id, buy_x.id, Decimal("6.000"))
        assert replayed_event_1 == event_1
        assert replayed_event_1.recorded_at == t1
        assert len(repo.append_calls) == prev_appends
        assert len(repo.get_portfolio_calls) == prev_get_portfolio  # No semantic query

        # Decimal representation mismatch fails as conflict
        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="Command ID conflict"):
            cmd_service_t2.allocate(alloc_cmd_1, p.id, fee_c.id, buy_x.id, Decimal("6"))

        # ── Step 5: SCENARIO E & 16 — Reversal & Capacity Release at T3 (12:00 UTC) ──
        t3 = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
        cmd_service_t3 = PortfolioFeeTaxAttributionCommandService(repo, clock=lambda: t3)

        rev_cmd_1 = uuid4()
        rev_event_1 = cmd_service_t3.reverse_allocation(rev_cmd_1, p.id, alloc_cmd_1)

        assert rev_event_1.id == rev_cmd_1
        assert rev_event_1.event_type == FeeTaxAttributionEventType.REVERSAL
        assert rev_event_1.reverses_attribution_event_id == alloc_cmd_1
        assert rev_event_1.recorded_at == t3

        # Query after reversal at T3
        view_t3 = query_service.get_attribution_view_as_of(p.id, t3)
        assert len(view_t3.persisted_history.allocation_events) == 2
        assert len(view_t3.persisted_history.reversal_events) == 1
        assert len(view_t3.persisted_history.active_allocation_events) == 1
        assert view_t3.persisted_history.active_allocation_events[0].id == alloc_cmd_2
        assert len(view_t3.attribution_set.attributions) == 1
        assert view_t3.attribution_set.attributions[0].target_transaction.id == sell_y.id
        # Capacity is restored
        assert view_t3.attribution_set.unallocated_amount_for_charge(fee_c.id) == Decimal("6.000")

        # ── Step 6: SCENARIO F — Reallocation with New Command ID at T4 (13:00 UTC) ──
        t4 = datetime(2026, 8, 29, 13, 0, 0, tzinfo=timezone.utc)
        cmd_service_t4 = PortfolioFeeTaxAttributionCommandService(repo, clock=lambda: t4)

        alloc_cmd_4 = uuid4()
        event_4 = cmd_service_t4.allocate(alloc_cmd_4, p.id, fee_c.id, buy_x.id, Decimal("6.000"))

        assert event_4.id == alloc_cmd_4
        assert event_4.recorded_at == t4

        view_t4 = query_service.get_attribution_view_as_of(p.id, t4)
        assert len(view_t4.persisted_history.active_allocation_events) == 2
        active_ids = [e.id for e in view_t4.persisted_history.active_allocation_events]
        assert active_ids == [alloc_cmd_2, alloc_cmd_4]
        assert view_t4.attribution_set.is_fully_allocated(fee_c.id)

        # ── Step 7: SCENARIO 18 — Old Command ID Replay Returns Original ALLOC_CMD_1 ──
        replayed_old_cmd = cmd_service_t4.allocate(alloc_cmd_1, p.id, fee_c.id, buy_x.id, Decimal("6.000"))
        assert replayed_old_cmd == event_1
        assert replayed_old_cmd.recorded_at == t1
        # State at T4 unchanged
        view_t4_check = query_service.get_attribution_view_as_of(p.id, t4)
        assert [e.id for e in view_t4_check.persisted_history.active_allocation_events] == [alloc_cmd_2, alloc_cmd_4]

        # ── Step 8: SCENARIO G & 20 — Reversal Retry & Second Reversal ──
        replayed_rev_1 = cmd_service_t4.reverse_allocation(rev_cmd_1, p.id, alloc_cmd_1)
        assert replayed_rev_1 == rev_event_1
        assert replayed_rev_1.recorded_at == t3

        rev_cmd_2 = uuid4()
        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="is not active at PIT cutoff"):
            cmd_service_t4.reverse_allocation(rev_cmd_2, p.id, alloc_cmd_1)

        # ── Step 9: SCENARIO H — Historical Point-In-Time Invariance ──
        # Historical PIT at T2 (before reversal)
        view_pit_t2 = query_service.get_attribution_view_as_of(p.id, t2)
        assert len(view_pit_t2.persisted_history.active_allocation_events) == 2
        assert view_pit_t2.persisted_history.is_allocation_active(alloc_cmd_1) is True
        assert view_pit_t2.persisted_history.is_allocation_active(alloc_cmd_2) is True
        assert len(view_pit_t2.persisted_history.reversal_events) == 0

        # Historical PIT at T3 (at reversal)
        view_pit_t3 = query_service.get_attribution_view_as_of(p.id, t3)
        assert len(view_pit_t3.persisted_history.reversal_events) == 1
        assert view_pit_t3.persisted_history.is_allocation_active(alloc_cmd_1) is False
        assert view_pit_t3.persisted_history.is_allocation_active(alloc_cmd_2) is True

    def test_tax_withholding_allocation_lifecycle(self):
        """Scenario I: Verifies TAX_WITHHOLDING charge allocation to DIVIDEND target."""
        p = _make_portfolio()
        a_id = uuid4()
        t0 = datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc)

        div_tx = _make_tx(p.id, a_id, TransactionType.DIVIDEND, cash_amount=Decimal("100.000"), recorded_at=t0)
        tax_tx = _make_tx(p.id, a_id, TransactionType.TAX_WITHHOLDING, cash_amount=Decimal("2.500"), recorded_at=t0)

        repo = StatefulLifecycleTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [div_tx, tax_tx]},
        )

        t1 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        cmd_service = PortfolioFeeTaxAttributionCommandService(repo, clock=lambda: t1)

        cmd_id = uuid4()
        event = cmd_service.allocate(cmd_id, p.id, tax_tx.id, div_tx.id, Decimal("2.500"))

        assert event.id == cmd_id
        assert event.charge_transaction_id == tax_tx.id
        assert event.target_transaction_id == div_tx.id
        assert event.allocated_amount.as_tuple() == Decimal("2.500").as_tuple()

        query_service = PortfolioFeeTaxAttributionQueryService(repo)
        view = query_service.get_attribution_view_as_of(p.id, t1)
        assert len(view.attribution_set.attributions) == 1
        attr = view.attribution_set.attributions[0]
        assert attr.charge_transaction.id == tax_tx.id
        assert attr.target_transaction.id == div_tx.id
        assert view.attribution_set.is_fully_allocated(tax_tx.id)

    def test_account_and_portfolio_isolation(self):
        """Scenarios J & 28: Proves account and portfolio cross-contamination is strictly rejected."""
        p1 = _make_portfolio()
        p2 = _make_portfolio()
        a1 = uuid4()
        a2 = uuid4()

        c_tx_p1_a1 = _make_tx(p1.id, a1, TransactionType.FEE, cash_amount=Decimal("10.000"))
        t_tx_p1_a2 = _make_tx(p1.id, a2, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))
        t_tx_p2_a1 = _make_tx(p2.id, a1, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StatefulLifecycleTestRepository(
            portfolios={p1.id: p1, p2.id: p2},
            transactions={p1.id: [c_tx_p1_a1, t_tx_p1_a2], p2.id: [t_tx_p2_a1]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo)

        # Cross-account within same portfolio rejected
        with pytest.raises(FeeTaxAttributionError, match="Cross-account attribution rejected"):
            service.allocate(uuid4(), p1.id, c_tx_p1_a1.id, t_tx_p1_a2.id, Decimal("5.000"))

        # Cross-portfolio rejected
        with pytest.raises(FeeTaxAttributionError, match="not found in active transactions at PIT cutoff"):
            service.allocate(uuid4(), p1.id, c_tx_p1_a1.id, t_tx_p2_a1.id, Decimal("5.000"))

    def test_inactive_ledger_charge_and_target_fail_closed(self):
        """Scenarios K & 31: Persisted attribution whose ledger charge or target is reversed fails closed."""
        p = _make_portfolio()
        a_id = uuid4()
        t1 = datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        t3 = datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc)

        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=t1)
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=t1)
        alloc = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), recorded_at=t2)

        # Reversal of target on ledger at t3
        target_rev = _make_tx(p.id, a_id, TransactionType.REVERSAL, reverses_transaction_id=t_tx.id, recorded_at=t3)

        repo = StatefulLifecycleTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx, target_rev]},
            attribution_events={p.id: [alloc]},
        )
        query_service = PortfolioFeeTaxAttributionQueryService(repo)

        # Query at T3 fails closed because target is not active
        with pytest.raises(FeeTaxAttributionBindingError, match="is not an active transaction at PIT"):
            query_service.get_attribution_view_as_of(p.id, t3)

    def test_same_timestamp_allocation_and_reversal(self):
        """Scenario M: ALLOCATION and REVERSAL sharing exact same recorded_at timestamp."""
        p = _make_portfolio()
        a_id = uuid4()
        t_exact = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)

        c_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=t_exact)
        t_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=t_exact)

        alloc_id = uuid4()
        alloc = _make_allocation_event(p.id, a_id, c_tx.id, t_tx.id, Decimal("6.000"), event_id=alloc_id, recorded_at=t_exact)
        rev = _make_reversal_event(p.id, a_id, alloc_id, recorded_at=t_exact)

        repo = StatefulLifecycleTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [c_tx, t_tx]},
            attribution_events={p.id: [alloc, rev]},
        )
        query_service = PortfolioFeeTaxAttributionQueryService(repo)

        view = query_service.get_attribution_view_as_of(p.id, t_exact)
        assert len(view.persisted_history.reversal_events) == 1
        assert view.persisted_history.is_allocation_active(alloc_id) is False
        assert len(view.attribution_set.attributions) == 0

    def test_multiple_charges_and_currencies_independence(self):
        """Scenarios N & 35: Multiple charges and multiple currencies maintain strict independence."""
        p = _make_portfolio()
        a_id = uuid4()
        t0 = datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc)

        fee_usd = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), cash_currency=Currency.USD, recorded_at=t0)
        fee_try = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("100.000"), cash_currency=Currency.TRY, recorded_at=t0)
        buy_usd = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), trade_currency=Currency.USD, recorded_at=t0)
        buy_try = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), trade_currency=Currency.TRY, recorded_at=t0)

        repo = StatefulLifecycleTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_usd, fee_try, buy_usd, buy_try]},
        )

        service = PortfolioFeeTaxAttributionCommandService(repo, clock=lambda: datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))

        ev_usd = service.allocate(uuid4(), p.id, fee_usd.id, buy_usd.id, Decimal("6.000"))
        ev_try = service.allocate(uuid4(), p.id, fee_try.id, buy_try.id, Decimal("50.000"))

        query_service = PortfolioFeeTaxAttributionQueryService(repo)
        view = query_service.get_attribution_view_as_of(p.id, datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))

        assert view.attribution_set.unallocated_amount_for_charge(fee_usd.id) == Decimal("4.000")
        assert view.attribution_set.unallocated_amount_for_charge(fee_try.id) == Decimal("50.000")
        assert len(view.attribution_set.attributions) == 2

    def test_large_exact_decimal_preservation(self):
        """Scenario 36: Large Decimal precision survives entire lifecycle without float conversion."""
        p = _make_portfolio()
        a_id = uuid4()
        t0 = datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc)

        large_amount = Decimal("12345678901234567890.123400")
        fee_large = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=large_amount, recorded_at=t0)
        buy_large = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("10"), unit_price=Decimal("100"), recorded_at=t0)

        repo = StatefulLifecycleTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_large, buy_large]},
        )
        service = PortfolioFeeTaxAttributionCommandService(repo, clock=lambda: datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))

        cmd_id = uuid4()
        event = service.allocate(cmd_id, p.id, fee_large.id, buy_large.id, large_amount)

        assert event.allocated_amount.as_tuple() == large_amount.as_tuple()

        query_service = PortfolioFeeTaxAttributionQueryService(repo)
        view = query_service.get_attribution_view_as_of(p.id, datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))
        attr = view.attribution_set.attributions[0]
        assert attr.allocated_amount.as_tuple() == large_amount.as_tuple()
        assert view.attribution_set.unallocated_amount_for_charge(fee_large.id).as_tuple() == Decimal("0.000000").as_tuple() or view.attribution_set.is_fully_allocated(fee_large.id)

    def test_concurrent_same_and_different_command_races(self):
        """Scenarios O, 37, 38, 40, 41, 42: Concurrent races and command ID namespace sharing."""
        p = _make_portfolio()
        a_id = uuid4()
        t0 = datetime(2026, 8, 29, 9, 0, 0, tzinfo=timezone.utc)

        fee_c = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), recorded_at=t0)
        buy_x = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), recorded_at=t0)

        repo = StatefulLifecycleTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_c, buy_x]},
        )

        cmd_id = uuid4()
        t1 = datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 29, 10, 0, 1, tzinfo=timezone.utc)

        committed_winner = _make_allocation_event(p.id, a_id, fee_c.id, buy_x.id, Decimal("6.000"), event_id=cmd_id, recorded_at=t1)

        def concurrent_append_loser(event: FeeTaxAttributionPersistenceEvent):
            repo.attribution_events.setdefault(p.id, []).append(committed_winner)
            raise RuntimeError("23505 unique violation")

        repo.append_override = concurrent_append_loser
        service = PortfolioFeeTaxAttributionCommandService(repo, clock=lambda: t2)

        # Same command converges to winner
        result = service.allocate(cmd_id, p.id, fee_c.id, buy_x.id, Decimal("6.000"))
        assert result == committed_winner
        assert result.recorded_at == t1

        # Command namespace collision: using allocation command ID for reversal fails
        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="Command ID conflict"):
            service.reverse_allocation(cmd_id, p.id, uuid4())

    def test_malicious_repository_response_fails_closed(self):
        """Scenario Q & 44: Malicious/malformed repository return fails closed."""
        p = _make_portfolio()
        a_id = uuid4()
        fee_c = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
        buy_x = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"))

        repo = StatefulLifecycleTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_c, buy_x]},
        )

        def malicious_append(event: FeeTaxAttributionPersistenceEvent) -> Any:
            mutated = deepcopy(event)
            object.__setattr__(mutated, "allocated_amount", Decimal("999.000"))
            return mutated

        repo.append_override = malicious_append
        service = PortfolioFeeTaxAttributionCommandService(repo)

        with pytest.raises(PortfolioFeeTaxAttributionCommandError, match="economic contents do not match"):
            service.allocate(uuid4(), p.id, fee_c.id, buy_x.id, Decimal("6.000"))


class TestStaticInvariantsAndNonGoalsAudit:
    """Static audit verifying no tax-law, cost-basis, FX, float, or mutable status leaks."""

    def test_owner_isolation_in_public_signatures(self):
        for service_cls in (
            PortfolioFeeTaxAttributionQueryService,
            PortfolioFeeTaxAttributionCommandService,
            PortfolioFeeTaxQueryService,
        ):
            for name, method in inspect.getmembers(service_cls, predicate=inspect.isfunction):
                if name.startswith("_"):
                    continue
                sig = inspect.signature(method)
                for prohibited in ("owner_id", "user_id", "auth_user_id"):
                    assert prohibited not in sig.parameters, f"Found prohibited parameter {prohibited} in {service_cls.__name__}.{name}"

    def test_zero_tax_law_claims_in_production_code(self):
        import backend.engine.private.portfolio.fee_tax as mod1
        import backend.engine.private.portfolio.fee_tax_attribution as mod2
        import backend.engine.private.portfolio.fee_tax_attribution_binding as mod3
        import backend.engine.private.portfolio.fee_tax_attribution_command as mod4
        import backend.engine.private.portfolio.fee_tax_attribution_history as mod5
        import backend.engine.private.portfolio.fee_tax_attribution_persistence as mod6
        import backend.engine.private.portfolio.fee_tax_attribution_service as mod7
        import backend.engine.private.portfolio.fee_tax_attribution_transport as mod8
        import backend.engine.private.portfolio.fee_tax_service as mod9

        modules = [mod1, mod2, mod3, mod4, mod5, mod6, mod7, mod8, mod9]
        prohibited_keywords = [
            "tax_liability",
            "tax_refund",
            "tax_credit",
            "tax_basis",
            "deductible",
            "nondeductible",
            "treaty_treatment",
        ]

        for mod in modules:
            src = inspect.getsource(mod)
            for kw in prohibited_keywords:
                assert kw not in src.lower(), f"Found prohibited tax-law term {kw!r} in {mod.__name__}"

    def test_zero_cost_basis_or_fx_mutation_in_attribution(self):
        import backend.engine.private.portfolio.fee_tax_attribution_command as cmd_mod
        src = inspect.getsource(cmd_mod)
        prohibited = [
            "cost_basis",
            "realized_pnl",
            "realized_gain",
            "fx_rate",
            "convert_currency",
            "exchange_rate",
        ]
        for term in prohibited:
            assert term not in src.lower(), f"Found prohibited term {term!r} in command module"

    def test_zero_float_usage_in_financial_paths(self):
        import backend.engine.private.portfolio.fee_tax as mod1
        import backend.engine.private.portfolio.fee_tax_attribution as mod2
        import backend.engine.private.portfolio.fee_tax_attribution_binding as mod3
        import backend.engine.private.portfolio.fee_tax_attribution_command as mod4
        import backend.engine.private.portfolio.fee_tax_attribution_history as mod5
        import backend.engine.private.portfolio.fee_tax_attribution_persistence as mod6
        import backend.engine.private.portfolio.fee_tax_attribution_transport as mod8

        modules = [mod1, mod2, mod3, mod4, mod5, mod6, mod8]
        for mod in modules:
            src = inspect.getsource(mod)
            assert "float(" not in src, f"Found lossy float() conversion in {mod.__name__}"
            assert "round(" not in src, f"Found round() conversion in {mod.__name__}"

    def test_append_only_repository_static_audit(self):
        import backend.engine.private.portfolio.repository as repo_mod
        src = inspect.getsource(repo_mod)
        assert ".update(" not in src or "portfolio_fee_tax_attribution_events" not in src
        assert ".delete(" not in src or "portfolio_fee_tax_attribution_events" not in src
        assert ".upsert(" not in src or "portfolio_fee_tax_attribution_events" not in src

    def test_no_mutable_active_status_fields(self):
        fields = [f.name for f in FeeTaxAttributionPersistenceEvent.__dataclass_fields__.values()]
        assert "is_active" not in fields
        assert "status" not in fields
        assert "active" not in fields

    def test_no_reversal_of_reversal_enforced_at_domain_and_history(self):
        p_id = uuid4()
        a_id = uuid4()
        rev1 = _make_reversal_event(p_id, a_id, uuid4())

        # Attempt to build history where a reversal targets another reversal
        rev2 = _make_reversal_event(p_id, a_id, rev1.id)

        with pytest.raises(Exception):
            build_persisted_fee_tax_attribution_history_view(
                portfolio_id=p_id,
                as_of_recorded_at=datetime.now(timezone.utc),
                events=[rev1, rev2],
            )

    def test_single_reversal_enforced_in_history(self):
        p_id = uuid4()
        a_id = uuid4()
        alloc = _make_allocation_event(p_id, a_id, uuid4(), uuid4())
        rev1 = _make_reversal_event(p_id, a_id, alloc.id)
        rev2 = _make_reversal_event(p_id, a_id, alloc.id)

        with pytest.raises(Exception):
            build_persisted_fee_tax_attribution_history_view(
                portfolio_id=p_id,
                as_of_recorded_at=datetime.now(timezone.utc),
                events=[alloc, rev1, rev2],
            )

    def test_system_knowledge_vs_economic_date_independence(self):
        p = _make_portfolio()
        a_id = uuid4()
        # Transaction effective date is 2025-01-01, but recorded_at is 2026-08-29 10:00 UTC
        fee_tx = _make_tx(p.id, a_id, TransactionType.FEE, cash_amount=Decimal("10.000"), effective_date=date(2025, 1, 1), recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))
        buy_tx = _make_tx(p.id, a_id, TransactionType.BUY, quantity=Decimal("5"), unit_price=Decimal("10"), effective_date=date(2025, 1, 1), recorded_at=datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc))
        alloc = _make_allocation_event(p.id, a_id, fee_tx.id, buy_tx.id, Decimal("5.000"), recorded_at=datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc))

        repo = StatefulLifecycleTestRepository(
            portfolios={p.id: p},
            transactions={p.id: [fee_tx, buy_tx]},
            attribution_events={p.id: [alloc]},
        )
        query_service = PortfolioFeeTaxAttributionQueryService(repo)

        # As of 2026-08-29 11:00 UTC (before attribution recorded_at), attribution is NOT known
        view_before = query_service.get_attribution_view_as_of(p.id, datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc))
        assert len(view_before.attribution_set.attributions) == 0

        # As of 2026-08-29 13:00 UTC (after attribution recorded_at), attribution IS known
        view_after = query_service.get_attribution_view_as_of(p.id, datetime(2026, 8, 29, 13, 0, 0, tzinfo=timezone.utc))
        assert len(view_after.attribution_set.attributions) == 1



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
        recorded_at=recorded_at or datetime(2026, 8, 29, 10, 0, 0, tzinfo=timezone.utc),
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
        recorded_at=recorded_at or datetime(2026, 8, 29, 11, 0, 0, tzinfo=timezone.utc),
    )
