"""
backend/tests/test_portfolio_fee_tax.py
=======================================
Comprehensive test suite for Observed Fee and Tax-Withholding Event Projection (Phase 14A).
"""

from __future__ import annotations

import ast
from datetime import date, datetime, timedelta, timezone
import decimal
from decimal import Decimal
import inspect
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
    build_cash_balance_projection,
)
from backend.engine.private.portfolio.fee_tax import (
    FeeTaxProjectionError,
    ObservedFeeTaxAggregateState,
    ObservedFeeTaxAggregation,
    ObservedFeeTaxProjection,
    build_observed_fee_tax_aggregation,
    build_observed_fee_tax_projection,
)
import backend.engine.private.portfolio.fee_tax as fee_tax_module
from backend.engine.private.portfolio.models import (
    Portfolio,
    PortfolioTransaction,
)
from backend.engine.private.portfolio.positions import (
    PositionQuantityProjection,
    PositionQuantityState,
    build_position_quantity_projection,
)
from backend.engine.private.portfolio.projection import (
    LedgerProjectionView,
    ProjectedTransactionState,
    build_ledger_projection_view,
)


def _make_portfolio(
    portfolio_id: UUID | None = None,
    mode: PortfolioMode = PortfolioMode.MY_PORTFOLIO,
) -> Portfolio:
    return Portfolio(
        id=portfolio_id or uuid4(),
        mode=mode,
        name="Test Portfolio",
        base_currency=Currency.USD,
        created_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
    )


def _make_tx(
    portfolio_id: UUID,
    account_id: UUID | None = None,
    transaction_type: TransactionType = TransactionType.FEE,
    effective_date: date = date(2026, 6, 1),
    recorded_at: datetime | None = None,
    cash_amount: Decimal | None = Decimal("10.00"),
    cash_currency: Currency | None = Currency.USD,
    instrument_id: UUID | None = None,
    quantity: Decimal | None = None,
    unit_price: Decimal | None = None,
    trade_currency: Currency | None = None,
    from_currency: Currency | None = None,
    from_amount: Decimal | None = None,
    to_currency: Currency | None = None,
    to_amount: Decimal | None = None,
    reverses_transaction_id: UUID | None = None,
    tx_id: UUID | None = None,
) -> PortfolioTransaction:
    rec_dt = recorded_at or datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    acc_id = account_id or uuid4()

    kwargs = {
        "id": tx_id or uuid4(),
        "portfolio_id": portfolio_id,
        "account_id": acc_id,
        "transaction_type": transaction_type,
        "effective_date": effective_date,
        "recorded_at": rec_dt,
    }

    if transaction_type in (TransactionType.BUY, TransactionType.SELL):
        kwargs.update({
            "instrument_id": instrument_id or uuid4(),
            "quantity": quantity or Decimal("10"),
            "unit_price": unit_price or Decimal("100"),
            "trade_currency": trade_currency or Currency.USD,
        })
    elif transaction_type in (
        TransactionType.CASH_DEPOSIT,
        TransactionType.CASH_WITHDRAWAL,
        TransactionType.DIVIDEND,
        TransactionType.INTEREST,
        TransactionType.FEE,
        TransactionType.TAX_WITHHOLDING,
    ):
        kwargs.update({
            "cash_amount": cash_amount,
            "cash_currency": cash_currency,
            "instrument_id": instrument_id,
        })
    elif transaction_type == TransactionType.FX_CONVERSION:
        kwargs.update({
            "from_currency": from_currency or Currency.USD,
            "from_amount": from_amount or Decimal("100"),
            "to_currency": to_currency or Currency.TRY,
            "to_amount": to_amount or Decimal("3400"),
        })
    elif transaction_type == TransactionType.REVERSAL:
        kwargs.update({
            "reverses_transaction_id": reverses_transaction_id,
        })

    return PortfolioTransaction(**kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Basic Filter & Order Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_basic_filter_preserves_only_fee_and_tax() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()

    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY, effective_date=date(2026, 6, 1))
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, effective_date=date(2026, 6, 2), cash_amount=Decimal("12.50"))
    div_tx = _make_tx(portfolio.id, account_id, TransactionType.DIVIDEND, effective_date=date(2026, 6, 3), cash_amount=Decimal("50.00"))
    tax_tx = _make_tx(portfolio.id, account_id, TransactionType.TAX_WITHHOLDING, effective_date=date(2026, 6, 4), cash_amount=Decimal("7.50"))
    fx_tx = _make_tx(portfolio.id, account_id, TransactionType.FX_CONVERSION, effective_date=date(2026, 6, 5))

    ledger_view = build_ledger_projection_view(
        portfolio,
        [buy_tx, fee_tx, div_tx, tax_tx, fx_tx],
    )

    proj = build_observed_fee_tax_projection(ledger_view)

    assert proj.portfolio_id == portfolio.id
    assert proj.mode == portfolio.mode
    assert proj.as_of_recorded_at is None
    assert proj.ledger_view is ledger_view

    # Only fee and tax withholding in exact relative order
    assert proj.events == (fee_tx, tax_tx)
    assert proj.event_count == 2
    assert proj.fee_count == 1
    assert proj.tax_withholding_count == 1
    assert proj.fee_events == (fee_tx,)
    assert proj.tax_withholding_events == (tax_tx,)


def test_order_preserves_ledger_view_active_order() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()

    tax_a = _make_tx(portfolio.id, account_id, TransactionType.TAX_WITHHOLDING, effective_date=date(2026, 6, 1))
    buy_b = _make_tx(portfolio.id, account_id, TransactionType.BUY, effective_date=date(2026, 6, 2))
    fee_c = _make_tx(portfolio.id, account_id, TransactionType.FEE, effective_date=date(2026, 6, 3))
    fee_d = _make_tx(portfolio.id, account_id, TransactionType.FEE, effective_date=date(2026, 6, 4))

    ledger_view = build_ledger_projection_view(
        portfolio,
        [tax_a, buy_b, fee_c, fee_d],
    )

    proj = build_observed_fee_tax_projection(ledger_view)

    assert proj.events == (tax_a, fee_c, fee_d)
    assert proj.events[0] is tax_a
    assert proj.events[1] is fee_c
    assert proj.events[2] is fee_d


def test_empty_projection() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()

    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY)
    dep_tx = _make_tx(portfolio.id, account_id, TransactionType.CASH_DEPOSIT)

    ledger_view = build_ledger_projection_view(portfolio, [buy_tx, dep_tx])
    proj = build_observed_fee_tax_projection(ledger_view)

    assert proj.events == ()
    assert proj.fee_events == ()
    assert proj.tax_withholding_events == ()
    assert proj.instrument_linked_events == ()
    assert proj.account_level_events == ()
    assert proj.event_count == 0
    assert proj.fee_count == 0
    assert proj.tax_withholding_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# 2. Event Representation & Optional Instrument Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_fee_event_exact_decimal_preservation() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()

    fee_tx = _make_tx(
        portfolio.id,
        account_id,
        TransactionType.FEE,
        cash_amount=Decimal("12.3400"),
        cash_currency=Currency.USD,
        instrument_id=None,
    )

    ledger_view = build_ledger_projection_view(portfolio, [fee_tx])
    proj = build_observed_fee_tax_projection(ledger_view)

    assert len(proj.events) == 1
    ev = proj.events[0]
    assert ev.cash_amount == Decimal("12.3400")
    assert str(ev.cash_amount) == "12.3400"
    assert ev.cash_currency == Currency.USD
    assert ev.instrument_id is None
    assert proj.fee_events == (fee_tx,)
    assert proj.account_level_events == (fee_tx,)
    assert proj.instrument_linked_events == ()


def test_tax_event_with_instrument_linkage() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    inst_id = uuid4()

    tax_tx = _make_tx(
        portfolio.id,
        account_id,
        TransactionType.TAX_WITHHOLDING,
        cash_amount=Decimal("15.00"),
        cash_currency=Currency.USD,
        instrument_id=inst_id,
    )

    ledger_view = build_ledger_projection_view(portfolio, [tax_tx])
    proj = build_observed_fee_tax_projection(ledger_view)

    assert len(proj.events) == 1
    ev = proj.events[0]
    assert ev.cash_amount == Decimal("15.00")
    assert ev.cash_currency == Currency.USD
    assert ev.instrument_id == inst_id
    assert proj.tax_withholding_events == (tax_tx,)
    assert proj.instrument_linked_events == (tax_tx,)
    assert proj.account_level_events == ()


