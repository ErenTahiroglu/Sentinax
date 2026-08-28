"""
backend/tests/test_portfolio_positions.py
=========================================
Tests for Phase 12C.2: Exact Reversal-Aware Position Quantity Projection.

Zero network calls (pytest-socket enforced).
Pure in-memory domain evaluation.

Test Matrix:
    1. Basic Position Projections (Empty, Single BUY, Multiple BUYs, Partial SELL, Exact Full SELL, Oversell/Negative)
    2. Account & Instrument Isolation (Same instrument across accounts, Multiple instruments in same account)
    3. Non-Position Event Invariance (Deposits, Withdrawals, Dividends, Interest, Fees, Taxes, FX)
    4. Real Reversal Integration & PIT Dynamics (Reversed BUY, Reversed SELL, PIT before/after reversal)
    5. Shuffled Input Invariance & Deterministic Ordering
    6. Exact Decimal Red-Team & Context Independence (Tiny precision, Huge precision, Exponent notation, Cancellation, Ambient prec=6, 1E+100 + 1E-100)
    7. View Integrity Guards (Wrong type, Forged active REVERSAL, Duplicate active UUIDs, Cross-portfolio active tx)
    8. Immutability & Mutation Defense (Tuples, Frozen dataclasses)
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone, tzinfo
from decimal import Decimal, localcontext
import random
from typing import Optional
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import Currency, PortfolioMode, TransactionType
from backend.engine.private.portfolio.models import Portfolio, PortfolioTransaction
from backend.engine.private.portfolio.positions import (
    PositionProjectionError,
    PositionQuantityProjection,
    PositionQuantityState,
    build_position_quantity_projection,
)
from backend.engine.private.portfolio.projection import (
    LedgerProjectionView,
    build_ledger_projection_view,
)


class NullOffsetTZ(tzinfo):
    """Custom tzinfo implementation returning None for utcoffset (non-None tzinfo but not aware)."""
    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "NULL"


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
        name="Position Test Portfolio",
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
    instrument_id: Optional[UUID] = None,
    quantity: Optional[Decimal] = None,
    unit_price: Optional[Decimal] = None,
    cash_amount: Optional[Decimal] = None,
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
            trade_currency=Currency.USD,
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
    elif tx_type == TransactionType.CASH_WITHDRAWAL:
        return PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.CASH_WITHDRAWAL,
            effective_date=eff,
            recorded_at=rec,
            executed_at=executed_at,
            cash_amount=cash_amount or Decimal("1000.00"),
            cash_currency=Currency.USD,
            external_source=ext_source,
            external_reference=ext_ref,
            id=id or uuid4(),
        )
    elif tx_type == TransactionType.DIVIDEND:
        return PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.DIVIDEND,
            instrument_id=instrument_id,  # Optional in dividend
            effective_date=eff,
            recorded_at=rec,
            executed_at=executed_at,
            cash_amount=cash_amount or Decimal("50.00"),
            cash_currency=Currency.USD,
            external_source=ext_source,
            external_reference=ext_ref,
            id=id or uuid4(),
        )
    elif tx_type == TransactionType.INTEREST:
        return PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.INTEREST,
            effective_date=eff,
            recorded_at=rec,
            executed_at=executed_at,
            cash_amount=cash_amount or Decimal("15.00"),
            cash_currency=Currency.USD,
            external_source=ext_source,
            external_reference=ext_ref,
            id=id or uuid4(),
        )
    elif tx_type == TransactionType.FEE:
        return PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.FEE,
            effective_date=eff,
            recorded_at=rec,
            executed_at=executed_at,
            cash_amount=cash_amount or Decimal("5.00"),
            cash_currency=Currency.USD,
            external_source=ext_source,
            external_reference=ext_ref,
            id=id or uuid4(),
        )
    elif tx_type == TransactionType.TAX_WITHHOLDING:
        return PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.TAX_WITHHOLDING,
            effective_date=eff,
            recorded_at=rec,
            executed_at=executed_at,
            cash_amount=cash_amount or Decimal("10.00"),
            cash_currency=Currency.USD,
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
            from_amount=Decimal("100.00"),
            from_currency=Currency.USD,
            to_amount=Decimal("3400.00"),
            to_currency=Currency.TRY,
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
# 1. Basic Position Projections
# ─────────────────────────────────────────────────────────────────────────────

class TestBasicPositions:
    """Verifies baseline position calculations for standard buy/sell flows."""

    def test_empty_view_produces_empty_positions(self):
        """A: Empty view produces empty positions and open_positions."""
        port = _make_portfolio()
        view = build_ledger_projection_view(port, [])
        proj = build_position_quantity_projection(view)

        assert proj.portfolio_id == port.id
        assert proj.mode == port.mode
        assert proj.as_of_recorded_at is None
        assert proj.positions == ()
        assert proj.open_positions == ()

    def test_single_buy_position(self):
        """B: One BUY produces open exact quantity."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()
        tx = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("10"))

        view = build_ledger_projection_view(port, [tx])
        proj = build_position_quantity_projection(view)

        assert len(proj.positions) == 1
        pos = proj.positions[0]
        assert pos.portfolio_id == port.id
        assert pos.account_id == acc_id
        assert pos.instrument_id == inst_id
        assert pos.quantity == Decimal("10")
        assert pos.is_open is True
        assert proj.open_positions == (pos,)

    def test_multiple_buys_exact_aggregation(self):
        """C: Multiple BUYs aggregate to exact sum."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()
        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("10"), recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("2.5"), recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))

        view = build_ledger_projection_view(port, [t1, t2])
        proj = build_position_quantity_projection(view)

        assert len(proj.positions) == 1
        assert proj.positions[0].quantity == Decimal("12.5")
        assert proj.open_positions == proj.positions

    def test_partial_sell_exact_remaining(self):
        """D: BUY 10 + SELL 4 leaves exact 6."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()
        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("10"), recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.SELL, instrument_id=inst_id, quantity=Decimal("4"), recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))

        view = build_ledger_projection_view(port, [t1, t2])
        proj = build_position_quantity_projection(view)

        assert len(proj.positions) == 1
        assert proj.positions[0].quantity == Decimal("6")
        assert proj.open_positions == proj.positions

    def test_exact_full_sell_retains_zero_in_positions_omitted_from_open(self):
        """E: BUY 10 + SELL 10 leaves quantity 0 in positions, but omitted from open_positions."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()
        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("10"), recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.SELL, instrument_id=inst_id, quantity=Decimal("10"), recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))

        view = build_ledger_projection_view(port, [t1, t2])
        proj = build_position_quantity_projection(view)

        assert len(proj.positions) == 1
        pos = proj.positions[0]
        assert pos.quantity == Decimal("0")
        assert pos.is_open is False
        assert proj.open_positions == ()

    def test_oversell_fails_closed(self):
        """F: BUY 10 + SELL 11 produces net negative quantity and raises PositionProjectionError."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()
        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("10"), recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.SELL, instrument_id=inst_id, quantity=Decimal("11"), recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))

        view = build_ledger_projection_view(port, [t1, t2])
        with pytest.raises(PositionProjectionError, match="Negative net position quantity"):
            build_position_quantity_projection(view)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Account & Instrument Isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestIsolation:
    """Verifies strict account-level and instrument-level partition."""

    def test_same_instrument_two_accounts_remain_separate(self):
        """G: Same instrument in Account A (10) and Account B (5) creates two separate position states."""
        port = _make_portfolio()
        acc_a = uuid4()
        acc_b = uuid4()
        inst_id = uuid4()

        t_a = _make_tx(port.id, acc_a, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("10"))
        t_b = _make_tx(port.id, acc_b, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("5"))

        view = build_ledger_projection_view(port, [t_a, t_b])
        proj = build_position_quantity_projection(view)

        assert len(proj.positions) == 2
        assert len(proj.open_positions) == 2

        pos_a = next(p for p in proj.positions if p.account_id == acc_a)
        pos_b = next(p for p in proj.positions if p.account_id == acc_b)

        assert pos_a.quantity == Decimal("10")
        assert pos_b.quantity == Decimal("5")

    def test_same_account_two_instruments_remain_separate(self):
        """H: Same account with Instrument X (10) and Instrument Y (20) creates two separate position states."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_x = uuid4()
        inst_y = uuid4()

        t_x = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_x, quantity=Decimal("10"))
        t_y = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_y, quantity=Decimal("20"))

        view = build_ledger_projection_view(port, [t_x, t_y])
        proj = build_position_quantity_projection(view)

        assert len(proj.positions) == 2
        pos_x = next(p for p in proj.positions if p.instrument_id == inst_x)
        pos_y = next(p for p in proj.positions if p.instrument_id == inst_y)

        assert pos_x.quantity == Decimal("10")
        assert pos_y.quantity == Decimal("20")


# ─────────────────────────────────────────────────────────────────────────────
# 3. Non-Position Event Invariance
# ─────────────────────────────────────────────────────────────────────────────

class TestNonPositionEvents:
    """Verifies cash, dividend, fee, tax, and FX events never alter security quantities."""

    def test_cash_and_corporate_events_do_not_alter_quantity(self):
        """I-O: Non-trade events do NOT change security quantity."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        txs = [
            _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("10")),
            _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00")),
            _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_WITHDRAWAL, cash_amount=Decimal("200.00")),
            _make_tx(port.id, acc_id, tx_type=TransactionType.DIVIDEND, instrument_id=inst_id, cash_amount=Decimal("50.00")),
            _make_tx(port.id, acc_id, tx_type=TransactionType.INTEREST, cash_amount=Decimal("10.00")),
            _make_tx(port.id, acc_id, tx_type=TransactionType.FEE),
            _make_tx(port.id, acc_id, tx_type=TransactionType.TAX_WITHHOLDING),
            _make_tx(port.id, acc_id, tx_type=TransactionType.FX_CONVERSION),
        ]

        view = build_ledger_projection_view(port, txs)
        proj = build_position_quantity_projection(view)

        assert len(proj.positions) == 1
        assert proj.positions[0].quantity == Decimal("10")
        assert proj.positions[0].instrument_id == inst_id


