"""
backend/tests/test_portfolio_projection.py
==========================================
Tests for Phase 12C.1: Reversal-Aware Point-in-Time Ledger View Foundation.

Zero network calls (pytest-socket enforced).
Pure in-memory domain evaluation.

Test Matrix:
    1. Basic Views (Empty, Single BUY, Single CASH_DEPOSIT, Basic Reversal)
    2. Point-in-Time Cutoff Semantics (Exact instant, microsecond boundary, timezone offsets, naive rejection)
    3. Deterministic Ordering (Audit ordering for known, Economic ordering for active, Shuffled input invariance)
    4. Reversal Integrity & Fail-Closed Guards (Missing target, Cross-portfolio, Cross-account, Reversal-of-reversal, Double reversal, Self-reversal)
    5. Portfolio & External Identity Persistence Integrity (Different portfolio, Duplicate physical ID, Normalized external collisions, Tab boundary preservation, Manual duplicate coexistence)
    6. Temporal Robustness (Reversal before target in sequence, Future reversal isolation)
    7. Immutability & Mutation Defense (Frozen dataclasses, Immutable tuples)
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import random
from typing import Optional
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import Currency, PortfolioMode, TransactionType
from backend.engine.private.portfolio.models import Portfolio, PortfolioTransaction
from backend.engine.private.portfolio.projection import (
    LedgerProjectionView,
    PortfolioProjectionError,
    ProjectedTransactionState,
    build_ledger_projection_view,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helper Factories
# ─────────────────────────────────────────────────────────────────────────────

def _make_portfolio(
    mode: PortfolioMode = PortfolioMode.MY_PORTFOLIO,
    owner_id: Optional[UUID] = None,
    id: Optional[UUID] = None,
) -> Portfolio:
    return Portfolio(
        owner_id=owner_id or uuid4(),
        name="Projection Test Portfolio",
        base_currency=Currency.USD,
        mode=mode,
        id=id or uuid4(),
        created_at=datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc),
    )


def _make_tx(
    portfolio_id: UUID,
    account_id: UUID,
    tx_type: TransactionType = TransactionType.BUY,
    effective_date: Optional[date] = None,
    recorded_at: Optional[datetime] = None,
    executed_at: Optional[datetime] = None,
    id: Optional[UUID] = None,
    reverses_tx_id: Optional[UUID] = None,
    ext_source: Optional[str] = None,
    ext_ref: Optional[str] = None,
    quantity: Optional[Decimal] = None,
    unit_price: Optional[Decimal] = None,
    cash_amount: Optional[Decimal] = None,
    instrument_id: Optional[UUID] = None,
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
            trade_currency=Currency.USD,
            external_source=ext_source,
            external_reference=ext_ref,
            id=id or uuid4(),
        )
    elif tx_type == TransactionType.CASH_DEPOSIT:
        return PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=eff,
            recorded_at=rec,
            executed_at=executed_at,
            cash_amount=cash_amount or Decimal("5000.00"),
            cash_currency=Currency.USD,
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
# 1. Basic Views
# ─────────────────────────────────────────────────────────────────────────────

class TestBasicViews:
    """Verifies baseline view construction for empty and standard single events."""

    def test_empty_transaction_history(self):
        """A: Empty history produces valid empty view."""
        port = _make_portfolio()
        view = build_ledger_projection_view(port, [])

        assert view.portfolio_id == port.id
        assert view.mode == port.mode
        assert view.as_of_recorded_at is None
        assert view.known_transactions == ()
        assert view.transaction_states == ()
        assert view.active_transactions == ()

    def test_single_buy_transaction(self):
        """B: Single BUY transaction is known and active."""
        port = _make_portfolio()
        acc_id = uuid4()
        tx = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY)

        view = build_ledger_projection_view(port, [tx])

        assert len(view.known_transactions) == 1
        assert view.known_transactions[0] == tx
        assert len(view.transaction_states) == 1
        assert view.transaction_states[0] == ProjectedTransactionState(
            transaction=tx,
            is_reversed=False,
            reversal_transaction_id=None,
        )
        assert len(view.active_transactions) == 1
        assert view.active_transactions[0] == tx

    def test_single_cash_deposit_transaction(self):
        """C: Single CASH_DEPOSIT transaction is known and active."""
        port = _make_portfolio()
        acc_id = uuid4()
        tx = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT)

        view = build_ledger_projection_view(port, [tx])

        assert len(view.known_transactions) == 1
        assert view.known_transactions[0] == tx
        assert len(view.transaction_states) == 1
        assert view.transaction_states[0].is_reversed is False
        assert len(view.active_transactions) == 1
        assert view.active_transactions[0] == tx

    def test_reversal_audit_retention_and_target_deactivation(self):
        """D: REVERSAL is retained in known_transactions audit history but excludes target from active_transactions."""
        port = _make_portfolio()
        acc_id = uuid4()
        t1 = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.BUY,
            recorded_at=datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        rev = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.REVERSAL,
            recorded_at=datetime(2026, 8, 2, 10, 0, 0, tzinfo=timezone.utc),
            reverses_tx_id=t1.id,
        )

        view = build_ledger_projection_view(port, [t1, rev])

        # Audit history includes both events
        assert len(view.known_transactions) == 2
        assert view.known_transactions == (t1, rev)

        # Base transaction state marked as reversed
        assert len(view.transaction_states) == 1
        assert view.transaction_states[0] == ProjectedTransactionState(
            transaction=t1,
            is_reversed=True,
            reversal_transaction_id=rev.id,
        )

        # Active economic transactions excludes both target and reversal
        assert view.active_transactions == ()


# ─────────────────────────────────────────────────────────────────────────────
# 2. Point-in-Time (PIT) Semantics
# ─────────────────────────────────────────────────────────────────────────────

class TestPointInTimeSemantics:
    """Verifies system-knowledge cutoff filtering and microsecond precision."""

    def test_target_active_before_reversal_recorded_at(self):
        """E: Target known before reversal is active when snapshot taken prior to reversal."""
        port = _make_portfolio()
        acc_id = uuid4()
        t1 = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.BUY,
            recorded_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        rev = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.REVERSAL,
            recorded_at=datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc),
            reverses_tx_id=t1.id,
        )

        # Snapshot taken on Aug 10 (after t1 recorded, before rev recorded)
        as_of_aug10 = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
        view_aug10 = build_ledger_projection_view(port, [t1, rev], as_of_recorded_at=as_of_aug10)

        assert view_aug10.as_of_recorded_at == as_of_aug10
        assert view_aug10.known_transactions == (t1,)
        assert len(view_aug10.transaction_states) == 1
        assert view_aug10.transaction_states[0].is_reversed is False
        assert view_aug10.active_transactions == (t1,)

    def test_cutoff_exactly_equals_reversal_recorded_at(self):
        """F: Cutoff exactly matching reversal recorded_at includes reversal and deactivates target."""
        port = _make_portfolio()
        acc_id = uuid4()
        t1 = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.BUY,
            recorded_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        rev_dt = datetime(2026, 8, 20, 14, 30, 0, 123456, tzinfo=timezone.utc)
        rev = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.REVERSAL,
            recorded_at=rev_dt,
            reverses_tx_id=t1.id,
        )

        view = build_ledger_projection_view(port, [t1, rev], as_of_recorded_at=rev_dt)

        assert len(view.known_transactions) == 2
        assert view.transaction_states[0].is_reversed is True
        assert view.transaction_states[0].reversal_transaction_id == rev.id
        assert view.active_transactions == ()

    def test_cutoff_one_microsecond_before_reversal(self):
        """G: Cutoff 1 microsecond before reversal excludes reversal and keeps target active."""
        port = _make_portfolio()
        acc_id = uuid4()
        t1 = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.BUY,
            recorded_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        rev_dt = datetime(2026, 8, 20, 14, 30, 0, 100000, tzinfo=timezone.utc)
        rev = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.REVERSAL,
            recorded_at=rev_dt,
            reverses_tx_id=t1.id,
        )

        cutoff_before = rev_dt - timedelta(microseconds=1)
        view = build_ledger_projection_view(port, [t1, rev], as_of_recorded_at=cutoff_before)

        assert view.known_transactions == (t1,)
        assert view.transaction_states[0].is_reversed is False
        assert view.active_transactions == (t1,)

    def test_equivalent_cutoff_timezone_offsets(self):
        """H: Equivalent physical instants across timezones produce identical PIT views."""
        port = _make_portfolio()
        acc_id = uuid4()
        t1 = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.BUY,
            recorded_at=datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        t2 = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.BUY,
            recorded_at=datetime(2026, 8, 1, 14, 0, 0, tzinfo=timezone.utc),
        )

        # 12:00:00 UTC == 15:00:00 +03:00 == 08:00:00 -04:00
        cutoff_utc = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
        cutoff_istanbul = datetime(2026, 8, 1, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))
        cutoff_ny = datetime(2026, 8, 1, 8, 0, 0, tzinfo=timezone(timedelta(hours=-4)))

        v_utc = build_ledger_projection_view(port, [t1, t2], as_of_recorded_at=cutoff_utc)
        v_ist = build_ledger_projection_view(port, [t1, t2], as_of_recorded_at=cutoff_istanbul)
        v_ny = build_ledger_projection_view(port, [t1, t2], as_of_recorded_at=cutoff_ny)

        assert v_utc.known_transactions == (t1,)
        assert v_ist.known_transactions == (t1,)
        assert v_ny.known_transactions == (t1,)
        assert v_utc.active_transactions == v_ist.active_transactions == v_ny.active_transactions == (t1,)

    def test_naive_cutoff_rejected(self):
        """I: Timezone-naive datetime or non-datetime rejected."""
        port = _make_portfolio()
        t1 = _make_tx(port.id, uuid4())

        with pytest.raises(ValueError, match="must be timezone-aware"):
            build_ledger_projection_view(port, [t1], as_of_recorded_at=datetime(2026, 8, 1, 12, 0, 0))

        with pytest.raises(TypeError, match="must be a datetime"):
            build_ledger_projection_view(port, [t1], as_of_recorded_at=True)  # type: ignore

        with pytest.raises(TypeError, match="must be a datetime"):
            build_ledger_projection_view(port, [t1], as_of_recorded_at="2026-08-01T12:00:00Z")  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 3. Deterministic Ordering
# ─────────────────────────────────────────────────────────────────────────────

class TestDeterministicOrdering:
    """Verifies audit vs economic sort contracts and sequence order invariance."""

    def test_shuffled_input_sequence_produces_identical_view(self):
        """J: Shuffling input sequence yields exactly identical projection views."""
        port = _make_portfolio()
        acc_id = uuid4()

        txs = [
            _make_tx(port.id, acc_id, effective_date=date(2026, 8, 10), recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)),
            _make_tx(port.id, acc_id, effective_date=date(2026, 8, 5), recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)),
            _make_tx(port.id, acc_id, effective_date=date(2026, 8, 8), recorded_at=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)),
            _make_tx(port.id, acc_id, effective_date=date(2026, 8, 1), recorded_at=datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)),
        ]

        base_view = build_ledger_projection_view(port, txs)

        for _ in range(10):
            shuffled = list(txs)
            random.shuffle(shuffled)
            shuffled_view = build_ledger_projection_view(port, shuffled)

            assert shuffled_view.known_transactions == base_view.known_transactions
            assert shuffled_view.transaction_states == base_view.transaction_states
            assert shuffled_view.active_transactions == base_view.active_transactions

    def test_known_transactions_audit_ordering(self):
        """K: known_transactions ordered by (recorded_at physical UTC instant, id)."""
        port = _make_portfolio()
        acc_id = uuid4()

        # Recorded at different times, economic dates opposite
        t_early_rec = _make_tx(
            port.id,
            acc_id,
            effective_date=date(2026, 8, 30),
            recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
        )
        t_late_rec = _make_tx(
            port.id,
            acc_id,
            effective_date=date(2026, 8, 1),
            recorded_at=datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc),
        )

        view = build_ledger_projection_view(port, [t_late_rec, t_early_rec])

        # Audit order: t_early_rec first
        assert view.known_transactions == (t_early_rec, t_late_rec)

    def test_active_transactions_economic_ordering(self):
        """L: active_transactions ordered by (effective_date, executed_at or UTC-min, recorded_at, id)."""
        port = _make_portfolio()
        acc_id = uuid4()

        t_eff_late = _make_tx(
            port.id,
            acc_id,
            effective_date=date(2026, 8, 30),
            recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
        )
        t_eff_early = _make_tx(
            port.id,
            acc_id,
            effective_date=date(2026, 8, 1),
            recorded_at=datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc),
        )

        view = build_ledger_projection_view(port, [t_eff_late, t_eff_early])

        # Economic order: t_eff_early first
        assert view.active_transactions == (t_eff_early, t_eff_late)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Reversal Integrity & Fail-Closed Guards
# ─────────────────────────────────────────────────────────────────────────────

class TestReversalIntegrityGuards:
    """Verifies that corrupted or impossible reversal history fails closed with PortfolioProjectionError."""

    def test_missing_reversal_target_raises(self):
        """M: Reversal targeting unknown/missing transaction raises PortfolioProjectionError."""
        port = _make_portfolio()
        acc_id = uuid4()
        rev = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.REVERSAL,
            reverses_tx_id=uuid4(),  # Non-existent target
        )

        with pytest.raises(PortfolioProjectionError, match="references unknown target transaction"):
            build_ledger_projection_view(port, [rev])

    def test_cross_account_reversal_raises(self):
        """N: Reversal with account_id different from target raises PortfolioProjectionError."""
        port = _make_portfolio()
        acc1 = uuid4()
        acc2 = uuid4()
        t1 = _make_tx(port.id, acc1, tx_type=TransactionType.BUY)
        rev = _make_tx(port.id, acc2, tx_type=TransactionType.REVERSAL, reverses_tx_id=t1.id)

        with pytest.raises(PortfolioProjectionError, match="Cross-account reversal rejected"):
            build_ledger_projection_view(port, [t1, rev])

    def test_reversal_of_reversal_raises(self):
        """O: Reversal targeting another REVERSAL raises PortfolioProjectionError."""
        port = _make_portfolio()
        acc_id = uuid4()
        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        rev1 = _make_tx(port.id, acc_id, tx_type=TransactionType.REVERSAL, recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc), reverses_tx_id=t1.id)
        rev_of_rev = _make_tx(port.id, acc_id, tx_type=TransactionType.REVERSAL, recorded_at=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc), reverses_tx_id=rev1.id)

        with pytest.raises(PortfolioProjectionError, match="Reversal of reversal rejected"):
            build_ledger_projection_view(port, [t1, rev1, rev_of_rev])

    def test_double_reversal_raises(self):
        """P: Multiple reversals targeting the same transaction raises PortfolioProjectionError."""
        port = _make_portfolio()
        acc_id = uuid4()
        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        rev1 = _make_tx(port.id, acc_id, tx_type=TransactionType.REVERSAL, recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc), reverses_tx_id=t1.id)
        rev2 = _make_tx(port.id, acc_id, tx_type=TransactionType.REVERSAL, recorded_at=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc), reverses_tx_id=t1.id)

        with pytest.raises(PortfolioProjectionError, match="Double reversal rejected"):
            build_ledger_projection_view(port, [t1, rev1, rev2])

    def test_duplicate_physical_id_raises(self):
        """Q: Duplicate physical transaction ID in input history raises PortfolioProjectionError."""
        port = _make_portfolio()
        acc_id = uuid4()
        shared_id = uuid4()

        t1 = _make_tx(port.id, acc_id, id=shared_id, recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        t2 = _make_tx(port.id, acc_id, id=shared_id, recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))

        with pytest.raises(PortfolioProjectionError, match="Duplicate physical transaction ID"):
            build_ledger_projection_view(port, [t1, t2])


# ─────────────────────────────────────────────────────────────────────────────
# 5. Portfolio & External Identity Persistence Integrity
# ─────────────────────────────────────────────────────────────────────────────

class TestPersistenceIntegrityGuards:
    """Verifies rejection of cross-portfolio contamination and duplicate canonical external identities."""

    def test_cross_portfolio_transaction_raises(self):
        """R: Transaction from a different portfolio raises PortfolioProjectionError."""
        port = _make_portfolio()
        other_port_id = uuid4()
        t_foreign = _make_tx(other_port_id, uuid4())

        with pytest.raises(PortfolioProjectionError, match="does not match portfolio.id"):
            build_ledger_projection_view(port, [t_foreign])

    def test_normalized_external_duplicate_persisted_twice_raises(self):
        """S: Two persisted rows with ' MIDAS ' and 'MIDAS' raise PortfolioProjectionError."""
        port = _make_portfolio()
        acc_id = uuid4()

        t1 = _make_tx(port.id, acc_id, ext_source=" MIDAS ", ext_ref="ORD-100")
        t2 = _make_tx(port.id, acc_id, ext_source="MIDAS", ext_ref="ORD-100")

        with pytest.raises(PortfolioProjectionError, match="Duplicate persisted canonical external identity"):
            build_ledger_projection_view(port, [t1, t2])

    def test_ascii_case_replay_persisted_twice_raises(self):
        """T: 'midas' and 'MIDAS' duplicate external identity raises PortfolioProjectionError."""
        port = _make_portfolio()
        acc_id = uuid4()

        t1 = _make_tx(port.id, acc_id, ext_source="midas", ext_ref="ORD-100")
        t2 = _make_tx(port.id, acc_id, ext_source="MIDAS", ext_ref="ORD-100")

        with pytest.raises(PortfolioProjectionError, match="Duplicate persisted canonical external identity"):
            build_ledger_projection_view(port, [t1, t2])

    def test_tab_boundary_distinction_preserves_both(self):
        """U: '\tMIDAS\t' vs 'MIDAS' are distinct canonical identities and both valid."""
        port = _make_portfolio()
        acc_id = uuid4()

        t_tabs = _make_tx(port.id, acc_id, ext_source="\tMIDAS\t", ext_ref="ORD-100")
        t_clean = _make_tx(port.id, acc_id, ext_source="MIDAS", ext_ref="ORD-100")

        view = build_ledger_projection_view(port, [t_tabs, t_clean])
        assert len(view.known_transactions) == 2
        assert len(view.active_transactions) == 2

    def test_manual_identical_economics_with_different_uuids_both_valid(self):
        """V: Manual transactions (source=None, ref=None) with identical economics are both valid."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        t1 = _make_tx(port.id, acc_id, instrument_id=inst_id, ext_source=None, ext_ref=None)
        t2 = _make_tx(port.id, acc_id, instrument_id=inst_id, ext_source=None, ext_ref=None)

        view = build_ledger_projection_view(port, [t1, t2])
        assert len(view.known_transactions) == 2
        assert len(view.active_transactions) == 2