def test_instrument_linked_and_account_level_mix() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    inst_id_1 = uuid4()
    inst_id_2 = uuid4()

    fee_acct = _make_tx(portfolio.id, account_id, TransactionType.FEE, effective_date=date(2026, 6, 1), cash_amount=Decimal("5.00"), instrument_id=None)
    fee_inst = _make_tx(portfolio.id, account_id, TransactionType.FEE, effective_date=date(2026, 6, 2), cash_amount=Decimal("1.25"), instrument_id=inst_id_1)
    tax_inst = _make_tx(portfolio.id, account_id, TransactionType.TAX_WITHHOLDING, effective_date=date(2026, 6, 3), cash_amount=Decimal("15.00"), instrument_id=inst_id_2)
    tax_acct = _make_tx(portfolio.id, account_id, TransactionType.TAX_WITHHOLDING, effective_date=date(2026, 6, 4), cash_amount=Decimal("2.50"), instrument_id=None)

    ledger_view = build_ledger_projection_view(portfolio, [fee_acct, fee_inst, tax_inst, tax_acct])
    proj = build_observed_fee_tax_projection(ledger_view)

    assert proj.instrument_linked_events == (fee_inst, tax_inst)
    assert proj.account_level_events == (fee_acct, tax_acct)
    assert proj.event_count == 4


# ─────────────────────────────────────────────────────────────────────────────
# 3. Direct Constructor Anti-Tamper Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_tamper_omitted_event_rejected() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()

    fee_a = _make_tx(portfolio.id, account_id, TransactionType.FEE, effective_date=date(2026, 6, 1), cash_amount=Decimal("10.00"))
    tax_b = _make_tx(portfolio.id, account_id, TransactionType.TAX_WITHHOLDING, effective_date=date(2026, 6, 2), cash_amount=Decimal("5.00"))

    ledger_view = build_ledger_projection_view(portfolio, [fee_a, tax_b])

    # Direct constructor omitting tax_b
    with pytest.raises(FeeTaxProjectionError, match="events count 1 does not match canonical filtered event count 2"):
        ObservedFeeTaxProjection(
            portfolio_id=ledger_view.portfolio_id,
            mode=ledger_view.mode,
            as_of_recorded_at=ledger_view.as_of_recorded_at,
            ledger_view=ledger_view,
            events=(fee_a,),
        )


def test_tamper_extra_non_fee_event_rejected() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()

    fee_a = _make_tx(portfolio.id, account_id, TransactionType.FEE, effective_date=date(2026, 6, 1), cash_amount=Decimal("10.00"))
    buy_b = _make_tx(portfolio.id, account_id, TransactionType.BUY, effective_date=date(2026, 6, 2))

    ledger_view = build_ledger_projection_view(portfolio, [fee_a, buy_b])

    # Direct constructor adding buy_b
    with pytest.raises(FeeTaxProjectionError, match="events count 2 does not match canonical filtered event count 1"):
        ObservedFeeTaxProjection(
            portfolio_id=ledger_view.portfolio_id,
            mode=ledger_view.mode,
            as_of_recorded_at=ledger_view.as_of_recorded_at,
            ledger_view=ledger_view,
            events=(fee_a, buy_b),
        )


def test_tamper_duplicate_event_rejected() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()

    fee_a = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    ledger_view = build_ledger_projection_view(portfolio, [fee_a])

    with pytest.raises(FeeTaxProjectionError, match="events count 2 does not match canonical filtered event count 1"):
        ObservedFeeTaxProjection(
            portfolio_id=ledger_view.portfolio_id,
            mode=ledger_view.mode,
            as_of_recorded_at=ledger_view.as_of_recorded_at,
            ledger_view=ledger_view,
            events=(fee_a, fee_a),
        )


def test_tamper_reordered_events_rejected() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()

    fee_a = _make_tx(portfolio.id, account_id, TransactionType.FEE, effective_date=date(2026, 6, 1))
    tax_b = _make_tx(portfolio.id, account_id, TransactionType.TAX_WITHHOLDING, effective_date=date(2026, 6, 2))

    ledger_view = build_ledger_projection_view(portfolio, [fee_a, tax_b])

    # Inverted order: tax_b, fee_a
    with pytest.raises(FeeTaxProjectionError, match="failed exact object-identity check"):
        ObservedFeeTaxProjection(
            portfolio_id=ledger_view.portfolio_id,
            mode=ledger_view.mode,
            as_of_recorded_at=ledger_view.as_of_recorded_at,
            ledger_view=ledger_view,
            events=(tax_b, fee_a),
        )


def test_tamper_semantic_copy_rejected() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    tx_id = uuid4()
    rec_dt = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    fee_orig = _make_tx(
        portfolio.id,
        account_id,
        TransactionType.FEE,
        cash_amount=Decimal("10.00"),
        recorded_at=rec_dt,
        tx_id=tx_id,
    )

    ledger_view = build_ledger_projection_view(portfolio, [fee_orig])

    # Reconstructed copy with same fields and UUID
    fee_copy = _make_tx(
        portfolio.id,
        account_id,
        TransactionType.FEE,
        cash_amount=Decimal("10.00"),
        recorded_at=rec_dt,
        tx_id=tx_id,
    )

    assert fee_orig == fee_copy
    assert fee_orig is not fee_copy

    with pytest.raises(FeeTaxProjectionError, match="failed exact object-identity check"):
        ObservedFeeTaxProjection(
            portfolio_id=ledger_view.portfolio_id,
            mode=ledger_view.mode,
            as_of_recorded_at=ledger_view.as_of_recorded_at,
            ledger_view=ledger_view,
            events=(fee_copy,),
        )


def test_tamper_event_from_another_ledger_view_rejected() -> None:
    portfolio_1 = _make_portfolio()
    portfolio_2 = _make_portfolio()
    account_id = uuid4()

    fee_1 = _make_tx(portfolio_1.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))
    fee_2 = _make_tx(portfolio_2.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"))

    view_1 = build_ledger_projection_view(portfolio_1, [fee_1])
    _view_2 = build_ledger_projection_view(portfolio_2, [fee_2])

    with pytest.raises(FeeTaxProjectionError, match="failed exact object-identity check"):
        ObservedFeeTaxProjection(
            portfolio_id=view_1.portfolio_id,
            mode=view_1.mode,
            as_of_recorded_at=view_1.as_of_recorded_at,
            ledger_view=view_1,
            events=(fee_2,),
        )


def test_tamper_metadata_mismatches_rejected() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE)

    cutoff = datetime(2026, 6, 2, 0, 0, 0, tzinfo=timezone.utc)
    ledger_view = build_ledger_projection_view(portfolio, [fee_tx], as_of_recorded_at=cutoff)

    # Wrong portfolio_id
    with pytest.raises(FeeTaxProjectionError, match="portfolio_id .* does not match"):
        ObservedFeeTaxProjection(
            portfolio_id=uuid4(),
            mode=ledger_view.mode,
            as_of_recorded_at=ledger_view.as_of_recorded_at,
            ledger_view=ledger_view,
            events=(fee_tx,),
        )

    # Wrong mode
    with pytest.raises(FeeTaxProjectionError, match="mode .* does not match"):
        ObservedFeeTaxProjection(
            portfolio_id=ledger_view.portfolio_id,
            mode=PortfolioMode.SANDBOX,
            as_of_recorded_at=ledger_view.as_of_recorded_at,
            ledger_view=ledger_view,
            events=(fee_tx,),
        )

    # Wrong as_of_recorded_at
    with pytest.raises(FeeTaxProjectionError, match="as_of_recorded_at .* does not match"):
        ObservedFeeTaxProjection(
            portfolio_id=ledger_view.portfolio_id,
            mode=ledger_view.mode,
            as_of_recorded_at=None,
            ledger_view=ledger_view,
            events=(fee_tx,),
        )


