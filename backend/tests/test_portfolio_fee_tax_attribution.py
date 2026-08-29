"""
backend/tests/test_portfolio_fee_tax_attribution.py
===================================================
Comprehensive unit, invariant, anti-tamper, and red-team test suite for
Explicit Fee/Tax Charge Attribution Evidence Foundation (Phase 14D).
"""

from __future__ import annotations

import ast
from datetime import date, datetime, timedelta, timezone
import decimal
from decimal import Decimal
import inspect
from typing import Optional, Sequence
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import (
    Currency,
    PortfolioMode,
    TransactionType,
)
from backend.engine.private.portfolio.fee_tax import (
    ObservedFeeTaxProjection,
    build_observed_fee_tax_projection,
)
from backend.engine.private.portfolio.fee_tax_attribution import (
    FeeTaxAttributionError,
    FeeTaxAttributionIntent,
    ObservedFeeTaxAttributionSet,
    ResolvedFeeTaxAttribution,
    _exact_decimal_sub,
    _exact_decimal_sum,
    build_observed_fee_tax_attribution_set,
)
import backend.engine.private.portfolio.fee_tax_attribution as attribution_module
from backend.engine.private.portfolio.models import (
    Portfolio,
    PortfolioTransaction,
)
from backend.engine.private.portfolio.projection import (
    LedgerProjectionView,
    build_ledger_projection_view,
)


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
    tx_type: TransactionType,
    *,
    tx_id: Optional[UUID] = None,
    recorded_at: Optional[datetime] = None,
    effective_date: Optional[date] = None,
    executed_at: Optional[datetime] = None,
    instrument_id: Optional[UUID] = None,
    quantity: Optional[Decimal] = None,
    unit_price: Optional[Decimal] = None,
    trade_currency: Optional[Currency] = None,
    cash_amount: Optional[Decimal] = None,
    cash_currency: Optional[Currency] = None,
    cash_bucket_id: Optional[UUID] = None,
    from_currency: Optional[Currency] = None,
    from_amount: Optional[Decimal] = None,
    to_currency: Optional[Currency] = None,
    to_amount: Optional[Decimal] = None,
    reverses_transaction_id: Optional[UUID] = None,
) -> PortfolioTransaction:
    rec_dt = recorded_at or datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    eff_d = effective_date or date(2026, 6, 1)

    if tx_type in (TransactionType.BUY, TransactionType.SELL):
        inst_id = instrument_id or uuid4()
        qty = quantity if quantity is not None else Decimal("10.00")
        price = unit_price if unit_price is not None else Decimal("100.00")
        t_curr = trade_currency or Currency.USD
        return PortfolioTransaction(
            id=tx_id or uuid4(),
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=tx_type,
            recorded_at=rec_dt,
            effective_date=eff_d,
            executed_at=executed_at or rec_dt,
            instrument_id=inst_id,
            quantity=qty,
            unit_price=price,
            trade_currency=t_curr,
        )

    if tx_type in (TransactionType.FEE, TransactionType.TAX_WITHHOLDING, TransactionType.INTEREST):
        c_amt = cash_amount if cash_amount is not None else Decimal("25.00")
        c_curr = cash_currency or Currency.USD
        return PortfolioTransaction(
            id=tx_id or uuid4(),
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=tx_type,
            recorded_at=rec_dt,
            effective_date=eff_d,
            executed_at=executed_at or rec_dt,
            instrument_id=instrument_id,
            cash_amount=c_amt,
            cash_currency=c_curr,
            cash_bucket_id=cash_bucket_id,
        )

    if tx_type == TransactionType.DIVIDEND:
        c_amt = cash_amount if cash_amount is not None else Decimal("25.00")
        c_curr = cash_currency or Currency.USD
        inst_id = instrument_id or uuid4()
        return PortfolioTransaction(
            id=tx_id or uuid4(),
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=tx_type,
            recorded_at=rec_dt,
            effective_date=eff_d,
            executed_at=executed_at or rec_dt,
            instrument_id=inst_id,
            cash_amount=c_amt,
            cash_currency=c_curr,
            cash_bucket_id=cash_bucket_id,
        )

    if tx_type in (TransactionType.CASH_DEPOSIT, TransactionType.CASH_WITHDRAWAL):
        c_amt = cash_amount if cash_amount is not None else Decimal("500.00")
        c_curr = cash_currency or Currency.USD
        return PortfolioTransaction(
            id=tx_id or uuid4(),
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=tx_type,
            recorded_at=rec_dt,
            effective_date=eff_d,
            executed_at=executed_at or rec_dt,
            cash_amount=c_amt,
            cash_currency=c_curr,
            cash_bucket_id=cash_bucket_id,
        )

    if tx_type == TransactionType.FX_CONVERSION:
        f_curr = from_currency or Currency.USD
        f_amt = from_amount if from_amount is not None else Decimal("100.00")
        t_curr = to_currency or Currency.TRY
        t_amt = to_amount if to_amount is not None else Decimal("3200.00")
        return PortfolioTransaction(
            id=tx_id or uuid4(),
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=tx_type,
            recorded_at=rec_dt,
            effective_date=eff_d,
            executed_at=executed_at or rec_dt,
            from_currency=f_curr,
            from_amount=f_amt,
            to_currency=t_curr,
            to_amount=t_amt,
        )

    if tx_type == TransactionType.REVERSAL:
        assert reverses_transaction_id is not None
        return PortfolioTransaction(
            id=tx_id or uuid4(),
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=tx_type,
            recorded_at=rec_dt,
            effective_date=eff_d,
            executed_at=executed_at or rec_dt,
            reverses_transaction_id=reverses_transaction_id,
        )

    raise ValueError(f"Unsupported transaction type: {tx_type}")


# ==============================================================================
# 1. Intent Model Validation Tests
# ==============================================================================