# ─────────────────────────────────────────────────────────────────────────────
# 6. Temporal Robustness
# ─────────────────────────────────────────────────────────────────────────────

class TestTemporalRobustness:
    """Verifies reversal position in input sequence and future reversal isolation."""

    def test_reversal_supplied_before_target_in_sequence_resolves_correctly(self):
        """W: Reversal appearing before target in input list still resolves correctly."""
        port = _make_portfolio()
        acc_id = uuid4()

        t1 = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.BUY,
            recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
        )
        rev = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.REVERSAL,
            recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc),
            reverses_tx_id=t1.id,
        )

        # Supplied in reversed order: [rev, t1]
        view = build_ledger_projection_view(port, [rev, t1])

        assert view.transaction_states[0].transaction.id == t1.id
        assert view.transaction_states[0].is_reversed is True
        assert view.transaction_states[0].reversal_transaction_id == rev.id
        assert view.active_transactions == ()

    def test_reversal_recorded_after_cutoff_does_not_affect_earlier_pit(self):
        """X: Reversal recorded in future does NOT contaminate earlier point-in-time snapshot."""
        port = _make_portfolio()
        acc_id = uuid4()

        t1 = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.BUY,
            recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
        )
        rev = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.REVERSAL,
            recorded_at=datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc),
            reverses_tx_id=t1.id,
        )

        # As of Aug 10:
        view_early = build_ledger_projection_view(
            port,
            [t1, rev],
            as_of_recorded_at=datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc),
        )
        assert len(view_early.known_transactions) == 1
        assert view_early.known_transactions[0] == t1
        assert view_early.transaction_states[0].is_reversed is False
        assert view_early.active_transactions == (t1,)

        # As of Aug 25:
        view_late = build_ledger_projection_view(
            port,
            [t1, rev],
            as_of_recorded_at=datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc),
        )
        assert len(view_late.known_transactions) == 2
        assert view_late.transaction_states[0].is_reversed is True
        assert view_late.active_transactions == ()


# ─────────────────────────────────────────────────────────────────────────────
# 7. Immutability & Mutation Defense
# ─────────────────────────────────────────────────────────────────────────────

class TestImmutabilityAndMutationDefense:
    """Verifies output encapsulation and frozen dataclass guarantees."""

    def test_output_collections_are_immutable_tuples(self):
        port = _make_portfolio()
        acc_id = uuid4()
        t1 = _make_tx(port.id, acc_id)

        view = build_ledger_projection_view(port, [t1])

        assert isinstance(view.known_transactions, tuple)
        assert isinstance(view.transaction_states, tuple)
        assert isinstance(view.active_transactions, tuple)

    def test_frozen_dataclass_mutations_rejected(self):
        port = _make_portfolio()
        acc_id = uuid4()
        t1 = _make_tx(port.id, acc_id)

        view = build_ledger_projection_view(port, [t1])

        with pytest.raises(FrozenInstanceError):
            view.as_of_recorded_at = datetime.now(timezone.utc)  # type: ignore

        with pytest.raises(FrozenInstanceError):
            view.known_transactions = ()  # type: ignore

        state = view.transaction_states[0]
        with pytest.raises(FrozenInstanceError):
            state.is_reversed = True  # type: ignore
