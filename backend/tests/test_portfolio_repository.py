"""
backend/tests/test_portfolio_repository.py
=========================================
Tests for Phase 12B.2C: Owner-Scoped Supabase Repository & Race-Safe Immutable Transaction Append.

Covers:
    1. Strict Owner Binding & Isolation (defense-in-depth against service-role bypass).
    2. Safe Read/Write Transport (explicit select projections, ::text casts, minimal inserts).
    3. Lifecycle Entities (create, read, list, parent-consistency, zero cash from PlannedContribution).
    4. System-Controlled recorded_at & Clock Authority.
    5. Transaction ID Idempotency (first append, duplicate replay, conflict).
    6. External Normalized Idempotency & Lexical Invariance (Migration 012 RPC integration).
    7. Reversal Lifecycle & Preflight.
    8. Concurrent Database Race Resolution (SQLSTATE 23505 deterministic readbacks & fail-closed).
    9. Error Propagation on Non-23505 APIErrors.
   10. Deterministic Pagination & Ordering.
   11. Mode & Portfolio Isolation (MY_PORTFOLIO vs SANDBOX).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional
from uuid import UUID, uuid4

from postgrest.exceptions import APIError
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
from backend.engine.private.portfolio.ledger import AppendResult, AppendStatus
from backend.engine.private.portfolio.models import (
    CashBucket,
    InvestmentGoal,
    PlannedContribution,
    Portfolio,
    PortfolioAccount,
    PortfolioTransaction,
)
from backend.engine.private.portfolio.postgrest_transport import (
    CASH_BUCKET_SELECT,
    INVESTMENT_GOAL_SELECT,
    PLANNED_CONTRIBUTION_SELECT,
    PORTFOLIO_ACCOUNT_SELECT,
    PORTFOLIO_SELECT,
    PORTFOLIO_TRANSACTION_SELECT,
)
from backend.engine.private.portfolio.repository import PortfolioRepository


# ─────────────────────────────────────────────────────────────────────────────
# Factory Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_portfolio(
    mode: PortfolioMode = PortfolioMode.MY_PORTFOLIO,
    name: str = "Test Portfolio",
    base_currency: Currency = Currency.USD,
    created_at: Optional[datetime] = None,
    id: Optional[UUID] = None,
    source_portfolio_id: Optional[UUID] = None,
    source_snapshot_time: Optional[datetime] = None,
) -> Portfolio:
    return Portfolio(
        mode=mode,
        name=name,
        base_currency=base_currency,
        created_at=created_at or datetime.now(timezone.utc),
        id=id or uuid4(),
        source_portfolio_id=source_portfolio_id,
        source_snapshot_time=source_snapshot_time,
    )


def make_account(
    portfolio_id: UUID,
    name: str = "Test Account",
    base_currency: Currency = Currency.USD,
    created_at: Optional[datetime] = None,
    id: Optional[UUID] = None,
    broker_label: Optional[str] = None,
) -> PortfolioAccount:
    return PortfolioAccount(
        portfolio_id=portfolio_id,
        name=name,
        base_currency=base_currency,
        created_at=created_at or datetime.now(timezone.utc),
        id=id or uuid4(),
        broker_label=broker_label,
    )


def make_bucket(
    portfolio_id: UUID,
    name: str = "Test Bucket",
    currency: Currency = Currency.USD,
    purpose: CashPurpose = CashPurpose.INVESTABLE,
    created_at: Optional[datetime] = None,
    id: Optional[UUID] = None,
    account_id: Optional[UUID] = None,
    included_in_investable_assets: Optional[bool] = None,
) -> CashBucket:
    return CashBucket(
        portfolio_id=portfolio_id,
        name=name,
        currency=currency,
        purpose=purpose,
        created_at=created_at or datetime.now(timezone.utc),
        id=id or uuid4(),
        account_id=account_id,
        included_in_investable_assets=included_in_investable_assets,
    )


def make_goal(
    portfolio_id: UUID,
    name: str = "Test Goal",
    target_amount: Decimal = Decimal("100000.00"),
    target_currency: Currency = Currency.USD,
    created_at: Optional[datetime] = None,
    id: Optional[UUID] = None,
    priority: GoalPriority = GoalPriority.MEDIUM,
    status: GoalStatus = GoalStatus.ACTIVE,
) -> InvestmentGoal:
    return InvestmentGoal(
        portfolio_id=portfolio_id,
        name=name,
        target_amount=target_amount,
        target_currency=target_currency,
        created_at=created_at or datetime.now(timezone.utc),
        id=id or uuid4(),
        priority=priority,
        status=status,
    )


def make_contribution(
    portfolio_id: UUID,
    amount: Decimal = Decimal("5000.00"),
    currency: Currency = Currency.USD,
    expected_date: Optional[date] = None,
    created_at: Optional[datetime] = None,
    id: Optional[UUID] = None,
    goal_id: Optional[UUID] = None,
    cash_bucket_id: Optional[UUID] = None,
    status: ContributionStatus = ContributionStatus.PLANNED,
) -> PlannedContribution:
    return PlannedContribution(
        portfolio_id=portfolio_id,
        expected_date=expected_date or date(2026, 9, 1),
        amount=amount,
        currency=currency,
        created_at=created_at or datetime.now(timezone.utc),
        id=id or uuid4(),
        goal_id=goal_id,
        cash_bucket_id=cash_bucket_id,
        status=status,
    )


def make_transaction(
    portfolio_id: UUID,
    account_id: UUID,
    transaction_type: TransactionType = TransactionType.CASH_DEPOSIT,
    effective_date: Optional[date] = None,
    recorded_at: Optional[datetime] = None,
    id: Optional[UUID] = None,
    **kwargs: Any,
) -> PortfolioTransaction:
    return PortfolioTransaction(
        portfolio_id=portfolio_id,
        account_id=account_id,
        transaction_type=transaction_type,
        effective_date=effective_date or date(2026, 8, 28),
        recorded_at=recorded_at or datetime.now(timezone.utc),
        id=id or uuid4(),
        **kwargs,
    )


# ─────────────────────────────────────────────────────────────────────────────
# In-Memory Mock Supabase / PostgREST Client
# ─────────────────────────────────────────────────────────────────────────────

class MockQueryResult:
    def __init__(self, data: Any, count: Optional[int] = None):
        self.data = data
        self.count = count


class MockQueryBuilder:
    def __init__(self, table_name: str, client_store: MockSupabaseClient):
        self.table_name = table_name
        self.client_store = client_store
        self.projection: Optional[str] = None
        self.eq_filters: Dict[str, Any] = {}
        self.is_filters: Dict[str, Any] = {}
        self.range_start: Optional[int] = None
        self.range_end: Optional[int] = None
        self.insert_payload: Optional[Any] = None
        self.returning_mode: Optional[str] = None

    def select(self, projection: str) -> MockQueryBuilder:
        self.projection = projection
        self.client_store.recorded_selects.append((self.table_name, projection))
        return self

    def eq(self, column: str, value: Any) -> MockQueryBuilder:
        self.eq_filters[column] = value
        return self

    def is_(self, column: str, value: Any) -> MockQueryBuilder:
        self.is_filters[column] = value
        return self

    def range(self, start: int, end: int) -> MockQueryBuilder:
        self.range_start = start
        self.range_end = end
        return self

    def insert(self, json_data: Any, returning: str = "representation") -> MockQueryBuilder:
        self.insert_payload = json_data
        self.returning_mode = returning
        self.client_store.recorded_inserts.append((self.table_name, json_data, returning))
        return self

    def execute(self) -> MockQueryResult:
        if self.insert_payload is not None and self.client_store.next_insert_error is not None:
            err = self.client_store.next_insert_error
            self.client_store.next_insert_error = None
            raise err

        if self.client_store.next_error is not None:
            err = self.client_store.next_error
            self.client_store.next_error = None
            raise err

        if self.insert_payload is not None:
            row = deepcopy(self.insert_payload)
            table_rows = self.client_store.tables.setdefault(self.table_name, [])
            table_rows.append(row)
            return MockQueryResult(data=[] if self.returning_mode == "minimal" else [row])

        table_rows = self.client_store.tables.get(self.table_name, [])
        filtered: List[Dict[str, Any]] = []

        for r in table_rows:
            match = True
            for col, val in self.eq_filters.items():
                if str(r.get(col)) != str(val):
                    match = False
                    break
            if not match:
                continue

            for col, val in self.is_filters.items():
                if val == "null" and r.get(col) is not None:
                    match = False
                    break
            if not match:
                continue

            filtered.append(deepcopy(r))

        if self.range_start is not None and self.range_end is not None:
            paged = filtered[self.range_start : self.range_end + 1]
            return MockQueryResult(data=paged, count=len(filtered))

        return MockQueryResult(data=filtered, count=len(filtered))


class MockRpcBuilder:
    def __init__(self, fn_name: str, params: Dict[str, Any], client_store: MockSupabaseClient):
        self.fn_name = fn_name
        self.params = params
        self.client_store = client_store

    def execute(self) -> MockQueryResult:
        self.client_store.recorded_rpcs.append((self.fn_name, self.params))
        if self.client_store.next_error is not None:
            err = self.client_store.next_error
            self.client_store.next_error = None
            raise err

        if self.fn_name == "lookup_portfolio_transaction_external_identity":
            owner_id = self.params.get("p_owner_id")
            portfolio_id = self.params.get("p_portfolio_id")
            account_id = self.params.get("p_account_id")
            source = (self.params.get("p_external_source") or "").strip().upper()
            ref = (self.params.get("p_external_reference") or "").strip()

            txs = self.client_store.tables.get("portfolio_transactions", [])
            for tx in txs:
                if (
                    str(tx.get("owner_id")) == str(owner_id)
                    and str(tx.get("portfolio_id")) == str(portfolio_id)
                    and str(tx.get("account_id")) == str(account_id)
                    and tx.get("external_source")
                    and tx.get("external_reference")
                    and tx.get("external_source").strip().upper() == source
                    and tx.get("external_reference").strip() == ref
                ):
                    return MockQueryResult(data=tx["id"])
            return MockQueryResult(data=None)

        return MockQueryResult(data=None)


class MockSupabaseClient:
    def __init__(self):
        self.tables: Dict[str, List[Dict[str, Any]]] = {}
        self.recorded_inserts: List[Any] = []
        self.recorded_selects: List[Any] = []
        self.recorded_rpcs: List[Any] = []
        self.next_error: Optional[Exception] = None
        self.next_insert_error: Optional[Exception] = None

    def table(self, table_name: str) -> MockQueryBuilder:
        return MockQueryBuilder(table_name, self)

    def rpc(self, fn_name: str, params: Optional[Dict[str, Any]] = None) -> MockRpcBuilder:
        return MockRpcBuilder(fn_name, params or {}, self)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_client() -> MockSupabaseClient:
    return MockSupabaseClient()


@pytest.fixture
def owner_id() -> UUID:
    return uuid4()


@pytest.fixture
def repo(mock_client: MockSupabaseClient, owner_id: UUID) -> PortfolioRepository:
    return PortfolioRepository(client=mock_client, owner_id=owner_id)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Strict Owner Binding & Isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestOwnerBindingAndIsolation:
    """Verifies strict owner binding at construction and explicit owner filters on all operations."""

    def test_canonical_owner_types_accepted(self, mock_client: MockSupabaseClient, owner_id: UUID):
        r1 = PortfolioRepository(client=mock_client, owner_id=owner_id)
        assert r1.owner_id == owner_id
        assert r1.owner_id_str == str(owner_id)

        r2 = PortfolioRepository(client=mock_client, owner_id=str(owner_id))
        assert r2.owner_id == owner_id
        assert r2.owner_id_str == str(owner_id)

    @pytest.mark.parametrize(
        "bad_owner",
        [
            None,
            True,
            False,
            12345,
            "",
            "   ",
            "550e8400-e29b-41d4-a716-446655440000\n",
            "550E8400-E29B-41D4-A716-446655440000",
            "not-a-uuid",
        ],
    )
    def test_invalid_owner_rejected(self, mock_client: MockSupabaseClient, bad_owner: Any):
        with pytest.raises((TypeError, ValueError)):
            PortfolioRepository(client=mock_client, owner_id=bad_owner)

    def test_every_read_and_write_strictly_uses_bound_owner(
        self, mock_client: MockSupabaseClient, repo: PortfolioRepository, owner_id: UUID
    ):
        p = make_portfolio(name="My Wealth", base_currency=Currency.TRY)
        created = repo.create_portfolio(p)
        assert created.id == p.id

        tbl, payload, ret = mock_client.recorded_inserts[-1]
        assert tbl == "portfolios"
        assert payload["owner_id"] == str(owner_id)
        assert ret == "minimal"

        other_id = uuid4()
        res = repo.get_portfolio(other_id)
        assert res is None

        last_select_table, last_select_proj = mock_client.recorded_selects[-1]
        assert last_select_table == "portfolios"
        assert last_select_proj == PORTFOLIO_SELECT


# ─────────────────────────────────────────────────────────────────────────────
# 2. Lifecycle Entities & Parent-Child Pre-Write Consistency
# ─────────────────────────────────────────────────────────────────────────────

class TestLifecycleEntitiesAndConsistency:
    """Verifies creation, hydration, consistency, and zero cash side-effects for all entities."""

    def test_full_portfolio_and_account_lifecycle(self, repo: PortfolioRepository):
        port = repo.create_portfolio(make_portfolio(name="Investments", base_currency=Currency.USD))
        assert port.name == "Investments"

        acc = repo.create_portfolio_account(
            make_account(
                portfolio_id=port.id,
                name="Interactive Brokers",
                base_currency=Currency.USD,
                broker_label="IBKR",
            )
        )
        assert acc.name == "Interactive Brokers"
        assert acc.portfolio_id == port.id

        fetched_acc = repo.get_portfolio_account(port.id, acc.id)
        assert fetched_acc is not None
        assert fetched_acc.id == acc.id

        accounts = repo.list_portfolio_accounts(port.id)
        assert len(accounts) == 1
        assert accounts[0].id == acc.id

    def test_cash_bucket_lifecycle_and_account_consistency(self, repo: PortfolioRepository):
        port = repo.create_portfolio(make_portfolio(name="Port", base_currency=Currency.TRY))
        acc = repo.create_portfolio_account(
            make_account(portfolio_id=port.id, name="Acc", base_currency=Currency.TRY)
        )

        b1 = repo.create_cash_bucket(
            make_bucket(
                portfolio_id=port.id,
                account_id=acc.id,
                name="TRY Cash",
                currency=Currency.TRY,
                purpose=CashPurpose.INVESTABLE,
                included_in_investable_assets=True,
            )
        )
        assert b1.name == "TRY Cash"

        b2 = repo.create_cash_bucket(
            make_bucket(
                portfolio_id=port.id,
                account_id=None,
                name="Emergency TRY",
                currency=Currency.TRY,
                purpose=CashPurpose.EMERGENCY_RESERVE,
                included_in_investable_assets=False,
            )
        )
        assert b2.account_id is None

        other_port = repo.create_portfolio(make_portfolio(name="Other", base_currency=Currency.TRY))
        with pytest.raises(ValueError, match="does not exist in portfolio"):
            repo.create_cash_bucket(
                make_bucket(
                    portfolio_id=other_port.id,
                    account_id=acc.id,
                    name="Invalid Bucket",
                    currency=Currency.TRY,
                    purpose=CashPurpose.INVESTABLE,
                    included_in_investable_assets=True,
                )
            )

    def test_investment_goal_lifecycle(self, repo: PortfolioRepository):
        port = repo.create_portfolio(make_portfolio(name="Retirement", base_currency=Currency.USD))
        goal = repo.create_investment_goal(
            make_goal(
                portfolio_id=port.id,
                name="Early Retirement",
                target_amount=Decimal("1000000.00"),
                target_currency=Currency.USD,
                priority=GoalPriority.HIGH,
            )
        )
        assert goal.target_amount == Decimal("1000000.00")
        assert repo.get_investment_goal(port.id, goal.id) is not None

    def test_planned_contribution_causes_zero_transaction_writes(
        self, repo: PortfolioRepository, mock_client: MockSupabaseClient
    ):
        """Phase 12B.2C critical invariant: PlannedContribution has ZERO transaction or cash side-effects."""
        port = repo.create_portfolio(make_portfolio(name="Port", base_currency=Currency.TRY))
        contrib = repo.create_planned_contribution(
            make_contribution(
                portfolio_id=port.id,
                expected_date=date(2026, 9, 1),
                amount=Decimal("50000.00"),
                currency=Currency.TRY,
                status=ContributionStatus.RECEIVED,
            )
        )
        assert contrib.amount == Decimal("50000.00")

        tx_inserts = [
            ins for ins in mock_client.recorded_inserts if ins[0] == "portfolio_transactions"
        ]
        assert len(tx_inserts) == 0, "PlannedContribution must NOT write to portfolio_transactions"

    def test_sandbox_source_portfolio_consistency(
        self, repo: PortfolioRepository, mock_client: MockSupabaseClient
    ):
        prod_port = repo.create_portfolio(make_portfolio(name="Prod", base_currency=Currency.TRY))
        sandbox = repo.create_portfolio(
            make_portfolio(
                mode=PortfolioMode.SANDBOX,
                name="Sandbox 1",
                base_currency=Currency.TRY,
                source_portfolio_id=prod_port.id,
                source_snapshot_time=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
            )
        )
        assert sandbox.mode == PortfolioMode.SANDBOX

        with pytest.raises(ValueError, match="does not exist under bound owner"):
            repo.create_portfolio(
                make_portfolio(
                    mode=PortfolioMode.SANDBOX,
                    name="Sandbox 2",
                    base_currency=Currency.TRY,
                    source_portfolio_id=uuid4(),
                    source_snapshot_time=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
                )
            )


# ─────────────────────────────────────────────────────────────────────────────
# 3. System-Controlled recorded_at Clock Authority
# ─────────────────────────────────────────────────────────────────────────────

class TestRecordedAtClockAuthority:
    """Verifies repository overrides caller recorded_at with injected clock."""

    def test_caller_recorded_at_replaced_by_repository_clock(
        self, mock_client: MockSupabaseClient, owner_id: UUID
    ):
        fixed_now = datetime(2026, 8, 28, 12, 34, 56, tzinfo=timezone.utc)
        repo = PortfolioRepository(client=mock_client, owner_id=owner_id, clock=lambda: fixed_now)

        port = repo.create_portfolio(make_portfolio(name="Port", base_currency=Currency.USD))
        acc = repo.create_portfolio_account(
            make_account(portfolio_id=port.id, name="Acc", base_currency=Currency.USD)
        )

        ancient_caller_time = datetime(1999, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        tx = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 28),
            cash_amount=Decimal("1000.00"),
            cash_currency=Currency.USD,
            recorded_at=ancient_caller_time,
        )

        result = repo.append_transaction(tx)
        assert result.status == AppendStatus.APPENDED

        persisted = repo.get_transaction(port.id, tx.id)
        assert persisted is not None
        assert persisted.recorded_at == fixed_now
        assert persisted.recorded_at != ancient_caller_time
        assert persisted.economic_fingerprint() == tx.economic_fingerprint()

    def test_naive_clock_fails_closed(self, mock_client: MockSupabaseClient, owner_id: UUID):
        naive_clock = lambda: datetime(2026, 8, 28, 10, 0, 0)
        repo = PortfolioRepository(client=mock_client, owner_id=owner_id, clock=naive_clock)

        port = repo.create_portfolio(make_portfolio(name="Port", base_currency=Currency.USD))
        acc = repo.create_portfolio_account(
            make_account(portfolio_id=port.id, name="Acc", base_currency=Currency.USD)
        )

        tx = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 28),
            cash_amount=Decimal("100.00"),
            cash_currency=Currency.USD,
        )
        with pytest.raises(ValueError, match="naive datetime"):
            repo.append_transaction(tx)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Transaction Append & Idempotency Matrix
# ─────────────────────────────────────────────────────────────────────────────

class TestTransactionAppendAndIdempotency:
    """Verifies physical ID, external normalized identity, and manual idempotency semantics."""

    def test_physical_id_idempotency_and_conflict(self, repo: PortfolioRepository):
        port = repo.create_portfolio(make_portfolio(name="Port", base_currency=Currency.USD))
        acc = repo.create_portfolio_account(
            make_account(portfolio_id=port.id, name="Acc", base_currency=Currency.USD)
        )

        tx_id = uuid4()
        tx1 = make_transaction(
            id=tx_id,
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 28),
            cash_amount=Decimal("1000.00"),
            cash_currency=Currency.USD,
        )

        res1 = repo.append_transaction(tx1)
        assert res1.status == AppendStatus.APPENDED
        assert res1.transaction_id == tx_id

        res2 = repo.append_transaction(tx1)
        assert res2.status == AppendStatus.IDEMPOTENT_DUPLICATE
        assert res2.transaction_id == tx_id

        tx_modified = replace(tx1, cash_amount=Decimal("2000.00"))
        res3 = repo.append_transaction(tx_modified)
        assert res3.status == AppendStatus.CONFLICT

    def test_external_normalized_idempotency_lexical_replay(self, repo: PortfolioRepository):
        port = repo.create_portfolio(make_portfolio(name="Port", base_currency=Currency.USD))
        acc = repo.create_portfolio_account(
            make_account(portfolio_id=port.id, name="Acc", base_currency=Currency.USD)
        )
        inst_id = uuid4()

        tx1 = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_id=inst_id,
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
            external_source="MIDAS",
            external_reference="ORD-001",
        )
        res1 = repo.append_transaction(tx1)
        assert res1.status == AppendStatus.APPENDED

        tx2 = make_transaction(
            id=uuid4(),
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_id=inst_id,
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
            external_source=" midas ",
            external_reference=" ORD-001 ",
        )
        res2 = repo.append_transaction(tx2)
        assert res2.status == AppendStatus.IDEMPOTENT_DUPLICATE
        assert res2.transaction_id == tx1.id

        tx3 = replace(tx2, unit_price=Decimal("200.00"))
        res3 = repo.append_transaction(tx3)
        assert res3.status == AppendStatus.CONFLICT

    def test_manual_transactions_with_identical_economics_both_append(
        self, repo: PortfolioRepository
    ):
        port = repo.create_portfolio(make_portfolio(name="Port", base_currency=Currency.USD))
        acc = repo.create_portfolio_account(
            make_account(portfolio_id=port.id, name="Acc", base_currency=Currency.USD)
        )

        tx1 = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 28),
            cash_amount=Decimal("500.00"),
            cash_currency=Currency.USD,
        )
        tx2 = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 28),
            cash_amount=Decimal("500.00"),
            cash_currency=Currency.USD,
        )

        res1 = repo.append_transaction(tx1)
        res2 = repo.append_transaction(tx2)

        assert res1.status == AppendStatus.APPENDED
        assert res2.status == AppendStatus.APPENDED
        assert res1.transaction_id != res2.transaction_id


# ─────────────────────────────────────────────────────────────────────────────
# 5. Reversals
# ─────────────────────────────────────────────────────────────────────────────

class TestReversals:
    """Verifies reversal validation, single-reversal enforcement, and error reporting."""

    def test_valid_reversal_and_single_reversal_guard(self, repo: PortfolioRepository):
        port = repo.create_portfolio(make_portfolio(name="Port", base_currency=Currency.USD))
        acc = repo.create_portfolio_account(
            make_account(portfolio_id=port.id, name="Acc", base_currency=Currency.USD)
        )

        orig = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 28),
            cash_amount=Decimal("1000.00"),
            cash_currency=Currency.USD,
        )
        res_orig = repo.append_transaction(orig)
        assert res_orig.status == AppendStatus.APPENDED

        rev1 = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.REVERSAL,
            effective_date=date(2026, 8, 29),
            reverses_transaction_id=orig.id,
        )
        res_rev1 = repo.append_transaction(rev1)
        assert res_rev1.status == AppendStatus.APPENDED

        rev2 = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.REVERSAL,
            effective_date=date(2026, 8, 30),
            reverses_transaction_id=orig.id,
        )
        res_rev2 = repo.append_transaction(rev2)
        assert res_rev2.status == AppendStatus.INVALID
        assert "already been reversed" in res_rev2.diagnostics[0]

        rev_of_rev = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.REVERSAL,
            effective_date=date(2026, 8, 31),
            reverses_transaction_id=rev1.id,
        )
        res_ror = repo.append_transaction(rev_of_rev)
        assert res_ror.status == AppendStatus.INVALID
        assert "itself a REVERSAL" in res_ror.diagnostics[0]


# ─────────────────────────────────────────────────────────────────────────────
# 6. Database Race Handling (SQLSTATE 23505) & Errors
# ─────────────────────────────────────────────────────────────────────────────

class TestDatabaseRaceAndErrors:
    """Verifies deterministic resolution of concurrent 23505 uniqueness violations."""

    def test_23505_race_physical_id_resolution_same_economics(
        self, repo: PortfolioRepository, mock_client: MockSupabaseClient
    ):
        port = repo.create_portfolio(make_portfolio(name="Port", base_currency=Currency.USD))
        acc = repo.create_portfolio_account(
            make_account(portfolio_id=port.id, name="Acc", base_currency=Currency.USD)
        )

        tx = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 28),
            cash_amount=Decimal("1000.00"),
            cash_currency=Currency.USD,
        )

        # First insert succeeds
        repo.append_transaction(tx)

        # Concurrent attempt where insert raises 23505
        mock_client.next_insert_error = APIError({"code": "23505", "message": "duplicate key value"})
        res = repo.append_transaction(tx)
        assert res.status == AppendStatus.IDEMPOTENT_DUPLICATE
        assert res.transaction_id == tx.id

    def test_23505_race_physical_id_resolution_different_economics(
        self, repo: PortfolioRepository, mock_client: MockSupabaseClient
    ):
        port = repo.create_portfolio(make_portfolio(name="Port", base_currency=Currency.USD))
        acc = repo.create_portfolio_account(
            make_account(portfolio_id=port.id, name="Acc", base_currency=Currency.USD)
        )

        tx_id = uuid4()
        tx1 = make_transaction(
            id=tx_id,
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 28),
            cash_amount=Decimal("1000.00"),
            cash_currency=Currency.USD,
        )
        repo.append_transaction(tx1)

        # Second worker attempts same ID with different cash_amount
        tx2 = make_transaction(
            id=tx_id,
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 28),
            cash_amount=Decimal("2000.00"),
            cash_currency=Currency.USD,
        )
        mock_client.next_insert_error = APIError({"code": "23505", "message": "duplicate key value"})
        res = repo.append_transaction(tx2)
        assert res.status == AppendStatus.CONFLICT

    def test_23505_race_external_identity_same_economics(
        self, repo: PortfolioRepository, mock_client: MockSupabaseClient
    ):
        port = repo.create_portfolio(make_portfolio(name="Port", base_currency=Currency.USD))
        acc = repo.create_portfolio_account(
            make_account(portfolio_id=port.id, name="Acc", base_currency=Currency.USD)
        )
        inst_id = uuid4()

        tx1 = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_id=inst_id,
            quantity=Decimal("10"),
            unit_price=Decimal("100.00"),
            trade_currency=Currency.USD,
            external_source="MIDAS",
            external_reference="ORD-99",
        )
        repo.append_transaction(tx1)

        # Worker 2 attempts same external identity concurrently
        tx2 = make_transaction(
            id=uuid4(),
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_id=inst_id,
            quantity=Decimal("10"),
            unit_price=Decimal("100.00"),
            trade_currency=Currency.USD,
            external_source=" midas ",
            external_reference=" ORD-99 ",
        )
        mock_client.next_insert_error = APIError({"code": "23505", "message": "duplicate key value"})
        res = repo.append_transaction(tx2)
        assert res.status == AppendStatus.IDEMPOTENT_DUPLICATE
        assert res.transaction_id == tx1.id

    def test_23505_race_external_identity_different_economics(
        self, repo: PortfolioRepository, mock_client: MockSupabaseClient
    ):
        port = repo.create_portfolio(make_portfolio(name="Port", base_currency=Currency.USD))
        acc = repo.create_portfolio_account(
            make_account(portfolio_id=port.id, name="Acc", base_currency=Currency.USD)
        )
        inst_id = uuid4()

        tx1 = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_id=inst_id,
            quantity=Decimal("10"),
            unit_price=Decimal("100.00"),
            trade_currency=Currency.USD,
            external_source="MIDAS",
            external_reference="ORD-99",
        )
        repo.append_transaction(tx1)

        tx2 = make_transaction(
            id=uuid4(),
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_id=inst_id,
            quantity=Decimal("10"),
            unit_price=Decimal("200.00"),  # Different economics!
            trade_currency=Currency.USD,
            external_source="MIDAS",
            external_reference="ORD-99",
        )
        mock_client.next_insert_error = APIError({"code": "23505", "message": "duplicate key value"})
        res = repo.append_transaction(tx2)
        assert res.status == AppendStatus.CONFLICT

    def test_23505_race_reversal_concurrent_insert(
        self, repo: PortfolioRepository, mock_client: MockSupabaseClient
    ):
        port = repo.create_portfolio(make_portfolio(name="Port", base_currency=Currency.USD))
        acc = repo.create_portfolio_account(
            make_account(portfolio_id=port.id, name="Acc", base_currency=Currency.USD)
        )

        orig = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 28),
            cash_amount=Decimal("1000.00"),
            cash_currency=Currency.USD,
        )
        repo.append_transaction(orig)

        rev1 = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.REVERSAL,
            effective_date=date(2026, 8, 29),
            reverses_transaction_id=orig.id,
        )
        repo.append_transaction(rev1)

        # Worker 2 attempts concurrent reversal
        rev2 = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.REVERSAL,
            effective_date=date(2026, 8, 30),
            reverses_transaction_id=orig.id,
        )
        mock_client.next_insert_error = APIError({"code": "23505", "message": "duplicate key value"})
        res = repo.append_transaction(rev2)
        assert res.status == AppendStatus.INVALID

    def test_unexplained_23505_re_raises(
        self, repo: PortfolioRepository, mock_client: MockSupabaseClient
    ):
        port = repo.create_portfolio(make_portfolio(name="Port", base_currency=Currency.USD))
        acc = repo.create_portfolio_account(
            make_account(portfolio_id=port.id, name="Acc", base_currency=Currency.USD)
        )

        tx = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 28),
            cash_amount=Decimal("1000.00"),
            cash_currency=Currency.USD,
        )

        mock_client.next_insert_error = APIError({"code": "23505", "message": "unexplained unique error"})
        with pytest.raises(APIError):
            repo.append_transaction(tx)

    def test_non_23505_apierror_propagated(
        self, repo: PortfolioRepository, mock_client: MockSupabaseClient
    ):
        port = repo.create_portfolio(make_portfolio(name="Port", base_currency=Currency.USD))
        acc = repo.create_portfolio_account(
            make_account(portfolio_id=port.id, name="Acc", base_currency=Currency.USD)
        )

        tx = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 28),
            cash_amount=Decimal("1000.00"),
            cash_currency=Currency.USD,
        )

        mock_client.next_insert_error = APIError({"code": "23503", "message": "foreign key violation"})
        with pytest.raises(APIError):
            repo.append_transaction(tx)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Pagination & Ordering
# ─────────────────────────────────────────────────────────────────────────────

class TestPaginationAndOrdering:
    """Verifies complete multi-page pagination and canonical deterministic ordering."""

    def test_multi_page_transactions_ordering(
        self, repo: PortfolioRepository, monkeypatch: pytest.MonkeyPatch
    ):
        import backend.engine.private.portfolio.repository as repo_module
        monkeypatch.setattr(repo_module, "PAGE_SIZE", 2)

        port = repo.create_portfolio(make_portfolio(name="Port", base_currency=Currency.USD))
        acc = repo.create_portfolio_account(
            make_account(portfolio_id=port.id, name="Acc", base_currency=Currency.USD)
        )

        t1 = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 25),
            cash_amount=Decimal("100.00"),
            cash_currency=Currency.USD,
        )
        t2 = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 20),
            cash_amount=Decimal("200.00"),
            cash_currency=Currency.USD,
        )
        t3 = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 28),
            cash_amount=Decimal("300.00"),
            cash_currency=Currency.USD,
        )
        t4 = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 22),
            cash_amount=Decimal("400.00"),
            cash_currency=Currency.USD,
        )
        t5 = make_transaction(
            portfolio_id=port.id,
            account_id=acc.id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 26),
            cash_amount=Decimal("500.00"),
            cash_currency=Currency.USD,
        )

        for t in [t1, t2, t3, t4, t5]:
            repo.append_transaction(t)

        listed = repo.list_transactions(port.id)
        assert len(listed) == 5

        dates = [tx.effective_date for tx in listed]
        assert dates == [
            date(2026, 8, 20),
            date(2026, 8, 22),
            date(2026, 8, 25),
            date(2026, 8, 26),
            date(2026, 8, 28),
        ]


# ─────────────────────────────────────────────────────────────────────────────
# 8. Mode and Portfolio Isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestModeAndPortfolioIsolation:
    """Verifies isolation between MY_PORTFOLIO and SANDBOX aggregates and cross-portfolio protection."""

    def test_mode_and_cross_portfolio_isolation(self, repo: PortfolioRepository):
        real_port = repo.create_portfolio(
            make_portfolio(mode=PortfolioMode.MY_PORTFOLIO, name="Real", base_currency=Currency.USD)
        )
        sandbox_port = repo.create_portfolio(
            make_portfolio(
                mode=PortfolioMode.SANDBOX,
                name="Sandbox",
                base_currency=Currency.USD,
                source_portfolio_id=real_port.id,
                source_snapshot_time=datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc),
            )
        )

        real_acc = repo.create_portfolio_account(
            make_account(portfolio_id=real_port.id, name="Real Acc", base_currency=Currency.USD)
        )
        sandbox_acc = repo.create_portfolio_account(
            make_account(portfolio_id=sandbox_port.id, name="Sandbox Acc", base_currency=Currency.USD)
        )

        # 1. Real transaction on real portfolio
        tx_real = make_transaction(
            portfolio_id=real_port.id,
            account_id=real_acc.id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 28),
            cash_amount=Decimal("1000.00"),
            cash_currency=Currency.USD,
        )
        res_real = repo.append_transaction(tx_real)
        assert res_real.status == AppendStatus.APPENDED

        # 2. Sandbox transaction on sandbox portfolio
        tx_sandbox = make_transaction(
            portfolio_id=sandbox_port.id,
            account_id=sandbox_acc.id,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 28),
            cash_amount=Decimal("5000.00"),
            cash_currency=Currency.USD,
        )
        res_sandbox = repo.append_transaction(tx_sandbox)
        assert res_sandbox.status == AppendStatus.APPENDED

        # 3. Cross-portfolio account usage rejected -> INVALID
        tx_cross = make_transaction(
            portfolio_id=sandbox_port.id,
            account_id=real_acc.id,  # real account used in sandbox portfolio!
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=date(2026, 8, 28),
            cash_amount=Decimal("100.00"),
            cash_currency=Currency.USD,
        )
        res_cross = repo.append_transaction(tx_cross)
        assert res_cross.status == AppendStatus.INVALID

        # 4. list_transactions isolation
        real_txs = repo.list_transactions(real_port.id)
        assert len(real_txs) == 1
        assert real_txs[0].id == tx_real.id

        sandbox_txs = repo.list_transactions(sandbox_port.id)
        assert len(sandbox_txs) == 1
        assert sandbox_txs[0].id == tx_sandbox.id