def test_intent_valid_construction() -> None:
    charge_id = uuid4()
    target_id = uuid4()
    amt = Decimal("12.50")
    intent = FeeTaxAttributionIntent(
        charge_transaction_id=charge_id,
        target_transaction_id=target_id,
        allocated_amount=amt,
    )
    assert intent.charge_transaction_id == charge_id
    assert intent.target_transaction_id == target_id
    assert intent.allocated_amount == amt


@pytest.mark.parametrize(
    "invalid_charge_id",
    [None, True, False, "c8a1e8e2-6bf2-411a-8c76-2f08960824b2", 12345, 3.14],
)
def test_intent_rejects_invalid_charge_id(invalid_charge_id: any) -> None:
    with pytest.raises(FeeTaxAttributionError, match="charge_transaction_id must be a UUID"):
        FeeTaxAttributionIntent(
            charge_transaction_id=invalid_charge_id,
            target_transaction_id=uuid4(),
            allocated_amount=Decimal("10.00"),
        )


@pytest.mark.parametrize(
    "invalid_target_id",
    [None, True, False, "c8a1e8e2-6bf2-411a-8c76-2f08960824b2", 12345, 3.14],
)
def test_intent_rejects_invalid_target_id(invalid_target_id: any) -> None:
    with pytest.raises(FeeTaxAttributionError, match="target_transaction_id must be a UUID"):
        FeeTaxAttributionIntent(
            charge_transaction_id=uuid4(),
            target_transaction_id=invalid_target_id,
            allocated_amount=Decimal("10.00"),
        )


@pytest.mark.parametrize(
    "invalid_amt",
    [
        None,
        True,
        False,
        "10.00",
        10,
        10.0,
        Decimal("0"),
        Decimal("-1.00"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_intent_rejects_invalid_allocated_amount(invalid_amt: any) -> None:
    with pytest.raises(FeeTaxAttributionError, match="allocated_amount"):
        FeeTaxAttributionIntent(
            charge_transaction_id=uuid4(),
            target_transaction_id=uuid4(),
            allocated_amount=invalid_amt,
        )


def test_intent_rejects_self_link() -> None:
    same_id = uuid4()
    with pytest.raises(FeeTaxAttributionError, match="Self-attribution rejected"):
        FeeTaxAttributionIntent(
            charge_transaction_id=same_id,
            target_transaction_id=same_id,
            allocated_amount=Decimal("5.00"),
        )


# ==============================================================================
# 2. Resolved Attribution Model Validation Tests
# ==============================================================================

def test_resolved_valid_construction() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    resolved = ResolvedFeeTaxAttribution(
        charge_transaction=fee_tx,
        target_transaction=buy_tx,
        allocated_amount=Decimal("6.00"),
    )
    assert resolved.charge_transaction is fee_tx
    assert resolved.target_transaction is buy_tx
    assert resolved.allocated_amount == Decimal("6.00")


@pytest.mark.parametrize(
    "invalid_charge_type",
    [
        TransactionType.BUY,
        TransactionType.SELL,
        TransactionType.DIVIDEND,
        TransactionType.INTEREST,
        TransactionType.CASH_DEPOSIT,
        TransactionType.CASH_WITHDRAWAL,
        TransactionType.FX_CONVERSION,
        TransactionType.REVERSAL,
    ],
)
def test_resolved_rejects_invalid_charge_type(invalid_charge_type: TransactionType) -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    non_charge = _make_tx(
        portfolio.id,
        account_id,
        invalid_charge_type,
        reverses_transaction_id=uuid4() if invalid_charge_type == TransactionType.REVERSAL else None,
    )
    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    with pytest.raises(FeeTaxAttributionError, match="charge_transaction must be FEE or TAX_WITHHOLDING"):
        ResolvedFeeTaxAttribution(
            charge_transaction=non_charge,
            target_transaction=buy_tx,
            allocated_amount=Decimal("5.00"),
        )


@pytest.mark.parametrize(
    "valid_target_type",
    [
        TransactionType.BUY,
        TransactionType.SELL,
        TransactionType.DIVIDEND,
        TransactionType.INTEREST,
        TransactionType.CASH_DEPOSIT,
        TransactionType.CASH_WITHDRAWAL,
        TransactionType.FX_CONVERSION,
    ],
)
def test_resolved_accepts_all_seven_valid_target_types(valid_target_type: TransactionType) -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    target_tx = _make_tx(portfolio.id, account_id, valid_target_type)

    resolved = ResolvedFeeTaxAttribution(
        charge_transaction=fee_tx,
        target_transaction=target_tx,
        allocated_amount=Decimal("5.00"),
    )
    assert resolved.target_transaction.transaction_type == valid_target_type
    assert resolved.charge_transaction is fee_tx


@pytest.mark.parametrize(
    "prohibited_target_type",
    [
        TransactionType.FEE,
        TransactionType.TAX_WITHHOLDING,
        TransactionType.REVERSAL,
    ],
)
def test_resolved_rejects_prohibited_target_type(prohibited_target_type: TransactionType) -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    bad_target = _make_tx(
        portfolio.id,
        account_id,
        prohibited_target_type,
        reverses_transaction_id=uuid4() if prohibited_target_type == TransactionType.REVERSAL else None,
    )

    with pytest.raises(FeeTaxAttributionError, match="target_transaction cannot be of type"):
        ResolvedFeeTaxAttribution(
            charge_transaction=fee_tx,
            target_transaction=bad_target,
            allocated_amount=Decimal("5.00"),
        )


def test_resolved_rejects_cross_account() -> None:
    portfolio = _make_portfolio()
    account_a = uuid4()
    account_b = uuid4()
    fee_tx = _make_tx(portfolio.id, account_a, TransactionType.FEE, cash_amount=Decimal("10.00"))
    buy_tx = _make_tx(portfolio.id, account_b, TransactionType.BUY)

    with pytest.raises(FeeTaxAttributionError, match="Cross-account attribution rejected"):
        ResolvedFeeTaxAttribution(
            charge_transaction=fee_tx,
            target_transaction=buy_tx,
            allocated_amount=Decimal("5.00"),
        )


def test_resolved_rejects_cross_portfolio() -> None:
    port_a = _make_portfolio()
    port_b = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(port_a.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    buy_tx = _make_tx(port_b.id, account_id, TransactionType.BUY)

    with pytest.raises(FeeTaxAttributionError, match="Cross-portfolio attribution rejected"):
        ResolvedFeeTaxAttribution(
            charge_transaction=fee_tx,
            target_transaction=buy_tx,
            allocated_amount=Decimal("5.00"),
        )


def test_resolved_rejects_single_allocation_exceeding_charge() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    with pytest.raises(FeeTaxAttributionError, match="exceeds charge cash_amount"):
        ResolvedFeeTaxAttribution(
            charge_transaction=fee_tx,
            target_transaction=buy_tx,
            allocated_amount=Decimal("10.01"),
        )


# ==============================================================================
# 3. Builder & Attribution Set Core Matrix Tests (A - G)
# ==============================================================================

def test_matrix_a_empty_intents() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx])
    proj = build_observed_fee_tax_projection(view)

    attr_set = build_observed_fee_tax_attribution_set(proj, ())
    assert attr_set.portfolio_id == portfolio.id
    assert attr_set.mode == portfolio.mode
    assert attr_set.as_of_recorded_at is None
    assert attr_set.intents == ()
    assert attr_set.attributions == ()
    assert attr_set.attribution_count == 0
    assert attr_set.charge_ids == ()
    assert attr_set.target_ids == ()

    # Unallocated query returns full observed charge amount
    assert attr_set.unallocated_amount_for_charge(fee_tx.id) == Decimal("10.00")
    assert attr_set.is_fully_allocated(fee_tx.id) is False
    assert attr_set.attributions_for_charge(fee_tx.id) == ()
    assert attr_set.attributions_for_target(buy_tx.id) == ()


def test_matrix_b_one_fee_to_buy() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("15.00"))
    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx])
    proj = build_observed_fee_tax_projection(view)

    intent = FeeTaxAttributionIntent(
        charge_transaction_id=fee_tx.id,
        target_transaction_id=buy_tx.id,
        allocated_amount=Decimal("15.00"),
    )
    attr_set = build_observed_fee_tax_attribution_set(proj, (intent,))

    assert attr_set.attribution_count == 1
    assert attr_set.charge_ids == (fee_tx.id,)
    assert attr_set.target_ids == (buy_tx.id,)

    resolved = attr_set.attributions[0]
    assert resolved.charge_transaction is fee_tx
    assert resolved.target_transaction is buy_tx
    assert resolved.allocated_amount == Decimal("15.00")

    assert attr_set.is_fully_allocated(fee_tx.id) is True
    assert attr_set.unallocated_amount_for_charge(fee_tx.id) == Decimal("0")
    assert attr_set.attributions_for_charge(fee_tx.id) == (resolved,)
    assert attr_set.attributions_for_target(buy_tx.id) == (resolved,)