def test_exact_datetime_representation_same_instant_different_offset_rejected() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE)

    utc_cutoff = datetime(2026, 6, 2, 0, 0, 0, tzinfo=timezone.utc)
    plus_three_cutoff = datetime(2026, 6, 2, 3, 0, 0, tzinfo=timezone(timedelta(hours=3)))

    # Red-Team: Verify standard Python equality considers them the same physical instant
    assert utc_cutoff == plus_three_cutoff

    ledger_view = build_ledger_projection_view(portfolio, [fee_tx], as_of_recorded_at=utc_cutoff)

    # Phase 14A.1 must reject direct construction using different offset representation
    with pytest.raises(FeeTaxProjectionError, match="as_of_recorded_at .* does not match"):
        ObservedFeeTaxProjection(
            portfolio_id=ledger_view.portfolio_id,
            mode=ledger_view.mode,
            as_of_recorded_at=plus_three_cutoff,
            ledger_view=ledger_view,
            events=(fee_tx,),
        )


def test_exact_datetime_representation_matching_aware_accepted() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE)

    cutoff_1 = datetime(2026, 6, 2, 0, 0, 0, 123456, tzinfo=timezone.utc)
    cutoff_2 = datetime(2026, 6, 2, 0, 0, 0, 123456, tzinfo=timezone.utc)

    # Distinct object instances but identical exact representation
    assert cutoff_1 is not cutoff_2
    assert cutoff_1 == cutoff_2

    ledger_view = build_ledger_projection_view(portfolio, [fee_tx], as_of_recorded_at=cutoff_1)
    proj = ObservedFeeTaxProjection(
        portfolio_id=ledger_view.portfolio_id,
        mode=ledger_view.mode,
        as_of_recorded_at=cutoff_2,
        ledger_view=ledger_view,
        events=(fee_tx,),
    )
    assert proj.as_of_recorded_at == cutoff_1


def test_exact_datetime_representation_different_microsecond_rejected() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE)

    cutoff_1 = datetime(2026, 6, 2, 0, 0, 0, 1, tzinfo=timezone.utc)
    cutoff_2 = datetime(2026, 6, 2, 0, 0, 0, 2, tzinfo=timezone.utc)

    ledger_view = build_ledger_projection_view(portfolio, [fee_tx], as_of_recorded_at=cutoff_1)

    with pytest.raises(FeeTaxProjectionError, match="as_of_recorded_at .* does not match"):
        ObservedFeeTaxProjection(
            portfolio_id=ledger_view.portfolio_id,
            mode=ledger_view.mode,
            as_of_recorded_at=cutoff_2,
            ledger_view=ledger_view,
            events=(fee_tx,),
        )


def test_exact_datetime_representation_different_fold_rejected() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE)

    cutoff_fold_0 = datetime(2026, 6, 2, 0, 0, 0, fold=0, tzinfo=timezone.utc)
    cutoff_fold_1 = datetime(2026, 6, 2, 0, 0, 0, fold=1, tzinfo=timezone.utc)

    ledger_view = build_ledger_projection_view(portfolio, [fee_tx], as_of_recorded_at=cutoff_fold_0)

    with pytest.raises(FeeTaxProjectionError, match="as_of_recorded_at .* does not match"):
        ObservedFeeTaxProjection(
            portfolio_id=ledger_view.portfolio_id,
            mode=ledger_view.mode,
            as_of_recorded_at=cutoff_fold_1,
            ledger_view=ledger_view,
            events=(fee_tx,),
        )


def test_exact_datetime_representation_none_semantics() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE)

    # Both None (Current non-PIT view)
    ledger_view_none = build_ledger_projection_view(portfolio, [fee_tx], as_of_recorded_at=None)
    proj_none = ObservedFeeTaxProjection(
        portfolio_id=ledger_view_none.portfolio_id,
        mode=ledger_view_none.mode,
        as_of_recorded_at=None,
        ledger_view=ledger_view_none,
        events=(fee_tx,),
    )
    assert proj_none.as_of_recorded_at is None

    # View has None, projection has aware datetime -> reject
    aware_dt = datetime(2026, 6, 2, 0, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(FeeTaxProjectionError, match="as_of_recorded_at .* does not match"):
        ObservedFeeTaxProjection(
            portfolio_id=ledger_view_none.portfolio_id,
            mode=ledger_view_none.mode,
            as_of_recorded_at=aware_dt,
            ledger_view=ledger_view_none,
            events=(fee_tx,),
        )

    # View has aware datetime, projection has None -> reject
    ledger_view_aware = build_ledger_projection_view(portfolio, [fee_tx], as_of_recorded_at=aware_dt)
    with pytest.raises(FeeTaxProjectionError, match="as_of_recorded_at .* does not match"):
        ObservedFeeTaxProjection(
            portfolio_id=ledger_view_aware.portfolio_id,
            mode=ledger_view_aware.mode,
            as_of_recorded_at=None,
            ledger_view=ledger_view_aware,
            events=(fee_tx,),
        )


def test_tamper_type_checks_rejected() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE)
    ledger_view = build_ledger_projection_view(portfolio, [fee_tx])

    # Bool portfolio_id
    with pytest.raises(FeeTaxProjectionError, match="portfolio_id must be a UUID"):
        ObservedFeeTaxProjection(
            portfolio_id=True,  # type: ignore[arg-type]
            mode=ledger_view.mode,
            as_of_recorded_at=ledger_view.as_of_recorded_at,
            ledger_view=ledger_view,
            events=(fee_tx,),
        )

    # Bool mode
    with pytest.raises(FeeTaxProjectionError, match="mode must be a PortfolioMode"):
        ObservedFeeTaxProjection(
            portfolio_id=ledger_view.portfolio_id,
            mode=False,  # type: ignore[arg-type]
            as_of_recorded_at=ledger_view.as_of_recorded_at,
            ledger_view=ledger_view,
            events=(fee_tx,),
        )

    # Naive datetime
    with pytest.raises(FeeTaxProjectionError, match="timezone-aware"):
        ObservedFeeTaxProjection(
            portfolio_id=ledger_view.portfolio_id,
            mode=ledger_view.mode,
            as_of_recorded_at=datetime(2026, 6, 1, 0, 0, 0),  # naive
            ledger_view=ledger_view,
            events=(fee_tx,),
        )

    # Non-tuple events
    with pytest.raises(FeeTaxProjectionError, match="events must be a tuple"):
        ObservedFeeTaxProjection(
            portfolio_id=ledger_view.portfolio_id,
            mode=ledger_view.mode,
            as_of_recorded_at=ledger_view.as_of_recorded_at,
            ledger_view=ledger_view,
            events=[fee_tx],  # type: ignore[arg-type]
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Reversal & Point-In-Time Semantics Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_pit_reversal_fee_event() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()

    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone.utc)

    fee_tx = _make_tx(
        portfolio.id,
        account_id,
        TransactionType.FEE,
        cash_amount=Decimal("15.50"),
        recorded_at=t1,
    )
    rev_tx = _make_tx(
        portfolio.id,
        account_id,
        TransactionType.REVERSAL,
        recorded_at=t2,
        reverses_transaction_id=fee_tx.id,
    )

    all_txs = [fee_tx, rev_tx]

    # PIT 1: Before reversal recorded_at (e.g. t1)
    view_at_t1 = build_ledger_projection_view(portfolio, all_txs, as_of_recorded_at=t1)
    proj_at_t1 = build_observed_fee_tax_projection(view_at_t1)
    assert proj_at_t1.event_count == 1
    assert proj_at_t1.events == (fee_tx,)
    assert proj_at_t1.fee_events == (fee_tx,)

    # PIT 2: At/after reversal recorded_at (e.g. t2)
    view_at_t2 = build_ledger_projection_view(portfolio, all_txs, as_of_recorded_at=t2)
    proj_at_t2 = build_observed_fee_tax_projection(view_at_t2)
    assert proj_at_t2.event_count == 0
    assert proj_at_t2.events == ()
    assert proj_at_t2.fee_events == ()

    # Current (no cutoff)
    view_current = build_ledger_projection_view(portfolio, all_txs)
    proj_current = build_observed_fee_tax_projection(view_current)
    assert proj_current.event_count == 0
    assert proj_current.events == ()


