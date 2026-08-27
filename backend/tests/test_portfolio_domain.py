"""
backend/tests/test_portfolio_domain.py
======================================
Comprehensive Domain Model Unit Tests for Private Portfolio Ledger.

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

    # SANDBOX with provenance is valid
    source_id = uuid4()
    sb = Portfolio(
        mode=PortfolioMode.SANDBOX,
        name="Sandbox 1",
        base_currency=Currency.TRY,
        created_at=now,
        source_portfolio_id=source_id,
        source_snapshot_time=now,
    )
    assert sb.source_portfolio_id == source_id


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