def test_matrix_c_one_fee_to_sell() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("8.50"))
    sell_tx = _make_tx(portfolio.id, account_id, TransactionType.SELL)

    view = build_ledger_projection_view(portfolio, [fee_tx, sell_tx])
    proj = build_observed_fee_tax_projection(view)

    intent = FeeTaxAttributionIntent(fee_tx.id, sell_tx.id, Decimal("8.50"))
    attr_set = build_observed_fee_tax_attribution_set(proj, (intent,))
    assert attr_set.attribution_count == 1
    assert attr_set.attributions[0].target_transaction is sell_tx


def test_matrix_d_tax_to_dividend() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    tax_tx = _make_tx(portfolio.id, account_id, TransactionType.TAX_WITHHOLDING, cash_amount=Decimal("20.00"), cash_currency=Currency.TRY)
    div_tx = _make_tx(portfolio.id, account_id, TransactionType.DIVIDEND, cash_amount=Decimal("100.00"), cash_currency=Currency.TRY)

    view = build_ledger_projection_view(portfolio, [tax_tx, div_tx])
    proj = build_observed_fee_tax_projection(view)

    intent = FeeTaxAttributionIntent(tax_tx.id, div_tx.id, Decimal("20.00"))
    attr_set = build_observed_fee_tax_attribution_set(proj, (intent,))
    assert attr_set.attribution_count == 1
    assert attr_set.attributions[0].charge_transaction is tax_tx
    assert attr_set.attributions[0].target_transaction is div_tx


def test_matrix_e_tax_to_interest() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    tax_tx = _make_tx(portfolio.id, account_id, TransactionType.TAX_WITHHOLDING, cash_amount=Decimal("5.00"), cash_currency=Currency.USD)
    int_tx = _make_tx(portfolio.id, account_id, TransactionType.INTEREST, cash_amount=Decimal("50.00"), cash_currency=Currency.USD)

    view = build_ledger_projection_view(portfolio, [tax_tx, int_tx])
    proj = build_observed_fee_tax_projection(view)

    intent = FeeTaxAttributionIntent(tax_tx.id, int_tx.id, Decimal("5.00"))
    attr_set = build_observed_fee_tax_attribution_set(proj, (intent,))
    assert attr_set.attribution_count == 1
    assert attr_set.attributions[0].target_transaction is int_tx


def test_matrix_f_fee_to_fx_conversion() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("2.00"), cash_currency=Currency.USD)
    fx_tx = _make_tx(portfolio.id, account_id, TransactionType.FX_CONVERSION)

    view = build_ledger_projection_view(portfolio, [fee_tx, fx_tx])
    proj = build_observed_fee_tax_projection(view)

    intent = FeeTaxAttributionIntent(fee_tx.id, fx_tx.id, Decimal("2.00"))
    attr_set = build_observed_fee_tax_attribution_set(proj, (intent,))
    assert attr_set.attribution_count == 1
    assert attr_set.attributions[0].target_transaction is fx_tx