def test_pit_reversal_tax_withholding_event() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()

    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone.utc)

    tax_tx = _make_tx(
        portfolio.id,
        account_id,
        TransactionType.TAX_WITHHOLDING,
        cash_amount=Decimal("30.00"),
        recorded_at=t1,
    )
    rev_tx = _make_tx(
        portfolio.id,
        account_id,
        TransactionType.REVERSAL,
        recorded_at=t2,
        reverses_transaction_id=tax_tx.id,
    )

    all_txs = [tax_tx, rev_tx]

    # Before reversal
    view_at_t1 = build_ledger_projection_view(portfolio, all_txs, as_of_recorded_at=t1)
    proj_at_t1 = build_observed_fee_tax_projection(view_at_t1)
    assert proj_at_t1.event_count == 1
    assert proj_at_t1.events == (tax_tx,)
    assert proj_at_t1.tax_withholding_events == (tax_tx,)

    # On/after reversal
    view_at_t2 = build_ledger_projection_view(portfolio, all_txs, as_of_recorded_at=t2)
    proj_at_t2 = build_observed_fee_tax_projection(view_at_t2)
    assert proj_at_t2.event_count == 0
    assert proj_at_t2.events == ()
    assert proj_at_t2.tax_withholding_events == ()


# ─────────────────────────────────────────────────────────────────────────────
# 5. Builder Input Validation
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("invalid_input", [
    None,
    [],
    (),
    {},
    "string",
    123,
    True,
    False,
])
def test_builder_rejects_invalid_input_types(invalid_input: object) -> None:
    with pytest.raises(TypeError, match="view must be an instance of LedgerProjectionView"):
        build_observed_fee_tax_projection(invalid_input)  # type: ignore[arg-type]


def test_builder_rejects_accounting_snapshot_type() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    dep_tx = _make_tx(portfolio.id, account_id, TransactionType.CASH_DEPOSIT, effective_date=date(2026, 6, 1), cash_amount=Decimal("100.00"))
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, effective_date=date(2026, 6, 2), cash_amount=Decimal("10.00"))
    ledger_view = build_ledger_projection_view(portfolio, [dep_tx, fee_tx])
    snapshot = build_portfolio_accounting_snapshot(ledger_view)

    with pytest.raises(TypeError, match="view must be an instance of LedgerProjectionView"):
        build_observed_fee_tax_projection(snapshot)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────────
# 6. Static AST and Code Purity Verification
# ─────────────────────────────────────────────────────────────────────────────

def test_purity_no_float_in_fee_tax_module() -> None:
    src = inspect.getsource(fee_tax_module)
    tree = ast.parse(src)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "float":
            pytest.fail("fee_tax module must contain zero float() references")
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            pytest.fail(f"fee_tax module must contain zero float literals, found: {node.value}")


def test_purity_no_monetary_aggregation_arithmetic() -> None:
    src = inspect.getsource(fee_tax_module)
    tree = ast.parse(src)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "sum":
                pytest.fail("fee_tax module must not use sum() for monetary aggregation")
        if isinstance(node, ast.Name):
            assert "effective_tax_rate" not in node.id
            assert "total_fees" not in node.id
            assert "total_taxes" not in node.id
            assert "total_cost" not in node.id


def test_purity_no_system_clock() -> None:
    src = inspect.getsource(fee_tax_module)
    tree = ast.parse(src)

    # Inspect all import and call nodes
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "time"
        if isinstance(node, ast.ImportFrom):
            if node.module == "datetime":
                for alias in node.names:
                    assert alias.name not in ("now", "utcnow")
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("now", "utcnow", "today")


def test_purity_no_uuid_generation() -> None:
    src = inspect.getsource(fee_tax_module)
    tree = ast.parse(src)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "uuid":
                for alias in node.names:
                    assert alias.name not in ("uuid4", "uuid5", "uuid1")
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("uuid4", "uuid5", "uuid1")
        if isinstance(node, ast.Name):
            assert node.id not in ("uuid4", "uuid5", "uuid1")


def test_purity_no_hashlib() -> None:
    src = inspect.getsource(fee_tax_module)
    tree = ast.parse(src)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "hashlib"
        if isinstance(node, ast.ImportFrom):
            assert node.module != "hashlib"
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("sha256", "md5", "hexdigest")
        if isinstance(node, ast.Name):
            assert node.id not in ("hashlib", "sha256", "md5")


def test_purity_no_db_transport() -> None:
    src = inspect.getsource(fee_tax_module)
    tree = ast.parse(src)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "supabase" not in alias.name.lower()
                assert "postgrest" not in alias.name.lower()
        if isinstance(node, ast.ImportFrom):
            if node.module:
                assert "supabase" not in node.module.lower()
                assert "postgrest" not in node.module.lower()
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("rpc", "table", "execute")


def test_purity_no_transaction_creation() -> None:
    src = inspect.getsource(fee_tax_module)
    tree = ast.parse(src)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "PortfolioTransaction":
                pytest.fail("fee_tax module must not construct new PortfolioTransaction instances")


def test_purity_no_round_or_quantize() -> None:
    src = inspect.getsource(fee_tax_module)
    tree = ast.parse(src)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "round":
                pytest.fail("fee_tax module must not use round()")
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("quantize", "fsum")


def test_purity_no_tax_law_rules() -> None:
    src = inspect.getsource(fee_tax_module)
    tree = ast.parse(src)

    tax_law_terms = {
        "tax_due", "tax_refund", "tax_credit", "estimated_tax", "remaining_tax",
        "tax_bracket", "tax_rate", "withholding_rate", "bist_fee", "sec_fee",
        "turkey", "turkish", "tefas", "eurobond", "stopaj"
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id.lower() not in tax_law_terms
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for term in tax_law_terms:
                assert term not in node.value.lower(), f"Found tax law term '{term}' in docstring/constant: {node.value}"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Red-Team Scenarios
# ─────────────────────────────────────────────────────────────────────────────

def test_red_team_cannot_count_dividend_or_interest_as_tax() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()

    div_tx = _make_tx(portfolio.id, account_id, TransactionType.DIVIDEND, cash_amount=Decimal("100.00"))
    int_tx = _make_tx(portfolio.id, account_id, TransactionType.INTEREST, cash_amount=Decimal("20.00"))

    ledger_view = build_ledger_projection_view(portfolio, [div_tx, int_tx])
    proj = build_observed_fee_tax_projection(ledger_view)

    assert proj.events == ()
    assert proj.fee_count == 0
    assert proj.tax_withholding_count == 0


def test_red_team_cannot_infer_fee_from_buy_or_sell() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()

    buy_tx = _make_tx(portfolio.id, account_id, TransactionType.BUY, quantity=Decimal("100"), unit_price=Decimal("50"))
    sell_tx = _make_tx(portfolio.id, account_id, TransactionType.SELL, quantity=Decimal("50"), unit_price=Decimal("60"))

    ledger_view = build_ledger_projection_view(portfolio, [buy_tx, sell_tx])
    proj = build_observed_fee_tax_projection(ledger_view)

    assert proj.events == ()
    assert proj.fee_count == 0
    assert proj.tax_withholding_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# 8. Phase 14B Aggregation Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_aggregation_empty() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    dep_tx = _make_tx(portfolio.id, account_id, TransactionType.CASH_DEPOSIT, cash_amount=Decimal("1000.00"))
    ledger_view = build_ledger_projection_view(portfolio, [dep_tx])
    proj = build_observed_fee_tax_projection(ledger_view)

    agg = build_observed_fee_tax_aggregation(proj)
    assert agg.portfolio_id == portfolio.id
    assert agg.mode == portfolio.mode
    assert agg.as_of_recorded_at is None
    assert agg.observed_projection is proj
    assert agg.states == ()
    assert agg.state_count == 0
    assert agg.account_ids == ()
    assert agg.fee_bearing_states == ()
    assert agg.tax_withholding_bearing_states == ()


def test_aggregation_one_fee() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("12.50"), cash_currency=Currency.USD)
    ledger_view = build_ledger_projection_view(portfolio, [fee_tx])
    proj = build_observed_fee_tax_projection(ledger_view)

    agg = build_observed_fee_tax_aggregation(proj)
    assert agg.state_count == 1
    assert agg.account_ids == (account_id,)
    state = agg.states[0]
    assert state.portfolio_id == portfolio.id
    assert state.account_id == account_id
    assert state.currency == Currency.USD
    assert state.fee_amount == Decimal("12.50")
    assert state.tax_withholding_amount == Decimal("0")
    assert state.fee_event_count == 1
    assert state.tax_withholding_event_count == 0
    assert state.total_observed_charge == Decimal("12.50")
    assert agg.fee_bearing_states == (state,)
    assert agg.tax_withholding_bearing_states == ()


