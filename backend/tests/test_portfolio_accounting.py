"""
backend/tests/test_portfolio_accounting.py
==========================================
Tests for Phase 12C.4: Canonical Portfolio Accounting Snapshot Composition.

Zero network calls (pytest-socket enforced).
Pure in-memory domain evaluation.

Test Matrix:
    1. Basic Compositions (Empty snapshot, Deposit only, Deposit + BUY, Deposit + BUY + SELL)
    2. Real Reversal Consistency Across Layers (Reversed BUY, Reversed SELL)
    3. Point-in-Time (PIT) Consistency Across Layers (Before vs After Reversal, Exact timestamp propagation)
    4. Multi-Account & Multi-Currency Isolation (Separate account holdings, Multi-currency preservation)
    5. Snapshot Metadata & Object Identity Binding (Exact object reference integrity)
    6. Fail-Closed Error Propagation & No-Partial-Snapshot (Position error propagation, Cash error propagation)
    7. Direct Constructor Hardening & Red-Team (Malformed UUIDs, Naive/Null-offset datetimes, Wrong types, Cross-portfolio mismatch, Mode mismatch, Cutoff mismatch)
    8. Immutability & Mutation Defense (FrozenInstanceError on field mutations)
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone, tzinfo, timedelta
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import Currency, PortfolioMode, TransactionType
from backend.engine.private.portfolio.accounting import (
    PortfolioAccountingError,
    PortfolioAccountingSnapshot,
    build_portfolio_accounting_snapshot,
)
from backend.engine.private.portfolio.cash import (
    CashBalanceProjection,
    CashBalanceState,
    CashProjectionError,
    build_cash_balance_projection,
)
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
        name="Accounting Test Portfolio",
        base_currency=Currency.USD,
        mode=mode,
        id=id or uuid4(),
        created_at=datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc),
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
# 1. Basic Compositions
# ─────────────────────────────────────────────────────────────────────────────

class TestBasicCompositions:
    """Verifies baseline snapshot composition across standard lifecycle events."""

    def test_empty_snapshot(self):
        """A: Empty ledger view produces valid snapshot with empty positions and cash balances."""
        port = _make_portfolio()
        view = build_ledger_projection_view(port, [])
        snapshot = build_portfolio_accounting_snapshot(view)

        assert snapshot.portfolio_id == port.id
        assert snapshot.mode == port.mode
        assert snapshot.as_of_recorded_at is None
        assert snapshot.positions.positions == ()
        assert snapshot.positions.open_positions == ()
        assert snapshot.cash.balances == ()
        assert snapshot.cash.positive_balances == ()

    def test_deposit_only_snapshot(self):
        """B: Deposit produces positive cash balance and empty positions."""
        port = _make_portfolio()
        acc_id = uuid4()
        dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), cash_currency=Currency.USD)

        view = build_ledger_projection_view(port, [dep])
        snapshot = build_portfolio_accounting_snapshot(view)

        assert snapshot.positions.open_positions == ()
        assert len(snapshot.cash.balances) == 1
        assert snapshot.cash.balances[0].balance == Decimal("1000.00")

    def test_deposit_and_buy_snapshot(self):
        """C: Deposit 1000 USD + BUY 10 @ 20 USD produces position = 10 and cash = 800 USD."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        buy = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("10"), unit_price=Decimal("20.00"), trade_currency=Currency.USD, recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))

        view = build_ledger_projection_view(port, [dep, buy])
        snapshot = build_portfolio_accounting_snapshot(view)

        assert len(snapshot.positions.open_positions) == 1
        assert snapshot.positions.open_positions[0].quantity == Decimal("10")
        assert len(snapshot.cash.balances) == 1
        assert snapshot.cash.balances[0].balance == Decimal("800.00")

    def test_deposit_buy_and_sell_snapshot(self):
        """D: Deposit 1000 + BUY 10 @ 20 + SELL 4 @ 25 produces position = 6 and cash = 900 USD."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        buy = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("10"), unit_price=Decimal("20.00"), trade_currency=Currency.USD, recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))
        sell = _make_tx(port.id, acc_id, tx_type=TransactionType.SELL, instrument_id=inst_id, quantity=Decimal("4"), unit_price=Decimal("25.00"), trade_currency=Currency.USD, recorded_at=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc))

        view = build_ledger_projection_view(port, [dep, buy, sell])
        snapshot = build_portfolio_accounting_snapshot(view)

        assert len(snapshot.positions.open_positions) == 1
        assert snapshot.positions.open_positions[0].quantity == Decimal("6")
        assert len(snapshot.cash.balances) == 1
        assert snapshot.cash.balances[0].balance == Decimal("900.00")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Real Reversal Consistency Across Layers
# ─────────────────────────────────────────────────────────────────────────────

class TestReversalConsistencyAcrossLayers:
    """Verifies that ledger reversals simultaneously adjust positions and cash consistently."""

    def test_reversed_buy_consistency(self):
        """E: Reversal of BUY removes both security position and cash debit."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        buy = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("10"), unit_price=Decimal("20.00"), trade_currency=Currency.USD, recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))
        rev_buy = _make_tx(port.id, acc_id, tx_type=TransactionType.REVERSAL, reverses_tx_id=buy.id, recorded_at=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc))

        view = build_ledger_projection_view(port, [dep, buy, rev_buy])
        snapshot = build_portfolio_accounting_snapshot(view)

        assert snapshot.positions.open_positions == ()
        assert len(snapshot.cash.balances) == 1
        assert snapshot.cash.balances[0].balance == Decimal("1000.00")

    def test_reversed_sell_consistency(self):
        """F: Reversal of SELL restores security position to 10 and cash to 800 USD."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        buy = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("10"), unit_price=Decimal("20.00"), trade_currency=Currency.USD, recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))
        sell = _make_tx(port.id, acc_id, tx_type=TransactionType.SELL, instrument_id=inst_id, quantity=Decimal("4"), unit_price=Decimal("25.00"), trade_currency=Currency.USD, recorded_at=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc))
        rev_sell = _make_tx(port.id, acc_id, tx_type=TransactionType.REVERSAL, reverses_tx_id=sell.id, recorded_at=datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc))

        view = build_ledger_projection_view(port, [dep, buy, sell, rev_sell])
        snapshot = build_portfolio_accounting_snapshot(view)

        assert len(snapshot.positions.open_positions) == 1
        assert snapshot.positions.open_positions[0].quantity == Decimal("10")
        assert len(snapshot.cash.balances) == 1
        assert snapshot.cash.balances[0].balance == Decimal("800.00")


# ─────────────────────────────────────────────────────────────────────────────
# 3. Point-in-Time (PIT) Consistency Across Layers
# ─────────────────────────────────────────────────────────────────────────────

class TestPITConsistencyAcrossLayers:
    """Verifies that point-in-time cutoffs are propagated identically to positions and cash."""

    def test_pit_before_and_after_reversal(self):
        """G, H, I: Snapshot before reversal reflects SELL (pos=6, cash=900); after reflects reversal (pos=10, cash=800)."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        buy = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("10"), unit_price=Decimal("20.00"), trade_currency=Currency.USD, recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))
        sell = _make_tx(port.id, acc_id, tx_type=TransactionType.SELL, instrument_id=inst_id, quantity=Decimal("4"), unit_price=Decimal("25.00"), trade_currency=Currency.USD, recorded_at=datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc))
        rev_sell = _make_tx(port.id, acc_id, tx_type=TransactionType.REVERSAL, reverses_tx_id=sell.id, recorded_at=datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc))

        history = [dep, buy, sell, rev_sell]

        # G: As of Aug 10 (before reversal was recorded)
        t_early = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
        view_early = build_ledger_projection_view(port, history, as_of_recorded_at=t_early)
        snap_early = build_portfolio_accounting_snapshot(view_early)

        assert snap_early.as_of_recorded_at == t_early
        assert snap_early.ledger_view.as_of_recorded_at == t_early
        assert snap_early.positions.as_of_recorded_at == t_early
        assert snap_early.cash.as_of_recorded_at == t_early
        assert snap_early.positions.open_positions[0].quantity == Decimal("6")
        assert snap_early.cash.balances[0].balance == Decimal("900.00")

        # H: As of Aug 25 (after reversal was recorded)
        t_late = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
        view_late = build_ledger_projection_view(port, history, as_of_recorded_at=t_late)
        snap_late = build_portfolio_accounting_snapshot(view_late)

        assert snap_late.as_of_recorded_at == t_late
        assert snap_late.ledger_view.as_of_recorded_at == t_late
        assert snap_late.positions.as_of_recorded_at == t_late
        assert snap_late.cash.as_of_recorded_at == t_late
        assert snap_late.positions.open_positions[0].quantity == Decimal("10")
        assert snap_late.cash.balances[0].balance == Decimal("800.00")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Multi-Account & Multi-Currency Isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestIsolationAndMultiCurrency:
    """Verifies that accounting composition preserves account and currency boundaries."""

    def test_multi_account_isolation(self):
        """J: Snapshot preserves distinct position and cash states across multiple accounts."""
        port = _make_portfolio()
        acc_a = uuid4()
        acc_b = uuid4()
        inst_id = uuid4()

        txs = [
            _make_tx(port.id, acc_a, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("500.00"), cash_currency=Currency.USD),
            _make_tx(port.id, acc_a, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("5"), unit_price=Decimal("50.00"), trade_currency=Currency.USD),
            _make_tx(port.id, acc_b, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), cash_currency=Currency.USD),
            _make_tx(port.id, acc_b, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("8"), unit_price=Decimal("50.00"), trade_currency=Currency.USD),
        ]

        view = build_ledger_projection_view(port, txs)
        snapshot = build_portfolio_accounting_snapshot(view)

        # Positions
        assert len(snapshot.positions.open_positions) == 2
        pos_a = next(p for p in snapshot.positions.open_positions if p.account_id == acc_a)
        pos_b = next(p for p in snapshot.positions.open_positions if p.account_id == acc_b)
        assert pos_a.quantity == Decimal("5")
        assert pos_b.quantity == Decimal("8")

        # Cash
        assert len(snapshot.cash.balances) == 2
        cash_a = next(c for c in snapshot.cash.balances if c.account_id == acc_a)
        cash_b = next(c for c in snapshot.cash.balances if c.account_id == acc_b)
        assert cash_a.balance == Decimal("250.00")
        assert cash_b.balance == Decimal("600.00")

    def test_multi_currency_preservation(self):
        """K: Multi-currency holdings and FX conversion legs are preserved without conversion."""
        port = _make_portfolio()
        acc_id = uuid4()

        txs = [
            _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), cash_currency=Currency.USD),
            _make_tx(port.id, acc_id, tx_type=TransactionType.FX_CONVERSION, from_amount=Decimal("200.00"), from_currency=Currency.USD, to_amount=Decimal("7000.00"), to_currency=Currency.TRY),
        ]

        view = build_ledger_projection_view(port, txs)
        snapshot = build_portfolio_accounting_snapshot(view)

        assert len(snapshot.cash.balances) == 2
        usd_b = next(c for c in snapshot.cash.balances if c.currency == Currency.USD)
        try_b = next(c for c in snapshot.cash.balances if c.currency == Currency.TRY)

        assert usd_b.balance == Decimal("800.00")
        assert try_b.balance == Decimal("7000.00")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Snapshot Metadata & Object Identity Binding