def test_matrix_g_fee_to_cash_withdrawal() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("3.50"), cash_currency=Currency.USD)
    wd_tx = _make_tx(portfolio.id, account_id, TransactionType.CASH_WITHDRAWAL)

    view = build_ledger_projection_view(portfolio, [fee_tx, wd_tx])
    proj = build_observed_fee_tax_projection(view)

    intent = FeeTaxAttributionIntent(fee_tx.id, wd_tx.id, Decimal("3.50"))
    attr_set = build_observed_fee_tax_attribution_set(proj, (intent,))
    assert attr_set.attribution_count == 1
    assert attr_set.attributions[0].target_transaction is wd_tx


# ==============================================================================
# 4. Multi-Target, Multi-Charge, Allocation Matrix Tests (H - L)
# ==============================================================================

def test_matrix_h_multiple_targets_for_one_charge() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    buy_a = _make_tx(portfolio.id, account_id, TransactionType.BUY)
    buy_b = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    view = build_ledger_projection_view(portfolio, [fee_tx, buy_a, buy_b])
    proj = build_observed_fee_tax_projection(view)

    intents = (
        FeeTaxAttributionIntent(fee_tx.id, buy_a.id, Decimal("6.00")),
        FeeTaxAttributionIntent(fee_tx.id, buy_b.id, Decimal("4.00")),
    )
    attr_set = build_observed_fee_tax_attribution_set(proj, intents)

    assert attr_set.attribution_count == 2
    assert attr_set.charge_ids == (fee_tx.id,)
    assert attr_set.target_ids == (buy_a.id, buy_b.id)
    assert attr_set.is_fully_allocated(fee_tx.id) is True
    assert attr_set.unallocated_amount_for_charge(fee_tx.id) == Decimal("0")
    assert len(attr_set.attributions_for_charge(fee_tx.id)) == 2


def test_matrix_i_multiple_charges_for_one_target() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_a = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("2.00"))
    fee_b = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("3.00"))
    buy_x = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    view = build_ledger_projection_view(portfolio, [fee_a, fee_b, buy_x])
    proj = build_observed_fee_tax_projection(view)

    intents = (
        FeeTaxAttributionIntent(fee_a.id, buy_x.id, Decimal("2.00")),
        FeeTaxAttributionIntent(fee_b.id, buy_x.id, Decimal("3.00")),
    )
    attr_set = build_observed_fee_tax_attribution_set(proj, intents)

    assert attr_set.attribution_count == 2
    assert attr_set.charge_ids == (fee_a.id, fee_b.id)
    assert attr_set.target_ids == (buy_x.id,)
    assert len(attr_set.attributions_for_target(buy_x.id)) == 2


def test_matrix_j_partial_allocation() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx])
    proj = build_observed_fee_tax_projection(view)

    intent = FeeTaxAttributionIntent(fee_tx.id, buy_tx.id, Decimal("6.00"))
    attr_set = build_observed_fee_tax_attribution_set(proj, (intent,))

    assert attr_set.is_fully_allocated(fee_tx.id) is False
    assert attr_set.unallocated_amount_for_charge(fee_tx.id) == Decimal("4.00")


def test_matrix_k_exact_full_allocation() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx])
    proj = build_observed_fee_tax_projection(view)

    intent = FeeTaxAttributionIntent(fee_tx.id, buy_tx.id, Decimal("10.00"))
    attr_set = build_observed_fee_tax_attribution_set(proj, (intent,))
    assert attr_set.is_fully_allocated(fee_tx.id) is True
    assert attr_set.unallocated_amount_for_charge(fee_tx.id) == Decimal("0")


def test_matrix_l_over_allocation_rejection() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    buy_a = _make_tx(portfolio.id, account_id, TransactionType.BUY)
    buy_b = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    view = build_ledger_projection_view(portfolio, [fee_tx, buy_a, buy_b])
    proj = build_observed_fee_tax_projection(view)

    intents = (
        FeeTaxAttributionIntent(fee_tx.id, buy_a.id, Decimal("6.00")),
        FeeTaxAttributionIntent(fee_tx.id, buy_b.id, Decimal("4.01")),
    )
    with pytest.raises(FeeTaxAttributionError, match="Over-allocation detected"):
        build_observed_fee_tax_attribution_set(proj, intents)


# ==============================================================================
# 5. Boundary & Rejection Tests (M - S)
# ==============================================================================

def test_matrix_m_duplicate_pair_rejection() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx])
    proj = build_observed_fee_tax_projection(view)

    intents = (
        FeeTaxAttributionIntent(fee_tx.id, buy_tx.id, Decimal("3.00")),
        FeeTaxAttributionIntent(fee_tx.id, buy_tx.id, Decimal("3.00")),
    )
    with pytest.raises(FeeTaxAttributionError, match="Duplicate attribution intent detected"):
        build_observed_fee_tax_attribution_set(proj, intents)


def test_matrix_n_unknown_charge_rejection() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    view = build_ledger_projection_view(portfolio, [buy_tx])
    proj = build_observed_fee_tax_projection(view)

    intent = FeeTaxAttributionIntent(uuid4(), buy_tx.id, Decimal("5.00"))
    with pytest.raises(FeeTaxAttributionError, match="not found in observed active charge events"):
        build_observed_fee_tax_attribution_set(proj, (intent,))


def test_matrix_o_unknown_target_rejection() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))

    view = build_ledger_projection_view(portfolio, [fee_tx])
    proj = build_observed_fee_tax_projection(view)

    intent = FeeTaxAttributionIntent(fee_tx.id, uuid4(), Decimal("5.00"))
    with pytest.raises(FeeTaxAttributionError, match="not found in active transactions"):
        build_observed_fee_tax_attribution_set(proj, (intent,))


