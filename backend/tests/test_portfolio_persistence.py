"""
backend/tests/test_portfolio_persistence.py
===========================================
Comprehensive Test Suite for Portfolio Persistence Codec & Serialization/Hydration (Phase 12B.2A).

Pure Python verification of:
    - Exact round-trip serialization and hydration for all 6 canonical entities.
    - Strict Decimal typing (exact string serialization, rejection of float, int, bool, NaN, Infinity).
    - Strict UUID typing (rejection of bool, int, malformed strings, empty strings).
    - Strict Datetime/Date typing (timezone awareness required, rejection of naive datetimes and date masquerading).
    - Strict Enum validation with canonical domain enums.
    - Deterministic 64-char SHA-256 economic_fingerprint validation on transaction hydration.
    - Fail-closed behavior on owner_id mismatch, missing required columns, or tampered rows.
    - Zero float emission in serialized dictionaries.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict
from uuid import UUID, uuid4
import pytest

from backend.engine.private.domain import (
    CashPurpose,
    ContributionStatus,
    Currency,
    GoalPriority,
    GoalStatus,
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
)
from backend.engine.private.portfolio.persistence import (
    hydrate_cash_bucket,
    hydrate_investment_goal,
    hydrate_planned_contribution,
    hydrate_portfolio,
    hydrate_portfolio_account,
    hydrate_portfolio_transaction,
    serialize_cash_bucket,
    serialize_investment_goal,
    serialize_planned_contribution,
    serialize_portfolio,
    serialize_portfolio_account,
    serialize_portfolio_transaction,
)


def assert_no_floats_recursive(data: Any) -> None:
    """Verifies that no float value exists anywhere in the serialized data structure."""
    if isinstance(data, float):
        pytest.fail(f"Found forbidden float in serialized data: {data!r}")
    elif isinstance(data, dict):
        for k, v in data.items():
            assert_no_floats_recursive(v)
    elif isinstance(data, list):
        for item in data:
            assert_no_floats_recursive(item)


@pytest.fixture
def owner_id() -> UUID:
    return uuid4()


@pytest.fixture
def portfolio_id() -> UUID:
    return uuid4()


@pytest.fixture
def account_id() -> UUID:
    return uuid4()


@pytest.fixture
def instrument_id() -> UUID:
    return uuid4()


@pytest.fixture
def bucket_id() -> UUID:
    return uuid4()


class TestPortfolioCodec:
    """Round-trip and adversarial tests for Portfolio entity codec."""

    def test_portfolio_my_portfolio_round_trip(self, owner_id: UUID):
        original = Portfolio(
            mode=PortfolioMode.MY_PORTFOLIO,
            name="Main Wealth Portfolio",
            base_currency=Currency.TRY,
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
            archived_at=None,
            owner_id=str(owner_id),
        )

        row = serialize_portfolio(original, owner_id)
        assert_no_floats_recursive(row)
        assert "is_active" not in row
        assert row["owner_id"] == str(owner_id)
        assert row["mode"] == "my_portfolio"
        assert row["base_currency"] == "TRY"

        hydrated = hydrate_portfolio(row, owner_id)
        assert hydrated.id == original.id
        assert hydrated.mode == original.mode
        assert hydrated.name == original.name
        assert hydrated.base_currency == original.base_currency
        assert hydrated.created_at == original.created_at
        assert hydrated.archived_at is None
        assert hydrated.source_portfolio_id is None
        assert hydrated.source_snapshot_time is None
        assert hydrated.owner_id == str(owner_id)

    def test_portfolio_sandbox_round_trip(self, owner_id: UUID):
        source_id = uuid4()
        original = Portfolio(
            mode=PortfolioMode.SANDBOX,
            name="Stress Test Sandbox",
            base_currency=Currency.USD,
            created_at=datetime(2026, 2, 1, 12, 0, 0, tzinfo=timezone.utc),
            archived_at=datetime(2026, 2, 10, 15, 30, 0, tzinfo=timezone.utc),
            source_portfolio_id=source_id,
            source_snapshot_time=datetime(2026, 1, 15, 0, 0, 0, tzinfo=timezone.utc),
            owner_id=str(owner_id),
        )

        row = serialize_portfolio(original, owner_id)
        assert_no_floats_recursive(row)
        assert row["source_portfolio_id"] == str(source_id)

        hydrated = hydrate_portfolio(row, owner_id)
        assert hydrated.id == original.id
        assert hydrated.mode == original.mode
        assert hydrated.source_portfolio_id == source_id
        assert hydrated.source_snapshot_time == original.source_snapshot_time
        assert hydrated.archived_at == original.archived_at

    def test_portfolio_owner_mismatch_fails_closed(self, owner_id: UUID):
        original = Portfolio(
            mode=PortfolioMode.MY_PORTFOLIO,
            name="Main Portfolio",
            base_currency=Currency.TRY,
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
            owner_id=str(owner_id),
        )
        wrong_owner = uuid4()

        # Serialize fails if model's owner_id != trusted owner_id
        with pytest.raises(ValueError, match="does not match trusted owner_id"):
            serialize_portfolio(original, wrong_owner)

        row = serialize_portfolio(original, owner_id)

        # Hydrate fails if row owner != expected_owner_id
        with pytest.raises(ValueError, match="Owner mismatch"):
            hydrate_portfolio(row, wrong_owner)


class TestPortfolioAccountCodec:
    """Round-trip and adversarial tests for PortfolioAccount entity codec."""

    def test_portfolio_account_round_trip(self, owner_id: UUID, portfolio_id: UUID):
        original = PortfolioAccount(
            portfolio_id=portfolio_id,
            name="Interactive Brokers Custody",
            base_currency=Currency.USD,
            broker_label="IBKR-PRO",
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
            archived_at=None,
        )

        row = serialize_portfolio_account(original, owner_id)
        assert_no_floats_recursive(row)
        assert "is_active" not in row
        assert row["portfolio_id"] == str(portfolio_id)
        assert row["owner_id"] == str(owner_id)

        hydrated = hydrate_portfolio_account(row, owner_id)
        assert hydrated.id == original.id
        assert hydrated.portfolio_id == original.portfolio_id
        assert hydrated.name == original.name
        assert hydrated.base_currency == original.base_currency
        assert hydrated.broker_label == original.broker_label
        assert hydrated.created_at == original.created_at
        assert hydrated.archived_at is None


class TestCashBucketCodec:
    """Round-trip and adversarial tests for CashBucket entity codec."""

    def test_cash_bucket_round_trip(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID):
        original = CashBucket(
            portfolio_id=portfolio_id,
            account_id=account_id,
            name="USD Core Liquidity",
            currency=Currency.USD,
            purpose=CashPurpose.INVESTABLE,
            included_in_investable_assets=True,
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
            archived_at=None,
        )

        row = serialize_cash_bucket(original, owner_id)
        assert_no_floats_recursive(row)
        assert "is_active" not in row
        assert row["purpose"] == "investable"
        assert row["included_in_investable_assets"] is True

        hydrated = hydrate_cash_bucket(row, owner_id)
        assert hydrated.id == original.id
        assert hydrated.portfolio_id == original.portfolio_id
        assert hydrated.account_id == original.account_id
        assert hydrated.name == original.name
        assert hydrated.currency == original.currency
        assert hydrated.purpose == original.purpose
        assert hydrated.included_in_investable_assets is True
        assert hydrated.created_at == original.created_at


class TestInvestmentGoalCodec:
    """Round-trip and adversarial tests for InvestmentGoal entity codec."""

    def test_investment_goal_round_trip(self, owner_id: UUID, portfolio_id: UUID):
        original = InvestmentGoal(
            portfolio_id=portfolio_id,
            name="Retirement Nest Egg",
            target_amount=Decimal("15000000.50"),
            target_currency=Currency.TRY,
            target_date=date(2045, 12, 31),
            priority=GoalPriority.CRITICAL,
            status=GoalStatus.ACTIVE,
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
            archived_at=None,
        )

        row = serialize_investment_goal(original, owner_id)
        assert_no_floats_recursive(row)
        assert isinstance(row["target_amount"], str)
        assert row["target_amount"] == "15000000.50"
        assert row["target_date"] == "2045-12-31"

        hydrated = hydrate_investment_goal(row, owner_id)
        assert hydrated.id == original.id
        assert hydrated.portfolio_id == original.portfolio_id
        assert hydrated.name == original.name
        assert hydrated.target_amount == original.target_amount
        assert hydrated.target_currency == original.target_currency
        assert hydrated.target_date == original.target_date
        assert hydrated.priority == original.priority
        assert hydrated.status == original.status


class TestPlannedContributionCodec:
    """Round-trip and adversarial tests for PlannedContribution entity codec."""

    def test_planned_contribution_round_trip(self, owner_id: UUID, portfolio_id: UUID, bucket_id: UUID):
        goal_id = uuid4()
        original = PlannedContribution(
            portfolio_id=portfolio_id,
            goal_id=goal_id,
            cash_bucket_id=bucket_id,
            expected_date=date(2026, 6, 15),
            amount=Decimal("50000.00"),
            currency=Currency.TRY,
            status=ContributionStatus.CONFIRMED,
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        )

        row = serialize_planned_contribution(original, owner_id)
        assert_no_floats_recursive(row)
        assert isinstance(row["amount"], str)
        assert row["amount"] == "50000.00"
        assert row["status"] == "confirmed"

        hydrated = hydrate_planned_contribution(row, owner_id)
        assert hydrated.id == original.id
        assert hydrated.portfolio_id == original.portfolio_id
        assert hydrated.goal_id == goal_id
        assert hydrated.cash_bucket_id == bucket_id
        assert hydrated.expected_date == original.expected_date
        assert hydrated.amount == original.amount
        assert hydrated.currency == original.currency
        assert hydrated.status == ContributionStatus.CONFIRMED


class TestPortfolioTransactionCodec:
    """Comprehensive Round-Trip matrix for all 13 required PortfolioTransaction cases."""

    def test_tx_round_trip_1_buy(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, instrument_id: UUID):
        tx = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 3, 1),
            executed_at=datetime(2026, 3, 1, 14, 30, 0, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 3, 1, 14, 30, 5, tzinfo=timezone.utc),
            instrument_id=instrument_id,
            quantity=Decimal("150.75"),
            unit_price=Decimal("42.50"),
            trade_currency=Currency.USD,
        )
        row = serialize_portfolio_transaction(tx, owner_id)
        assert_no_floats_recursive(row)
        assert row["economic_fingerprint"] == tx.economic_fingerprint()

        hydrated = hydrate_portfolio_transaction(row, owner_id)
        assert hydrated == tx

    def test_tx_round_trip_2_sell(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, instrument_id: UUID):
        tx = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.SELL,
            effective_date=date(2026, 3, 2),
            recorded_at=datetime(2026, 3, 2, 15, 0, 0, tzinfo=timezone.utc),
            instrument_id=instrument_id,
            quantity=Decimal("50.00"),
            unit_price=Decimal("45.00"),
            trade_currency=Currency.USD,
        )
        row = serialize_portfolio_transaction(tx, owner_id)
        assert_no_floats_recursive(row)
        hydrated = hydrate_portfolio_transaction(row, owner_id)
        assert hydrated == tx

    def test_tx_round_trip_3_cash_deposit(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, bucket_id: UUID):
        tx = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 3, 3),
            recorded_at=datetime(2026, 3, 3, 9, 0, 0, tzinfo=timezone.utc),
            cash_amount=Decimal("10000.00"),
            cash_currency=Currency.TRY,
            cash_bucket_id=bucket_id,
        )
        row = serialize_portfolio_transaction(tx, owner_id)
        assert_no_floats_recursive(row)
        hydrated = hydrate_portfolio_transaction(row, owner_id)
        assert hydrated == tx

    def test_tx_round_trip_4_cash_withdrawal(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, bucket_id: UUID):
        tx = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.CASH_WITHDRAWAL,
            effective_date=date(2026, 3, 4),
            recorded_at=datetime(2026, 3, 4, 11, 0, 0, tzinfo=timezone.utc),
            cash_amount=Decimal("2500.00"),
            cash_currency=Currency.TRY,
            cash_bucket_id=bucket_id,
        )
        row = serialize_portfolio_transaction(tx, owner_id)
        assert_no_floats_recursive(row)
        hydrated = hydrate_portfolio_transaction(row, owner_id)
        assert hydrated == tx

    def test_tx_round_trip_5_dividend_with_instrument(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, instrument_id: UUID, bucket_id: UUID):
        tx = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.DIVIDEND,
            effective_date=date(2026, 3, 5),
            recorded_at=datetime(2026, 3, 5, 12, 0, 0, tzinfo=timezone.utc),
            instrument_id=instrument_id,
            cash_amount=Decimal("350.25"),
            cash_currency=Currency.USD,
            cash_bucket_id=bucket_id,
        )
        row = serialize_portfolio_transaction(tx, owner_id)
        assert_no_floats_recursive(row)
        hydrated = hydrate_portfolio_transaction(row, owner_id)
        assert hydrated == tx

    def test_tx_round_trip_6_dividend_without_instrument(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, bucket_id: UUID):
        tx = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.DIVIDEND,
            effective_date=date(2026, 3, 6),
            recorded_at=datetime(2026, 3, 6, 12, 0, 0, tzinfo=timezone.utc),
            cash_amount=Decimal("120.00"),
            cash_currency=Currency.TRY,
            cash_bucket_id=bucket_id,
        )
        row = serialize_portfolio_transaction(tx, owner_id)
        assert_no_floats_recursive(row)
        hydrated = hydrate_portfolio_transaction(row, owner_id)
        assert hydrated == tx

    def test_tx_round_trip_7_interest(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, bucket_id: UUID):
        tx = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.INTEREST,
            effective_date=date(2026, 3, 7),
            recorded_at=datetime(2026, 3, 7, 16, 0, 0, tzinfo=timezone.utc),
            cash_amount=Decimal("45.80"),
            cash_currency=Currency.USD,
            cash_bucket_id=bucket_id,
        )
        row = serialize_portfolio_transaction(tx, owner_id)
        assert_no_floats_recursive(row)
        hydrated = hydrate_portfolio_transaction(row, owner_id)
        assert hydrated == tx

    def test_tx_round_trip_8_fee(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, bucket_id: UUID):
        tx = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.FEE,
            effective_date=date(2026, 3, 8),
            recorded_at=datetime(2026, 3, 8, 17, 0, 0, tzinfo=timezone.utc),
            cash_amount=Decimal("15.00"),
            cash_currency=Currency.USD,
            cash_bucket_id=bucket_id,
        )
        row = serialize_portfolio_transaction(tx, owner_id)
        assert_no_floats_recursive(row)
        hydrated = hydrate_portfolio_transaction(row, owner_id)
        assert hydrated == tx

    def test_tx_round_trip_9_tax_withholding(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, bucket_id: UUID):
        tx = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.TAX_WITHHOLDING,
            effective_date=date(2026, 3, 9),
            recorded_at=datetime(2026, 3, 9, 18, 0, 0, tzinfo=timezone.utc),
            cash_amount=Decimal("52.50"),
            cash_currency=Currency.USD,
            cash_bucket_id=bucket_id,
        )
        row = serialize_portfolio_transaction(tx, owner_id)
        assert_no_floats_recursive(row)
        hydrated = hydrate_portfolio_transaction(row, owner_id)
        assert hydrated == tx

    def test_tx_round_trip_10_fx_conversion(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID):
        tx = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.FX_CONVERSION,
            effective_date=date(2026, 3, 10),
            recorded_at=datetime(2026, 3, 10, 19, 0, 0, tzinfo=timezone.utc),
            from_currency=Currency.USD,
            from_amount=Decimal("1000.00"),
            to_currency=Currency.TRY,
            to_amount=Decimal("38000.00"),
        )
        row = serialize_portfolio_transaction(tx, owner_id)
        assert_no_floats_recursive(row)
        hydrated = hydrate_portfolio_transaction(row, owner_id)
        assert hydrated == tx

    def test_tx_round_trip_11_reversal(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID):
        orig_tx_id = uuid4()
        tx = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.REVERSAL,
            effective_date=date(2026, 3, 11),
            recorded_at=datetime(2026, 3, 11, 20, 0, 0, tzinfo=timezone.utc),
            reverses_transaction_id=orig_tx_id,
        )
        row = serialize_portfolio_transaction(tx, owner_id)
        assert_no_floats_recursive(row)
        hydrated = hydrate_portfolio_transaction(row, owner_id)
        assert hydrated == tx

    def test_tx_round_trip_12_manual_transaction(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, instrument_id: UUID):
        tx = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 3, 12),
            recorded_at=datetime(2026, 3, 12, 21, 0, 0, tzinfo=timezone.utc),
            instrument_id=instrument_id,
            quantity=Decimal("10"),
            unit_price=Decimal("100"),
            trade_currency=Currency.EUR,
            external_source=None,
            external_reference=None,
            notes="Manual booking",
        )
        row = serialize_portfolio_transaction(tx, owner_id)
        assert_no_floats_recursive(row)
        assert row["external_source"] is None
        assert row["external_reference"] is None

        hydrated = hydrate_portfolio_transaction(row, owner_id)
        assert hydrated == tx

    def test_tx_round_trip_13_external_transaction(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, instrument_id: UUID):
        # Raw external strings must be preserved exactly
        tx = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 3, 13),
            recorded_at=datetime(2026, 3, 13, 22, 0, 0, tzinfo=timezone.utc),
            instrument_id=instrument_id,
            quantity=Decimal("10"),
            unit_price=Decimal("100"),
            trade_currency=Currency.EUR,
            external_source="  tefas  ",
            external_reference=" ABC-1 ",
        )
        row = serialize_portfolio_transaction(tx, owner_id)
        assert_no_floats_recursive(row)
        assert row["external_source"] == "  tefas  "
        assert row["external_reference"] == " ABC-1 "

        hydrated = hydrate_portfolio_transaction(row, owner_id)
        assert hydrated == tx
        assert hydrated.external_source == "  tefas  "
        assert hydrated.external_reference == " ABC-1 "


class TestAdversarialCodecFailures:
    """Exhaustive adversarial failure testing for all codec boundaries."""

    def test_reject_float_in_financial_fields(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, instrument_id: UUID):
        # Quantity as float
        row: Dict[str, Any] = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "account_id": str(account_id),
            "owner_id": str(owner_id),
            "transaction_type": "buy",
            "effective_date": "2026-03-01",
            "recorded_at": "2026-03-01T10:00:00Z",
            "instrument_id": str(instrument_id),
            "quantity": 150.75,  # float rejected!
            "unit_price": "42.50",
            "trade_currency": "USD",
            "economic_fingerprint": "0" * 64,
        }
        with pytest.raises(TypeError, match="must be Decimal or exact decimal str"):
            hydrate_portfolio_transaction(row, owner_id)

        # Unit price as float
        row["quantity"] = "150.75"
        row["unit_price"] = 42.50  # float rejected!
        with pytest.raises(TypeError, match="must be Decimal or exact decimal str"):
            hydrate_portfolio_transaction(row, owner_id)

    def test_reject_int_and_bool_in_financial_fields(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, instrument_id: UUID):
        row: Dict[str, Any] = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "account_id": str(account_id),
            "owner_id": str(owner_id),
            "transaction_type": "buy",
            "effective_date": "2026-03-01",
            "recorded_at": "2026-03-01T10:00:00Z",
            "instrument_id": str(instrument_id),
            "quantity": 100,  # int rejected!
            "unit_price": "42.50",
            "trade_currency": "USD",
            "economic_fingerprint": "0" * 64,
        }
        with pytest.raises(TypeError, match="must be Decimal or exact decimal str"):
            hydrate_portfolio_transaction(row, owner_id)

        row["quantity"] = True  # bool rejected!
        with pytest.raises(TypeError, match="must be Decimal or exact decimal str"):
            hydrate_portfolio_transaction(row, owner_id)

    def test_reject_non_finite_decimals_and_strings(self, owner_id: UUID, portfolio_id: UUID):
        row: Dict[str, Any] = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "owner_id": str(owner_id),
            "name": "Goal",
            "target_amount": Decimal("NaN"),  # Decimal NaN
            "target_currency": "TRY",
            "priority": "medium",
            "status": "active",
            "created_at": "2026-01-01T10:00:00Z",
        }
        with pytest.raises(ValueError, match="must be a finite Decimal"):
            hydrate_investment_goal(row, owner_id)

        row["target_amount"] = "NaN"
        with pytest.raises(ValueError, match="must be a finite Decimal"):
            hydrate_investment_goal(row, owner_id)

        row["target_amount"] = "Infinity"
        with pytest.raises(ValueError, match="must be a finite Decimal"):
            hydrate_investment_goal(row, owner_id)

        row["target_amount"] = "-Infinity"
        with pytest.raises(ValueError, match="must be a finite Decimal"):
            hydrate_investment_goal(row, owner_id)

        row["target_amount"] = "invalid_numeric"
        with pytest.raises(ValueError, match="Invalid decimal string"):
            hydrate_investment_goal(row, owner_id)

    def test_reject_malformed_and_invalid_uuids(self, owner_id: UUID, portfolio_id: UUID):
        row: Dict[str, Any] = {
            "id": "not-a-valid-uuid",
            "portfolio_id": str(portfolio_id),
            "owner_id": str(owner_id),
            "mode": "my_portfolio",
            "name": "Portfolio",
            "base_currency": "TRY",
            "created_at": "2026-01-01T10:00:00Z",
        }
        with pytest.raises(ValueError, match="invalid UUID"):
            hydrate_portfolio(row, owner_id)

        row["id"] = ""
        with pytest.raises(ValueError, match="invalid UUID"):
            hydrate_portfolio(row, owner_id)

        row["id"] = True
        with pytest.raises(TypeError, match="must be UUID or canonical UUID str"):
            hydrate_portfolio(row, owner_id)

        row["id"] = 12345
        with pytest.raises(TypeError, match="must be UUID or canonical UUID str"):
            hydrate_portfolio(row, owner_id)

    def test_reject_naive_datetimes_and_invalid_dates(self, owner_id: UUID, portfolio_id: UUID):
        row: Dict[str, Any] = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "owner_id": str(owner_id),
            "mode": "my_portfolio",
            "name": "Portfolio",
            "base_currency": "TRY",
            "created_at": datetime(2026, 1, 1, 10, 0, 0),  # naive datetime!
        }
        with pytest.raises(ValueError, match="must be timezone-aware"):
            hydrate_portfolio(row, owner_id)

        row["created_at"] = "2026-01-01T10:00:00"  # timezone-less ISO string!
        with pytest.raises(ValueError, match="must be timezone-aware"):
            hydrate_portfolio(row, owner_id)

        # Datetime masquerading as date
        row_goal: Dict[str, Any] = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "owner_id": str(owner_id),
            "name": "Goal",
            "target_amount": "1000",
            "target_currency": "TRY",
            "target_date": datetime(2026, 12, 31, 0, 0, 0, tzinfo=timezone.utc),  # datetime rejected as date!
            "priority": "medium",
            "status": "active",
            "created_at": "2026-01-01T10:00:00Z",
        }
        with pytest.raises(TypeError, match="must be date, not datetime"):
            hydrate_investment_goal(row_goal, owner_id)

    def test_reject_unknown_enums(self, owner_id: UUID, portfolio_id: UUID):
        row: Dict[str, Any] = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "owner_id": str(owner_id),
            "mode": "unknown_mode",
            "name": "Portfolio",
            "base_currency": "TRY",
            "created_at": "2026-01-01T10:00:00Z",
        }
        with pytest.raises(ValueError, match="Invalid value 'unknown_mode' for enum PortfolioMode"):
            hydrate_portfolio(row, owner_id)

        row["mode"] = "my_portfolio"
        row["base_currency"] = "JPY"  # Non-canonical currency!
        with pytest.raises(ValueError, match="Invalid value 'JPY' for enum Currency"):
            hydrate_portfolio(row, owner_id)

    def test_transaction_fingerprint_integrity_validation(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, instrument_id: UUID):
        tx = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 3, 1),
            recorded_at=datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc),
            instrument_id=instrument_id,
            quantity=Decimal("100"),
            unit_price=Decimal("50"),
            trade_currency=Currency.USD,
        )
        row = serialize_portfolio_transaction(tx, owner_id)

        # Missing fingerprint
        row_no_fp = dict(row)
        del row_no_fp["economic_fingerprint"]
        with pytest.raises(KeyError, match="Missing required columns"):
            hydrate_portfolio_transaction(row_no_fp, owner_id)

        # Malformed fingerprint (length/case/non-hex)
        row_bad_fp = dict(row)
        row_bad_fp["economic_fingerprint"] = "abc123"
        with pytest.raises(ValueError, match="Invalid economic_fingerprint in row"):
            hydrate_portfolio_transaction(row_bad_fp, owner_id)

        row_bad_fp["economic_fingerprint"] = row["economic_fingerprint"].upper()
        with pytest.raises(ValueError, match="Invalid economic_fingerprint in row"):
            hydrate_portfolio_transaction(row_bad_fp, owner_id)

        # Fingerprint mismatch (tampering with quantity in row)
        row_tampered = dict(row)
        row_tampered["quantity"] = "105"  # tampered without updating fingerprint
        with pytest.raises(ValueError, match="Economic fingerprint mismatch"):
            hydrate_portfolio_transaction(row_tampered, owner_id)

    def test_domain_post_init_validation_rejections(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, instrument_id: UUID):
        # BUY without instrument_id fails via domain __post_init__
        row_buy: Dict[str, Any] = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "account_id": str(account_id),
            "owner_id": str(owner_id),
            "transaction_type": "buy",
            "effective_date": "2026-03-01",
            "recorded_at": "2026-03-01T10:00:00Z",
            "instrument_id": None,
            "quantity": "100",
            "unit_price": "50",
            "trade_currency": "USD",
            "economic_fingerprint": "0" * 64,
        }
        with pytest.raises(ValueError, match="BUY requires instrument_id"):
            hydrate_portfolio_transaction(row_buy, owner_id)

        # SELL with cash_amount fails via domain __post_init__
        row_sell: Dict[str, Any] = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "account_id": str(account_id),
            "owner_id": str(owner_id),
            "transaction_type": "sell",
            "effective_date": "2026-03-01",
            "recorded_at": "2026-03-01T10:00:00Z",
            "instrument_id": str(instrument_id),
            "quantity": "100",
            "unit_price": "50",
            "trade_currency": "USD",
            "cash_amount": "5000",
            "economic_fingerprint": "0" * 64,
        }
        with pytest.raises(ValueError, match="SELL must not contain cash_amount or cash_currency"):
            hydrate_portfolio_transaction(row_sell, owner_id)

        # REVERSAL with independent economics fails via domain __post_init__
        row_reversal: Dict[str, Any] = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "account_id": str(account_id),
            "owner_id": str(owner_id),
            "transaction_type": "reversal",
            "effective_date": "2026-03-01",
            "recorded_at": "2026-03-01T10:00:00Z",
            "reverses_transaction_id": str(uuid4()),
            "cash_amount": "1000",
            "economic_fingerprint": "0" * 64,
        }
        with pytest.raises(ValueError, match="REVERSAL must not contain cash_amount or cash_currency"):
            hydrate_portfolio_transaction(row_reversal, owner_id)

    def test_extreme_and_arbitrary_precision_decimals(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, instrument_id: UUID):
        """Validates that arbitrarily small or large exact decimals round-trip without precision loss."""
        tiny_decimal = Decimal("0.000000000000000001")
        huge_decimal = Decimal("12345678901234567890.123456789")

        tx = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 3, 1),
            recorded_at=datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc),
            instrument_id=instrument_id,
            quantity=tiny_decimal,
            unit_price=huge_decimal,
            trade_currency=Currency.USD,
        )

        row = serialize_portfolio_transaction(tx, owner_id)
        assert row["quantity"] == "0.000000000000000001"
        assert row["unit_price"] == "12345678901234567890.123456789"

        hydrated = hydrate_portfolio_transaction(row, owner_id)
        assert hydrated.quantity == tiny_decimal
        assert hydrated.unit_price == huge_decimal
        assert hydrated.economic_fingerprint() == tx.economic_fingerprint()

    def test_exponent_form_decimals_round_trip(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, instrument_id: UUID):
        """Phase 12B.2A.5: Codec must round-trip exponent-form Decimals, preserving numeric values and matching economic fingerprints."""
        # BUY with 1E+3 and 1E-8
        tx_buy = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 3, 1),
            recorded_at=datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc),
            instrument_id=instrument_id,
            quantity=Decimal("1E+3"),
            unit_price=Decimal("1E-8"),
            trade_currency=Currency.USD,
        )
        row_buy = serialize_portfolio_transaction(tx_buy, owner_id)
        hydrated_buy = hydrate_portfolio_transaction(row_buy, owner_id)
        assert hydrated_buy.quantity == tx_buy.quantity
        assert hydrated_buy.unit_price == tx_buy.unit_price
        assert hydrated_buy.economic_fingerprint() == tx_buy.economic_fingerprint()

        # FX with 1E+6 and 3.8123456789E+7
        tx_fx = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.FX_CONVERSION,
            effective_date=date(2026, 3, 2),
            recorded_at=datetime(2026, 3, 2, 10, 0, 0, tzinfo=timezone.utc),
            from_currency=Currency.USD,
            from_amount=Decimal("1E+6"),
            to_currency=Currency.TRY,
            to_amount=Decimal("3.8123456789E+7"),
        )
        row_fx = serialize_portfolio_transaction(tx_fx, owner_id)
        hydrated_fx = hydrate_portfolio_transaction(row_fx, owner_id)
        assert hydrated_fx.from_amount == tx_fx.from_amount
        assert hydrated_fx.to_amount == tx_fx.to_amount
        assert hydrated_fx.economic_fingerprint() == tx_fx.economic_fingerprint()

    def test_missing_status_and_priority_fails_closed(self, owner_id: UUID, portfolio_id: UUID):
        """Phase 12B.2A.5: Missing priority or status in row must fail closed (no silent default invention)."""
        valid_goal_row: Dict[str, Any] = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "owner_id": str(owner_id),
            "name": "Goal",
            "target_amount": "1000",
            "target_currency": "TRY",
            "priority": "high",
            "status": "active",
            "created_at": "2026-01-01T10:00:00Z",
        }

        # Missing priority
        row_no_priority = dict(valid_goal_row)
        del row_no_priority["priority"]
        with pytest.raises(KeyError, match="Missing required columns for InvestmentGoal"):
            hydrate_investment_goal(row_no_priority, owner_id)

        # Missing status
        row_no_status = dict(valid_goal_row)
        del row_no_status["status"]
        with pytest.raises(KeyError, match="Missing required columns for InvestmentGoal"):
            hydrate_investment_goal(row_no_status, owner_id)

        # PlannedContribution missing status
        valid_contrib_row: Dict[str, Any] = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "owner_id": str(owner_id),
            "expected_date": "2026-06-15",
            "amount": "5000",
            "currency": "TRY",
            "status": "planned",
            "created_at": "2026-01-01T10:00:00Z",
        }
        row_contrib_no_status = dict(valid_contrib_row)
        del row_contrib_no_status["status"]
        with pytest.raises(KeyError, match="Missing required columns for PlannedContribution"):
            hydrate_planned_contribution(row_contrib_no_status, owner_id)

    def test_strict_canonical_uuid_strings_on_hydration(self, owner_id: UUID, portfolio_id: UUID):
        """Phase 12B.2A.5: Only canonical lowercase hyphenated UUID strings are accepted on hydration."""
        base_row: Dict[str, Any] = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "owner_id": str(owner_id),
            "mode": "my_portfolio",
            "name": "Main Portfolio",
            "base_currency": "TRY",
            "created_at": "2026-01-01T10:00:00Z",
        }

        # Uppercase UUID rejected
        row_upper = dict(base_row, id=str(uuid4()).upper())
        with pytest.raises(ValueError, match="Non-canonical or invalid UUID string"):
            hydrate_portfolio(row_upper, owner_id)

        # Hyphenless UUID rejected
        row_nohyphen = dict(base_row, id=str(uuid4()).replace("-", ""))
        with pytest.raises(ValueError, match="Non-canonical or invalid UUID string"):
            hydrate_portfolio(row_nohyphen, owner_id)

        # Braces rejected
        row_braces = dict(base_row, id=f"{{{uuid4()}}}")
        with pytest.raises(ValueError, match="Non-canonical or invalid UUID string"):
            hydrate_portfolio(row_braces, owner_id)

        # Whitespace rejected
        row_ws = dict(base_row, id=f" {uuid4()} ")
        with pytest.raises(ValueError, match="Non-canonical or invalid UUID string"):
            hydrate_portfolio(row_ws, owner_id)

    def test_strict_canonical_date_strings_on_hydration(self, owner_id: UUID, portfolio_id: UUID):
        """Phase 12B.2A.5: Only canonical YYYY-MM-DD date strings are accepted."""
        valid_goal_row: Dict[str, Any] = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "owner_id": str(owner_id),
            "name": "Goal",
            "target_amount": "1000",
            "target_currency": "TRY",
            "target_date": "2026-12-31",
            "priority": "medium",
            "status": "active",
            "created_at": "2026-01-01T10:00:00Z",
        }

        # Alternative format (YYYYMMDD) rejected
        row_alt = dict(valid_goal_row, target_date="20261231")
        with pytest.raises(ValueError, match="canonical YYYY-MM-DD format"):
            hydrate_investment_goal(row_alt, owner_id)

        # Whitespace rejected
        row_ws = dict(valid_goal_row, target_date=" 2026-12-31 ")
        with pytest.raises(ValueError, match="canonical YYYY-MM-DD format"):
            hydrate_investment_goal(row_ws, owner_id)

    def test_outbound_mutation_validation_red_team(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID):
        """Phase 12B.2A.5: Serializers must fail closed if domain entity fields are mutated to invalid types/values."""
        # 1. Mutate goal.target_amount = float
        goal = InvestmentGoal(
            portfolio_id=portfolio_id,
            name="Goal",
            target_amount=Decimal("1000"),
            target_currency=Currency.TRY,
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        goal.target_amount = 1.25  # type: ignore
        with pytest.raises(TypeError, match="must be Decimal instance"):
            serialize_investment_goal(goal, owner_id)

        # 2. Mutate goal.target_amount = int
        goal.target_amount = 100  # type: ignore
        with pytest.raises(TypeError, match="must be Decimal instance"):
            serialize_investment_goal(goal, owner_id)

        # 3. Mutate goal.target_amount = bool
        goal.target_amount = True  # type: ignore
        with pytest.raises(TypeError, match="must be Decimal instance"):
            serialize_investment_goal(goal, owner_id)

        # 4. Mutate goal.target_amount = Decimal("NaN")
        goal.target_amount = Decimal("NaN")
        with pytest.raises(ValueError, match="must be finite Decimal"):
            serialize_investment_goal(goal, owner_id)

        # 5. Mutate contribution.amount = int
        contrib = PlannedContribution(
            portfolio_id=portfolio_id,
            expected_date=date(2026, 6, 15),
            amount=Decimal("5000"),
            currency=Currency.TRY,
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        contrib.amount = 5000  # type: ignore
        with pytest.raises(TypeError, match="must be Decimal instance"):
            serialize_planned_contribution(contrib, owner_id)

        # 6. Mutate portfolio.created_at = naive datetime
        port = Portfolio(
            mode=PortfolioMode.MY_PORTFOLIO,
            name="Portfolio",
            base_currency=Currency.TRY,
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        port.created_at = datetime(2026, 1, 1, 10, 0, 0)  # naive!
        with pytest.raises(ValueError, match="must be timezone-aware"):
            serialize_portfolio(port, owner_id)

        # 7. Mutate account.id = malformed string
        account = PortfolioAccount(
            portfolio_id=portfolio_id,
            name="Account",
            base_currency=Currency.USD,
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        account.id = "not-a-uuid"  # type: ignore
        with pytest.raises(TypeError, match="must be UUID instance"):
            serialize_portfolio_account(account, owner_id)

        # 8. Mutate cash_bucket.included_in_investable_assets = 1 (int)
        bucket = CashBucket(
            portfolio_id=portfolio_id,
            name="Bucket",
            currency=Currency.USD,
            purpose=CashPurpose.INVESTABLE,
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        bucket.included_in_investable_assets = 1  # type: ignore
        with pytest.raises(TypeError, match="must be strict bool"):
            serialize_cash_bucket(bucket, owner_id)

        # 9. Mutate goal.target_date = datetime
        goal_date = InvestmentGoal(
            portfolio_id=portfolio_id,
            name="Goal",
            target_amount=Decimal("1000"),
            target_currency=Currency.TRY,
            target_date=date(2026, 12, 31),
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        goal_date.target_date = datetime(2026, 12, 31, 0, 0, 0, tzinfo=timezone.utc)  # type: ignore
        with pytest.raises(TypeError, match="must be date instance"):
            serialize_investment_goal(goal_date, owner_id)

    def test_simulated_timestamptz_normalization_round_trip(self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, instrument_id: UUID):
        """
        Phase 12B.2A.6: When PostgreSQL normalizes executed_at (e.g. +03:00 -> UTC),
        hydrating the normalized instant matches the original economic fingerprint.
        """
        from datetime import timedelta
        tz_plus_3 = timezone(timedelta(hours=3))

        tx = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            executed_at=datetime(2026, 8, 28, 13, 0, 0, tzinfo=tz_plus_3),
            recorded_at=datetime(2026, 8, 28, 15, 0, 0, tzinfo=timezone.utc),
            instrument_id=instrument_id,
            quantity=Decimal("100"),
            unit_price=Decimal("50.00"),
            trade_currency=Currency.USD,
        )

        row = serialize_portfolio_transaction(tx, owner_id)
        original_fp = row["economic_fingerprint"]

        # Simulate PostgreSQL TIMESTAMPTZ representation change to UTC instant
        row_utc = dict(row, executed_at="2026-08-28T10:00:00+00:00")
        hydrated = hydrate_portfolio_transaction(row_utc, owner_id)
        assert hydrated.executed_at == datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc)
        assert hydrated.economic_fingerprint() == original_fp

        # Genuine instant change must fail fingerprint validation on hydration
        row_tampered = dict(row, executed_at="2026-08-28T10:00:01+00:00")
        with pytest.raises(ValueError, match="Economic fingerprint mismatch"):
            hydrate_portfolio_transaction(row_tampered, owner_id)

    def test_portfolio_provenance_outbound_mutation_rejection(self, owner_id: UUID):
        """
        Phase 12B.2A.6: Serializer must fail closed if a valid Portfolio is later mutated
        into an invalid state violating provenance rules.
        """
        # Case A: MY_PORTFOLIO mutated with source_portfolio_id -> rejected
        port_a = Portfolio(
            mode=PortfolioMode.MY_PORTFOLIO,
            name="Main Portfolio",
            base_currency=Currency.TRY,
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        port_a.source_portfolio_id = uuid4()
        with pytest.raises(ValueError, match="MY_PORTFOLIO cannot have source_portfolio_id"):
            serialize_portfolio(port_a, owner_id)

        # Case B: MY_PORTFOLIO mutated with source_snapshot_time -> rejected
        port_b = Portfolio(
            mode=PortfolioMode.MY_PORTFOLIO,
            name="Main Portfolio",
            base_currency=Currency.TRY,
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        port_b.source_snapshot_time = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        with pytest.raises(ValueError, match="MY_PORTFOLIO cannot have source_snapshot_time"):
            serialize_portfolio(port_b, owner_id)

        # Case C: SANDBOX mutated to have snapshot time without source portfolio -> rejected
        src_id = uuid4()
        port_c = Portfolio(
            mode=PortfolioMode.SANDBOX,
            name="Sandbox Portfolio",
            base_currency=Currency.TRY,
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
            source_portfolio_id=src_id,
            source_snapshot_time=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        port_c.source_portfolio_id = None
        with pytest.raises(ValueError, match="SANDBOX with source_snapshot_time must specify source_portfolio_id"):
            serialize_portfolio(port_c, owner_id)

        # Case D: SANDBOX mutated to reference self -> rejected
        port_d = Portfolio(
            mode=PortfolioMode.SANDBOX,
            name="Sandbox Portfolio",
            base_currency=Currency.TRY,
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        port_d.source_portfolio_id = port_d.id
        with pytest.raises(ValueError, match="SANDBOX source_portfolio_id cannot reference self"):
            serialize_portfolio(port_d, owner_id)

        # Case E: Unchanged valid MY_PORTFOLIO -> succeeds
        port_e = Portfolio(
            mode=PortfolioMode.MY_PORTFOLIO,
            name="Main Portfolio",
            base_currency=Currency.TRY,
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        row_e = serialize_portfolio(port_e, owner_id)
        assert row_e["mode"] == "my_portfolio"
        assert row_e["source_portfolio_id"] is None
        assert row_e["source_snapshot_time"] is None

        # Case F: Valid SANDBOX with legitimate distinct source portfolio -> succeeds
        port_f = Portfolio(
            mode=PortfolioMode.SANDBOX,
            name="Sandbox Portfolio",
            base_currency=Currency.TRY,
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
            source_portfolio_id=src_id,
            source_snapshot_time=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        row_f = serialize_portfolio(port_f, owner_id)
        assert row_f["mode"] == "sandbox"
        assert row_f["source_portfolio_id"] == str(src_id)
        assert row_f["source_snapshot_time"] == "2026-01-01T10:00:00+00:00"