def test_aggregation_one_tax_withholding() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    tax_tx = _make_tx(portfolio.id, account_id, TransactionType.TAX_WITHHOLDING, cash_amount=Decimal("15.75"), cash_currency=Currency.TRY)
    ledger_view = build_ledger_projection_view(portfolio, [tax_tx])
    proj = build_observed_fee_tax_projection(ledger_view)

    agg = build_observed_fee_tax_aggregation(proj)
    assert agg.state_count == 1
    assert agg.account_ids == (account_id,)
    state = agg.states[0]
    assert state.portfolio_id == portfolio.id
    assert state.account_id == account_id
    assert state.currency == Currency.TRY
    assert state.fee_amount == Decimal("0")
    assert state.tax_withholding_amount == Decimal("15.75")
    assert state.fee_event_count == 0
    assert state.tax_withholding_event_count == 1
    assert state.total_observed_charge == Decimal("15.75")
    assert agg.fee_bearing_states == ()
    assert agg.tax_withholding_bearing_states == (state,)


def test_aggregation_mixed_fee_and_tax_same_account_currency() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 11, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    fee1 = _make_tx(portfolio.id, account_id, TransactionType.FEE, recorded_at=t1, cash_amount=Decimal("1.20"), cash_currency=Currency.USD)
    tax1 = _make_tx(portfolio.id, account_id, TransactionType.TAX_WITHHOLDING, recorded_at=t2, cash_amount=Decimal("3.400"), cash_currency=Currency.USD)
    fee2 = _make_tx(portfolio.id, account_id, TransactionType.FEE, recorded_at=t3, cash_amount=Decimal("2.300"), cash_currency=Currency.USD)

    ledger_view = build_ledger_projection_view(portfolio, [fee1, tax1, fee2])
    proj = build_observed_fee_tax_projection(ledger_view)
    agg = build_observed_fee_tax_aggregation(proj)

    assert agg.state_count == 1
    state = agg.states[0]
    assert state.fee_event_count == 2
    assert state.tax_withholding_event_count == 1
    assert state.fee_amount == Decimal("3.500")
    assert state.fee_amount.as_tuple() == Decimal("3.500").as_tuple()
    assert state.tax_withholding_amount == Decimal("3.400")
    assert state.tax_withholding_amount.as_tuple() == Decimal("3.400").as_tuple()
    assert state.total_observed_charge == Decimal("6.900")
    assert state.total_observed_charge.as_tuple() == Decimal("6.900").as_tuple()
    assert agg.fee_bearing_states == (state,)
    assert agg.tax_withholding_bearing_states == (state,)


def test_aggregation_same_account_different_currency_separation() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 11, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    fee_usd = _make_tx(portfolio.id, account_id, TransactionType.FEE, recorded_at=t1, cash_amount=Decimal("1.00"), cash_currency=Currency.USD)
    fee_try = _make_tx(portfolio.id, account_id, TransactionType.FEE, recorded_at=t2, cash_amount=Decimal("10.00"), cash_currency=Currency.TRY)
    tax_usd = _make_tx(portfolio.id, account_id, TransactionType.TAX_WITHHOLDING, recorded_at=t3, cash_amount=Decimal("2.00"), cash_currency=Currency.USD)

    ledger_view = build_ledger_projection_view(portfolio, [fee_usd, fee_try, tax_usd])
    proj = build_observed_fee_tax_projection(ledger_view)
    agg = build_observed_fee_tax_aggregation(proj)

    assert agg.state_count == 2
    assert agg.account_ids == (account_id,)

    state_usd = agg.states[0]
    assert state_usd.currency == Currency.USD
    assert state_usd.fee_amount == Decimal("1.00")
    assert state_usd.tax_withholding_amount == Decimal("2.00")
    assert state_usd.fee_event_count == 1
    assert state_usd.tax_withholding_event_count == 1

    state_try = agg.states[1]
    assert state_try.currency == Currency.TRY
    assert state_try.fee_amount == Decimal("10.00")
    assert state_try.tax_withholding_amount == Decimal("0")
    assert state_try.fee_event_count == 1
    assert state_try.tax_withholding_event_count == 0


def test_aggregation_different_account_same_currency_separation() -> None:
    portfolio = _make_portfolio()
    account_a = uuid4()
    account_b = uuid4()
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 11, 0, 0, tzinfo=timezone.utc)

    fee_a = _make_tx(portfolio.id, account_a, TransactionType.FEE, recorded_at=t1, cash_amount=Decimal("1.00"), cash_currency=Currency.USD)
    fee_b = _make_tx(portfolio.id, account_b, TransactionType.FEE, recorded_at=t2, cash_amount=Decimal("2.00"), cash_currency=Currency.USD)

    ledger_view = build_ledger_projection_view(portfolio, [fee_a, fee_b])
    proj = build_observed_fee_tax_projection(ledger_view)
    agg = build_observed_fee_tax_aggregation(proj)

    assert agg.state_count == 2
    assert agg.account_ids == (account_a, account_b)
    assert agg.states[0].account_id == account_a
    assert agg.states[0].fee_amount == Decimal("1.00")
    assert agg.states[1].account_id == account_b
    assert agg.states[1].fee_amount == Decimal("2.00")


def test_aggregation_first_seen_state_ordering() -> None:
    portfolio = _make_portfolio()
    account_a = uuid4()
    account_b = uuid4()

    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 11, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    t4 = datetime(2026, 6, 1, 13, 0, 0, tzinfo=timezone.utc)

    # Sequence: (B, USD), (A, TRY), (B, USD), (A, EUR)
    tx1 = _make_tx(portfolio.id, account_b, TransactionType.FEE, recorded_at=t1, cash_amount=Decimal("5.00"), cash_currency=Currency.USD)
    tx2 = _make_tx(portfolio.id, account_a, TransactionType.FEE, recorded_at=t2, cash_amount=Decimal("10.00"), cash_currency=Currency.TRY)
    tx3 = _make_tx(portfolio.id, account_b, TransactionType.TAX_WITHHOLDING, recorded_at=t3, cash_amount=Decimal("1.50"), cash_currency=Currency.USD)
    tx4 = _make_tx(portfolio.id, account_a, TransactionType.FEE, recorded_at=t4, cash_amount=Decimal("20.00"), cash_currency=Currency.EUR)

    ledger_view = build_ledger_projection_view(portfolio, [tx1, tx2, tx3, tx4])
    proj = build_observed_fee_tax_projection(ledger_view)
    agg = build_observed_fee_tax_aggregation(proj)

    assert agg.state_count == 3
    assert agg.states[0].account_id == account_b and agg.states[0].currency == Currency.USD
    assert agg.states[1].account_id == account_a and agg.states[1].currency == Currency.TRY
    assert agg.states[2].account_id == account_a and agg.states[2].currency == Currency.EUR
    assert agg.account_ids == (account_b, account_a)


def test_aggregation_instrument_linked_and_account_level_coexistence() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    inst_id = uuid4()

    fee_acc = _make_tx(portfolio.id, account_id, TransactionType.FEE, instrument_id=None, cash_amount=Decimal("10.00"), cash_currency=Currency.USD)
    fee_inst = _make_tx(portfolio.id, account_id, TransactionType.FEE, instrument_id=inst_id, cash_amount=Decimal("5.00"), cash_currency=Currency.USD)

    ledger_view = build_ledger_projection_view(portfolio, [fee_acc, fee_inst])
    proj = build_observed_fee_tax_projection(ledger_view)
    agg = build_observed_fee_tax_aggregation(proj)

    assert agg.state_count == 1
    state = agg.states[0]
    assert state.fee_event_count == 2
    assert state.fee_amount == Decimal("15.00")


def test_aggregation_exact_decimal_representation_preservation() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()

    fee1 = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("1.20"), cash_currency=Currency.USD)
    fee2 = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("2.300"), cash_currency=Currency.USD)

    ledger_view = build_ledger_projection_view(portfolio, [fee1, fee2])
    proj = build_observed_fee_tax_projection(ledger_view)
    agg = build_observed_fee_tax_aggregation(proj)

    state = agg.states[0]
    assert state.fee_amount == Decimal("3.500")
    assert state.fee_amount.as_tuple() == (0, (3, 5, 0, 0), -3)