def test_matrix_p_cross_account_rejection() -> None:
    portfolio = _make_portfolio()
    account_a = uuid4()
    account_b = uuid4()
    fee_tx = _make_tx(portfolio.id, account_a, TransactionType.FEE, cash_amount=Decimal("10.00"))
    buy_tx = _make_tx(portfolio.id, account_b, TransactionType.BUY)

    view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx])
    proj = build_observed_fee_tax_projection(view)

    intent = FeeTaxAttributionIntent(fee_tx.id, buy_tx.id, Decimal("5.00"))
    with pytest.raises(FeeTaxAttributionError, match="Cross-account attribution rejected"):
        build_observed_fee_tax_attribution_set(proj, (intent,))


def test_matrix_q_cross_portfolio_rejection() -> None:
    port_a = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(port_a.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))

    view = build_ledger_projection_view(port_a, [fee_tx])
    proj = build_observed_fee_tax_projection(view)

    # Corrupt target from different portfolio (simulated direct construction)
    port_b = _make_portfolio()
    buy_other_port = _make_tx(port_b.id, account_id, TransactionType.BUY)

    with pytest.raises(FeeTaxAttributionError, match="not found in active transactions"):
        build_observed_fee_tax_attribution_set(
            proj,
            (FeeTaxAttributionIntent(fee_tx.id, buy_other_port.id, Decimal("5.00")),),
        )


def test_matrix_r_charge_as_target_rejection() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_a = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    fee_b = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("5.00"))

    view = build_ledger_projection_view(portfolio, [fee_a, fee_b])
    proj = build_observed_fee_tax_projection(view)

    intent = FeeTaxAttributionIntent(fee_a.id, fee_b.id, Decimal("5.00"))
    with pytest.raises(FeeTaxAttributionError, match="target_transaction cannot be of type FEE"):
        build_observed_fee_tax_attribution_set(proj, (intent,))


def test_matrix_s_reversal_as_target_rejection() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY)
    rev_tx = _make_tx(portfolio.id, account_id, TransactionType.REVERSAL, reverses_transaction_id=buy_tx.id)

    view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx, rev_tx])
    proj = build_observed_fee_tax_projection(view)

    # Direct tamper intent referencing the reversal transaction ID
    intent = FeeTaxAttributionIntent(fee_tx.id, rev_tx.id, Decimal("5.00"))
    with pytest.raises(FeeTaxAttributionError, match="not found in active transactions"):
        build_observed_fee_tax_attribution_set(proj, (intent,))


# ==============================================================================
# 6. PIT & Reversal Lifecycle Tests (T - W)
# ==============================================================================

def test_matrix_t_reversed_charge_pit_lifecycle() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 6, 3, 10, 0, 0, tzinfo=timezone.utc)

    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, recorded_at=t1, cash_amount=Decimal("10.00"))
    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY, recorded_at=t1)
    rev_tx = _make_tx(portfolio.id, account_id, TransactionType.REVERSAL, recorded_at=t3, reverses_transaction_id=fee_tx.id)

    all_txs = [fee_tx, buy_tx, rev_tx]
    intent = FeeTaxAttributionIntent(fee_tx.id, buy_tx.id, Decimal("10.00"))

    # At PIT T2 (before reversal): charge is active -> attribution resolves
    view_t2 = build_ledger_projection_view(portfolio, all_txs, as_of_recorded_at=t2)
    proj_t2 = build_observed_fee_tax_projection(view_t2)
    attr_t2 = build_observed_fee_tax_attribution_set(proj_t2, (intent,))
    assert attr_t2.attribution_count == 1

    # At PIT T3 (after reversal): charge is reversed -> intent rejected
    view_t3 = build_ledger_projection_view(portfolio, all_txs, as_of_recorded_at=t3)
    proj_t3 = build_observed_fee_tax_projection(view_t3)
    with pytest.raises(FeeTaxAttributionError, match="not found in observed active charge events"):
        build_observed_fee_tax_attribution_set(proj_t3, (intent,))


def test_matrix_u_reversed_target_pit_lifecycle() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 6, 3, 10, 0, 0, tzinfo=timezone.utc)

    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY, recorded_at=t1)
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, recorded_at=t2, cash_amount=Decimal("10.00"))
    rev_tx = _make_tx(portfolio.id, account_id, TransactionType.REVERSAL, recorded_at=t3, reverses_transaction_id=buy_tx.id)

    all_txs = [buy_tx, fee_tx, rev_tx]
    intent = FeeTaxAttributionIntent(fee_tx.id, buy_tx.id, Decimal("10.00"))

    # At PIT T2: target is active -> attribution resolves
    view_t2 = build_ledger_projection_view(portfolio, all_txs, as_of_recorded_at=t2)
    proj_t2 = build_observed_fee_tax_projection(view_t2)
    attr_t2 = build_observed_fee_tax_attribution_set(proj_t2, (intent,))
    assert attr_t2.attribution_count == 1

    # At PIT T3: target is reversed -> intent rejected
    view_t3 = build_ledger_projection_view(portfolio, all_txs, as_of_recorded_at=t3)
    proj_t3 = build_observed_fee_tax_projection(view_t3)
    with pytest.raises(FeeTaxAttributionError, match="not found in active transactions"):
        build_observed_fee_tax_attribution_set(proj_t3, (intent,))


def test_matrix_v_future_charge_pit_rejection() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone.utc)

    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY, recorded_at=t1)
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, recorded_at=t2, cash_amount=Decimal("10.00"))

    # Cutoff at T1: fee_tx is in future -> not in projection events
    view_t1 = build_ledger_projection_view(portfolio, [buy_tx, fee_tx], as_of_recorded_at=t1)
    proj_t1 = build_observed_fee_tax_projection(view_t1)

    intent = FeeTaxAttributionIntent(fee_tx.id, buy_tx.id, Decimal("10.00"))
    with pytest.raises(FeeTaxAttributionError, match="not found in observed active charge events"):
        build_observed_fee_tax_attribution_set(proj_t1, (intent,))


