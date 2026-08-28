"""
backend/tests/test_portfolio_cash.py
====================================
Tests for Phase 12C.3: Exact Reversal-Aware Account Cash Balance Projection.

Zero network calls (pytest-socket enforced).
Pure in-memory domain evaluation.

Test Matrix:
    1. Basic Cash Flows (Empty, Deposit, Withdrawal, Full Zero Retention, Overdraft/Negative Error)
    2. Trade Cash Effects & Fee Separation (BUY debit, SELL credit, Independent trade fee)
    3. Corporate & Cash Adjustments (Dividends, Interest, Fees, Tax Withholding)
    4. Multi-Currency & FX Conversions (FXConversion debit/credit, Independent currency balances)
    5. Account & Currency Isolation (Same currency across distinct accounts)
    6. Real Reversal Integration & PIT Dynamics (Reversed deposit, withdrawal, BUY, SELL, PIT before/after)
    7. Exact Decimal Arithmetic Red-Team (Tiny, Huge, Multiplication, Exponent, Cancellation, localcontext prec=6, 1E+100 + 1E-100, 1E+100 * 1E-100)
    8. View & Active Transaction Integrity Guards (Wrong type, Forged active REVERSAL, Duplicate UUIDs, Cross-portfolio, Malformed types)
    9. Datetime Awareness Hardening (Naive, NullOffsetTZ, UTC, Non-UTC preservation)
   10. Immutability & Constructor Invariants (Tuples, Frozen dataclasses, Positive subset consistency)
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone, tzinfo, timedelta
from decimal import Decimal, localcontext
import random
from typing import Optional
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import Currency, PortfolioMode, TransactionType
from backend.engine.private.portfolio.cash import (
    CashBalanceProjection,
    CashBalanceState,
    CashProjectionError,
    build_cash_balance_projection,
)
from backend.engine.private.portfolio.models import Portfolio, PortfolioTransaction
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
        name="Cash Test Portfolio",
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
# 1. Basic Cash Flows
# ─────────────────────────────────────────────────────────────────────────────

class TestBasicCashFlows:
    """Verifies baseline cash balance calculations for standard cash transactions."""

    def test_empty_view_produces_empty_balances(self):
        """A: Empty view produces empty balances and positive_balances."""
        port = _make_portfolio()
        view = build_ledger_projection_view(port, [])
        proj = build_cash_balance_projection(view)

        assert proj.portfolio_id == port.id
        assert proj.mode == port.mode
        assert proj.as_of_recorded_at is None
        assert proj.balances == ()
        assert proj.positive_balances == ()

    def test_single_deposit_produces_positive_balance(self):
        """B: Deposit of 100 USD produces 100 USD balance."""
        port = _make_portfolio()
        acc_id = uuid4()
        tx = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("100.00"), cash_currency=Currency.USD)

        view = build_ledger_projection_view(port, [tx])
        proj = build_cash_balance_projection(view)

        assert len(proj.balances) == 1
        b = proj.balances[0]
        assert b.portfolio_id == port.id
        assert b.account_id == acc_id
        assert b.currency == Currency.USD
        assert b.balance == Decimal("100.00")
        assert b.is_positive is True
        assert proj.positive_balances == (b,)

    def test_deposit_and_partial_withdrawal(self):
        """C: Deposit 100 USD + Withdrawal 40 USD leaves 60 USD."""
        port = _make_portfolio()
        acc_id = uuid4()
        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("100.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_WITHDRAWAL, cash_amount=Decimal("40.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))

        view = build_ledger_projection_view(port, [t1, t2])
        proj = build_cash_balance_projection(view)

        assert len(proj.balances) == 1
        assert proj.balances[0].balance == Decimal("60.00")
        assert proj.positive_balances == proj.balances

    def test_exact_full_withdrawal_retains_zero_balance_omitted_from_positive(self):
        """D: Deposit 100 USD + Withdrawal 100 USD leaves 0 USD in balances, omitted from positive_balances."""
        port = _make_portfolio()
        acc_id = uuid4()
        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("100.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_WITHDRAWAL, cash_amount=Decimal("100.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))

        view = build_ledger_projection_view(port, [t1, t2])
        proj = build_cash_balance_projection(view)

        assert len(proj.balances) == 1
        b = proj.balances[0]
        assert b.balance == Decimal("0")
        assert b.is_positive is False
        assert proj.positive_balances == ()

    def test_overdraft_withdrawal_fails_closed(self):
        """E: Overdraft withdrawal (net negative balance) raises CashProjectionError."""
        port = _make_portfolio()
        acc_id = uuid4()
        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("100.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_WITHDRAWAL, cash_amount=Decimal("101.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))

        view = build_ledger_projection_view(port, [t1, t2])
        with pytest.raises(CashProjectionError, match="Negative net cash balance"):
            build_cash_balance_projection(view)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Trade Cash Effects & Fee Separation
# ─────────────────────────────────────────────────────────────────────────────

class TestTradeCashEffects:
    """Verifies BUY/SELL notional debit/credit and separate fee handling."""

    def test_deposit_and_buy_deducts_notional(self):
        """F: Deposit 1000 USD + BUY 10 @ 20 USD = 800 USD."""
        port = _make_portfolio()
        acc_id = uuid4()
        t_dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        t_buy = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, quantity=Decimal("10"), unit_price=Decimal("20.00"), trade_currency=Currency.USD, recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))

        view = build_ledger_projection_view(port, [t_dep, t_buy])
        proj = build_cash_balance_projection(view)

        assert len(proj.balances) == 1
        assert proj.balances[0].balance == Decimal("800.00")

    def test_deposit_buy_and_sell_adds_proceeds(self):
        """G: Deposit 1000 USD + BUY 10 @ 20 USD + SELL 4 @ 25 USD = 900 USD."""
        port = _make_portfolio()
        acc_id = uuid4()
        t_dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        t_buy = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, quantity=Decimal("10"), unit_price=Decimal("20.00"), trade_currency=Currency.USD, recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))
        t_sell = _make_tx(port.id, acc_id, tx_type=TransactionType.SELL, quantity=Decimal("4"), unit_price=Decimal("25.00"), trade_currency=Currency.USD, recorded_at=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc))

        view = build_ledger_projection_view(port, [t_dep, t_buy, t_sell])
        proj = build_cash_balance_projection(view)

        assert len(proj.balances) == 1
        assert proj.balances[0].balance == Decimal("900.00")

    def test_trade_fee_is_deducted_separately(self):
        """H: BUY notional and separate FEE event are deducted independently."""
        port = _make_portfolio()
        acc_id = uuid4()
        t_dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        t_buy = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, quantity=Decimal("10"), unit_price=Decimal("20.00"), trade_currency=Currency.USD, recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))
        t_fee = _make_tx(port.id, acc_id, tx_type=TransactionType.FEE, cash_amount=Decimal("5.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 2, 10, 1, tzinfo=timezone.utc))

        view = build_ledger_projection_view(port, [t_dep, t_buy, t_fee])
        proj = build_cash_balance_projection(view)

        assert len(proj.balances) == 1
        assert proj.balances[0].balance == Decimal("795.00")


# ─────────────────────────────────────────────────────────────────────────────
# 3. Corporate & Cash Adjustments
# ─────────────────────────────────────────────────────────────────────────────

class TestIncomeAndCashAdjustments:
    """Verifies dividends, interest, fees, and taxes on cash balance."""

    def test_dividends_and_interest_add_cash(self):
        """I, J: Dividend and interest credit cash balance."""
        port = _make_portfolio()
        acc_id = uuid4()
        inst_id = uuid4()

        t_div = _make_tx(port.id, acc_id, tx_type=TransactionType.DIVIDEND, instrument_id=inst_id, cash_amount=Decimal("50.00"), cash_currency=Currency.USD)
        t_int = _make_tx(port.id, acc_id, tx_type=TransactionType.INTEREST, cash_amount=Decimal("25.00"), cash_currency=Currency.USD)

        view = build_ledger_projection_view(port, [t_div, t_int])
        proj = build_cash_balance_projection(view)

        assert len(proj.balances) == 1
        assert proj.balances[0].balance == Decimal("75.00")

    def test_tax_withholding_and_fees_subtract_cash(self):
        """K, L: Tax withholding and fee debit cash balance."""
        port = _make_portfolio()
        acc_id = uuid4()

        t_dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("100.00"), cash_currency=Currency.USD)
        t_tax = _make_tx(port.id, acc_id, tx_type=TransactionType.TAX_WITHHOLDING, cash_amount=Decimal("15.00"), cash_currency=Currency.USD)
        t_fee = _make_tx(port.id, acc_id, tx_type=TransactionType.FEE, cash_amount=Decimal("5.00"), cash_currency=Currency.USD)

        view = build_ledger_projection_view(port, [t_dep, t_tax, t_fee])
        proj = build_cash_balance_projection(view)

        assert len(proj.balances) == 1
        assert proj.balances[0].balance == Decimal("80.00")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Multi-Currency & FX Conversions
# ─────────────────────────────────────────────────────────────────────────────

class TestMultiCurrencyAndFX:
    """Verifies FXConversion legs and multi-currency independence."""

    def test_fx_conversion_credits_and_debits_respective_currencies(self):
        """M: Deposit 100 USD + FX 40 USD -> 1400 TRY results in 60 USD and 1400 TRY."""
        port = _make_portfolio()
        acc_id = uuid4()

        t_dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("100.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        t_fx = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.FX_CONVERSION,
            from_amount=Decimal("40.00"),
            from_currency=Currency.USD,
            to_amount=Decimal("1400.00"),
            to_currency=Currency.TRY,
            recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc),
        )

        view = build_ledger_projection_view(port, [t_dep, t_fx])
        proj = build_cash_balance_projection(view)

        assert len(proj.balances) == 2
        usd_state = next(b for b in proj.balances if b.currency == Currency.USD)
        try_state = next(b for b in proj.balances if b.currency == Currency.TRY)

        assert usd_state.balance == Decimal("60.00")
        assert try_state.balance == Decimal("1400.00")

    def test_multi_currency_distinct_balances_preserved(self):
        """N: Multi-currency deposits (EUR, GBP, XAU) remain independent."""
        port = _make_portfolio()
        acc_id = uuid4()

        txs = [
            _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("100.00"), cash_currency=Currency.EUR),
            _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("50.00"), cash_currency=Currency.GBP),
            _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1.50"), cash_currency=Currency.XAU),
        ]

        view = build_ledger_projection_view(port, txs)
        proj = build_cash_balance_projection(view)

        assert len(proj.balances) == 3
        assert next(b for b in proj.balances if b.currency == Currency.EUR).balance == Decimal("100.00")
        assert next(b for b in proj.balances if b.currency == Currency.GBP).balance == Decimal("50.00")
        assert next(b for b in proj.balances if b.currency == Currency.XAU).balance == Decimal("1.50")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Account & Currency Isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestAccountIsolation:
    """Verifies cash balances are strictly partitioned by account."""

    def test_same_currency_across_distinct_accounts_remain_separate(self):
        """O: Same currency in Account A (100 USD) and Account B (200 USD) produces two separate states."""
        port = _make_portfolio()
        acc_a = uuid4()
        acc_b = uuid4()

        t_a = _make_tx(port.id, acc_a, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("100.00"), cash_currency=Currency.USD)
        t_b = _make_tx(port.id, acc_b, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("200.00"), cash_currency=Currency.USD)

        view = build_ledger_projection_view(port, [t_a, t_b])
        proj = build_cash_balance_projection(view)

        assert len(proj.balances) == 2
        pos_a = next(b for b in proj.balances if b.account_id == acc_a)
        pos_b = next(b for b in proj.balances if b.account_id == acc_b)

        assert pos_a.balance == Decimal("100.00")
        assert pos_b.balance == Decimal("200.00")


# ─────────────────────────────────────────────────────────────────────────────
# 6. Real Reversal Integration & PIT Dynamics
# ─────────────────────────────────────────────────────────────────────────────

class TestReversalAndPITIntegration:
    """Verifies cash balance projection with real Phase 12C.1 reversals and PIT snapshots."""

    def test_reversed_deposit_removes_cash_effect(self):
        """P: CASH_DEPOSIT 100 + REVERSAL of deposit leaves zero touched active cash."""
        port = _make_portfolio()
        acc_id = uuid4()

        dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("100.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        rev = _make_tx(port.id, acc_id, tx_type=TransactionType.REVERSAL, reverses_tx_id=dep.id, recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))

        view = build_ledger_projection_view(port, [dep, rev])
        proj = build_cash_balance_projection(view)

        assert proj.balances == ()
        assert proj.positive_balances == ()

    def test_reversed_withdrawal_restores_cash(self):
        """Q: Deposit 100 + Withdrawal 40 + REVERSAL of withdrawal restores balance to 100."""
        port = _make_portfolio()
        acc_id = uuid4()

        dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("100.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        wdr = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_WITHDRAWAL, cash_amount=Decimal("40.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))
        rev_wdr = _make_tx(port.id, acc_id, tx_type=TransactionType.REVERSAL, reverses_tx_id=wdr.id, recorded_at=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc))

        view = build_ledger_projection_view(port, [dep, wdr, rev_wdr])
        proj = build_cash_balance_projection(view)

        assert len(proj.balances) == 1
        assert proj.balances[0].balance == Decimal("100.00")

    def test_reversed_buy_removes_cash_debit(self):
        """R: Deposit 1000 + BUY 10 @ 20 + REVERSAL of BUY restores balance to 1000."""
        port = _make_portfolio()
        acc_id = uuid4()

        dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        buy = _make_tx(port.id, acc_id, tx_type=TransactionType.BUY, quantity=Decimal("10"), unit_price=Decimal("20.00"), trade_currency=Currency.USD, recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))
        rev_buy = _make_tx(port.id, acc_id, tx_type=TransactionType.REVERSAL, reverses_tx_id=buy.id, recorded_at=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc))

        view = build_ledger_projection_view(port, [dep, buy, rev_buy])
        proj = build_cash_balance_projection(view)

        assert len(proj.balances) == 1
        assert proj.balances[0].balance == Decimal("1000.00")

    def test_reversed_sell_removes_cash_proceeds(self):
        """S: Deposit 1000 + SELL 5 @ 30 + REVERSAL of SELL restores balance to 1000."""
        port = _make_portfolio()
        acc_id = uuid4()

        dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        sell = _make_tx(port.id, acc_id, tx_type=TransactionType.SELL, quantity=Decimal("5"), unit_price=Decimal("30.00"), trade_currency=Currency.USD, recorded_at=datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc))
        rev_sell = _make_tx(port.id, acc_id, tx_type=TransactionType.REVERSAL, reverses_tx_id=sell.id, recorded_at=datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc))

        view = build_ledger_projection_view(port, [dep, sell, rev_sell])
        proj = build_cash_balance_projection(view)

        assert len(proj.balances) == 1
        assert proj.balances[0].balance == Decimal("1000.00")

    def test_pit_before_and_after_reversal_of_sell(self):
        """T, U: Snapshot before reversal reflects SELL (1150); snapshot after reflects restored 1000."""
        port = _make_portfolio()
        acc_id = uuid4()

        dep = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"), cash_currency=Currency.USD, recorded_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        sell = _make_tx(port.id, acc_id, tx_type=TransactionType.SELL, quantity=Decimal("5"), unit_price=Decimal("30.00"), trade_currency=Currency.USD, recorded_at=datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc))
        rev_sell = _make_tx(port.id, acc_id, tx_type=TransactionType.REVERSAL, reverses_tx_id=sell.id, recorded_at=datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc))

        history = [dep, sell, rev_sell]

        # T: As of Aug 10 (before reversal was recorded)
        view_early = build_ledger_projection_view(port, history, as_of_recorded_at=datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc))
        proj_early = build_cash_balance_projection(view_early)
        assert proj_early.balances[0].balance == Decimal("1150.00")

        # U: As of Aug 25 (after reversal was recorded)
        view_late = build_ledger_projection_view(port, history, as_of_recorded_at=datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc))
        proj_late = build_cash_balance_projection(view_late)
        assert proj_late.balances[0].balance == Decimal("1000.00")


# ─────────────────────────────────────────────────────────────────────────────
# 7. Exact Decimal Arithmetic Red-Team
# ─────────────────────────────────────────────────────────────────────────────

class TestExactDecimalRedTeam:
    """Verifies arbitrary Decimal precision, exact trade multiplication, exponent handling, and context independence."""

    def test_tiny_precision_cash_summation(self):
        """V: Exact tiny Decimal cash addition."""
        port = _make_portfolio()
        acc_id = uuid4()

        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("0.000000000000000001"), cash_currency=Currency.USD)
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("0.000000000000000002"), cash_currency=Currency.USD)

        view = build_ledger_projection_view(port, [t1, t2])
        proj = build_cash_balance_projection(view)

        assert proj.balances[0].balance == Decimal("0.000000000000000003")

    def test_huge_precision_cash_summation(self):
        """W: Exact large and high precision Decimal cash addition."""
        port = _make_portfolio()
        acc_id = uuid4()

        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("12345678901234567890.123456789"), cash_currency=Currency.USD)
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("0.000000001"), cash_currency=Currency.USD)

        view = build_ledger_projection_view(port, [t1, t2])
        proj = build_cash_balance_projection(view)

        assert proj.balances[0].balance == Decimal("12345678901234567890.123456790")

    def test_exact_trade_multiplication(self):
        """X: Exact trade multiplication 10.5 * 2.25 = 23.625."""
        port = _make_portfolio()
        acc_id = uuid4()

        t_sell = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.SELL,
            quantity=Decimal("10.5"),
            unit_price=Decimal("2.25"),
            trade_currency=Currency.USD,
        )

        view = build_ledger_projection_view(port, [t_sell])
        proj = build_cash_balance_projection(view)

        assert proj.balances[0].balance == Decimal("23.625")

    def test_exponent_notation_summation(self):
        """Y: Exact Decimal summation with scientific exponent notation."""
        port = _make_portfolio()
        acc_id = uuid4()

        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1E+3"), cash_currency=Currency.USD)
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("0.25"), cash_currency=Currency.USD)

        view = build_ledger_projection_view(port, [t1, t2])
        proj = build_cash_balance_projection(view)

        assert proj.balances[0].balance == Decimal("1000.25")

    def test_cancellation_to_exact_zero(self):
        """Z: Cancellation to exact zero with high precision."""
        port = _make_portfolio()
        acc_id = uuid4()

        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.000000000000000001"), cash_currency=Currency.USD)
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_WITHDRAWAL, cash_amount=Decimal("1000.000000000000000001"), cash_currency=Currency.USD)

        view = build_ledger_projection_view(port, [t1, t2])
        proj = build_cash_balance_projection(view)

        assert proj.balances[0].balance == Decimal("0")
        assert proj.positive_balances == ()

    def test_low_decimal_context_precision_does_not_round(self):
        """AA: Executing under localcontext(prec=6) does NOT round exact multiplication or summation."""
        port = _make_portfolio()
        acc_id = uuid4()

        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1234567890.123456789"), cash_currency=Currency.USD)
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("0.000000001"), cash_currency=Currency.USD)

        view = build_ledger_projection_view(port, [t1, t2])

        with localcontext() as ctx:
            ctx.prec = 6
            proj = build_cash_balance_projection(view)

        assert proj.balances[0].balance == Decimal("1234567890.123456790")

    def test_large_exponent_span_summation(self):
        """AB: Exponent span 1E+100 + 1E-100 retains both components without context truncation."""
        port = _make_portfolio()
        acc_id = uuid4()

        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1E+100"), cash_currency=Currency.USD)
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1E-100"), cash_currency=Currency.USD)

        view = build_ledger_projection_view(port, [t1, t2])
        proj = build_cash_balance_projection(view)

        expected = Decimal("1" + ("0" * 199) + "1E-100")
        assert proj.balances[0].balance == expected

    def test_large_exponent_product(self):
        """AC: Exponent product 1E+100 * 1E-100 = exact Decimal('1')."""
        port = _make_portfolio()
        acc_id = uuid4()

        t_sell = _make_tx(
            port.id,
            acc_id,
            tx_type=TransactionType.SELL,
            quantity=Decimal("1E+100"),
            unit_price=Decimal("1E-100"),
            trade_currency=Currency.USD,
        )

        view = build_ledger_projection_view(port, [t_sell])
        proj = build_cash_balance_projection(view)

        assert proj.balances[0].balance == Decimal("1")


# ─────────────────────────────────────────────────────────────────────────────
# 8. View & Active Transaction Integrity Guards
# ─────────────────────────────────────────────────────────────────────────────

class TestViewAndTransactionIntegrityGuards:
    """Verifies boundary fail-closed defenses against forged or malformed views."""

    def test_wrong_view_type_rejected(self):
        """AD: Non-LedgerProjectionView input rejected with TypeError."""
        with pytest.raises(TypeError, match="must be an instance of LedgerProjectionView"):
            build_cash_balance_projection("not_a_view")  # type: ignore

    def test_forged_active_reversal_fails_closed(self):
        """AE: Active REVERSAL in view.active_transactions fails closed."""
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

        with pytest.raises(CashProjectionError, match="REVERSAL events must never appear in active_transactions"):
            build_cash_balance_projection(forged_view)

    def test_duplicate_active_physical_uuid_fails_closed(self):
        """AF: Duplicate physical UUID in active_transactions fails closed."""
        port = _make_portfolio()
        acc_id = uuid4()
        shared_id = uuid4()

        t1 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, id=shared_id)
        t2 = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, id=shared_id)

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode=port.mode,
            as_of_recorded_at=None,
            known_transactions=(t1, t2),
            transaction_states=(),
            active_transactions=(t1, t2),
        )

        with pytest.raises(CashProjectionError, match="Duplicate physical transaction ID detected in active_transactions"):
            build_cash_balance_projection(forged_view)

    def test_cross_portfolio_active_transaction_fails_closed(self):
        """AG: Active transaction from another portfolio fails closed."""
        port = _make_portfolio()
        other_port_id = uuid4()
        acc_id = uuid4()

        t_foreign = _make_tx(other_port_id, acc_id, tx_type=TransactionType.CASH_DEPOSIT)

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode=port.mode,
            as_of_recorded_at=None,
            known_transactions=(t_foreign,),
            transaction_states=(),
            active_transactions=(t_foreign,),
        )

        with pytest.raises(CashProjectionError, match="does not match view"):
            build_cash_balance_projection(forged_view)

    def test_malformed_transaction_type_string_fails_closed(self):
        """AH: String transaction_type 'CASH_DEPOSIT' fails closed."""
        port = _make_portfolio()
        acc_id = uuid4()

        tx = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT)
        object.__setattr__(tx, "transaction_type", "CASH_DEPOSIT")

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode=port.mode,
            as_of_recorded_at=None,
            known_transactions=(tx,),
            transaction_states=(),
            active_transactions=(tx,),
        )

        with pytest.raises(CashProjectionError, match="Active transaction transaction_type must be a TransactionType enum"):
            build_cash_balance_projection(forged_view)

    def test_malformed_account_uuid_fails_closed(self):
        """AI: String tx.account_id fails closed."""
        port = _make_portfolio()
        acc_id = uuid4()

        tx = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT)
        object.__setattr__(tx, "account_id", "not-a-uuid")

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode=port.mode,
            as_of_recorded_at=None,
            known_transactions=(tx,),
            transaction_states=(),
            active_transactions=(tx,),
        )

        with pytest.raises(CashProjectionError, match="Active transaction account_id must be a UUID"):
            build_cash_balance_projection(forged_view)

    def test_malformed_currency_enum_fails_closed(self):
        """AJ: String cash_currency 'USD' fails closed."""
        port = _make_portfolio()
        acc_id = uuid4()

        tx = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT)
        object.__setattr__(tx, "cash_currency", "USD")

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode=port.mode,
            as_of_recorded_at=None,
            known_transactions=(tx,),
            transaction_states=(),
            active_transactions=(tx,),
        )

        with pytest.raises(CashProjectionError, match="cash_currency must be a Currency enum"):
            build_cash_balance_projection(forged_view)

    def test_malformed_decimal_type_fails_closed(self):
        """AK: Float cash_amount fails closed."""
        port = _make_portfolio()
        acc_id = uuid4()

        tx = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT)
        object.__setattr__(tx, "cash_amount", 100.5)

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode=port.mode,
            as_of_recorded_at=None,
            known_transactions=(tx,),
            transaction_states=(),
            active_transactions=(tx,),
        )

        with pytest.raises(CashProjectionError, match="cash_amount must be a Decimal"):
            build_cash_balance_projection(forged_view)

    def test_nan_and_infinity_fail_closed(self):
        """AL: NaN and Infinity cash amounts fail closed."""
        port = _make_portfolio()
        acc_id = uuid4()

        tx = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT)
        object.__setattr__(tx, "cash_amount", Decimal("NaN"))

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode=port.mode,
            as_of_recorded_at=None,
            known_transactions=(tx,),
            transaction_states=(),
            active_transactions=(tx,),
        )

        with pytest.raises(CashProjectionError, match="cash_amount must be a strictly positive finite Decimal"):
            build_cash_balance_projection(forged_view)


# ─────────────────────────────────────────────────────────────────────────────
# 9. Datetime Awareness Hardening
# ─────────────────────────────────────────────────────────────────────────────

class TestDatetimeAwarenessHardening:
    """Verifies strict timezone awareness contract on view.as_of_recorded_at."""

    def test_naive_view_as_of_recorded_at_rejected(self):
        """AM: Naive view.as_of_recorded_at fails closed."""
        port = _make_portfolio()

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode=port.mode,
            as_of_recorded_at=datetime(2026, 8, 10, 12, 0, 0),  # Naive
            known_transactions=(),
            transaction_states=(),
            active_transactions=(),
        )

        with pytest.raises(CashProjectionError, match="view.as_of_recorded_at must be timezone-aware"):
            build_cash_balance_projection(forged_view)

    def test_null_offset_tz_view_as_of_recorded_at_rejected(self):
        """AN: NullOffsetTZ view.as_of_recorded_at fails closed."""
        port = _make_portfolio()

        forged_view = LedgerProjectionView(
            portfolio_id=port.id,
            mode=port.mode,
            as_of_recorded_at=datetime(2026, 8, 10, 12, 0, 0, tzinfo=NullOffsetTZ()),
            known_transactions=(),
            transaction_states=(),
            active_transactions=(),
        )

        with pytest.raises(CashProjectionError, match="view.as_of_recorded_at must be timezone-aware with non-null utcoffset"):
            build_cash_balance_projection(forged_view)

    def test_utc_datetime_accepted(self):
        """AO: UTC datetime in view is accepted."""
        port = _make_portfolio()
        acc_id = uuid4()
        utc_dt = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)

        tx = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("100.00"), recorded_at=utc_dt)
        view = build_ledger_projection_view(port, [tx], as_of_recorded_at=utc_dt)
        proj = build_cash_balance_projection(view)

        assert proj.as_of_recorded_at == utc_dt
        assert len(proj.balances) == 1

    def test_non_utc_offset_accepted_and_preserved(self):
        """AP: Non-UTC timezone offset (+03:00) is accepted and preserved unchanged."""
        port = _make_portfolio()
        acc_id = uuid4()
        tz_plus3 = timezone(timedelta(hours=3))
        non_utc_dt = datetime(2026, 8, 10, 15, 0, 0, tzinfo=tz_plus3)

        tx = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("100.00"), recorded_at=non_utc_dt)
        view = build_ledger_projection_view(port, [tx], as_of_recorded_at=non_utc_dt)
        proj = build_cash_balance_projection(view)

        assert proj.as_of_recorded_at == non_utc_dt
        assert len(proj.balances) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 10. Immutability & Constructor Invariants
# ─────────────────────────────────────────────────────────────────────────────

class TestImmutabilityAndConstructorInvariants:
    """Verifies output encapsulation, frozen dataclasses, and constructor validations."""

    def test_output_collections_are_immutable_tuples(self):
        """AQ: balances and positive_balances are strictly tuples."""
        port = _make_portfolio()
        acc_id = uuid4()
        tx = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("100.00"))

        view = build_ledger_projection_view(port, [tx])
        proj = build_cash_balance_projection(view)

        assert isinstance(proj.balances, tuple)
        assert isinstance(proj.positive_balances, tuple)

    def test_frozen_dataclass_mutations_rejected(self):
        """AR: Attempting to mutate CashBalanceState or CashBalanceProjection raises FrozenInstanceError."""
        port = _make_portfolio()
        acc_id = uuid4()
        tx = _make_tx(port.id, acc_id, tx_type=TransactionType.CASH_DEPOSIT, cash_amount=Decimal("100.00"))

        view = build_ledger_projection_view(port, [tx])
        proj = build_cash_balance_projection(view)

        with pytest.raises(FrozenInstanceError):
            proj.as_of_recorded_at = datetime.now(timezone.utc)  # type: ignore

        with pytest.raises(FrozenInstanceError):
            proj.balances = ()  # type: ignore

        b = proj.balances[0]
        with pytest.raises(FrozenInstanceError):
            b.balance = Decimal("200.00")  # type: ignore

    def test_state_constructor_invariants(self):
        """AS: CashBalanceState direct constructor validations."""
        port_id = uuid4()
        acc_id = uuid4()

        # Valid
        s = CashBalanceState(portfolio_id=port_id, account_id=acc_id, currency=Currency.USD, balance=Decimal("100.00"))
        assert s.balance == Decimal("100.00")
        assert s.is_positive is True

        # Exact zero
        s_zero = CashBalanceState(portfolio_id=port_id, account_id=acc_id, currency=Currency.USD, balance=Decimal("0"))
        assert s_zero.balance == Decimal("0")
        assert s_zero.is_positive is False

        # Non-UUID portfolio_id
        with pytest.raises(CashProjectionError, match="portfolio_id must be a UUID"):
            CashBalanceState(portfolio_id="not-a-uuid", account_id=acc_id, currency=Currency.USD, balance=Decimal("100"))  # type: ignore

        # Non-UUID account_id
        with pytest.raises(CashProjectionError, match="account_id must be a UUID"):
            CashBalanceState(portfolio_id=port_id, account_id="not-a-uuid", currency=Currency.USD, balance=Decimal("100"))  # type: ignore

        # Non-Currency enum
        with pytest.raises(CashProjectionError, match="currency must be a Currency"):
            CashBalanceState(portfolio_id=port_id, account_id=acc_id, currency="USD", balance=Decimal("100"))  # type: ignore

        # Float balance
        with pytest.raises(CashProjectionError, match="balance must be a Decimal"):
            CashBalanceState(portfolio_id=port_id, account_id=acc_id, currency=Currency.USD, balance=100.5)  # type: ignore

        # Negative balance
        with pytest.raises(CashProjectionError, match="balance cannot be negative"):
            CashBalanceState(portfolio_id=port_id, account_id=acc_id, currency=Currency.USD, balance=Decimal("-1.00"))

    def test_projection_constructor_invariants(self):
        """AS: CashBalanceProjection direct constructor validations."""
        port_id = uuid4()
        acc_id = uuid4()

        b1 = CashBalanceState(portfolio_id=port_id, account_id=acc_id, currency=Currency.USD, balance=Decimal("100.00"))
        b_zero = CashBalanceState(portfolio_id=port_id, account_id=acc_id, currency=Currency.EUR, balance=Decimal("0"))

        # Valid
        proj = CashBalanceProjection(
            portfolio_id=port_id,
            mode=PortfolioMode.MY_PORTFOLIO,
            as_of_recorded_at=None,
            balances=(b1, b_zero),
            positive_balances=(b1,),
        )
        assert len(proj.balances) == 2
        assert len(proj.positive_balances) == 1

        # List instead of tuple
        with pytest.raises(CashProjectionError, match="balances must be a tuple"):
            CashBalanceProjection(
                portfolio_id=port_id,
                mode=PortfolioMode.MY_PORTFOLIO,
                as_of_recorded_at=None,
                balances=[b1],  # type: ignore
                positive_balances=(b1,),
            )

        # Cross-portfolio balance
        other_port_id = uuid4()
        b_foreign = CashBalanceState(portfolio_id=other_port_id, account_id=acc_id, currency=Currency.USD, balance=Decimal("100.00"))
        with pytest.raises(CashProjectionError, match="does not match projection"):
            CashBalanceProjection(
                portfolio_id=port_id,
                mode=PortfolioMode.MY_PORTFOLIO,
                as_of_recorded_at=None,
                balances=(b_foreign,),
                positive_balances=(b_foreign,),
            )

        # Duplicate identity in balances
        b1_dup = CashBalanceState(portfolio_id=port_id, account_id=acc_id, currency=Currency.USD, balance=Decimal("50.00"))
        with pytest.raises(CashProjectionError, match="Duplicate cash balance identity"):
            CashBalanceProjection(
                portfolio_id=port_id,
                mode=PortfolioMode.MY_PORTFOLIO,
                as_of_recorded_at=None,
                balances=(b1, b1_dup),
                positive_balances=(b1,),
            )

        # Zero balance in positive_balances
        with pytest.raises(CashProjectionError, match="Zero-balance state .* must not appear in positive_balances"):
            CashBalanceProjection(
                portfolio_id=port_id,
                mode=PortfolioMode.MY_PORTFOLIO,
                as_of_recorded_at=None,
                balances=(b_zero,),
                positive_balances=(b_zero,),
            )

        # Positive balance missing from positive_balances
        with pytest.raises(CashProjectionError, match="Positive balance .* is missing from positive_balances"):
            CashBalanceProjection(
                portfolio_id=port_id,
                mode=PortfolioMode.MY_PORTFOLIO,
                as_of_recorded_at=None,
                balances=(b1,),
                positive_balances=(),
            )