def test_aggregation_ambient_decimal_precision_independence() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()

    fee1 = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("1234567890.123456789"), cash_currency=Currency.USD)
    fee2 = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("9876543210.987654321"), cash_currency=Currency.USD)

    ledger_view = build_ledger_projection_view(portfolio, [fee1, fee2])
    proj = build_observed_fee_tax_projection(ledger_view)

    original_prec = decimal.getcontext().prec
    try:
        # Lower ambient precision drastically to 2 digits
        decimal.getcontext().prec = 2

        agg = build_observed_fee_tax_aggregation(proj)
        state = agg.states[0]
        expected_fee = Decimal("11111111101.111111110")
        assert state.fee_amount == expected_fee
        assert state.fee_amount.as_tuple() == expected_fee.as_tuple()
        assert state.total_observed_charge == expected_fee
    finally:
        decimal.getcontext().prec = original_prec


def test_aggregation_pit_fee_reversal_inheritance() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone.utc)

    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, recorded_at=t1, cash_amount=Decimal("50.00"), cash_currency=Currency.USD)
    rev_tx = _make_tx(portfolio.id, account_id, TransactionType.REVERSAL, recorded_at=t2, reverses_transaction_id=fee_tx.id)

    all_txs = [fee_tx, rev_tx]

    # PIT before reversal
    view_t1 = build_ledger_projection_view(portfolio, all_txs, as_of_recorded_at=t1)
    proj_t1 = build_observed_fee_tax_projection(view_t1)
    agg_t1 = build_observed_fee_tax_aggregation(proj_t1)
    assert agg_t1.state_count == 1
    assert agg_t1.states[0].fee_amount == Decimal("50.00")

    # PIT on/after reversal
    view_t2 = build_ledger_projection_view(portfolio, all_txs, as_of_recorded_at=t2)
    proj_t2 = build_observed_fee_tax_projection(view_t2)
    agg_t2 = build_observed_fee_tax_aggregation(proj_t2)
    assert agg_t2.state_count == 0
    assert agg_t2.states == ()


def test_aggregation_pit_tax_reversal_inheritance() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone.utc)

    tax_tx = _make_tx(portfolio.id, account_id, TransactionType.TAX_WITHHOLDING, recorded_at=t1, cash_amount=Decimal("25.00"), cash_currency=Currency.TRY)
    rev_tx = _make_tx(portfolio.id, account_id, TransactionType.REVERSAL, recorded_at=t2, reverses_transaction_id=tax_tx.id)

    all_txs = [tax_tx, rev_tx]

    # PIT before reversal
    view_t1 = build_ledger_projection_view(portfolio, all_txs, as_of_recorded_at=t1)
    proj_t1 = build_observed_fee_tax_projection(view_t1)
    agg_t1 = build_observed_fee_tax_aggregation(proj_t1)
    assert agg_t1.state_count == 1
    assert agg_t1.states[0].tax_withholding_amount == Decimal("25.00")

    # PIT on/after reversal
    view_t2 = build_ledger_projection_view(portfolio, all_txs, as_of_recorded_at=t2)
    proj_t2 = build_observed_fee_tax_projection(view_t2)
    agg_t2 = build_observed_fee_tax_aggregation(proj_t2)
    assert agg_t2.state_count == 0
    assert agg_t2.states == ()


def test_aggregation_exact_pit_metadata_representation_binding() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("10.00"), cash_currency=Currency.USD)

    utc_cutoff = datetime(2026, 6, 2, 0, 0, 0, tzinfo=timezone.utc)
    plus_three_cutoff = datetime(2026, 6, 2, 3, 0, 0, tzinfo=timezone(timedelta(hours=3)))

    ledger_view = build_ledger_projection_view(portfolio, [fee_tx], as_of_recorded_at=utc_cutoff)
    proj = build_observed_fee_tax_projection(ledger_view)

    # Valid construction via builder
    agg = build_observed_fee_tax_aggregation(proj)
    assert agg.as_of_recorded_at == utc_cutoff

    # Direct constructor tampering with different offset representation of same instant
    with pytest.raises(FeeTaxProjectionError, match="as_of_recorded_at .* does not match"):
        ObservedFeeTaxAggregation(
            portfolio_id=proj.portfolio_id,
            mode=proj.mode,
            as_of_recorded_at=plus_three_cutoff,
            observed_projection=proj,
            states=agg.states,
        )


def test_aggregation_tamper_omitted_state() -> None:
    portfolio = _make_portfolio()
    account_a = uuid4()
    account_b = uuid4()
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 11, 0, 0, tzinfo=timezone.utc)

    fee_a = _make_tx(portfolio.id, account_a, TransactionType.FEE, recorded_at=t1, cash_amount=Decimal("1.00"), cash_currency=Currency.USD)
    fee_b = _make_tx(portfolio.id, account_b, TransactionType.FEE, recorded_at=t2, cash_amount=Decimal("2.00"), cash_currency=Currency.USD)

    ledger_view = build_ledger_projection_view(portfolio, [fee_a, fee_b])
    proj = build_observed_fee_tax_projection(ledger_view)
    agg = build_observed_fee_tax_aggregation(proj)
    assert agg.state_count == 2

    # Supply only 1 state
    with pytest.raises(FeeTaxProjectionError, match="states count .* does not match"):
        ObservedFeeTaxAggregation(
            portfolio_id=proj.portfolio_id,
            mode=proj.mode,
            as_of_recorded_at=proj.as_of_recorded_at,
            observed_projection=proj,
            states=(agg.states[0],),
        )


def test_aggregation_tamper_extra_state() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("1.00"), cash_currency=Currency.USD)

    ledger_view = build_ledger_projection_view(portfolio, [fee_tx])
    proj = build_observed_fee_tax_projection(ledger_view)
    agg = build_observed_fee_tax_aggregation(proj)

    extra_state = ObservedFeeTaxAggregateState(
        portfolio_id=portfolio.id,
        account_id=uuid4(),
        currency=Currency.EUR,
        fee_amount=Decimal("5.00"),
        tax_withholding_amount=Decimal("0"),
        fee_event_count=1,
        tax_withholding_event_count=0,
    )

    with pytest.raises(FeeTaxProjectionError, match="states count .* does not match"):
        ObservedFeeTaxAggregation(
            portfolio_id=proj.portfolio_id,
            mode=proj.mode,
            as_of_recorded_at=proj.as_of_recorded_at,
            observed_projection=proj,
            states=(agg.states[0], extra_state),
        )


def test_aggregation_tamper_reordered_states() -> None:
    portfolio = _make_portfolio()
    account_a = uuid4()
    account_b = uuid4()
    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 11, 0, 0, tzinfo=timezone.utc)

    fee_a = _make_tx(portfolio.id, account_a, TransactionType.FEE, recorded_at=t1, cash_amount=Decimal("1.00"), cash_currency=Currency.USD)
    fee_b = _make_tx(portfolio.id, account_b, TransactionType.FEE, recorded_at=t2, cash_amount=Decimal("2.00"), cash_currency=Currency.USD)

    ledger_view = build_ledger_projection_view(portfolio, [fee_a, fee_b])
    proj = build_observed_fee_tax_projection(ledger_view)
    agg = build_observed_fee_tax_aggregation(proj)

    # Reorder (B then A instead of A then B)
    with pytest.raises(FeeTaxProjectionError, match="account_id .* does not match"):
        ObservedFeeTaxAggregation(
            portfolio_id=proj.portfolio_id,
            mode=proj.mode,
            as_of_recorded_at=proj.as_of_recorded_at,
            observed_projection=proj,
            states=(agg.states[1], agg.states[0]),
        )


def test_aggregation_tamper_wrong_account() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("1.00"), cash_currency=Currency.USD)

    ledger_view = build_ledger_projection_view(portfolio, [fee_tx])
    proj = build_observed_fee_tax_projection(ledger_view)
    agg = build_observed_fee_tax_aggregation(proj)

    tampered_state = ObservedFeeTaxAggregateState(
        portfolio_id=portfolio.id,
        account_id=uuid4(),  # wrong account
        currency=Currency.USD,
        fee_amount=Decimal("1.00"),
        tax_withholding_amount=Decimal("0"),
        fee_event_count=1,
        tax_withholding_event_count=0,
    )

    with pytest.raises(FeeTaxProjectionError, match="account_id .* does not match"):
        ObservedFeeTaxAggregation(
            portfolio_id=proj.portfolio_id,
            mode=proj.mode,
            as_of_recorded_at=proj.as_of_recorded_at,
            observed_projection=proj,
            states=(tampered_state,),
        )