def test_matrix_w_future_target_pit_rejection() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone.utc)

    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, recorded_at=t1, cash_amount=Decimal("10.00"))
    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY, recorded_at=t2)

    # Cutoff at T1: buy_tx is in future -> not in active_transactions
    view_t1 = build_ledger_projection_view(portfolio, [fee_tx, buy_tx], as_of_recorded_at=t1)
    proj_t1 = build_observed_fee_tax_projection(view_t1)

    intent = FeeTaxAttributionIntent(fee_tx.id, buy_tx.id, Decimal("10.00"))
    with pytest.raises(FeeTaxAttributionError, match="not found in active transactions"):
        build_observed_fee_tax_attribution_set(proj_t1, (intent,))


# ==============================================================================
# 7. Exact Decimal Representation & Arithmetic Purity Tests (X - Z)
# ==============================================================================

def test_matrix_x_exact_decimal_representation_preservation() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.000"))
    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx])
    proj = build_observed_fee_tax_projection(view)

    amt = Decimal("6.000")
    intent = FeeTaxAttributionIntent(fee_tx.id, buy_tx.id, amt)
    attr_set = build_observed_fee_tax_attribution_set(proj, (intent,))

    resolved_amt = attr_set.attributions[0].allocated_amount
    assert resolved_amt == amt
    assert resolved_amt.as_tuple() == amt.as_tuple()
    assert str(resolved_amt) == "6.000"


def test_matrix_y_ambient_context_independent_sum() -> None:
    orig_prec = decimal.getcontext().prec
    try:
        decimal.getcontext().prec = 2

        amounts = [
            Decimal("10000000000.111111111"),
            Decimal("20000000000.222222222"),
        ]
        exact_sum = _exact_decimal_sum(amounts)
        expected = Decimal("30000000000.333333333")
        assert exact_sum == expected
        assert exact_sum.as_tuple() == expected.as_tuple()
    finally:
        decimal.getcontext().prec = orig_prec


def test_matrix_z_ambient_context_independent_remainder() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(
        portfolio.id,
        account_id,
        TransactionType.FEE,
        cash_amount=Decimal("10000000000.555555555"),
    )
    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx])
    proj = build_observed_fee_tax_projection(view)

    alloc_amt = Decimal("4000000000.222222222")
    intent = FeeTaxAttributionIntent(fee_tx.id, buy_tx.id, alloc_amt)
    attr_set = build_observed_fee_tax_attribution_set(proj, (intent,))

    orig_prec = decimal.getcontext().prec
    try:
        decimal.getcontext().prec = 2

        unallocated = attr_set.unallocated_amount_for_charge(fee_tx.id)
        expected = Decimal("6000000000.333333333")
        assert unallocated == expected
        assert unallocated.as_tuple() == expected.as_tuple()
    finally:
        decimal.getcontext().prec = orig_prec


# ==============================================================================
# 8. Anti-Tamper & Direct-Constructor Invariant Tests (AA - AG)
# ==============================================================================

def test_matrix_aa_exact_pit_metadata_representation_binding() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    utc_cutoff = datetime(2026, 6, 2, 0, 0, 0, tzinfo=timezone.utc)
    plus_three_cutoff = datetime(2026, 6, 2, 3, 0, 0, tzinfo=timezone(timedelta(hours=3)))

    view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx], as_of_recorded_at=utc_cutoff)
    proj = build_observed_fee_tax_projection(view)

    intent = FeeTaxAttributionIntent(fee_tx.id, buy_tx.id, Decimal("10.00"))
    attr_set = build_observed_fee_tax_attribution_set(proj, (intent,))

    # Direct constructor tampering with different offset representation of same instant
    with pytest.raises(FeeTaxAttributionError, match="as_of_recorded_at .* does not match"):
        ObservedFeeTaxAttributionSet(
            portfolio_id=proj.portfolio_id,
            mode=proj.mode,
            as_of_recorded_at=plus_three_cutoff,
            observed_projection=proj,
            intents=attr_set.intents,
            attributions=attr_set.attributions,
        )


def test_matrix_ab_direct_constructor_omitted_attribution() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    buy_a = _make_tx(portfolio.id, account_id, TransactionType.BUY)
    buy_b = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    view = build_ledger_projection_view(portfolio, [fee_tx, buy_a, buy_b])
    proj = build_observed_fee_tax_projection(view)

    intents = (
        FeeTaxAttributionIntent(fee_tx.id, buy_a.id, Decimal("6.00")),
        FeeTaxAttributionIntent(fee_tx.id, buy_b.id, Decimal("4.00")),
    )
    attr_set = build_observed_fee_tax_attribution_set(proj, intents)

    # Supply only 1 attribution when 2 intents provided
    with pytest.raises(FeeTaxAttributionError, match="attributions count .* does not match"):
        ObservedFeeTaxAttributionSet(
            portfolio_id=proj.portfolio_id,
            mode=proj.mode,
            as_of_recorded_at=proj.as_of_recorded_at,
            observed_projection=proj,
            intents=intents,
            attributions=(attr_set.attributions[0],),
        )


def test_matrix_ac_direct_constructor_extra_attribution() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx])
    proj = build_observed_fee_tax_projection(view)

    intents = (FeeTaxAttributionIntent(fee_tx.id, buy_tx.id, Decimal("5.00")),)
    attr_set = build_observed_fee_tax_attribution_set(proj, intents)

    extra_attr = ResolvedFeeTaxAttribution(
        charge_transaction=fee_tx,
        target_transaction=buy_tx,
        allocated_amount=Decimal("2.00"),
    )
    with pytest.raises(FeeTaxAttributionError, match="attributions count .* does not match"):
        ObservedFeeTaxAttributionSet(
            portfolio_id=proj.portfolio_id,
            mode=proj.mode,
            as_of_recorded_at=proj.as_of_recorded_at,
            observed_projection=proj,
            intents=intents,
            attributions=(attr_set.attributions[0], extra_attr),
        )