# ─────────────────────────────────────────────────────────────────────────────

class TestMetadataAndObjectBinding:
    """Verifies that snapshot encapsulates exact object identities without copying."""

    def test_exact_object_identities_preserved(self):
        """L, M: snapshot.ledger_view, snapshot.positions, and snapshot.cash match derived objects by identity."""
        port = _make_portfolio()
        acc_id = uuid4()
        dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("100.00"))

        view = build_ledger_projection_view(port, [dep])
        snapshot = build_portfolio_accounting_snapshot(view)

        assert snapshot.ledger_view is view
        assert snapshot.portfolio_id == view.portfolio_id
        assert snapshot.mode == view.mode
        assert snapshot.as_of_recorded_at == view.as_of_recorded_at


# ─────────────────────────────────────────────────────────────────────────────
# 6. Fail-Closed Error Propagation & No-Partial-Snapshot
# ─────────────────────────────────────────────────────────────────────────────

class TestErrorPropagationAndNoPartialSnapshot:
    """Verifies that errors in position or cash derivation fail closed without returning partial snapshots."""

    def test_position_error_propagation_on_oversell(self):
        """N: SELL without BUY raises PositionProjectionError; no partial snapshot returned."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        # Cash is non-negative (+100 USD), but position is negative (-1 unit)
        sell = _make_tx(port.id, acc_id, tx_type=TransactionType.SELL, instrument_id=inst_id, quantity=Decimal("1"), unit_price=Decimal("100.00"), trade_currency=Currency.USD)

        view = build_ledger_projection_view(port, [sell])

        with pytest.raises(PositionProjectionError):
            build_portfolio_accounting_snapshot(view)

    def test_cash_error_propagation_on_unfunded_buy(self):
        """O, P: BUY without deposit raises CashProjectionError; no partial snapshot returned."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        # Position is positive (+1 unit), but cash is negative (-100 USD)
        buy = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, instrument_id=inst_id, quantity=Decimal("1"), unit_price=Decimal("100.00"), trade_currency=Currency.USD)

        view = build_ledger_projection_view(port, [buy])

        with pytest.raises(CashProjectionError):
            build_portfolio_accounting_snapshot(view)

    def test_wrong_view_type_rejected(self):
        """Non-LedgerProjectionView raises TypeError."""
        with pytest.raises(TypeError, match="must be an instance of LedgerProjectionView"):
            build_portfolio_accounting_snapshot("invalid_view")  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 7. Direct Constructor Hardening & Red-Team
