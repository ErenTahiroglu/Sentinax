"""
backend/tests/test_portfolio_domain.py
======================================
Comprehensive Domain Model Unit Tests for Private Portfolio Ledger (Phase 12A, 12A.5 & 12A.6).

Zero external network calls (pytest-socket enforced).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import (
    CashPurpose,
    ContributionStatus,
    Currency,
    GoalPriority,
    GoalStatus,
    LotStatus,
    PortfolioMode,
    TransactionType,
)
from backend.engine.private.portfolio.models import (
    CashBucket,
    InvestmentGoal,
    PlannedContribution,
    Portfolio,
    PortfolioAccount,
    PortfolioTransaction,
    PositionLot,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Portfolio Model Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_portfolio_creation_and_serialization():
    p_id = uuid4()
    now = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)
    p = Portfolio(
        id=p_id,
        mode=PortfolioMode.MY_PORTFOLIO,
        name="Ana Portföy",
        base_currency=Currency.TRY,
        created_at=now,
    )
    assert p.id == p_id
    assert p.mode == PortfolioMode.MY_PORTFOLIO
    assert p.name == "Ana Portföy"
    assert p.base_currency == Currency.TRY
    assert p.is_active is True

    d = p.to_dict()
    assert d["id"] == str(p_id)
    assert d["mode"] == "my_portfolio"
    assert d["name"] == "Ana Portföy"
    assert d["base_currency"] == "TRY"
    assert d["created_at"] == now.isoformat()
    assert d["is_active"] is True


def test_portfolio_validation_empty_name_or_naive_time():
    now = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="name cannot be empty"):
        Portfolio(mode=PortfolioMode.MY_PORTFOLIO, name="", base_currency=Currency.TRY, created_at=now)

    naive = datetime(2026, 8, 27, 10, 0, 0)
    with pytest.raises(ValueError, match="must be timezone-aware"):
        Portfolio(mode=PortfolioMode.MY_PORTFOLIO, name="Test", base_currency=Currency.TRY, created_at=naive)


def test_portfolio_my_portfolio_cannot_have_cloning_provenance():
    now = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="MY_PORTFOLIO cannot have source_portfolio_id"):
        Portfolio(
            mode=PortfolioMode.MY_PORTFOLIO,
            name="Real",
            base_currency=Currency.TRY,
            created_at=now,
            source_portfolio_id=uuid4(),
        )

    with pytest.raises(ValueError, match="MY_PORTFOLIO cannot have source_snapshot_time"):
        Portfolio(
            mode=PortfolioMode.MY_PORTFOLIO,
            name="Real",
            base_currency=Currency.TRY,
            created_at=now,
            source_snapshot_time=now,
        )


def test_sandbox_provenance_validation():
    now = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)
    sb_id = uuid4()
    source_id = uuid4()

    # Valid sandbox with provenance
    sb = Portfolio(
        id=sb_id,
        mode=PortfolioMode.SANDBOX,
        name="Sandbox 1",
        base_currency=Currency.TRY,
        created_at=now,
        source_portfolio_id=source_id,
        source_snapshot_time=now,
    )
    assert sb.source_portfolio_id == source_id
    assert sb.source_snapshot_time == now

    # Reject source_snapshot_time without source_portfolio_id
    with pytest.raises(ValueError, match="source_snapshot_time must specify source_portfolio_id"):
        Portfolio(
            mode=PortfolioMode.SANDBOX,
            name="Sandbox Bad",
            base_currency=Currency.TRY,
            created_at=now,
            source_snapshot_time=now,
            source_portfolio_id=None,
        )

    # Reject self-cloning provenance
    with pytest.raises(ValueError, match="cannot reference self"):
        Portfolio(
            id=sb_id,
            mode=PortfolioMode.SANDBOX,
            name="Self Clone",
            base_currency=Currency.TRY,
            created_at=now,
            source_portfolio_id=sb_id,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Portfolio Account Model Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_portfolio_account_creation_and_serialization():
    p_id = uuid4()
    a_id = uuid4()
    now = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)
    acc = PortfolioAccount(
        id=a_id,
        portfolio_id=p_id,
        name="Garanti Yatırım",
        base_currency=Currency.TRY,
        broker_label="Garanti BBVA",
        created_at=now,
    )
    assert acc.id == a_id
    assert acc.portfolio_id == p_id
    assert acc.is_active is True

    d = acc.to_dict()
    assert d["id"] == str(a_id)
    assert d["portfolio_id"] == str(p_id)
    assert d["name"] == "Garanti Yatırım"
    assert d["broker_label"] == "Garanti BBVA"


def test_portfolio_account_validation():
    now = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="Account name cannot be empty"):
        PortfolioAccount(portfolio_id=uuid4(), name="  ", base_currency=Currency.TRY, created_at=now)

    naive = datetime(2026, 8, 27, 10, 0, 0)
    with pytest.raises(ValueError, match="must be timezone-aware"):
        PortfolioAccount(portfolio_id=uuid4(), name="Acc", base_currency=Currency.TRY, created_at=naive)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Transaction Model Contract Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_buy_transaction_valid():
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    rec_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    exec_time = datetime(2026, 8, 27, 11, 30, 0, tzinfo=timezone.utc)

    tx = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.BUY,
        instrument_id=inst_id,
        effective_date=date(2026, 8, 27),
        executed_at=exec_time,
        recorded_at=rec_time,
        quantity=Decimal("100"),
        unit_price=Decimal("45.50"),
        trade_currency=Currency.TRY,
    )
    assert tx.transaction_type == TransactionType.BUY
    assert tx.quantity == Decimal("100")
    assert tx.unit_price == Decimal("45.50")
    assert tx.trade_currency == Currency.TRY

    fp = tx.economic_fingerprint()
    assert len(fp) == 64


def test_buy_transaction_invalids():
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    rec_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # Missing instrument
    with pytest.raises(ValueError, match="BUY requires instrument_id"):
        PortfolioTransaction(
            portfolio_id=p_id, account_id=a_id, transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 27), recorded_at=rec_time,
            quantity=Decimal("10"), unit_price=Decimal("10"), trade_currency=Currency.TRY,
        )

    # Zero quantity
    with pytest.raises(ValueError, match="strictly positive"):
        PortfolioTransaction(
            portfolio_id=p_id, account_id=a_id, transaction_type=TransactionType.BUY,
            instrument_id=inst_id, effective_date=date(2026, 8, 27), recorded_at=rec_time,
            quantity=Decimal("0"), unit_price=Decimal("10"), trade_currency=Currency.TRY,
        )

    # Negative quantity
    with pytest.raises(ValueError, match="strictly positive"):
        PortfolioTransaction(
            portfolio_id=p_id, account_id=a_id, transaction_type=TransactionType.BUY,
            instrument_id=inst_id, effective_date=date(2026, 8, 27), recorded_at=rec_time,
            quantity=Decimal("-10"), unit_price=Decimal("10"), trade_currency=Currency.TRY,
        )

    # Float quantity (strict Decimal)
    with pytest.raises(TypeError, match="must be a Decimal"):
        PortfolioTransaction(
            portfolio_id=p_id, account_id=a_id, transaction_type=TransactionType.BUY,
            instrument_id=inst_id, effective_date=date(2026, 8, 27), recorded_at=rec_time,
            quantity=10.5,  # type: ignore
            unit_price=Decimal("10"), trade_currency=Currency.TRY,
        )

    # Float unit_price
    with pytest.raises(TypeError, match="must be a Decimal"):
        PortfolioTransaction(
            portfolio_id=p_id, account_id=a_id, transaction_type=TransactionType.BUY,
            instrument_id=inst_id, effective_date=date(2026, 8, 27), recorded_at=rec_time,
            quantity=Decimal("10"), unit_price=10.5, trade_currency=Currency.TRY,  # type: ignore
        )

    # Contradictory FX fields in BUY
    with pytest.raises(ValueError, match="must not contain FX"):
        PortfolioTransaction(
            portfolio_id=p_id, account_id=a_id, transaction_type=TransactionType.BUY,
            instrument_id=inst_id, effective_date=date(2026, 8, 27), recorded_at=rec_time,
            quantity=Decimal("10"), unit_price=Decimal("10"), trade_currency=Currency.TRY,
            from_currency=Currency.USD, from_amount=Decimal("100"),
        )

    # Contradictory cash fields in BUY
    with pytest.raises(ValueError, match="must not contain cash_amount or cash_currency"):
        PortfolioTransaction(
            portfolio_id=p_id, account_id=a_id, transaction_type=TransactionType.BUY,
            instrument_id=inst_id, effective_date=date(2026, 8, 27), recorded_at=rec_time,
            quantity=Decimal("10"), unit_price=Decimal("10"), trade_currency=Currency.TRY,
            cash_amount=Decimal("100"),
        )


def test_sell_transaction_valid_positive_quantity():
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    rec_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # Positive semantic quantity
    tx = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.SELL,
        instrument_id=inst_id,
        effective_date=date(2026, 8, 27),
        recorded_at=rec_time,
        quantity=Decimal("50"),
        unit_price=Decimal("120.00"),
        trade_currency=Currency.TRY,
    )
    assert tx.quantity == Decimal("50")

    # Negative quantity in SELL must be rejected
    with pytest.raises(ValueError, match="strictly positive"):
        PortfolioTransaction(
            portfolio_id=p_id, account_id=a_id, transaction_type=TransactionType.SELL,
            instrument_id=inst_id, effective_date=date(2026, 8, 27), recorded_at=rec_time,
            quantity=Decimal("-50"), unit_price=Decimal("120.00"), trade_currency=Currency.TRY,
        )

    # Contradictory cash fields in SELL
    with pytest.raises(ValueError, match="must not contain cash_amount or cash_currency"):
        PortfolioTransaction(
            portfolio_id=p_id, account_id=a_id, transaction_type=TransactionType.SELL,
            instrument_id=inst_id, effective_date=date(2026, 8, 27), recorded_at=rec_time,
            quantity=Decimal("50"), unit_price=Decimal("120.00"), trade_currency=Currency.TRY,
            cash_amount=Decimal("6000"),
        )


def test_cash_deposit_and_withdrawal():
    p_id = uuid4()
    a_id = uuid4()
    rec_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # Valid Deposit
    dep = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.CASH_DEPOSIT,
        effective_date=date(2026, 8, 27),
        recorded_at=rec_time,
        cash_amount=Decimal("50000.00"),
        cash_currency=Currency.TRY,
    )
    assert dep.cash_amount == Decimal("50000.00")

    # Valid Withdrawal
    withd = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.CASH_WITHDRAWAL,
        effective_date=date(2026, 8, 27),
        recorded_at=rec_time,
        cash_amount=Decimal("10000.00"),
        cash_currency=Currency.TRY,
    )
    assert withd.cash_amount == Decimal("10000.00")

    # Reject deposit with negative amount
    with pytest.raises(ValueError, match="strictly positive"):
        PortfolioTransaction(
            portfolio_id=p_id, account_id=a_id, transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 27), recorded_at=rec_time,
            cash_amount=Decimal("-100"), cash_currency=Currency.TRY,
        )

    # Reject deposit with security quantity
    with pytest.raises(ValueError, match="must not contain trade security fields"):
        PortfolioTransaction(
            portfolio_id=p_id, account_id=a_id, transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 27), recorded_at=rec_time,
            cash_amount=Decimal("1000"), cash_currency=Currency.TRY,
            quantity=Decimal("50"),
        )

    # Reject deposit with instrument_id
    with pytest.raises(ValueError, match="must not contain instrument_id"):
        PortfolioTransaction(
            portfolio_id=p_id, account_id=a_id, transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 27), recorded_at=rec_time,
            cash_amount=Decimal("1000"), cash_currency=Currency.TRY,
            instrument_id=uuid4(),
        )

    # Reject withdrawal with instrument_id
    with pytest.raises(ValueError, match="must not contain instrument_id"):
        PortfolioTransaction(
            portfolio_id=p_id, account_id=a_id, transaction_type=TransactionType.CASH_WITHDRAWAL,
            effective_date=date(2026, 8, 27), recorded_at=rec_time,
            cash_amount=Decimal("1000"), cash_currency=Currency.TRY,
            instrument_id=uuid4(),
        )


def test_cash_flow_rejects_trade_fields():
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    rec_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    for tt in (TransactionType.DIVIDEND, TransactionType.INTEREST, TransactionType.FEE, TransactionType.TAX_WITHHOLDING):
        # Valid cash-flow event with optional instrument_id
        valid_tx = PortfolioTransaction(
            portfolio_id=p_id, account_id=a_id, transaction_type=tt,
            effective_date=date(2026, 8, 27), recorded_at=rec_time,
            cash_amount=Decimal("250.00"), cash_currency=Currency.TRY,
            instrument_id=inst_id,
        )
        assert valid_tx.cash_amount == Decimal("250.00")

        # Reject quantity
        with pytest.raises(ValueError, match="must not contain quantity, unit_price, or trade_currency"):
            PortfolioTransaction(
                portfolio_id=p_id, account_id=a_id, transaction_type=tt,
                effective_date=date(2026, 8, 27), recorded_at=rec_time,
                cash_amount=Decimal("250.00"), cash_currency=Currency.TRY,
                quantity=Decimal("10"),
            )

        # Reject unit_price
        with pytest.raises(ValueError, match="must not contain quantity, unit_price, or trade_currency"):
            PortfolioTransaction(
                portfolio_id=p_id, account_id=a_id, transaction_type=tt,
                effective_date=date(2026, 8, 27), recorded_at=rec_time,
                cash_amount=Decimal("250.00"), cash_currency=Currency.TRY,
                unit_price=Decimal("10.00"),
            )


def test_fx_conversion_contract():
    p_id = uuid4()
    a_id = uuid4()
    rec_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # Valid two-leg conversion: USD 100 -> TRY 3400
    fx = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.FX_CONVERSION,
        effective_date=date(2026, 8, 27),
        recorded_at=rec_time,
        from_currency=Currency.USD,
        from_amount=Decimal("100.00"),
        to_currency=Currency.TRY,
        to_amount=Decimal("3400.00"),
    )
    assert fx.from_amount == Decimal("100.00")
    assert fx.to_amount == Decimal("3400.00")

    # Reject same currency on both legs
    with pytest.raises(ValueError, match="distinct currencies"):
        PortfolioTransaction(
            portfolio_id=p_id, account_id=a_id, transaction_type=TransactionType.FX_CONVERSION,
            effective_date=date(2026, 8, 27), recorded_at=rec_time,
            from_currency=Currency.TRY, from_amount=Decimal("100"),
            to_currency=Currency.TRY, to_amount=Decimal("100"),
        )

    # Reject missing leg
    with pytest.raises(ValueError, match="FX_CONVERSION requires to_amount"):
        PortfolioTransaction(
            portfolio_id=p_id, account_id=a_id, transaction_type=TransactionType.FX_CONVERSION,
            effective_date=date(2026, 8, 27), recorded_at=rec_time,
            from_currency=Currency.USD, from_amount=Decimal("100"),
            to_currency=Currency.TRY,
        )

    # Reject cash_bucket_id on FX_CONVERSION
    with pytest.raises(ValueError, match="must not contain cash_bucket_id"):
        PortfolioTransaction(
            portfolio_id=p_id, account_id=a_id, transaction_type=TransactionType.FX_CONVERSION,
            effective_date=date(2026, 8, 27), recorded_at=rec_time,
            from_currency=Currency.USD, from_amount=Decimal("100"),
            to_currency=Currency.TRY, to_amount=Decimal("3400"),
            cash_bucket_id=uuid4(),
        )

    # Reject trade security fields on FX_CONVERSION
    with pytest.raises(ValueError, match="must not contain security trade fields"):
        PortfolioTransaction(
            portfolio_id=p_id, account_id=a_id, transaction_type=TransactionType.FX_CONVERSION,
            effective_date=date(2026, 8, 27), recorded_at=rec_time,
            from_currency=Currency.USD, from_amount=Decimal("100"),
            to_currency=Currency.TRY, to_amount=Decimal("3400"),
            quantity=Decimal("10"),
        )


def test_time_axes_and_late_import():
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    rec_time = datetime(2026, 8, 27, 18, 0, 0, tzinfo=timezone.utc)

    # Trade executed on August 1st, imported on August 27th
    tx = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.BUY,
        instrument_id=inst_id,
        effective_date=date(2026, 8, 1),
        recorded_at=rec_time,
        quantity=Decimal("10"),
        unit_price=Decimal("100"),
        trade_currency=Currency.TRY,
    )
    assert tx.effective_date == date(2026, 8, 1)
    assert tx.recorded_at == rec_time

    # Naive recorded_at rejected
    with pytest.raises(ValueError, match="recorded_at must be timezone-aware"):
        PortfolioTransaction(
            portfolio_id=p_id, account_id=a_id, transaction_type=TransactionType.BUY,
            instrument_id=inst_id, effective_date=date(2026, 8, 1),
            recorded_at=datetime(2026, 8, 27, 18, 0, 0),  # Naive
            quantity=Decimal("10"), unit_price=Decimal("100"), trade_currency=Currency.TRY,
        )

    # Naive executed_at rejected
    with pytest.raises(ValueError, match="executed_at must be timezone-aware"):
        PortfolioTransaction(
            portfolio_id=p_id, account_id=a_id, transaction_type=TransactionType.BUY,
            instrument_id=inst_id, effective_date=date(2026, 8, 1),
            recorded_at=rec_time,
            executed_at=datetime(2026, 8, 1, 10, 0, 0),  # Naive
            quantity=Decimal("10"), unit_price=Decimal("100"), trade_currency=Currency.TRY,
        )


def test_reversal_constructor_validation():
    p_id = uuid4()
    a_id = uuid4()
    orig_id = uuid4()
    rec_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    rev = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.REVERSAL,
        effective_date=date(2026, 8, 27),
        recorded_at=rec_time,
        reverses_transaction_id=orig_id,
    )
    assert rev.reverses_transaction_id == orig_id

    # Self reversal rejected
    same_id = uuid4()
    with pytest.raises(ValueError, match="self-reversal"):
        PortfolioTransaction(
            id=same_id,
            portfolio_id=p_id,
            account_id=a_id,
            transaction_type=TransactionType.REVERSAL,
            effective_date=date(2026, 8, 27),
            recorded_at=rec_time,
            reverses_transaction_id=same_id,
        )


@pytest.mark.parametrize("bad_field,bad_val", [
    ("instrument_id", uuid4()),
    ("quantity", Decimal("10")),
    ("unit_price", Decimal("100.00")),
    ("trade_currency", Currency.TRY),
    ("cash_amount", Decimal("1000.00")),
    ("cash_currency", Currency.TRY),
    ("cash_bucket_id", uuid4()),
    ("from_currency", Currency.USD),
    ("from_amount", Decimal("100")),
    ("to_currency", Currency.TRY),
    ("to_amount", Decimal("3400")),
])
def test_reversal_rejects_all_economic_fields(bad_field, bad_val):
    """REVERSAL must be strictly reference-only with zero independent economics."""
    p_id = uuid4()
    a_id = uuid4()
    orig_id = uuid4()
    rec_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    kwargs = {
        "portfolio_id": p_id,
        "account_id": a_id,
        "transaction_type": TransactionType.REVERSAL,
        "effective_date": date(2026, 8, 27),
        "recorded_at": rec_time,
        "reverses_transaction_id": orig_id,
        bad_field: bad_val,
    }
    with pytest.raises(ValueError, match="REVERSAL must not contain"):
        PortfolioTransaction(**kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# 3b. External Idempotency Identity Tests (Phase 12A.6)
# ─────────────────────────────────────────────────────────────────────────────

def test_external_identity_valid_manual_and_pair():
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Valid manual (None/None)
    tx_manual = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.BUY,
        instrument_id=inst_id,
        effective_date=date(2026, 8, 27),
        recorded_at=now,
        quantity=Decimal("10"),
        unit_price=Decimal("100"),
        trade_currency=Currency.TRY,
        external_source=None,
        external_reference=None,
    )
    assert tx_manual.external_source is None
    assert tx_manual.external_reference is None

    # 2. Valid external pair
    tx_ext = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.BUY,
        instrument_id=inst_id,
        effective_date=date(2026, 8, 27),
        recorded_at=now,
        quantity=Decimal("10"),
        unit_price=Decimal("100"),
        trade_currency=Currency.TRY,
        external_source="MIDAS",
        external_reference="ORD-1001",
    )
    assert tx_ext.external_source == "MIDAS"
    assert tx_ext.external_reference == "ORD-1001"


@pytest.mark.parametrize("src,ref,err_type,match_msg", [
    ("MIDAS", None, ValueError, "external_reference is missing"),
    (None, "ORD-1", ValueError, "external_source is missing"),
    ("", "ORD-1", ValueError, "external_source cannot be empty"),
    ("MIDAS", "", ValueError, "external_reference cannot be empty"),
    ("   ", "ORD-1", ValueError, "external_source cannot be empty"),
    ("MIDAS", "   ", ValueError, "external_reference cannot be empty"),
    (123, "ORD-1", TypeError, "external_source must be a str"),
    ("MIDAS", 123, TypeError, "external_reference must be a str"),
    (True, "ORD-1", TypeError, "external_source must be a str"),
    ("MIDAS", True, TypeError, "external_reference must be a str"),
])
def test_external_identity_fail_closed_rejections(src, ref, err_type, match_msg):
    """Phase 12A.6: Malformed external identity fails closed and is never downgraded to manual."""
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    now = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    with pytest.raises(err_type, match=match_msg):
        PortfolioTransaction(
            portfolio_id=p_id,
            account_id=a_id,
            transaction_type=TransactionType.BUY,
            instrument_id=inst_id,
            effective_date=date(2026, 8, 27),
            recorded_at=now,
            quantity=Decimal("10"),
            unit_price=Decimal("100"),
            trade_currency=Currency.TRY,
            external_source=src,  # type: ignore
            external_reference=ref,  # type: ignore
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Cash Bucket Model Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_cash_bucket_purpose_defaults():
    p_id = uuid4()
    now = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)

    # INVESTABLE -> True by default
    cb1 = CashBucket(
        portfolio_id=p_id,
        name="Investable TL",
        currency=Currency.TRY,
        purpose=CashPurpose.INVESTABLE,
        created_at=now,
    )
    assert cb1.included_in_investable_assets is True

    # EMERGENCY_RESERVE -> False by default
    cb2 = CashBucket(
        portfolio_id=p_id,
        name="Acil Fon",
        currency=Currency.TRY,
        purpose=CashPurpose.EMERGENCY_RESERVE,
        created_at=now,
    )
    assert cb2.included_in_investable_assets is False

    # NEAR_TERM -> False by default
    cb3 = CashBucket(
        portfolio_id=p_id,
        name="Ev Peşinatı",
        currency=Currency.USD,
        purpose=CashPurpose.NEAR_TERM,
        created_at=now,
    )
    assert cb3.included_in_investable_assets is False

    # RESTRICTED_OTHER -> False by default
    cb4 = CashBucket(
        portfolio_id=p_id,
        name="Teminat",
        currency=Currency.TRY,
        purpose=CashPurpose.RESTRICTED_OTHER,
        created_at=now,
    )
    assert cb4.included_in_investable_assets is False

    # Explicit override allowed
    cb5 = CashBucket(
        portfolio_id=p_id,
        name="Özel",
        currency=Currency.TRY,
        purpose=CashPurpose.EMERGENCY_RESERVE,
        included_in_investable_assets=True,
        created_at=now,
    )
    assert cb5.included_in_investable_assets is True


@pytest.mark.parametrize("invalid_bool", [1, 0, "true", "yes", Decimal("1")])
def test_cash_bucket_bool_type_safety(invalid_bool):
    """included_in_investable_assets must be strict bool (or None)."""
    p_id = uuid4()
    now = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)

    with pytest.raises(TypeError, match="must be a strict bool"):
        CashBucket(
            portfolio_id=p_id,
            name="Type Error Bucket",
            currency=Currency.TRY,
            purpose=CashPurpose.INVESTABLE,
            included_in_investable_assets=invalid_bool,  # type: ignore
            created_at=now,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. Investment Goal Model Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_investment_goal_model():
    p_id = uuid4()
    now = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)

    goal = InvestmentGoal(
        portfolio_id=p_id,
        name="Emeklilik 2035",
        target_amount=Decimal("5000000.00"),
        target_currency=Currency.TRY,
        target_date=date(2035, 12, 31),
        priority=GoalPriority.HIGH,
        created_at=now,
    )
    assert goal.target_amount == Decimal("5000000.00")
    assert goal.target_date == date(2035, 12, 31)
    assert goal.priority == GoalPriority.HIGH
    assert goal.status == GoalStatus.ACTIVE

    d = goal.to_dict()
    assert d["target_amount"] == "5000000.00"
    assert d["target_date"] == "2035-12-31"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Planned Contribution Model Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_planned_contribution_model():
    p_id = uuid4()
    now = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc)

    pc = PlannedContribution(
        portfolio_id=p_id,
        expected_date=date(2026, 9, 15),
        amount=Decimal("25000.00"),
        currency=Currency.TRY,
        created_at=now,
    )
    assert pc.amount == Decimal("25000.00")
    assert pc.status == ContributionStatus.PLANNED

    d = pc.to_dict()
    assert d["amount"] == "25000.00"
    assert d["currency"] == "TRY"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Position Lot Model Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_position_lot_projection_model():
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    tx_id = uuid4()

    lot = PositionLot(
        portfolio_id=p_id,
        account_id=a_id,
        instrument_id=inst_id,
        origin_transaction_id=tx_id,
        acquired_date=date(2026, 8, 1),
        quantity_open=Decimal("100"),
        original_quantity=Decimal("100"),
        native_unit_cost=Decimal("50.25"),
        currency=Currency.TRY,
        status=LotStatus.OPEN,
    )
    assert lot.quantity_open == Decimal("100")
    assert lot.native_unit_cost == Decimal("50.25")

    # quantity_open cannot exceed original_quantity
    with pytest.raises(ValueError, match="cannot exceed"):
        PositionLot(
            portfolio_id=p_id, account_id=a_id, instrument_id=inst_id,
            origin_transaction_id=tx_id, acquired_date=date(2026, 8, 1),
            quantity_open=Decimal("150"), original_quantity=Decimal("100"),
            native_unit_cost=Decimal("50.25"), currency=Currency.TRY,
        )


@pytest.mark.parametrize("invalid_val", [
    Decimal("NaN"),
    Decimal("Infinity"),
    Decimal("-Infinity"),
    float(10.0),
    int(10),
    "10.0",
    True,
])
def test_position_lot_finite_decimal_rejection(invalid_val):
    """PositionLot fields reject non-finite Decimals, floats, ints, strings, and bools."""
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    tx_id = uuid4()

    # original_quantity
    with pytest.raises((ValueError, TypeError)):
        PositionLot(
            portfolio_id=p_id, account_id=a_id, instrument_id=inst_id,
            origin_transaction_id=tx_id, acquired_date=date(2026, 8, 1),
            quantity_open=Decimal("100"), original_quantity=invalid_val,  # type: ignore
            native_unit_cost=Decimal("50.25"), currency=Currency.TRY,
        )

    # quantity_open
    with pytest.raises((ValueError, TypeError)):
        PositionLot(
            portfolio_id=p_id, account_id=a_id, instrument_id=inst_id,
            origin_transaction_id=tx_id, acquired_date=date(2026, 8, 1),
            quantity_open=invalid_val, original_quantity=Decimal("100"),  # type: ignore
            native_unit_cost=Decimal("50.25"), currency=Currency.TRY,
        )

    # native_unit_cost
    with pytest.raises((ValueError, TypeError)):
        PositionLot(
            portfolio_id=p_id, account_id=a_id, instrument_id=inst_id,
            origin_transaction_id=tx_id, acquired_date=date(2026, 8, 1),
            quantity_open=Decimal("100"), original_quantity=Decimal("100"),
            native_unit_cost=invalid_val, currency=Currency.TRY,  # type: ignore
        )


# ─────────────────────────────────────────────────────────────────────────────
# 8. Economic Fingerprint Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_economic_fingerprint_uuid_independence():
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    rec_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    tx1 = PortfolioTransaction(
        id=uuid4(),
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.BUY,
        instrument_id=inst_id,
        effective_date=date(2026, 8, 27),
        recorded_at=rec_time,
        quantity=Decimal("100"),
        unit_price=Decimal("45.50"),
        trade_currency=Currency.TRY,
        notes="First entry note",
    )

    tx2 = PortfolioTransaction(
        id=uuid4(),  # Different physical ID
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.BUY,
        instrument_id=inst_id,
        effective_date=date(2026, 8, 27),
        recorded_at=datetime(2026, 8, 27, 15, 0, 0, tzinfo=timezone.utc),  # Different recorded_at
        quantity=Decimal("100"),
        unit_price=Decimal("45.50"),
        trade_currency=Currency.TRY,
        notes="Completely different notes",  # Different notes
    )

    # Identical economic fingerprint despite different id, recorded_at, and notes
    assert tx1.economic_fingerprint() == tx2.economic_fingerprint()


def test_economic_fingerprint_decimal_lexical_equivalence():
    """Phase 12B.2A.5: Numerically identical Decimals with different lexical forms must produce the SAME fingerprint."""
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    rec_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # 1 vs 1.0 vs 1.00 vs 1E+0
    tx1 = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.BUY,
        instrument_id=inst_id,
        effective_date=date(2026, 8, 27),
        recorded_at=rec_time,
        quantity=Decimal("1"),
        unit_price=Decimal("1000"),
        trade_currency=Currency.TRY,
    )
    tx2 = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.BUY,
        instrument_id=inst_id,
        effective_date=date(2026, 8, 27),
        recorded_at=rec_time,
        quantity=Decimal("1.00"),
        unit_price=Decimal("1000.00"),
        trade_currency=Currency.TRY,
    )
    tx3 = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.BUY,
        instrument_id=inst_id,
        effective_date=date(2026, 8, 27),
        recorded_at=rec_time,
        quantity=Decimal("1E+0"),
        unit_price=Decimal("1E+3"),
        trade_currency=Currency.TRY,
    )

    assert tx1.economic_fingerprint() == tx2.economic_fingerprint()
    assert tx2.economic_fingerprint() == tx3.economic_fingerprint()


def test_economic_fingerprint_numeric_difference_produces_different_hash():
    """Phase 12B.2A.5: Genuine numeric differences must produce distinct fingerprints."""
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    rec_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    tx1 = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.BUY,
        instrument_id=inst_id,
        effective_date=date(2026, 8, 27),
        recorded_at=rec_time,
        quantity=Decimal("1.00"),
        unit_price=Decimal("100.00"),
        trade_currency=Currency.TRY,
    )
    tx2 = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.BUY,
        instrument_id=inst_id,
        effective_date=date(2026, 8, 27),
        recorded_at=rec_time,
        quantity=Decimal("1.0000001"),
        unit_price=Decimal("100.00"),
        trade_currency=Currency.TRY,
    )

    assert tx1.economic_fingerprint() != tx2.economic_fingerprint()


def test_economic_fingerprint_executed_at_instant_normalization():
    """Phase 12B.2A.6: Executed_at timestamps representing the same physical instant must produce the SAME fingerprint."""
    from datetime import timedelta
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    rec_time = datetime(2026, 8, 28, 15, 0, 0, tzinfo=timezone.utc)
    tz_plus_3 = timezone(timedelta(hours=3))

    # TX A: 10:00:00 UTC
    tx_a = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.BUY,
        instrument_id=inst_id,
        effective_date=date(2026, 8, 28),
        executed_at=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
        recorded_at=rec_time,
        quantity=Decimal("100"),
        unit_price=Decimal("50.00"),
        trade_currency=Currency.USD,
    )

    # TX B: 13:00:00 +03:00 (Same physical instant)
    tx_b = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.BUY,
        instrument_id=inst_id,
        effective_date=date(2026, 8, 28),
        executed_at=datetime(2026, 8, 28, 13, 0, 0, tzinfo=tz_plus_3),
        recorded_at=rec_time,
        quantity=Decimal("100"),
        unit_price=Decimal("50.00"),
        trade_currency=Currency.USD,
    )

    # TX C: 10:00:01 UTC (Different physical instant)
    tx_c = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.BUY,
        instrument_id=inst_id,
        effective_date=date(2026, 8, 28),
        executed_at=datetime(2026, 8, 28, 10, 0, 1, tzinfo=timezone.utc),
        recorded_at=rec_time,
        quantity=Decimal("100"),
        unit_price=Decimal("50.00"),
        trade_currency=Currency.USD,
    )

    # TX D: 10:00:00.123456 UTC (Microsecond precision)
    tx_d = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.BUY,
        instrument_id=inst_id,
        effective_date=date(2026, 8, 28),
        executed_at=datetime(2026, 8, 28, 10, 0, 0, 123456, tzinfo=timezone.utc),
        recorded_at=rec_time,
        quantity=Decimal("100"),
        unit_price=Decimal("50.00"),
        trade_currency=Currency.USD,
    )

    # TX E: 13:00:00.123456 +03:00 (Same microsecond instant as D)
    tx_e = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.BUY,
        instrument_id=inst_id,
        effective_date=date(2026, 8, 28),
        executed_at=datetime(2026, 8, 28, 13, 0, 0, 123456, tzinfo=tz_plus_3),
        recorded_at=rec_time,
        quantity=Decimal("100"),
        unit_price=Decimal("50.00"),
        trade_currency=Currency.USD,
    )

    assert tx_a.economic_fingerprint() == tx_b.economic_fingerprint()
    assert tx_a.economic_fingerprint() != tx_c.economic_fingerprint()
    assert tx_d.economic_fingerprint() == tx_e.economic_fingerprint()
    assert tx_a.economic_fingerprint() != tx_d.economic_fingerprint()


def test_economic_fingerprint_colon_boundary_disambiguation():
    """Phase 12B.2A.7: Preimage boundaries must be unambiguous; colon in fields must NOT cause collision."""
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    rec_time = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)

    # TX A: source="A:B", ref="C"
    tx_a = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.BUY,
        instrument_id=inst_id,
        effective_date=date(2026, 8, 28),
        recorded_at=rec_time,
        quantity=Decimal("100"),
        unit_price=Decimal("50.00"),
        trade_currency=Currency.USD,
        external_source="A:B",
        external_reference="C",
    )

    # TX B: source="A", ref="B:C"
    tx_b = PortfolioTransaction(
        portfolio_id=p_id,
        account_id=a_id,
        transaction_type=TransactionType.BUY,
        instrument_id=inst_id,
        effective_date=date(2026, 8, 28),
        recorded_at=rec_time,
        quantity=Decimal("100"),
        unit_price=Decimal("50.00"),
        trade_currency=Currency.USD,
        external_source="A",
        external_reference="B:C",
    )

    # Under naive colon-join, both produced ...:A:B:C:... and collided.
    # Under structured JSON encoding, they MUST have distinct fingerprints.
    assert tx_a.economic_fingerprint() != tx_b.economic_fingerprint()


def test_economic_fingerprint_special_character_and_delimiter_red_team():
    """Phase 12B.2A.7: Red-team structured encoding against delimiters, JSON characters, and Unicode."""
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    rec_time = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)

    def _make_tx(source: str, ref: str) -> PortfolioTransaction:
        return PortfolioTransaction(
            portfolio_id=p_id,
            account_id=a_id,
            transaction_type=TransactionType.BUY,
            instrument_id=inst_id,
            effective_date=date(2026, 8, 28),
            recorded_at=rec_time,
            quantity=Decimal("100"),
            unit_price=Decimal("50.00"),
            trade_currency=Currency.USD,
            external_source=source,
            external_reference=ref,
        )

    # Comma boundary
    tx_comma_1 = _make_tx("A,B", "C")
    tx_comma_2 = _make_tx("A", "B,C")
    assert tx_comma_1.economic_fingerprint() != tx_comma_2.economic_fingerprint()

    # Brackets
    tx_brack_1 = _make_tx("A[B]", "C")
    tx_brack_2 = _make_tx("A", "[B]C")
    assert tx_brack_1.economic_fingerprint() != tx_brack_2.economic_fingerprint()

    # Quotes & backslashes
    tx_quote_1 = _make_tx('A"B"', "C")
    tx_quote_2 = _make_tx("A", '"B"C')
    assert tx_quote_1.economic_fingerprint() != tx_quote_2.economic_fingerprint()

    # Braces
    tx_brace_1 = _make_tx("A{B}", "C")
    tx_brace_2 = _make_tx("A", "{B}C")
    assert tx_brace_1.economic_fingerprint() != tx_brace_2.economic_fingerprint()

    # Unicode determinism
    tx_uni_1 = _make_tx("İŞBANK", "HESAP-101 ₺")
    tx_uni_2 = _make_tx("İŞBANK", "HESAP-101 ₺")
    assert tx_uni_1.economic_fingerprint() == tx_uni_2.economic_fingerprint()


def test_external_identity_normalization_parity_and_boundaries():
    """Phase 12B.2C.1: Canonical cross-language external identity normalization matrix."""
    p_id = uuid4()
    a_id = uuid4()
    inst_id = uuid4()
    rec_time = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)

    def _make_tx(source: str, ref: str) -> PortfolioTransaction:
        return PortfolioTransaction(
            portfolio_id=p_id,
            account_id=a_id,
            transaction_type=TransactionType.BUY,
            instrument_id=inst_id,
            effective_date=date(2026, 8, 28),
            recorded_at=rec_time,
            quantity=Decimal("100"),
            unit_price=Decimal("50.00"),
            trade_currency=Currency.USD,
            external_source=source,
            external_reference=ref,
        )

    # 1. Source: spaces vs trim
    tx_src_spaces = _make_tx("  MIDAS  ", "ORD-1")
    tx_src_clean = _make_tx("MIDAS", "ORD-1")
    assert tx_src_spaces.economic_fingerprint() == tx_src_clean.economic_fingerprint()

    # 2. Source: ASCII lowercase vs uppercase
    tx_src_lower = _make_tx("midas", "ORD-1")
    assert tx_src_lower.economic_fingerprint() == tx_src_clean.economic_fingerprint()

    # 3. Source: tabs vs clean (different identities under canonical contract)
    tx_src_tabs = _make_tx("\tMIDAS\t", "ORD-1")
    assert tx_src_tabs.economic_fingerprint() != tx_src_clean.economic_fingerprint()

    # 4. Source: newlines vs clean (different identities under canonical contract)
    tx_src_newlines = _make_tx("\nMIDAS\n", "ORD-1")
    assert tx_src_newlines.economic_fingerprint() != tx_src_clean.economic_fingerprint()

    # 5. Source: Non-ASCII characters (locale-independent preservation)
    tx_isbank_upper = _make_tx("İŞBANK", "ORD-1")
    tx_isbank_lower = _make_tx("işbank", "ORD-1")
    # ASCII translate only maps 'a-z' -> 'A-Z', preserving Turkish 'i' with dot or 'ş'
    assert tx_isbank_upper.economic_fingerprint() != tx_isbank_lower.economic_fingerprint()

    # 6. Reference: spaces vs trim
    tx_ref_spaces = _make_tx("MIDAS", "  ORD-1  ")
    assert tx_ref_spaces.economic_fingerprint() == tx_src_clean.economic_fingerprint()

    # 7. Reference: tabs vs clean (different identities)
    tx_ref_tabs = _make_tx("MIDAS", "\tORD-1\t")
    assert tx_ref_tabs.economic_fingerprint() != tx_src_clean.economic_fingerprint()

    # 8. Reference: case sensitivity (ORD-1 vs ord-1 are different)
    tx_ref_lower = _make_tx("MIDAS", "ord-1")
    assert tx_ref_lower.economic_fingerprint() != tx_src_clean.economic_fingerprint()

    # 9. Reference: Unicode determinism
    tx_ref_uni1 = _make_tx("MIDAS", "HESAP-101 ₺")
    tx_ref_uni2 = _make_tx("MIDAS", "HESAP-101 ₺")
    assert tx_ref_uni1.economic_fingerprint() == tx_ref_uni2.economic_fingerprint()