# ─────────────────────────────────────────────────────────────────────────────
# 4. Real Reversal Integration & PIT Dynamics
# ─────────────────────────────────────────────────────────────────────────────

class TestReversalAndPITIntegration:
    """Verifies position quantity behaves properly with real Phase 12C.1 reversals and PIT snapshots."""

    def test_reversed_buy_results_in_zero_open_positions(self):
        """P: BUY 10 + REVERSAL of BUY results in zero open positions."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        buy = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.BUY,
            instrument_id=inst_id,
            quantity=Decimal("10"),
            recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
        )
        rev = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.REVERSAL,
            reverses_tx_id=buy.id,
            recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc),
        )

        view = build_ledger_projection_view(port, [buy, rev])
        proj = build_position_quantity_projection(view)

        assert proj.positions == ()
        assert proj.open_positions == ()

    def test_reversed_sell_restores_original_quantity(self):
        """Q: BUY 10 + SELL 4 + REVERSAL of SELL restores quantity to 10."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        buy = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.BUY,
            instrument_id=inst_id,
            quantity=Decimal("10"),
            recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
        )
        sell = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.SELL,
            instrument_id=inst_id,
            quantity=Decimal("4"),
            recorded_at=datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc),
        )
        rev_sell = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.REVERSAL,
            reverses_tx_id=sell.id,
            recorded_at=datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc),
        )

        view = build_ledger_projection_view(port, [buy, sell, rev_sell])
        proj = build_position_quantity_projection(view)

        assert len(proj.positions) == 1
        assert proj.positions[0].quantity == Decimal("10")

    def test_pit_before_and_after_reversal_of_sell(self):
        """R, S: Snapshot before reversal reflects SELL (6); snapshot after reversal reflects restored 10."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        buy = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.BUY,
            instrument_id=inst_id,
            quantity=Decimal("10"),
            recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
        )
        sell = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.SELL,
            instrument_id=inst_id,
            quantity=Decimal("4"),
            recorded_at=datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc),
        )
        rev_sell = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.REVERSAL,
            reverses_tx_id=sell.id,
            recorded_at=datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc),
        )

        history = [buy, sell, rev_sell]

        # R: As of Aug 10 (before reversal was recorded)
        view_early = build_ledger_projection_view(port, history, as_of_recorded_at=datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc))
        proj_early = build_position_quantity_projection(view_early)
        assert proj_early.positions[0].quantity == Decimal("6")

        # S: As of Aug 25 (after reversal was recorded)
        view_late = build_ledger_projection_view(port, history, as_of_recorded_at=datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc))
        proj_late = build_position_quantity_projection(view_late)
        assert proj_late.positions[0].quantity == Decimal("10")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Shuffled Input Invariance & Deterministic Ordering
# ─────────────────────────────────────────────────────────────────────────────

class TestOrderingAndInvariance:
    """Verifies deterministic output independent of input active transaction order."""

    def test_shuffled_active_economics_produces_identical_projection(self):
        """T: Shuffled active transactions produce identical position states and ordering."""
        port = _make_portfolio()
        acc1 = uuid4()
        acc2 = uuid4()
        inst1 = uuid4()
        inst2 = uuid4()

        txs = [
            _make_tx(port.id, acc1, tx_type=TransactionType.BUY, instrument_id=inst1, quantity=Decimal("10")),
            _make_tx(port.id, acc1, tx_type=TransactionType.BUY, instrument_id=inst2, quantity=Decimal("20")),
            _make_tx(port.id, acc2, tx_type=TransactionType.BUY, instrument_id=inst1, quantity=Decimal("30")),
            _make_tx(port.id, acc2, tx_type=TransactionType.SELL, instrument_id=inst1, quantity=Decimal("5")),
        ]

        base_view = build_ledger_projection_view(port, txs)
        base_proj = build_position_quantity_projection(base_view)

        for _ in range(10):
            shuffled_txs = list(txs)
            random.shuffle(shuffled_txs)
            shuffled_view = build_ledger_projection_view(port, shuffled_txs)
            shuffled_proj = build_position_quantity_projection(shuffled_view)

            assert shuffled_proj.positions == base_proj.positions
            assert shuffled_proj.open_positions == base_proj.open_positions


# ─────────────────────────────────────────────────────────────────────────────
# 6. Exact Decimal Red-Team & Context Independence
# ─────────────────────────────────────────────────────────────────────────────

class TestExactDecimalRedTeam:
    """Verifies arbitrary Decimal precision, exponent handling, and ambient context independence."""

    def test_tiny_precision_summation(self):
        """U: Exact tiny Decimal addition."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("0.000000000000000001"))
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("0.000000000000000002"))

        view = build_ledger_projection_view(port, [t1, t2])
        proj = build_position_quantity_projection(view)

        assert proj.positions[0].quantity == Decimal("0.000000000000000003")

    def test_huge_precision_summation(self):
        """V: Exact large and high precision Decimal addition."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("12345678901234567890.123456789"))
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("0.000000001"))

        view = build_ledger_projection_view(port, [t1, t2])
        proj = build_position_quantity_projection(view)

        assert proj.positions[0].quantity == Decimal("12345678901234567890.123456790")

    def test_exponent_notation_summation(self):
        """W: Exact Decimal summation with scientific exponent notation."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("1E+3"))
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("0.25"))

        view = build_ledger_projection_view(port, [t1, t2])
        proj = build_position_quantity_projection(view)

        assert proj.positions[0].quantity == Decimal("1000.25")

    def test_cancellation_to_exact_zero(self):
        """X: Cancellation to exact zero with high precision."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("1000.000000000000000001"))
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.SELL, instrument_id=inst_id, quantity=Decimal("1000.000000000000000001"))

        view = build_ledger_projection_view(port, [t1, t2])
        proj = build_position_quantity_projection(view)

        assert proj.positions[0].quantity == Decimal("0")
        assert proj.open_positions == ()

    def test_low_decimal_context_precision_does_not_round_summation(self):
        """Y: Executing under localcontext(prec=6) does NOT round exact summation."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("1234567890.123456789"))
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("0.000000001"))

        view = build_ledger_projection_view(port, [t1, t2])

        with localcontext() as ctx:
            ctx.prec = 6
            proj = build_position_quantity_projection(view)

        assert proj.positions[0].quantity == Decimal("1234567890.123456790")

    def test_large_exponent_span_summation(self):
        """Z: Exponent span 1E+100 + 1E-100 retains both components without context truncation."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("1E+100"))
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("1E-100"))

        view = build_ledger_projection_view(port, [t1, t2])
        proj = build_position_quantity_projection(view)

        expected = Decimal("1" + ("0" * 199) + "1E-100")
        assert proj.positions[0].quantity == expected


# ─────────────────────────────────────────────────────────────────────────────
# 7. View Integrity Guards
# ─────────────────────────────────────────────────────────────────────────────

class TestViewIntegrityGuards:
    """Verifies boundary fail-closed defenses against forged or malformed views."""

    def test_wrong_input_type_rejected(self):
        """AA: Non-LedgerProjectionView input rejected with TypeError."""
        with pytest.raises(TypeError, match="must be an instance of LedgerProjectionView"):
            build_position_quantity_projection("not_a_view")  # type: ignore

    def test_forged_active_reversal_fails_closed(self):
        """AB: Active REVERSAL in view.active_transactions fails closed."""
        port = _make_portfolio()
        acc_id = uuid4()
        rev = _make_tx(port.id, acc_id, tx_type=TransactionType.REVERSAL, reverses_tx_id=uuid4())

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode=port.mode,
            as_of_recorded_at=None,
            known_transactions=(rev,),
            transaction_states=(),
            active_transactions=(rev,),  # Forged active reversal
        )

        with pytest.raises(PositionProjectionError, match="REVERSAL events must never appear in active_transactions"):
            build_position_quantity_projection(forged_view)

    def test_duplicate_active_physical_uuid_fails_closed(self):
        """AC: Duplicate physical UUID in active_transactions fails closed."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()
        shared_id = uuid4()

        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, id=shared_id)
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, id=shared_id)

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode=port.mode,
            as_of_recorded_at=None,
            known_transactions=(t1, t2),
            transaction_states=(),
            active_transactions=(t1, t2),
        )

        with pytest.raises(PositionProjectionError, match="Duplicate physical transaction ID detected in active_transactions"):
            build_position_quantity_projection(forged_view)

    def test_cross_portfolio_active_transaction_fails_closed(self):
        """AD: Active transaction from another portfolio fails closed."""
        port = _make_portfolio()
        other_port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()

        t_foreign = _make_tx(other_port_id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id)

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode=port.mode,
            as_of_recorded_at=None,
            known_transactions=(t_foreign,),
            transaction_states=(),
            active_transactions=(t_foreign,),
        )

        with pytest.raises(PositionProjectionError, match="does not match view"):
            build_position_quantity_projection(forged_view)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Immutability & Mutation Defense