def test_aggregation_tamper_wrong_currency() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("1.00"), cash_currency=Currency.USD)

    ledger_view = build_ledger_projection_view(portfolio, [fee_tx])
    proj = build_observed_fee_tax_projection(ledger_view)
    agg = build_observed_fee_tax_aggregation(proj)

    tampered_state = ObservedFeeTaxAggregateState(
        portfolio_id=portfolio.id,
        account_id=account_id,
        currency=Currency.EUR,  # wrong currency
        fee_amount=Decimal("1.00"),
        tax_withholding_amount=Decimal("0"),
        fee_event_count=1,
        tax_withholding_event_count=0,
    )

    with pytest.raises(FeeTaxProjectionError, match="currency .* does not match"):
        ObservedFeeTaxAggregation(
            portfolio_id=proj.portfolio_id,
            mode=proj.mode,
            as_of_recorded_at=proj.as_of_recorded_at,
            observed_projection=proj,
            states=(tampered_state,),
        )


def test_aggregation_tamper_wrong_fee_amount() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("3.500"), cash_currency=Currency.USD)

    ledger_view = build_ledger_projection_view(portfolio, [fee_tx])
    proj = build_observed_fee_tax_projection(ledger_view)
    agg = build_observed_fee_tax_aggregation(proj)

    tampered_state = ObservedFeeTaxAggregateState(
        portfolio_id=portfolio.id,
        account_id=account_id,
        currency=Currency.USD,
        fee_amount=Decimal("3.501"),  # wrong amount
        tax_withholding_amount=Decimal("0"),
        fee_event_count=1,
        tax_withholding_event_count=0,
    )

    with pytest.raises(FeeTaxProjectionError, match="fee_amount representation .* does not match"):
        ObservedFeeTaxAggregation(
            portfolio_id=proj.portfolio_id,
            mode=proj.mode,
            as_of_recorded_at=proj.as_of_recorded_at,
            observed_projection=proj,
            states=(tampered_state,),
        )


def test_aggregation_tamper_wrong_tax_amount() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    tax_tx = _make_tx(portfolio.id, account_id, TransactionType.TAX_WITHHOLDING, cash_amount=Decimal("5.00"), cash_currency=Currency.TRY)

    ledger_view = build_ledger_projection_view(portfolio, [tax_tx])
    proj = build_observed_fee_tax_projection(ledger_view)
    agg = build_observed_fee_tax_aggregation(proj)

    tampered_state = ObservedFeeTaxAggregateState(
        portfolio_id=portfolio.id,
        account_id=account_id,
        currency=Currency.TRY,
        fee_amount=Decimal("0"),
        tax_withholding_amount=Decimal("5.01"),  # wrong amount
        fee_event_count=0,
        tax_withholding_event_count=1,
    )

    with pytest.raises(FeeTaxProjectionError, match="tax_withholding_amount representation .* does not match"):
        ObservedFeeTaxAggregation(
            portfolio_id=proj.portfolio_id,
            mode=proj.mode,
            as_of_recorded_at=proj.as_of_recorded_at,
            observed_projection=proj,
            states=(tampered_state,),
        )


def test_aggregation_tamper_equal_value_different_representation() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee_tx = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("3.500"), cash_currency=Currency.USD)

    ledger_view = build_ledger_projection_view(portfolio, [fee_tx])
    proj = build_observed_fee_tax_projection(ledger_view)
    agg = build_observed_fee_tax_aggregation(proj)

    assert Decimal("3.500") == Decimal("3.5")
    assert Decimal("3.500").as_tuple() != Decimal("3.5").as_tuple()

    tampered_state = ObservedFeeTaxAggregateState(
        portfolio_id=portfolio.id,
        account_id=account_id,
        currency=Currency.USD,
        fee_amount=Decimal("3.5"),  # different scale representation
        tax_withholding_amount=Decimal("0"),
        fee_event_count=1,
        tax_withholding_event_count=0,
    )

    with pytest.raises(FeeTaxProjectionError, match="fee_amount representation .* does not match"):
        ObservedFeeTaxAggregation(
            portfolio_id=proj.portfolio_id,
            mode=proj.mode,
            as_of_recorded_at=proj.as_of_recorded_at,
            observed_projection=proj,
            states=(tampered_state,),
        )


def test_aggregation_tamper_wrong_fee_count() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    fee1 = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("1.00"), cash_currency=Currency.USD)
    fee2 = _make_tx(portfolio.id, account_id, TransactionType.FEE, cash_amount=Decimal("2.00"), cash_currency=Currency.USD)

    ledger_view = build_ledger_projection_view(portfolio, [fee1, fee2])
    proj = build_observed_fee_tax_projection(ledger_view)
    agg = build_observed_fee_tax_aggregation(proj)

    tampered_state = ObservedFeeTaxAggregateState(
        portfolio_id=portfolio.id,
        account_id=account_id,
        currency=Currency.USD,
        fee_amount=Decimal("3.00"),
        tax_withholding_amount=Decimal("0"),
        fee_event_count=1,  # wrong count (expected 2)
        tax_withholding_event_count=0,
    )

    with pytest.raises(FeeTaxProjectionError, match="fee_event_count .* does not match"):
        ObservedFeeTaxAggregation(
            portfolio_id=proj.portfolio_id,
            mode=proj.mode,
            as_of_recorded_at=proj.as_of_recorded_at,
            observed_projection=proj,
            states=(tampered_state,),
        )


def test_aggregation_tamper_wrong_tax_count() -> None:
    portfolio = _make_portfolio()
    account_id = uuid4()
    tax1 = _make_tx(portfolio.id, account_id, TransactionType.TAX_WITHHOLDING, cash_amount=Decimal("1.00"), cash_currency=Currency.TRY)
    tax2 = _make_tx(portfolio.id, account_id, TransactionType.TAX_WITHHOLDING, cash_amount=Decimal("2.00"), cash_currency=Currency.TRY)

    ledger_view = build_ledger_projection_view(portfolio, [tax1, tax2])
    proj = build_observed_fee_tax_projection(ledger_view)
    agg = build_observed_fee_tax_aggregation(proj)

    tampered_state = ObservedFeeTaxAggregateState(
        portfolio_id=portfolio.id,
        account_id=account_id,
        currency=Currency.TRY,
        fee_amount=Decimal("0"),
        tax_withholding_amount=Decimal("3.00"),
        fee_event_count=0,
        tax_withholding_event_count=1,  # wrong count (expected 2)
    )

    with pytest.raises(FeeTaxProjectionError, match="tax_withholding_event_count .* does not match"):
        ObservedFeeTaxAggregation(
            portfolio_id=proj.portfolio_id,
            mode=proj.mode,
            as_of_recorded_at=proj.as_of_recorded_at,
            observed_projection=proj,
            states=(tampered_state,),
        )


