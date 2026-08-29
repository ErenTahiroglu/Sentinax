"""
backend/tests/test_portfolio_postgrest_transport.py
===================================================
Tests for Phase 12B.2B: Exact Supabase / PostgREST Numeric Transport Contract.

Verifies:
    1. Direct JSON numeric parsing via postgrest.APIResponse produces float/int,
       causing precision loss, and is strictly rejected by canonical codecs.
    2. PostgREST `::text` vertical-select casts preserve exact string representation.
    3. High precision (18 decimal places) and large precision values round-trip losslessly.
    4. Outbound write payloads emit exact string numbers (never float/int/bool/leaked Decimal).
    5. Accidental uncast NUMERIC responses fail closed across all 7 financial fields.
    6. Select projection constants are complete, explicit, and contain exact ::text casts.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
import json
from typing import Any, Dict
from uuid import UUID, uuid4

import httpx
from postgrest import APIResponse
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
    InvestmentGoal,
    PlannedContribution,
    PortfolioTransaction,
)
from backend.engine.private.portfolio.persistence import (
    hydrate_investment_goal,
    hydrate_planned_contribution,
    hydrate_portfolio_transaction,
    serialize_investment_goal,
    serialize_planned_contribution,
    serialize_portfolio_transaction,
)
from backend.engine.private.portfolio.postgrest_transport import (
    ALL_SEVEN_FINANCIAL_NUMERIC_COLUMNS,
    CASH_BUCKET_SELECT,
    FEE_TAX_ATTRIBUTION_EVENT_SELECT,
    FINANCIAL_NUMERIC_COLUMNS_BY_TABLE,
    INVESTMENT_GOAL_SELECT,
    PLANNED_CONTRIBUTION_SELECT,
    PORTFOLIO_ACCOUNT_SELECT,
    PORTFOLIO_SELECT,
    PORTFOLIO_TRANSACTION_SELECT,
)



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


# ─────────────────────────────────────────────────────────────────────────────
# 1. Direct JSON Numeric Is Unsafe (PostgREST Response Parser Tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestDirectJsonNumericUnsafe:
    """Proves direct JSON numeric parsing is lossy and rejected by canonical codecs."""

    def test_direct_json_float_precision_loss(self, owner_id: UUID, portfolio_id: UUID):
        """Direct JSON decimal parsing produces float with precision loss; codec rejects it."""
        raw_json = b'[{"amount": 12345678901234567890.123456789}]'
        http_req = httpx.Request("GET", "http://localhost/planned_contributions")
        http_resp = httpx.Response(200, content=raw_json, request=http_req)

        # Parse through installed PostgREST APIResponse path
        api_resp = APIResponse.from_http_request_response(http_resp)
        parsed_row = api_resp.data[0]

        # Parsed value became float with precision loss
        assert isinstance(parsed_row["amount"], float)
        assert parsed_row["amount"] != Decimal("12345678901234567890.123456789")

        # Codec fails closed on float
        row: Dict[str, Any] = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "owner_id": str(owner_id),
            "expected_date": "2026-06-15",
            "amount": parsed_row["amount"],  # float!
            "currency": "TRY",
            "status": "planned",
            "created_at": "2026-01-01T10:00:00Z",
        }
        with pytest.raises(TypeError, match="must be Decimal or exact decimal str, got float"):
            hydrate_planned_contribution(row, owner_id)

    def test_direct_json_large_integer_rejected(self, owner_id: UUID, portfolio_id: UUID):
        """Direct JSON integer NUMERIC is parsed as int; codec strictly rejects it."""
        raw_json = b'[{"amount": 123456789012345678901234567890}]'
        http_req = httpx.Request("GET", "http://localhost/planned_contributions")
        http_resp = httpx.Response(200, content=raw_json, request=http_req)

        api_resp = APIResponse.from_http_request_response(http_resp)
        parsed_row = api_resp.data[0]

        assert isinstance(parsed_row["amount"], int)

        row: Dict[str, Any] = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "owner_id": str(owner_id),
            "expected_date": "2026-06-15",
            "amount": parsed_row["amount"],  # int!
            "currency": "TRY",
            "status": "planned",
            "created_at": "2026-01-01T10:00:00Z",
        }
        with pytest.raises(TypeError, match="must be Decimal or exact decimal str, got int"):
            hydrate_planned_contribution(row, owner_id)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Safe Read Transport (PostgREST `::text` Cast Tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestSafeReadTransport:
    """Validates that PostgREST `::text` cast responses are exact strings and hydrate cleanly."""

    def test_safe_read_high_and_large_precision_transaction(
        self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, instrument_id: UUID
    ):
        """Simulates PostgREST response from PORTFOLIO_TRANSACTION_SELECT containing ::text casts."""
        tiny_str = "0.000000000000000001"
        huge_str = "12345678901234567890.123456789"

        # Construct raw JSON as emitted by PostgREST when column::text is selected
        row_dict = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "account_id": str(account_id),
            "owner_id": str(owner_id),
            "transaction_type": "buy",
            "effective_date": "2026-08-28",
            "executed_at": "2026-08-28T10:00:00+00:00",
            "recorded_at": "2026-08-28T10:00:00+00:00",
            "instrument_id": str(instrument_id),
            "quantity": tiny_str,
            "unit_price": huge_str,
            "trade_currency": "USD",
            "cash_amount": None,
            "cash_currency": None,
            "cash_bucket_id": None,
            "from_currency": None,
            "from_amount": None,
            "to_currency": None,
            "to_amount": None,
            "external_source": "MIDAS",
            "external_reference": "ORD-1234",
            "reverses_transaction_id": None,
            "notes": "Safe text read",
        }

        # Calculate canonical fingerprint matching these exact text values
        tx_expected = PortfolioTransaction(
            id=UUID(row_dict["id"]),
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            executed_at=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
            instrument_id=instrument_id,
            quantity=Decimal(tiny_str),
            unit_price=Decimal(huge_str),
            trade_currency=Currency.USD,
            external_source="MIDAS",
            external_reference="ORD-1234",
            notes="Safe text read",
        )
        row_dict["economic_fingerprint"] = tx_expected.economic_fingerprint()

        raw_json = json.dumps([row_dict]).encode("utf-8")
        http_req = httpx.Request("GET", "http://localhost/portfolio_transactions")
        http_resp = httpx.Response(200, content=raw_json, request=http_req)

        # Parse via PostgREST APIResponse
        api_resp = APIResponse.from_http_request_response(http_resp)
        response_row = api_resp.data[0]

        # Values remain exact Python str
        assert response_row["quantity"] == tiny_str
        assert response_row["unit_price"] == huge_str

        # Hydration succeeds with exact Decimal precision and verified fingerprint
        hydrated = hydrate_portfolio_transaction(response_row, owner_id)
        assert hydrated.quantity == Decimal(tiny_str)
        assert hydrated.unit_price == Decimal(huge_str)
        assert hydrated.economic_fingerprint() == tx_expected.economic_fingerprint()

    def test_safe_read_investment_goal(self, owner_id: UUID, portfolio_id: UUID):
        """Simulates PostgREST response from INVESTMENT_GOAL_SELECT."""
        target_str = "5000000.50"
        row_dict = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "owner_id": str(owner_id),
            "name": "Retirement Fund",
            "target_amount": target_str,
            "target_currency": "TRY",
            "target_date": "2035-12-31",
            "priority": "high",
            "status": "active",
            "created_at": "2026-01-01T10:00:00Z",
            "archived_at": None,
        }
        raw_json = json.dumps([row_dict]).encode("utf-8")
        http_req = httpx.Request("GET", "http://localhost/investment_goals")
        http_resp = httpx.Response(200, content=raw_json, request=http_req)

        api_resp = APIResponse.from_http_request_response(http_resp)
        response_row = api_resp.data[0]

        assert isinstance(response_row["target_amount"], str)
        hydrated = hydrate_investment_goal(response_row, owner_id)
        assert hydrated.target_amount == Decimal(target_str)

    def test_safe_read_planned_contribution(self, owner_id: UUID, portfolio_id: UUID):
        """Simulates PostgREST response from PLANNED_CONTRIBUTION_SELECT."""
        amount_str = "15000.00"
        row_dict = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "owner_id": str(owner_id),
            "goal_id": None,
            "cash_bucket_id": None,
            "expected_date": "2026-09-15",
            "amount": amount_str,
            "currency": "TRY",
            "status": "planned",
            "created_at": "2026-01-01T10:00:00Z",
        }
        raw_json = json.dumps([row_dict]).encode("utf-8")
        http_req = httpx.Request("GET", "http://localhost/planned_contributions")
        http_resp = httpx.Response(200, content=raw_json, request=http_req)

        api_resp = APIResponse.from_http_request_response(http_resp)
        response_row = api_resp.data[0]

        assert isinstance(response_row["amount"], str)
        hydrated = hydrate_planned_contribution(response_row, owner_id)
        assert hydrated.amount == Decimal(amount_str)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Safe Outbound Write Payloads (Phase 12B.2A Serializer Preservation)
# ─────────────────────────────────────────────────────────────────────────────

class TestSafeWriteTransport:
    """Validates that serializers emit exact strings for financial fields, never float/int/bool."""

    def test_write_payload_all_financial_entities(
        self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, instrument_id: UUID
    ):
        """Verifies outbound serialization for all financial entities produces exact str/None."""
        # 1. InvestmentGoal
        goal = InvestmentGoal(
            portfolio_id=portfolio_id,
            name="House Downpayment",
            target_amount=Decimal("1234567.89"),
            target_currency=Currency.TRY,
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        row_goal = serialize_investment_goal(goal, owner_id)
        assert isinstance(row_goal["target_amount"], str)
        assert row_goal["target_amount"] == "1234567.89"

        # 2. PlannedContribution
        contrib = PlannedContribution(
            portfolio_id=portfolio_id,
            expected_date=date(2026, 9, 1),
            amount=Decimal("25000.50"),
            currency=Currency.TRY,
            created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        row_contrib = serialize_planned_contribution(contrib, owner_id)
        assert isinstance(row_contrib["amount"], str)
        assert row_contrib["amount"] == "25000.50"

        # 3. BUY Transaction
        tx_buy = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_id=instrument_id,
            quantity=Decimal("100.50"),
            unit_price=Decimal("45.25"),
            trade_currency=Currency.USD,
            recorded_at=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
        )
        row_buy = serialize_portfolio_transaction(tx_buy, owner_id)
        assert row_buy["quantity"] == "100.50"
        assert row_buy["unit_price"] == "45.25"
        assert row_buy["cash_amount"] is None
        assert row_buy["from_amount"] is None
        assert row_buy["to_amount"] is None

        # 4. CASH_DEPOSIT Transaction
        tx_dep = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 28),
            cash_amount=Decimal("10000.00"),
            cash_currency=Currency.TRY,
            recorded_at=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
        )
        row_dep = serialize_portfolio_transaction(tx_dep, owner_id)
        assert row_dep["cash_amount"] == "10000.00"
        assert row_dep["quantity"] is None
        assert row_dep["unit_price"] is None

        # 5. FX_CONVERSION Transaction
        tx_fx = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.FX_CONVERSION,
            effective_date=date(2026, 8, 28),
            from_currency=Currency.USD,
            from_amount=Decimal("5000.00"),
            to_currency=Currency.TRY,
            to_amount=Decimal("190000.00"),
            recorded_at=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
        )
        row_fx = serialize_portfolio_transaction(tx_fx, owner_id)
        assert row_fx["from_amount"] == "5000.00"
        assert row_fx["to_amount"] == "190000.00"
        assert row_fx["quantity"] is None
        assert row_fx["unit_price"] is None

    def test_write_payload_exponent_decimals(
        self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, instrument_id: UUID
    ):
        """Verifies exponent Decimals serialize to fixed-point strings without float conversion."""
        tx = PortfolioTransaction(
            portfolio_id=portfolio_id,
            account_id=account_id,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_id=instrument_id,
            quantity=Decimal("1E+3"),
            unit_price=Decimal("1E-8"),
            trade_currency=Currency.USD,
            recorded_at=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
        )
        row = serialize_portfolio_transaction(tx, owner_id)
        assert row["quantity"] == "1000"
        assert row["unit_price"] == "0.00000001"
        assert isinstance(row["quantity"], str)
        assert isinstance(row["unit_price"], str)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Fail-Closed Guards Across All 7 Financial Fields
# ─────────────────────────────────────────────────────────────────────────────

class TestFailClosedGuards:
    """Verifies that accidental uncast float/int values fail closed on all 7 financial fields."""

    def test_uncast_numeric_fails_closed_across_all_seven_fields(
        self, owner_id: UUID, portfolio_id: UUID, account_id: UUID, instrument_id: UUID
    ):
        # 1. target_amount (float)
        row_goal: Dict[str, Any] = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "owner_id": str(owner_id),
            "name": "Goal",
            "target_amount": 1000.50,  # float!
            "target_currency": "TRY",
            "priority": "medium",
            "status": "active",
            "created_at": "2026-01-01T10:00:00Z",
        }
        with pytest.raises(TypeError, match="must be Decimal or exact decimal str, got float"):
            hydrate_investment_goal(row_goal, owner_id)

        # 2. amount (int)
        row_contrib: Dict[str, Any] = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "owner_id": str(owner_id),
            "expected_date": "2026-06-15",
            "amount": 5000,  # int!
            "currency": "TRY",
            "status": "planned",
            "created_at": "2026-01-01T10:00:00Z",
        }
        with pytest.raises(TypeError, match="must be Decimal or exact decimal str, got int"):
            hydrate_planned_contribution(row_contrib, owner_id)

        # Base transaction row for fields 3-7
        base_tx_row: Dict[str, Any] = {
            "id": str(uuid4()),
            "portfolio_id": str(portfolio_id),
            "account_id": str(account_id),
            "owner_id": str(owner_id),
            "effective_date": "2026-08-28",
            "recorded_at": "2026-08-28T10:00:00Z",
            "economic_fingerprint": "0" * 64,
        }

        # 3. quantity (float)
        row_qty = dict(base_tx_row, transaction_type="buy", instrument_id=str(instrument_id), quantity=100.0, unit_price="50.00", trade_currency="USD")
        with pytest.raises(TypeError, match="must be Decimal or exact decimal str, got float"):
            hydrate_portfolio_transaction(row_qty, owner_id)

        # 4. unit_price (int)
        row_price = dict(base_tx_row, transaction_type="buy", instrument_id=str(instrument_id), quantity="100", unit_price=50, trade_currency="USD")
        with pytest.raises(TypeError, match="must be Decimal or exact decimal str, got int"):
            hydrate_portfolio_transaction(row_price, owner_id)

        # 5. cash_amount (float)
        row_cash = dict(base_tx_row, transaction_type="cash_deposit", cash_amount=500.0, cash_currency="TRY")
        with pytest.raises(TypeError, match="must be Decimal or exact decimal str, got float"):
            hydrate_portfolio_transaction(row_cash, owner_id)

        # 6. from_amount (int)
        row_from = dict(base_tx_row, transaction_type="fx_conversion", from_currency="USD", from_amount=1000, to_currency="TRY", to_amount="38000.00")
        with pytest.raises(TypeError, match="must be Decimal or exact decimal str, got int"):
            hydrate_portfolio_transaction(row_from, owner_id)

        # 7. to_amount (float)
        row_to = dict(base_tx_row, transaction_type="fx_conversion", from_currency="USD", from_amount="1000.00", to_currency="TRY", to_amount=38000.0)
        with pytest.raises(TypeError, match="must be Decimal or exact decimal str, got float"):
            hydrate_portfolio_transaction(row_to, owner_id)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Static Contract Integrity of Select Constants
# ─────────────────────────────────────────────────────────────────────────────

class TestSelectContractIntegrity:
    """Verifies that all select projection constants are explicit, complete, and have ::text casts."""

    def test_no_wildcard_in_select_projections(self):
        """Wildcard select (*) is strictly forbidden."""
        for name, select_str in [
            ("PORTFOLIO_SELECT", PORTFOLIO_SELECT),
            ("PORTFOLIO_ACCOUNT_SELECT", PORTFOLIO_ACCOUNT_SELECT),
            ("CASH_BUCKET_SELECT", CASH_BUCKET_SELECT),
            ("INVESTMENT_GOAL_SELECT", INVESTMENT_GOAL_SELECT),
            ("PLANNED_CONTRIBUTION_SELECT", PLANNED_CONTRIBUTION_SELECT),
            ("PORTFOLIO_TRANSACTION_SELECT", PORTFOLIO_TRANSACTION_SELECT),
            ("FEE_TAX_ATTRIBUTION_EVENT_SELECT", FEE_TAX_ATTRIBUTION_EVENT_SELECT),
        ]:
            assert "*" not in select_str, f"{name} contains wildcard select '*'"

    def test_all_seven_financial_numeric_fields_have_text_cast(self):
        """All 7 financial NUMERIC fields must be explicitly selected with ::text."""
        assert "target_amount::text" in INVESTMENT_GOAL_SELECT
        assert "amount::text" in PLANNED_CONTRIBUTION_SELECT
        assert "quantity::text" in PORTFOLIO_TRANSACTION_SELECT
        assert "unit_price::text" in PORTFOLIO_TRANSACTION_SELECT
        assert "cash_amount::text" in PORTFOLIO_TRANSACTION_SELECT
        assert "from_amount::text" in PORTFOLIO_TRANSACTION_SELECT
        assert "to_amount::text" in PORTFOLIO_TRANSACTION_SELECT
        assert "allocated_amount::text" in FEE_TAX_ATTRIBUTION_EVENT_SELECT

        # Total count across whitelist
        assert len(ALL_SEVEN_FINANCIAL_NUMERIC_COLUMNS) == 7
        assert sum(len(cols) for cols in FINANCIAL_NUMERIC_COLUMNS_BY_TABLE.values()) == 8


    def test_select_projections_satisfy_codec_hydration_requirements(self):
        """Every select projection contains all required columns for its hydrator."""
        # 1. Portfolio
        portfolio_cols = {c.split("::")[0].strip() for c in PORTFOLIO_SELECT.split(",")}
        assert {"id", "owner_id", "mode", "name", "base_currency", "created_at"}.issubset(portfolio_cols)

        # 2. PortfolioAccount
        account_cols = {c.split("::")[0].strip() for c in PORTFOLIO_ACCOUNT_SELECT.split(",")}
        assert {"id", "portfolio_id", "owner_id", "name", "base_currency", "created_at"}.issubset(account_cols)

        # 3. CashBucket
        bucket_cols = {c.split("::")[0].strip() for c in CASH_BUCKET_SELECT.split(",")}
        assert {"id", "portfolio_id", "owner_id", "name", "currency", "purpose", "included_in_investable_assets", "created_at"}.issubset(bucket_cols)

        # 4. InvestmentGoal
        goal_cols = {c.split("::")[0].strip() for c in INVESTMENT_GOAL_SELECT.split(",")}
        assert {"id", "portfolio_id", "owner_id", "name", "target_amount", "target_currency", "priority", "status", "created_at"}.issubset(goal_cols)

        # 5. PlannedContribution
        contrib_cols = {c.split("::")[0].strip() for c in PLANNED_CONTRIBUTION_SELECT.split(",")}
        assert {"id", "portfolio_id", "owner_id", "expected_date", "amount", "currency", "status", "created_at"}.issubset(contrib_cols)

        # 6. PortfolioTransaction
        tx_cols = {c.split("::")[0].strip() for c in PORTFOLIO_TRANSACTION_SELECT.split(",")}
        assert {"id", "portfolio_id", "account_id", "owner_id", "transaction_type", "effective_date", "recorded_at", "economic_fingerprint"}.issubset(tx_cols)