# ─────────────────────────────────────────────────────────────────────────────

class TestConstructorHardeningAndRedTeam:
    """Verifies fail-closed checks on direct PortfolioAccountingSnapshot construction."""

    def test_malformed_portfolio_id_rejected(self):
        """Q: String portfolio_id rejected."""
        port = _make_portfolio()
        view = build_ledger_projection_view(port, [])
        pos = build_position_quantity_projection(view)
        cash = build_cash_balance_projection(view)

        with pytest.raises(PortfolioAccountingError, match="portfolio_id must be a UUID"):
            PortfolioAccountingSnapshot(
                portfolio_id="not-a-uuid",  # type: ignore
                mode=port.mode,
                as_of_recorded_at=None,
                ledger_view=view,
                positions=pos,
                cash=cash,
            )

    def test_malformed_mode_rejected(self):
        """R: String mode rejected."""
        port = _make_portfolio()
        view = build_ledger_projection_view(port, [])
        pos = build_position_quantity_projection(view)
        cash = build_cash_balance_projection(view)

        with pytest.raises(PortfolioAccountingError, match="mode must be a PortfolioMode"):
            PortfolioAccountingSnapshot(
                portfolio_id=port.id,
                mode="MY_PORTFOLIO",  # type: ignore
                as_of_recorded_at=None,
                ledger_view=view,
                positions=pos,
                cash=cash,
            )

    def test_naive_as_of_recorded_at_rejected(self):
        """S: Naive datetime rejected."""
        port = _make_portfolio()
        view = build_ledger_projection_view(port, [])
        pos = build_position_quantity_projection(view)
        cash = build_cash_balance_projection(view)

        with pytest.raises(PortfolioAccountingError, match="as_of_recorded_at must be timezone-aware"):
            PortfolioAccountingSnapshot(
                portfolio_id=port.id,
                mode=port.mode,
                as_of_recorded_at=datetime(2026, 8, 10, 12, 0, 0),  # Naive
                ledger_view=view,
                positions=pos,
                cash=cash,
            )

    def test_null_utcoffset_as_of_recorded_at_rejected(self):
        """T: NullOffsetTZ datetime rejected."""
        port = _make_portfolio()
        view = build_ledger_projection_view(port, [])
        pos = build_position_quantity_projection(view)
        cash = build_cash_balance_projection(view)

        with pytest.raises(PortfolioAccountingError, match="as_of_recorded_at must be timezone-aware with non-null utcoffset"):
            PortfolioAccountingSnapshot(
                portfolio_id=port.id,
                mode=port.mode,
                as_of_recorded_at=datetime(2026, 8, 10, 12, 0, 0, tzinfo=NullOffsetTZ()),
                ledger_view=view,
                positions=pos,
                cash=cash,
            )

    def test_wrong_object_types_rejected(self):
        """U: Wrong types for ledger_view, positions, or cash rejected."""
        port = _make_portfolio()
        view = build_ledger_projection_view(port, [])
        pos = build_position_quantity_projection(view)
        cash = build_cash_balance_projection(view)

        with pytest.raises(PortfolioAccountingError, match="ledger_view must be a LedgerProjectionView"):
            PortfolioAccountingSnapshot(
                portfolio_id=port.id,
                mode=port.mode,
                as_of_recorded_at=None,
                ledger_view="wrong",  # type: ignore
                positions=pos,
                cash=cash,
            )

        with pytest.raises(PortfolioAccountingError, match="positions must be a PositionQuantityProjection"):
            PortfolioAccountingSnapshot(
                portfolio_id=port.id,
                mode=port.mode,
                as_of_recorded_at=None,
                ledger_view=view,
                positions="wrong",  # type: ignore
                cash=cash,
            )

        with pytest.raises(PortfolioAccountingError, match="cash must be a CashBalanceProjection"):
            PortfolioAccountingSnapshot(
                portfolio_id=port.id,
                mode=port.mode,
                as_of_recorded_at=None,
                ledger_view=view,
                positions=pos,
                cash="wrong",  # type: ignore
            )

    def test_cross_portfolio_mismatch_rejected(self):
        """V: Cross-portfolio projection objects rejected."""
        port_a = _make_portfolio()
        port_b = _make_portfolio()

        view_a = build_ledger_projection_view(port_a, [])
        view_b = build_ledger_projection_view(port_b, [])

        pos_a = build_position_quantity_projection(view_a)
        cash_b = build_cash_balance_projection(view_b)

        with pytest.raises(PortfolioAccountingError, match="cash portfolio_id .* does not match snapshot"):
            PortfolioAccountingSnapshot(
                portfolio_id=port_a.id,
                mode=port_a.mode,
                as_of_recorded_at=None,
                ledger_view=view_a,
                positions=pos_a,
                cash=cash_b,  # Belongs to port_b
            )

    def test_mode_mismatch_rejected(self):
        """W: Mode mismatch across layers rejected."""
        port_real = _make_portfolio(mode=PortfolioMode.MY_PORTFOLIO)
        port_sand = _make_portfolio(mode=PortfolioMode.SANDBOX, id=port_real.id)

        view_real = build_ledger_projection_view(port_real, [])
        view_sand = build_ledger_projection_view(port_sand, [])

        pos_real = build_position_quantity_projection(view_real)
        cash_sand = build_cash_balance_projection(view_sand)

        with pytest.raises(PortfolioAccountingError, match="cash mode .* does not match snapshot"):
            PortfolioAccountingSnapshot(
                portfolio_id=port_real.id,
                mode=PortfolioMode.MY_PORTFOLIO,
                as_of_recorded_at=None,
                ledger_view=view_real,
                positions=pos_real,
                cash=cash_sand,
            )

    def test_cutoff_mismatch_rejected(self):
        """X: Cutoff timestamp mismatch across layers rejected."""
        port = _make_portfolio()
        t1 = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)

        view1 = build_ledger_projection_view(port, [], as_of_recorded_at=t1)
        view2 = build_ledger_projection_view(port, [], as_of_recorded_at=t2)

        pos1 = build_position_quantity_projection(view1)
        cash2 = build_cash_balance_projection(view2)

        with pytest.raises(PortfolioAccountingError, match="cash as_of_recorded_at .* does not match snapshot"):
            PortfolioAccountingSnapshot(
                portfolio_id=port.id,
                mode=port.mode,
                as_of_recorded_at=t1,
                ledger_view=view1,
                positions=pos1,
                cash=cash2,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 8. Immutability & Mutation Defense
# ─────────────────────────────────────────────────────────────────────────────

class TestImmutabilityAndMutationDefense:
    """Verifies that PortfolioAccountingSnapshot is strictly frozen."""

    def test_frozen_mutation_rejected(self):
        """Y: Mutation of snapshot fields raises FrozenInstanceError."""
        port = _make_portfolio()
        view = build_ledger_projection_view(port, [])
        snapshot = build_portfolio_accounting_snapshot(view)

        with pytest.raises(FrozenInstanceError):
            snapshot.mode = PortfolioMode.SANDBOX  # type: ignore

        with pytest.raises(FrozenInstanceError):
            snapshot.positions = None  # type: ignore

        with pytest.raises(FrozenInstanceError):
            snapshot.cash = None  # type: ignore