def test_aggregate_state_invalids() -> None:
    pid = uuid4()
    aid = uuid4()

    # Zero / Zero count
    with pytest.raises(FeeTaxProjectionError, match="requires at least one fee or tax withholding event"):
        ObservedFeeTaxAggregateState(
            portfolio_id=pid,
            account_id=aid,
            currency=Currency.USD,
            fee_amount=Decimal("0"),
            tax_withholding_amount=Decimal("0"),
            fee_event_count=0,
            tax_withholding_event_count=0,
        )

    # Incoherent fee: count 0, amount > 0
    with pytest.raises(FeeTaxProjectionError, match="fee_event_count is 0 but fee_amount is non-zero"):
        ObservedFeeTaxAggregateState(
            portfolio_id=pid,
            account_id=aid,
            currency=Currency.USD,
            fee_amount=Decimal("10.00"),
            tax_withholding_amount=Decimal("0"),
            fee_event_count=0,
            tax_withholding_event_count=1,
        )

    # Incoherent fee: count > 0, amount == 0
    with pytest.raises(FeeTaxProjectionError, match="fee_event_count is 1 but fee_amount is not strictly positive"):
        ObservedFeeTaxAggregateState(
            portfolio_id=pid,
            account_id=aid,
            currency=Currency.USD,
            fee_amount=Decimal("0"),
            tax_withholding_amount=Decimal("10.00"),
            fee_event_count=1,
            tax_withholding_event_count=1,
        )

    # Incoherent tax: count 0, amount > 0
    with pytest.raises(FeeTaxProjectionError, match="tax_withholding_event_count is 0 but tax_withholding_amount is non-zero"):
        ObservedFeeTaxAggregateState(
            portfolio_id=pid,
            account_id=aid,
            currency=Currency.USD,
            fee_amount=Decimal("10.00"),
            tax_withholding_amount=Decimal("5.00"),
            fee_event_count=1,
            tax_withholding_event_count=0,
        )

    # Incoherent tax: count > 0, amount == 0
    with pytest.raises(FeeTaxProjectionError, match="tax_withholding_event_count is 1 but tax_withholding_amount is not strictly positive"):
        ObservedFeeTaxAggregateState(
            portfolio_id=pid,
            account_id=aid,
            currency=Currency.USD,
            fee_amount=Decimal("10.00"),
            tax_withholding_amount=Decimal("0"),
            fee_event_count=1,
            tax_withholding_event_count=1,
        )

    # Negative fee amount
    with pytest.raises(FeeTaxProjectionError, match="fee_amount must be a finite non-negative Decimal"):
        ObservedFeeTaxAggregateState(
            portfolio_id=pid,
            account_id=aid,
            currency=Currency.USD,
            fee_amount=Decimal("-1.00"),
            tax_withholding_amount=Decimal("0"),
            fee_event_count=1,
            tax_withholding_event_count=0,
        )

    # Negative tax amount
    with pytest.raises(FeeTaxProjectionError, match="tax_withholding_amount must be a finite non-negative Decimal"):
        ObservedFeeTaxAggregateState(
            portfolio_id=pid,
            account_id=aid,
            currency=Currency.USD,
            fee_amount=Decimal("1.00"),
            tax_withholding_amount=Decimal("-5.00"),
            fee_event_count=1,
            tax_withholding_event_count=0,
        )

    # Non-finite Decimal (Infinity / NaN)
    with pytest.raises(FeeTaxProjectionError, match="fee_amount must be a finite non-negative Decimal"):
        ObservedFeeTaxAggregateState(
            portfolio_id=pid,
            account_id=aid,
            currency=Currency.USD,
            fee_amount=Decimal("Infinity"),
            tax_withholding_amount=Decimal("0"),
            fee_event_count=1,
            tax_withholding_event_count=0,
        )

    with pytest.raises(FeeTaxProjectionError, match="fee_amount must be a finite non-negative Decimal"):
        ObservedFeeTaxAggregateState(
            portfolio_id=pid,
            account_id=aid,
            currency=Currency.USD,
            fee_amount=Decimal("NaN"),
            tax_withholding_amount=Decimal("0"),
            fee_event_count=1,
            tax_withholding_event_count=0,
        )

    # Type safety: bool rejected for counts
    with pytest.raises(FeeTaxProjectionError, match="fee_event_count must be a non-negative int"):
        ObservedFeeTaxAggregateState(
            portfolio_id=pid,
            account_id=aid,
            currency=Currency.USD,
            fee_amount=Decimal("10.00"),
            tax_withholding_amount=Decimal("0"),
            fee_event_count=True,  # type: ignore[arg-type]
            tax_withholding_event_count=0,
        )


@pytest.mark.parametrize("invalid_input", [
    None,
    [],
    (),
    {},
    "string",
    123,
    True,
    False,
])
def test_aggregation_builder_rejects_invalid_types(invalid_input: object) -> None:
    with pytest.raises(TypeError, match="observed must be an instance of ObservedFeeTaxProjection"):
        build_observed_fee_tax_aggregation(invalid_input)  # type: ignore[arg-type]


def test_final_red_team_scenario() -> None:
    """
    Final Red-Team scenario (Section 66):
    Account A / USD: FEE 1.20, TAX 3.400, FEE 2.300
    Account A / TRY: FEE 10
    Account B / USD: TAX 5
    """
    portfolio = _make_portfolio()
    account_a = uuid4()
    account_b = uuid4()

    t1 = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 1, 11, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    t4 = datetime(2026, 6, 1, 13, 0, 0, tzinfo=timezone.utc)
    t5 = datetime(2026, 6, 1, 14, 0, 0, tzinfo=timezone.utc)

    tx_a_usd_fee1 = _make_tx(portfolio.id, account_a, TransactionType.FEE, recorded_at=t1, cash_amount=Decimal("1.20"), cash_currency=Currency.USD)
    tx_a_usd_tax1 = _make_tx(portfolio.id, account_a, TransactionType.TAX_WITHHOLDING, recorded_at=t2, cash_amount=Decimal("3.400"), cash_currency=Currency.USD)
    tx_a_usd_fee2 = _make_tx(portfolio.id, account_a, TransactionType.FEE, recorded_at=t3, cash_amount=Decimal("2.300"), cash_currency=Currency.USD)
    tx_a_try_fee1 = _make_tx(portfolio.id, account_a, TransactionType.FEE, recorded_at=t4, cash_amount=Decimal("10"), cash_currency=Currency.TRY)
    tx_b_usd_tax1 = _make_tx(portfolio.id, account_b, TransactionType.TAX_WITHHOLDING, recorded_at=t5, cash_amount=Decimal("5"), cash_currency=Currency.USD)

    ledger_view = build_ledger_projection_view(
        portfolio,
        [tx_a_usd_fee1, tx_a_usd_tax1, tx_a_usd_fee2, tx_a_try_fee1, tx_b_usd_tax1],
    )
    proj = build_observed_fee_tax_projection(ledger_view)
    agg = build_observed_fee_tax_aggregation(proj)

    # 1. Verify 3 states in first-seen order: A/USD, A/TRY, B/USD
    assert agg.state_count == 3
    assert agg.states[0].account_id == account_a and agg.states[0].currency == Currency.USD
    assert agg.states[1].account_id == account_a and agg.states[1].currency == Currency.TRY
    assert agg.states[2].account_id == account_b and agg.states[2].currency == Currency.USD

    # 2. Verify A/USD state
    s_a_usd = agg.states[0]
    assert s_a_usd.fee_event_count == 2
    assert s_a_usd.fee_amount == Decimal("3.500")
    assert s_a_usd.fee_amount.as_tuple() == Decimal("3.500").as_tuple()
    assert s_a_usd.tax_withholding_event_count == 1
    assert s_a_usd.tax_withholding_amount == Decimal("3.400")
    assert s_a_usd.tax_withholding_amount.as_tuple() == Decimal("3.400").as_tuple()
    assert s_a_usd.total_observed_charge == Decimal("6.900")
    assert s_a_usd.total_observed_charge.as_tuple() == Decimal("6.900").as_tuple()

    # 3. Verify A/TRY state
    s_a_try = agg.states[1]
    assert s_a_try.fee_event_count == 1
    assert s_a_try.fee_amount == Decimal("10")
    assert s_a_try.tax_withholding_event_count == 0
    assert s_a_try.tax_withholding_amount == Decimal("0")
    assert s_a_try.total_observed_charge == Decimal("10")

    # 4. Verify B/USD state
    s_b_usd = agg.states[2]
    assert s_b_usd.fee_event_count == 0
    assert s_b_usd.fee_amount == Decimal("0")
    assert s_b_usd.tax_withholding_event_count == 1
    assert s_b_usd.tax_withholding_amount == Decimal("5")
    assert s_b_usd.total_observed_charge == Decimal("5")

    # 5. Low ambient precision test
    original_prec = decimal.getcontext().prec
    try:
        decimal.getcontext().prec = 2
        agg_low_prec = build_observed_fee_tax_aggregation(proj)
        assert agg_low_prec.states[0].fee_amount == Decimal("3.500")
        assert agg_low_prec.states[0].fee_amount.as_tuple() == Decimal("3.500").as_tuple()
    finally:
        decimal.getcontext().prec = original_prec

    # 6. Representation tamper check: Decimal("3.5") vs Decimal("3.500")
    tampered_a_usd = ObservedFeeTaxAggregateState(
        portfolio_id=portfolio.id,
        account_id=account_a,
        currency=Currency.USD,
        fee_amount=Decimal("3.5"),
        tax_withholding_amount=Decimal("3.400"),
        fee_event_count=2,
        tax_withholding_event_count=1,
    )
    with pytest.raises(FeeTaxProjectionError, match="fee_amount representation .* does not match"):
        ObservedFeeTaxAggregation(
            portfolio_id=proj.portfolio_id,
            mode=proj.mode,
            as_of_recorded_at=proj.as_of_recorded_at,
            observed_projection=proj,
            states=(tampered_a_usd, s_a_try, s_b_usd),
        )