# ─────────────────────────────────────────────────────────────────────────────

class TestImmutabilityAndMutationDefense:
    """Verifies output encapsulation and frozen dataclass guarantees."""

    def test_output_collections_are_immutable_tuples(self):
        """AE: Output collections are strictly tuples."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()
        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id)

        view = build_ledger_projection_view(port, [t1])
        proj = build_position_quantity_projection(view)

        assert isinstance(proj.positions, tuple)
        assert isinstance(proj.open_positions, tuple)

    def test_frozen_dataclass_mutations_rejected(self):
        """AF: Attempting to mutate PositionQuantityState or PositionQuantityProjection raises FrozenInstanceError."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()
        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id)

        view = build_ledger_projection_view(port, [t1])
        proj = build_position_quantity_projection(view)

        with pytest.raises(FrozenInstanceError):
            proj.as_of_recorded_at = datetime.now(timezone.utc)  # type: ignore

        with pytest.raises(FrozenInstanceError):
            proj.positions = ()  # type: ignore

        pos = proj.positions[0]
        with pytest.raises(FrozenInstanceError):
            pos.quantity = Decimal("100")  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 9. Phase 12C.2.1 Runtime Type Hardening & Boundary Defense
# ─────────────────────────────────────────────────────────────────────────────

class TestRuntimeTypeHardening:
    """Verifies runtime type enforcement and explicit enum matching against forged views."""

    def test_forged_string_buy_transaction_type_fails_closed(self):
        """A: Forged string transaction_type 'BUY' fails closed with PositionProjectionError."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        tx = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id)
        # Force string transaction_type bypassing annotations
        object.__setattr__(tx, "transaction_type", "BUY")

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode=port.mode,
            as_of_recorded_at=None,
            known_transactions=(tx,),
            transaction_states=(),
            active_transactions=(tx,),
        )

        with pytest.raises(PositionProjectionError, match="Active transaction transaction_type must be a TransactionType enum"):
            build_position_quantity_projection(forged_view)

    def test_forged_string_cash_deposit_transaction_type_fails_closed(self):
        """B: Forged string transaction_type 'cash_deposit' fails closed with PositionProjectionError."""
        port = _make_portfolio()
        acc_id = uuid4()

        tx = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT)
        object.__setattr__(tx, "transaction_type", "cash_deposit")

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode=port.mode,
            as_of_recorded_at=None,
            known_transactions=(tx,),
            transaction_states=(),
            active_transactions=(tx,),
        )

        with pytest.raises(PositionProjectionError, match="Active transaction transaction_type must be a TransactionType enum"):
            build_position_quantity_projection(forged_view)

    def test_forged_arbitrary_transaction_type_fails_closed(self):
        """C: Forged arbitrary transaction_type fails closed with PositionProjectionError."""
        port = _make_portfolio()
        acc_id = uuid4()

        tx = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY)
        object.__setattr__(tx, "transaction_type", 12345)

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode=port.mode,
            as_of_recorded_at=None,
            known_transactions=(tx,),
            transaction_states=(),
            active_transactions=(tx,),
        )

        with pytest.raises(PositionProjectionError, match="Active transaction transaction_type must be a TransactionType enum"):
            build_position_quantity_projection(forged_view)

    def test_malformed_tx_id_string_fails_closed(self):
        """D: String tx.id fails closed with PositionProjectionError."""
        port = _make_portfolio()
        acc_id = uuid4()

        tx = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY)
        object.__setattr__(tx, "id", "not-a-uuid")

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode=port.mode,
            as_of_recorded_at=None,
            known_transactions=(tx,),
            transaction_states=(),
            active_transactions=(tx,),
        )

        with pytest.raises(PositionProjectionError, match="Active transaction id must be a UUID"):
            build_position_quantity_projection(forged_view)

    def test_malformed_tx_account_id_string_fails_closed(self):
        """E: String tx.account_id fails closed with PositionProjectionError."""
        port = _make_portfolio()
        acc_id = uuid4()

        tx = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY)
        object.__setattr__(tx, "account_id", "not-a-uuid")

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode=port.mode,
            as_of_recorded_at=None,
            known_transactions=(tx,),
            transaction_states=(),
            active_transactions=(tx,),
        )

        with pytest.raises(PositionProjectionError, match="Active transaction account_id must be a UUID"):
            build_position_quantity_projection(forged_view)

    def test_malformed_tx_portfolio_id_type_fails_closed(self):
        """F: String tx.portfolio_id fails closed with PositionProjectionError."""
        port = _make_portfolio()
        acc_id = uuid4()

        tx = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY)
        object.__setattr__(tx, "portfolio_id", str(port.id))

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode=port.mode,
            as_of_recorded_at=None,
            known_transactions=(tx,),
            transaction_states=(),
            active_transactions=(tx,),
        )

        with pytest.raises(PositionProjectionError, match="Active transaction portfolio_id must be a UUID"):
            build_position_quantity_projection(forged_view)

    def test_malformed_view_portfolio_id_fails_closed(self):
        """G: String view.portfolio_id fails closed with PositionProjectionError."""
        port = _make_portfolio()

        forged_view = LedgerProjectionView(
            portfolio_id=str(port.id),  # type: ignore
            mode=port.mode,
            as_of_recorded_at=None,
            known_transactions=(),
            transaction_states=(),
            active_transactions=(),
        )

        with pytest.raises(PositionProjectionError, match="view.portfolio_id must be a UUID"):
            build_position_quantity_projection(forged_view)

    def test_malformed_view_mode_fails_closed(self):
        """H: String view.mode fails closed with PositionProjectionError."""
        port = _make_portfolio()

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode="MY_PORTFOLIO",  # type: ignore
            as_of_recorded_at=None,
            known_transactions=(),
            transaction_states=(),
            active_transactions=(),
        )

        with pytest.raises(PositionProjectionError, match="view.mode must be a PortfolioMode"):
            build_position_quantity_projection(forged_view)

    def test_naive_view_as_of_recorded_at_fails_closed(self):
        """I: Naive datetime view.as_of_recorded_at fails closed with PositionProjectionError."""
        port = _make_portfolio()

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode=port.mode,
            as_of_recorded_at=datetime(2026, 8, 10, 12, 0, 0),  # Naive
            known_transactions=(),
            transaction_states=(),
            active_transactions=(),
        )

        with pytest.raises(PositionProjectionError, match="view.as_of_recorded_at must be timezone-aware"):
            build_position_quantity_projection(forged_view)

    def test_null_utcoffset_view_as_of_recorded_at_fails_closed(self):
        """Phase 12C.2.2: view.as_of_recorded_at with NullOffsetTZ fails closed with PositionProjectionError."""
        port = _make_portfolio()

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode=port.mode,
            as_of_recorded_at=datetime(2026, 8, 10, 12, 0, 0, tzinfo=NullOffsetTZ()),
            known_transactions=(),
            transaction_states=(),
            active_transactions=(),
        )

        with pytest.raises(PositionProjectionError, match="view.as_of_recorded_at must be timezone-aware with non-null utcoffset"):
            build_position_quantity_projection(forged_view)

    def test_valid_timezone_aware_non_utc_timestamp_accepted(self):
        """J: Timezone-aware non-UTC timestamp in view is accepted unchanged."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()
        from datetime import timezone as tz, timedelta
        tz_plus3 = tz(timedelta(hours=3))
        non_utc_dt = datetime(2026, 8, 10, 15, 0, 0, tzinfo=tz_plus3)

        tx = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("10"))
        view = build_ledger_projection_view(port, [tx], as_of_recorded_at=non_utc_dt)
        proj = build_position_quantity_projection(view)

        assert proj.as_of_recorded_at == non_utc_dt
        assert len(proj.positions) == 1
        assert proj.positions[0].quantity == Decimal("10")