def test_matrix_ad_direct_constructor_reordered_attributions() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    buy_a = _make_tx(portfolio.id, account_id, TransactionType.BUY)
    buy_b = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    view = build_ledger_projection_view(portfolio, [fee_tx, buy_a, buy_b])
    proj = build_observed_fee_tax_projection(view)

    intents = (
        FeeTaxAttributionIntent(fee_tx.id, buy_a.id, Decimal("6.00")),
        FeeTaxAttributionIntent(fee_tx.id, buy_b.id, Decimal("4.00")),
    )
    attr_set = build_observed_fee_tax_attribution_set(proj, intents)

    # Reorder attributions (B then A instead of A then B)
    with pytest.raises(FeeTaxAttributionError, match="target_transaction object does not match"):
        ObservedFeeTaxAttributionSet(
            portfolio_id=proj.portfolio_id,
            mode=proj.mode,
            as_of_recorded_at=proj.as_of_recorded_at,
            observed_projection=proj,
            intents=intents,
            attributions=(attr_set.attributions[1], attr_set.attributions[0]),
        )


def test_matrix_ae_semantic_transaction_copy_tamper() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx])
    proj = build_observed_fee_tax_projection(view)

    intent = FeeTaxAttributionIntent(fee_tx.id, buy_tx.id, Decimal("10.00"))

    # Synthesize identical transaction with different object identity
    fake_buy = _make_tx(
        portfolio.id,
        account_id,
        TransactionType.BUY,
        tx_id=buy_tx.id,
        recorded_at=buy_tx.recorded_at,
        effective_date=buy_tx.effective_date,
        executed_at=buy_tx.executed_at,
        instrument_id=buy_tx.instrument_id,
        quantity=buy_tx.quantity,
        unit_price=buy_tx.unit_price,
        trade_currency=buy_tx.trade_currency,
        cash_amount=buy_tx.cash_amount,
        cash_currency=buy_tx.cash_currency,
    )
    tampered_resolved = ResolvedFeeTaxAttribution(
        charge_transaction=fee_tx,
        target_transaction=fake_buy,
        allocated_amount=Decimal("10.00"),
    )

    with pytest.raises(FeeTaxAttributionError, match="target_transaction object does not match"):
        ObservedFeeTaxAttributionSet(
            portfolio_id=proj.portfolio_id,
            mode=proj.mode,
            as_of_recorded_at=proj.as_of_recorded_at,
            observed_projection=proj,
            intents=(intent,),
            attributions=(tampered_resolved,),
        )


def test_matrix_af_allocated_amount_representation_tamper() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx])
    proj = build_observed_fee_tax_projection(view)

    intent = FeeTaxAttributionIntent(fee_tx.id, buy_tx.id, Decimal("6.00"))

    # Tampered representation: Decimal("6.0") instead of Decimal("6.00")
    tampered_resolved = ResolvedFeeTaxAttribution(
        charge_transaction=fee_tx,
        target_transaction=buy_tx,
        allocated_amount=Decimal("6.0"),
    )
    with pytest.raises(FeeTaxAttributionError, match="allocated_amount representation .* does not match"):
        ObservedFeeTaxAttributionSet(
            portfolio_id=proj.portfolio_id,
            mode=proj.mode,
            as_of_recorded_at=proj.as_of_recorded_at,
            observed_projection=proj,
            intents=(intent,),
            attributions=(tampered_resolved,),
        )


@pytest.mark.parametrize(
    "invalid_input",
    [None, True, False, "not_tuple", [1, 2, 3], 12345],
)
def test_matrix_ag_wrong_input_types(invalid_input: any) -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY)

    view = build_ledger_projection_view(portfolio, [fee_tx, buy_tx])
    proj = build_observed_fee_tax_projection(view)

    with pytest.raises(TypeError):
        build_observed_fee_tax_attribution_set(invalid_input, ())

    with pytest.raises(TypeError):
        build_observed_fee_tax_attribution_set(proj, invalid_input)


# ==============================================================================
# 9. No Heuristic Attribution & Final Red-Team Tests (AH & Section 80)
# ==============================================================================

def test_matrix_ah_no_heuristic_attribution() -> None:
    """
    Two economic transactions with matching instrument, account, date, and nearby
    timestamps must produce ZERO attributions unless an explicit intent is supplied.
    """
    portfolio = _make_portfolio()
    account_id = uuid4()
    common_inst_id = uuid4()
    common_date = date(2026, 6, 1)

    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 10, 0, 1, tzinfo=timezone.utc)

    buy_tx = _make_tx(
        portfolio.id,
        account_id,
        TransactionType.BUY,
        instrument_id=common_inst_id,
        effective_date=common_date,
        recorded_at=t1,
    )
    fee_tx = _make_tx(
        portfolio.id,
        account_id,
        TransactionType.FEE,
        instrument_id=common_inst_id,
        effective_date=common_date,
        recorded_at=t2,
        cash_amount=Decimal("10.00"),
    )

    view = build_ledger_projection_view(portfolio, [buy_tx, fee_tx])
    proj = build_observed_fee_tax_projection(view)

    # With empty intents, zero attributions are formed
    attr_set = build_observed_fee_tax_attribution_set(proj, ())
    assert attr_set.attribution_count == 0
    assert attr_set.attributions == ()
    assert attr_set.unallocated_amount_for_charge(fee_tx.id) == Decimal("10.00")
    assert attr_set.is_fully_allocated(fee_tx.id) is False