# ─────────────────────────────────────────────────────────────────────────────
# 10. State & Projection Constructor Invariant Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestStateConstructorInvariants:
    """Verifies direct PositionQuantityState constructor invariants."""

    def test_valid_state_accepted(self):
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()

        state = PositionQuantityState(
            portfolio_id=port_id,
            account_id=acc_id,
            instrument_id=inst_id,
            quantity=Decimal("100.5"),
        )
        assert state.portfolio_id == port_id
        assert state.account_id == acc_id
        assert state.instrument_id == inst_id
        assert state.quantity == Decimal("100.5")
        assert state.is_open is True

    def test_exact_zero_quantity_accepted(self):
        state = PositionQuantityState(
            portfolio_id=uuid4(),
            account_id=uuid4(),
            instrument_id=uuid4(),
            quantity=Decimal("0"),
        )
        assert state.quantity == Decimal("0")
        assert state.is_open is False

    def test_string_portfolio_id_rejected(self):
        with pytest.raises(PositionProjectionError, match="portfolio_id must be a UUID"):
            PositionQuantityState(
                portfolio_id="not-a-uuid",  # type: ignore
                account_id=uuid4(),
                instrument_id=uuid4(),
                quantity=Decimal("10"),
            )

    def test_string_account_id_rejected(self):
        with pytest.raises(PositionProjectionError, match="account_id must be a UUID"):
            PositionQuantityState(
                portfolio_id=uuid4(),
                account_id="not-a-uuid",  # type: ignore
                instrument_id=uuid4(),
                quantity=Decimal("10"),
            )

    def test_string_instrument_id_rejected(self):
        with pytest.raises(PositionProjectionError, match="instrument_id must be a UUID"):
            PositionQuantityState(
                portfolio_id=uuid4(),
                account_id=uuid4(),
                instrument_id="not-a-uuid",  # type: ignore
                quantity=Decimal("10"),
            )

    def test_float_quantity_rejected(self):
        with pytest.raises(PositionProjectionError, match="quantity must be a Decimal"):
            PositionQuantityState(
                portfolio_id=uuid4(),
                account_id=uuid4(),
                instrument_id=uuid4(),
                quantity=10.5,  # type: ignore
            )

    def test_int_quantity_rejected(self):
        with pytest.raises(PositionProjectionError, match="quantity must be a Decimal"):
            PositionQuantityState(
                portfolio_id=uuid4(),
                account_id=uuid4(),
                instrument_id=uuid4(),
                quantity=10,  # type: ignore
            )

    def test_bool_quantity_rejected(self):
        with pytest.raises(PositionProjectionError, match="quantity must be a Decimal"):
            PositionQuantityState(
                portfolio_id=uuid4(),
                account_id=uuid4(),
                instrument_id=uuid4(),
                quantity=True,  # type: ignore
            )

    def test_nan_quantity_rejected(self):
        with pytest.raises(PositionProjectionError, match="quantity must be finite"):
            PositionQuantityState(
                portfolio_id=uuid4(),
                account_id=uuid4(),
                instrument_id=uuid4(),
                quantity=Decimal("NaN"),
            )

    def test_infinity_quantity_rejected(self):
        with pytest.raises(PositionProjectionError, match="quantity must be finite"):
            PositionQuantityState(
                portfolio_id=uuid4(),
                account_id=uuid4(),
                instrument_id=uuid4(),
                quantity=Decimal("Infinity"),
            )

    def test_negative_quantity_rejected(self):
        with pytest.raises(PositionProjectionError, match="quantity cannot be negative"):
            PositionQuantityState(
                portfolio_id=uuid4(),
                account_id=uuid4(),
                instrument_id=uuid4(),
                quantity=Decimal("-0.01"),
            )