def test_final_red_team_scenario_section_80() -> None:
    """
    Final Red-Team specification from Section 80:
    BUY A, BUY B, FEE = USD 10.000
    Intents: FEE -> BUY A: USD 6.000, FEE -> BUY B: USD 4.000
    1. Verify fully allocated, unallocated = exact zero
    2. Try 6.000 + 4.001 -> REJECTED
    3. Remove BUY B from active PIT snapshot -> FAILS
    4. Give no intents -> attribution set empty, charge exists, unallocated = full charge
    5. Two economic transactions with same instrument, account, date, nearby timestamp -> ZERO attribution without intent
    """
    portfolio = _make_portfolio()
    account_id = uuid4()
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 11, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    buy_a = _make_tx(portfolio.id, account_id, TransactionType.BUY, recorded_at=t1)
    buy_b = _make_tx(portfolio.id, account_id, TransactionType.BUY, recorded_at=t2)
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, recorded_at=t3, cash_amount=Decimal("10.000"))

    all_txs = [buy_a, buy_b, fee_tx]
    view = build_ledger_projection_view(portfolio, all_txs)
    proj = build_observed_fee_tax_projection(view)

    # 1. Valid split
    intents = (
        FeeTaxAttributionIntent(fee_tx.id, buy_a.id, Decimal("6.000")),
        FeeTaxAttributionIntent(fee_tx.id, buy_b.id, Decimal("4.000")),
    )
    attr_set = build_observed_fee_tax_attribution_set(proj, intents)
    assert attr_set.is_fully_allocated(fee_tx.id) is True
    assert attr_set.unallocated_amount_for_charge(fee_tx.id) == Decimal("0")
    assert attr_set.attribution_count == 2

    # 2. Over-allocation: 6.000 + 4.001
    bad_intents = (
        FeeTaxAttributionIntent(fee_tx.id, buy_a.id, Decimal("6.000")),
        FeeTaxAttributionIntent(fee_tx.id, buy_b.id, Decimal("4.001")),
    )
    with pytest.raises(FeeTaxAttributionError, match="Over-allocation detected"):
        build_observed_fee_tax_attribution_set(proj, bad_intents)

    # 3. Remove BUY B from active PIT snapshot (by setting cutoff before T2)
    view_pit_t1 = build_ledger_projection_view(portfolio, all_txs, as_of_recorded_at=t1)
    # Notice fee_tx is at T3, so at T1 fee_tx is also not known. Let's make fee_tx at T1, buy_b at T2
    fee_t1 = _make_tx(portfolio.id, account_id, TransactionType.FEE, recorded_at=t1, cash_amount=Decimal("10.000"))
    view_pit = build_ledger_projection_view(portfolio, [buy_a, fee_t1, buy_b], as_of_recorded_at=t1)
    proj_pit = build_observed_fee_tax_projection(view_pit)
    # At T1, buy_b is not active in snapshot
    with pytest.raises(FeeTaxAttributionError, match="Target transaction .* not found in active transactions"):
        build_observed_fee_tax_attribution_set(
            proj_pit,
            (
                FeeTaxAttributionIntent(fee_t1.id, buy_a.id, Decimal("6.000")),
                FeeTaxAttributionIntent(fee_t1.id, buy_b.id, Decimal("4.000")),
            ),
        )

    # 4. No intents supplied
    empty_attr = build_observed_fee_tax_attribution_set(proj, ())
    assert empty_attr.attribution_count == 0
    assert empty_attr.unallocated_amount_for_charge(fee_tx.id) == Decimal("10.000")
    assert empty_attr.is_fully_allocated(fee_tx.id) is False

    # 5. Unknown charge ID in query raises FeeTaxAttributionError
    with pytest.raises(FeeTaxAttributionError, match="not found in observed active charge events"):
        empty_attr.unallocated_amount_for_charge(uuid4())

    with pytest.raises(FeeTaxAttributionError, match="not found in observed active charge events"):
        empty_attr.is_fully_allocated(uuid4())


# ==============================================================================
# 10. Static Purity & AST Isolation Tests
# ==============================================================================

def test_static_purity_ast_checks() -> None:
    source_path = inspect.getfile(attribution_module)
    with open(source_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=source_path)

    prohibited_names = {
        "now",
        "utcnow",
        "today",
        "uuid4",
        "uuid5",
        "hashlib",
        "sha256",
        "rpc",
        "table",
        "PortfolioRepository",
        "Supabase",
        "PostgREST",
        "float",
        "round",
        "quantize",
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id not in prohibited_names, f"Prohibited identifier '{node.id}' found in {source_path}"
        elif isinstance(node, ast.Attribute):
            assert node.attr not in prohibited_names, f"Prohibited attribute access '{node.attr}' found in {source_path}"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id not in prohibited_names, f"Prohibited call '{node.func.id}()' found in {source_path}"
            elif isinstance(node.func, ast.Attribute):
                assert node.func.attr not in prohibited_names, f"Prohibited method call '.{node.func.attr}()' found in {source_path}"


def test_target_policy_immutability_and_no_mutable_module_sets() -> None:
    """
    Red-Team validation: Production module must NOT define or expose mutable
    _VALID_TARGET_TYPES or _PROHIBITED_TARGET_TYPES sets/dicts at module level.
    """
    source_path = inspect.getfile(attribution_module)
    with open(source_path, "r", encoding="utf-8") as f:
        content = f.read()
        tree = ast.parse(content, filename=source_path)

    # 1. Text checks
    assert "_VALID_TARGET_TYPES = {" not in content
    assert "_PROHIBITED_TARGET_TYPES = {" not in content

    # 2. Module namespace checks
    assert not hasattr(attribution_module, "_VALID_TARGET_TYPES")
    assert not hasattr(attribution_module, "_PROHIBITED_TARGET_TYPES")

    # 3. AST check: no module-level Assign of Set or Dict literal
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    assert not isinstance(stmt.value, (ast.Set, ast.Dict, ast.List)), (
                        f"Found mutable collection assigned to module-level variable '{target.id}' in {source_path}"
                    )


def test_future_or_unknown_enum_fails_closed() -> None:
    """
    If an unknown or future TransactionType is passed, it must fail closed.
    """
    assert attribution_module._is_valid_attribution_target_type(None) is False
    assert attribution_module._is_valid_attribution_target_type(True) is False
    assert attribution_module._is_valid_attribution_target_type(False) is False
    assert attribution_module._is_valid_attribution_target_type("BUY") is False
    assert attribution_module._is_valid_attribution_target_type(123) is False