class TestProjectionConstructorInvariants:
    """Verifies direct PositionQuantityProjection constructor invariants."""

    def test_valid_projection_accepted(self):
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()

        pos = PositionQuantityState(
            portfolio_id=port_id,
            account_id=acc_id,
            instrument_id=inst_id,
            quantity=Decimal("10"),
        )

        proj = PositionQuantityProjection(
            portfolio_id=port_id,
            mode=PortfolioMode.MY_PORTFOLIO,
            as_of_recorded_at=None,
            positions=(pos,),
            open_positions=(pos,),
        )
        assert proj.portfolio_id == port_id
        assert proj.positions == (pos,)
        assert proj.open_positions == (pos,)

    def test_list_instead_of_tuple_positions_rejected(self):
        port_id = uuid4()
        with pytest.raises(PositionProjectionError, match="positions must be a tuple"):
            PositionQuantityProjection(
                portfolio_id=port_id,
                mode=PortfolioMode.MY_PORTFOLIO,
                as_of_recorded_at=None,
                positions=[],  # type: ignore
                open_positions=(),
            )

    def test_list_instead_of_tuple_open_positions_rejected(self):
        port_id = uuid4()
        with pytest.raises(PositionProjectionError, match="open_positions must be a tuple"):
            PositionQuantityProjection(
                portfolio_id=port_id,
                mode=PortfolioMode.MY_PORTFOLIO,
                as_of_recorded_at=None,
                positions=(),
                open_positions=[],  # type: ignore
            )

    def test_cross_portfolio_state_rejected(self):
        port_id = uuid4()
        other_port_id = uuid4()
        pos = PositionQuantityState(
            portfolio_id=other_port_id,
            account_id=uuid4(),
            instrument_id=uuid4(),
            quantity=Decimal("10"),
        )
        with pytest.raises(PositionProjectionError, match="does not match projection"):
            PositionQuantityProjection(
                portfolio_id=port_id,
                mode=PortfolioMode.MY_PORTFOLIO,
                as_of_recorded_at=None,
                positions=(pos,),
                open_positions=(pos,),
            )

    def test_duplicate_identity_in_positions_rejected(self):
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()

        p1 = PositionQuantityState(portfolio_id=port_id, account_id=acc_id, instrument_id=inst_id, quantity=Decimal("10"))
        p2 = PositionQuantityState(portfolio_id=port_id, account_id=acc_id, instrument_id=inst_id, quantity=Decimal("20"))

        with pytest.raises(PositionProjectionError, match="Duplicate position identity"):
            PositionQuantityProjection(
                portfolio_id=port_id,
                mode=PortfolioMode.MY_PORTFOLIO,
                as_of_recorded_at=None,
                positions=(p1, p2),
                open_positions=(p1,),
            )

    def test_zero_state_inside_open_positions_rejected(self):
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()

        p_zero = PositionQuantityState(portfolio_id=port_id, account_id=acc_id, instrument_id=inst_id, quantity=Decimal("0"))

        with pytest.raises(PositionProjectionError, match="Zero-quantity position .* must not appear in open_positions"):
            PositionQuantityProjection(
                portfolio_id=port_id,
                mode=PortfolioMode.MY_PORTFOLIO,
                as_of_recorded_at=None,
                positions=(p_zero,),
                open_positions=(p_zero,),
            )

    def test_open_position_absent_from_positions_rejected(self):
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()

        p1 = PositionQuantityState(portfolio_id=port_id, account_id=acc_id, instrument_id=inst_id, quantity=Decimal("10"))

        with pytest.raises(PositionProjectionError, match="Open position .* not found in positions"):
            PositionQuantityProjection(
                portfolio_id=port_id,
                mode=PortfolioMode.MY_PORTFOLIO,
                as_of_recorded_at=None,
                positions=(),
                open_positions=(p1,),
            )

    def test_positive_position_missing_from_open_positions_rejected(self):
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()

        p1 = PositionQuantityState(portfolio_id=port_id, account_id=acc_id, instrument_id=inst_id, quantity=Decimal("10"))

        with pytest.raises(PositionProjectionError, match="Positive position .* is missing from open_positions"):
            PositionQuantityProjection(
                portfolio_id=port_id,
                mode=PortfolioMode.MY_PORTFOLIO,
                as_of_recorded_at=None,
                positions=(p1,),
                open_positions=(),  # Missing p1
            )

    def test_malformed_mode_rejected(self):
        port_id = uuid4()
        with pytest.raises(PositionProjectionError, match="mode must be a PortfolioMode"):
            PositionQuantityProjection(
                portfolio_id=port_id,
                mode="INVALID_MODE",  # type: ignore
                as_of_recorded_at=None,
                positions=(),
                open_positions=(),
            )

    def test_naive_as_of_recorded_at_rejected(self):
        port_id = uuid4()
        with pytest.raises(PositionProjectionError, match="as_of_recorded_at must be timezone-aware"):
            PositionQuantityProjection(
                portfolio_id=port_id,
                mode=PortfolioMode.MY_PORTFOLIO,
                as_of_recorded_at=datetime(2026, 8, 10, 12, 0, 0),  # Naive
                positions=(),
                open_positions=(),
            )

    def test_null_utcoffset_as_of_recorded_at_rejected(self):
        """Phase 12C.2.2: Direct constructor with NullOffsetTZ fails closed."""
        port_id = uuid4()
        with pytest.raises(PositionProjectionError, match="as_of_recorded_at must be timezone-aware with non-null utcoffset"):
            PositionQuantityProjection(
                portfolio_id=port_id,
                mode=PortfolioMode.MY_PORTFOLIO,
                as_of_recorded_at=datetime(2026, 8, 10, 12, 0, 0, tzinfo=NullOffsetTZ()),
                positions=(),
                open_positions=(),
            )

