"""
backend/engine/private/portfolio/repository.py
=============================================
Owner-Scoped Supabase Repository & Race-Safe Immutable Transaction Append.

Key Architectural Invariants:
    1. Zero Implicit State / Explicit Dependency Injection:
       - Client is injected; no env var reading, no client factory inside repository.
       - Trusted owner context is bound once at construction.
       - Injected clock dependency for recorded_at (defaults to datetime.now(timezone.utc)).
    2. Strict Owner Isolation (Defense-in-Depth against Service Role Bypass):
       - Every SELECT query has an explicit eq("owner_id", str(self._owner_id)) filter.
       - Every write payload uses the trusted self._owner_id via canonical serializers.
       - Cross-owner rows are strictly inaccessible.
    3. Safe Read/Write Contracts:
       - Reads use explicit Phase 12B.2B select projections with ::text casts for NUMERIC columns.
       - Writes use Phase 12B.2A serializers and returning="minimal".
       - Inserted rows are read back and hydrated via Phase 12B.2A hydrators.
    4. Immutable Append-Only Transactions & Race Safety (SQLSTATE 23505):
       - Never update, delete, or upsert portfolio_transactions.
       - System clock replaces caller-provided recorded_at.
       - Complete preflight checks (consistency, physical ID, external identity, reversal target).
       - Database uniqueness constraint race condition (SQLSTATE 23505) is resolved via
         deterministic readback (physical ID -> external identity -> reversal target).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import re
from typing import Any, Callable, Dict, List, Optional
from uuid import UUID, uuid4

from postgrest.exceptions import APIError

from backend.engine.private.domain import (
    CashPurpose,
    ContributionStatus,
    Currency,
    GoalPriority,
    GoalStatus,
    PortfolioMode,
    TransactionType,
)
from backend.engine.private.portfolio.import_commit import ImportLedgerBindingIntent
from backend.engine.private.portfolio.import_commit_persistence import (
    serialize_import_ledger_binding,
)
from backend.engine.private.portfolio.ledger import (
    AppendResult,
    AppendStatus,
    PortfolioLedgerValidator,
)
from backend.engine.private.portfolio.models import (
    CashBucket,
    InvestmentGoal,
    PlannedContribution,
    Portfolio,
    PortfolioAccount,
    PortfolioTransaction,
)
from backend.engine.private.portfolio.normalization import (
    normalize_external_reference,
    normalize_external_source,
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
from backend.engine.private.portfolio.postgrest_transport import (
    CASH_BUCKET_SELECT,
    INVESTMENT_GOAL_SELECT,
    PLANNED_CONTRIBUTION_SELECT,
    PORTFOLIO_ACCOUNT_SELECT,
    PORTFOLIO_SELECT,
    PORTFOLIO_TRANSACTION_SELECT,
)

_CANONICAL_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

PAGE_SIZE = 1000


def _validate_owner_id(val: Any) -> UUID:
    """Validates and parses owner_id into a UUID, failing closed on non-canonical formats."""
    if val is None or isinstance(val, bool) or isinstance(val, int):
        raise TypeError(
            f"owner_id must be UUID or canonical lowercase hyphenated UUID str, got {type(val).__name__}: {val!r}"
        )
    if isinstance(val, UUID):
        return val
    if isinstance(val, str):
        if not _CANONICAL_UUID_PATTERN.match(val):
            raise ValueError(f"Non-canonical or invalid owner_id UUID string: {val!r}")
        return UUID(val)
    raise TypeError(
        f"owner_id must be UUID or canonical lowercase hyphenated UUID str, got {type(val).__name__}: {val!r}"
    )


def _normalize_uuid(val: Any, field_name: str) -> UUID:
    """Normalizes an entity identifier, failing closed on non-canonical strings or wrong types."""
    if val is None or isinstance(val, bool) or isinstance(val, int):
        raise TypeError(
            f"{field_name} must be UUID or canonical UUID str, got {type(val).__name__}: {val!r}"
        )
    if isinstance(val, UUID):
        return val
    if isinstance(val, str):
        if not _CANONICAL_UUID_PATTERN.match(val):
            raise ValueError(f"Invalid or non-canonical UUID string for '{field_name}': {val!r}")
        return UUID(val)
    raise TypeError(
        f"{field_name} must be UUID or canonical UUID str, got {type(val).__name__}: {val!r}"
    )


class PortfolioRepository:
    """
    Owner-bound Supabase repository for authoritative portfolio entities.
    """

    def __init__(
        self,
        client: Any,
        owner_id: UUID | str,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if client is None:
            raise ValueError("client must not be None")
        self._client = client
        self._owner_id = _validate_owner_id(owner_id)
        self._owner_id_str = str(self._owner_id)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def owner_id(self) -> UUID:
        return self._owner_id

    @property
    def owner_id_str(self) -> str:
        return self._owner_id_str

    def _get_system_time(self) -> datetime:
        """Invokes injected clock, validating timezone-awareness and converting to UTC."""
        now = self._clock()
        if isinstance(now, bool) or not isinstance(now, datetime):
            raise TypeError(f"Clock returned non-datetime: {type(now).__name__}: {now!r}")
        if now.tzinfo is None:
            raise ValueError(f"Clock returned naive datetime, must be timezone-aware: {now}")
        return now.astimezone(timezone.utc)

    # ─────────────────────────────────────────────────────────────────────────
    # 1. Portfolios
    # ─────────────────────────────────────────────────────────────────────────

    def create_portfolio(self, portfolio: Portfolio) -> Portfolio:
        """Creates a new Portfolio aggregate under the bound owner."""
        if not isinstance(portfolio, Portfolio):
            raise TypeError(f"Expected Portfolio instance, got {type(portfolio).__name__}")
        portfolio.validate()

        if portfolio.mode == PortfolioMode.SANDBOX and portfolio.source_portfolio_id is not None:
            src = self.get_portfolio(portfolio.source_portfolio_id)
            if src is None:
                raise ValueError(
                    f"Sandbox source portfolio {portfolio.source_portfolio_id} does not exist under bound owner."
                )

        row = serialize_portfolio(portfolio, self._owner_id)
        self._client.table("portfolios").insert(row, returning="minimal").execute()

        created = self.get_portfolio(portfolio.id)
        if created is None:
            raise RuntimeError(f"Failed to read back created portfolio {portfolio.id}")
        return created

    def get_portfolio(self, portfolio_id: UUID | str) -> Optional[Portfolio]:
        """Retrieves a Portfolio by ID strictly scoped to bound owner."""
        p_id = _normalize_uuid(portfolio_id, "portfolio_id")
        res = (
            self._client.table("portfolios")
            .select(PORTFOLIO_SELECT)
            .eq("owner_id", self._owner_id_str)
            .eq("id", str(p_id))
            .execute()
        )
        if not res.data:
            return None
        return hydrate_portfolio(res.data[0], self._owner_id)

    def list_portfolios(
        self,
        mode: Optional[PortfolioMode] = None,
        include_archived: bool = False,
    ) -> List[Portfolio]:
        """Lists portfolios for the bound owner with deterministic server ordering and pagination."""
        results: List[Portfolio] = []
        offset = 0

        while True:
            q = (
                self._client.table("portfolios")
                .select(PORTFOLIO_SELECT)
                .eq("owner_id", self._owner_id_str)
            )
            if mode is not None:
                q = q.eq("mode", mode.value)
            if not include_archived:
                q = q.is_("archived_at", "null")
            q = q.order("created_at", desc=False).order("id", desc=False)

            res = q.range(offset, offset + PAGE_SIZE - 1).execute()
            rows = res.data or []
            if not rows:
                break
            for r in rows:
                results.append(hydrate_portfolio(r, self._owner_id))

            offset += len(rows)

        results.sort(key=lambda p: (p.created_at, p.id))
        return results

    # ─────────────────────────────────────────────────────────────────────────
    # 2. Portfolio Accounts
    # ─────────────────────────────────────────────────────────────────────────

    def create_portfolio_account(self, account: PortfolioAccount) -> PortfolioAccount:
        """Creates a new PortfolioAccount under the specified portfolio and bound owner."""
        if not isinstance(account, PortfolioAccount):
            raise TypeError(f"Expected PortfolioAccount instance, got {type(account).__name__}")

        port = self.get_portfolio(account.portfolio_id)
        if port is None:
            raise ValueError(f"Portfolio {account.portfolio_id} does not exist under bound owner.")

        row = serialize_portfolio_account(account, self._owner_id)
        self._client.table("portfolio_accounts").insert(row, returning="minimal").execute()

        created = self.get_portfolio_account(account.portfolio_id, account.id)
        if created is None:
            raise RuntimeError(f"Failed to read back created portfolio account {account.id}")
        return created

    def get_portfolio_account(
        self,
        portfolio_id: UUID | str,
        account_id: UUID | str,
    ) -> Optional[PortfolioAccount]:
        """Retrieves a PortfolioAccount by ID strictly scoped to portfolio and owner."""
        p_id = _normalize_uuid(portfolio_id, "portfolio_id")
        a_id = _normalize_uuid(account_id, "account_id")
        res = (
            self._client.table("portfolio_accounts")
            .select(PORTFOLIO_ACCOUNT_SELECT)
            .eq("owner_id", self._owner_id_str)
            .eq("portfolio_id", str(p_id))
            .eq("id", str(a_id))
            .execute()
        )
        if not res.data:
            return None
        return hydrate_portfolio_account(res.data[0], self._owner_id)

    def list_portfolio_accounts(
        self,
        portfolio_id: UUID | str,
        include_archived: bool = False,
    ) -> List[PortfolioAccount]:
        """Lists portfolio accounts for the specified portfolio."""
        p_id = _normalize_uuid(portfolio_id, "portfolio_id")
        results: List[PortfolioAccount] = []
        offset = 0

        while True:
            q = (
                self._client.table("portfolio_accounts")
                .select(PORTFOLIO_ACCOUNT_SELECT)
                .eq("owner_id", self._owner_id_str)
                .eq("portfolio_id", str(p_id))
            )
            if not include_archived:
                q = q.is_("archived_at", "null")
            q = q.order("created_at", desc=False).order("id", desc=False)

            res = q.range(offset, offset + PAGE_SIZE - 1).execute()
            rows = res.data or []
            if not rows:
                break
            for r in rows:
                results.append(hydrate_portfolio_account(r, self._owner_id))

            offset += len(rows)

        results.sort(key=lambda a: (a.created_at, a.id))
        return results

    # ─────────────────────────────────────────────────────────────────────────
    # 3. Cash Buckets
    # ─────────────────────────────────────────────────────────────────────────

    def create_cash_bucket(self, bucket: CashBucket) -> CashBucket:
        """Creates a new CashBucket under the specified portfolio and bound owner."""
        if not isinstance(bucket, CashBucket):
            raise TypeError(f"Expected CashBucket instance, got {type(bucket).__name__}")

        port = self.get_portfolio(bucket.portfolio_id)
        if port is None:
            raise ValueError(f"Portfolio {bucket.portfolio_id} does not exist under bound owner.")

        if bucket.account_id is not None:
            acc = self.get_portfolio_account(bucket.portfolio_id, bucket.account_id)
            if acc is None:
                raise ValueError(
                    f"Account {bucket.account_id} does not exist in portfolio {bucket.portfolio_id}."
                )

        row = serialize_cash_bucket(bucket, self._owner_id)
        self._client.table("cash_buckets").insert(row, returning="minimal").execute()

        created = self.get_cash_bucket(bucket.portfolio_id, bucket.id)
        if created is None:
            raise RuntimeError(f"Failed to read back created cash bucket {bucket.id}")
        return created

    def get_cash_bucket(
        self,
        portfolio_id: UUID | str,
        cash_bucket_id: UUID | str,
    ) -> Optional[CashBucket]:
        """Retrieves a CashBucket by ID strictly scoped to portfolio and owner."""
        p_id = _normalize_uuid(portfolio_id, "portfolio_id")
        b_id = _normalize_uuid(cash_bucket_id, "cash_bucket_id")
        res = (
            self._client.table("cash_buckets")
            .select(CASH_BUCKET_SELECT)
            .eq("owner_id", self._owner_id_str)
            .eq("portfolio_id", str(p_id))
            .eq("id", str(b_id))
            .execute()
        )
        if not res.data:
            return None
        return hydrate_cash_bucket(res.data[0], self._owner_id)

    def list_cash_buckets(
        self,
        portfolio_id: UUID | str,
        account_id: Optional[UUID | str] = None,
        include_archived: bool = False,
    ) -> List[CashBucket]:
        """Lists cash buckets for the specified portfolio."""
        p_id = _normalize_uuid(portfolio_id, "portfolio_id")
        results: List[CashBucket] = []
        offset = 0

        while True:
            q = (
                self._client.table("cash_buckets")
                .select(CASH_BUCKET_SELECT)
                .eq("owner_id", self._owner_id_str)
                .eq("portfolio_id", str(p_id))
            )
            if account_id is not None:
                a_id = _normalize_uuid(account_id, "account_id")
                q = q.eq("account_id", str(a_id))
            if not include_archived:
                q = q.is_("archived_at", "null")
            q = q.order("created_at", desc=False).order("id", desc=False)

            res = q.range(offset, offset + PAGE_SIZE - 1).execute()
            rows = res.data or []
            if not rows:
                break
            for r in rows:
                results.append(hydrate_cash_bucket(r, self._owner_id))

            offset += len(rows)

        results.sort(key=lambda b: (b.created_at, b.id))
        return results

    # ─────────────────────────────────────────────────────────────────────────
    # 4. Investment Goals
    # ─────────────────────────────────────────────────────────────────────────

    def create_investment_goal(self, goal: InvestmentGoal) -> InvestmentGoal:
        """Creates a new InvestmentGoal under the specified portfolio and bound owner."""
        if not isinstance(goal, InvestmentGoal):
            raise TypeError(f"Expected InvestmentGoal instance, got {type(goal).__name__}")

        port = self.get_portfolio(goal.portfolio_id)
        if port is None:
            raise ValueError(f"Portfolio {goal.portfolio_id} does not exist under bound owner.")

        PortfolioLedgerValidator.validate_goal_consistency(goal, port)

        row = serialize_investment_goal(goal, self._owner_id)
        self._client.table("investment_goals").insert(row, returning="minimal").execute()

        created = self.get_investment_goal(goal.portfolio_id, goal.id)
        if created is None:
            raise RuntimeError(f"Failed to read back created investment goal {goal.id}")
        return created

    def get_investment_goal(
        self,
        portfolio_id: UUID | str,
        goal_id: UUID | str,
    ) -> Optional[InvestmentGoal]:
        """Retrieves an InvestmentGoal by ID strictly scoped to portfolio and owner."""
        p_id = _normalize_uuid(portfolio_id, "portfolio_id")
        g_id = _normalize_uuid(goal_id, "goal_id")
        res = (
            self._client.table("investment_goals")
            .select(INVESTMENT_GOAL_SELECT)
            .eq("owner_id", self._owner_id_str)
            .eq("portfolio_id", str(p_id))
            .eq("id", str(g_id))
            .execute()
        )
        if not res.data:
            return None
        return hydrate_investment_goal(res.data[0], self._owner_id)

    def list_investment_goals(
        self,
        portfolio_id: UUID | str,
        status: Optional[GoalStatus] = None,
        include_archived: bool = False,
    ) -> List[InvestmentGoal]:
        """Lists investment goals for the specified portfolio."""
        p_id = _normalize_uuid(portfolio_id, "portfolio_id")
        results: List[InvestmentGoal] = []
        offset = 0

        while True:
            q = (
                self._client.table("investment_goals")
                .select(INVESTMENT_GOAL_SELECT)
                .eq("owner_id", self._owner_id_str)
                .eq("portfolio_id", str(p_id))
            )
            if status is not None:
                q = q.eq("status", status.value)
            if not include_archived:
                q = q.is_("archived_at", "null")
            q = q.order("created_at", desc=False).order("id", desc=False)

            res = q.range(offset, offset + PAGE_SIZE - 1).execute()
            rows = res.data or []
            if not rows:
                break
            for r in rows:
                results.append(hydrate_investment_goal(r, self._owner_id))

            offset += len(rows)

        results.sort(key=lambda g: (g.created_at, g.id))
        return results

    # ─────────────────────────────────────────────────────────────────────────
    # 5. Planned Contributions (Zero Cash Side-Effects Authority)
    # ─────────────────────────────────────────────────────────────────────────

    def create_planned_contribution(self, contribution: PlannedContribution) -> PlannedContribution:
        """
        Creates a new PlannedContribution under the specified portfolio.
        Guarantees ZERO transaction writes, ZERO cash balance mutations, and ZERO side-effects.
        """
        if not isinstance(contribution, PlannedContribution):
            raise TypeError(
                f"Expected PlannedContribution instance, got {type(contribution).__name__}"
            )

        port = self.get_portfolio(contribution.portfolio_id)
        if port is None:
            raise ValueError(
                f"Portfolio {contribution.portfolio_id} does not exist under bound owner."
            )

        goal = None
        if contribution.goal_id is not None:
            goal = self.get_investment_goal(contribution.portfolio_id, contribution.goal_id)
            if goal is None:
                raise ValueError(
                    f"Goal {contribution.goal_id} does not exist in portfolio {contribution.portfolio_id}."
                )

        bucket = None
        if contribution.cash_bucket_id is not None:
            bucket = self.get_cash_bucket(contribution.portfolio_id, contribution.cash_bucket_id)
            if bucket is None:
                raise ValueError(
                    f"Cash bucket {contribution.cash_bucket_id} does not exist in portfolio {contribution.portfolio_id}."
                )

        PortfolioLedgerValidator.validate_contribution_consistency(
            contribution, port, goal=goal, cash_bucket=bucket
        )

        row = serialize_planned_contribution(contribution, self._owner_id)
        self._client.table("planned_contributions").insert(row, returning="minimal").execute()

        created = self.get_planned_contribution(contribution.portfolio_id, contribution.id)
        if created is None:
            raise RuntimeError(
                f"Failed to read back created planned contribution {contribution.id}"
            )
        return created

    def get_planned_contribution(
        self,
        portfolio_id: UUID | str,
        contribution_id: UUID | str,
    ) -> Optional[PlannedContribution]:
        """Retrieves a PlannedContribution by ID strictly scoped to portfolio and owner."""
        p_id = _normalize_uuid(portfolio_id, "portfolio_id")
        c_id = _normalize_uuid(contribution_id, "contribution_id")
        res = (
            self._client.table("planned_contributions")
            .select(PLANNED_CONTRIBUTION_SELECT)
            .eq("owner_id", self._owner_id_str)
            .eq("portfolio_id", str(p_id))
            .eq("id", str(c_id))
            .execute()
        )
        if not res.data:
            return None
        return hydrate_planned_contribution(res.data[0], self._owner_id)

    def list_planned_contributions(
        self,
        portfolio_id: UUID | str,
        status: Optional[ContributionStatus] = None,
    ) -> List[PlannedContribution]:
        """Lists planned contributions for the specified portfolio."""
        p_id = _normalize_uuid(portfolio_id, "portfolio_id")
        results: List[PlannedContribution] = []
        offset = 0

        while True:
            q = (
                self._client.table("planned_contributions")
                .select(PLANNED_CONTRIBUTION_SELECT)
                .eq("owner_id", self._owner_id_str)
                .eq("portfolio_id", str(p_id))
            )
            if status is not None:
                q = q.eq("status", status.value)
            q = q.order("expected_date", desc=False).order("created_at", desc=False).order("id", desc=False)

            res = q.range(offset, offset + PAGE_SIZE - 1).execute()
            rows = res.data or []
            if not rows:
                break
            for r in rows:
                results.append(hydrate_planned_contribution(r, self._owner_id))

            offset += len(rows)

        results.sort(key=lambda c: (c.expected_date, c.created_at, c.id))
        return results

    # ─────────────────────────────────────────────────────────────────────────
    # 6. Portfolio Transactions (Append-Only Immutable Ledger)
    # ─────────────────────────────────────────────────────────────────────────

    def get_transaction(
        self,
        portfolio_id: UUID | str,
        transaction_id: UUID | str,
    ) -> Optional[PortfolioTransaction]:
        """Retrieves an immutable transaction by ID strictly scoped to portfolio and owner."""
        p_id = _normalize_uuid(portfolio_id, "portfolio_id")
        t_id = _normalize_uuid(transaction_id, "transaction_id")
        res = (
            self._client.table("portfolio_transactions")
            .select(PORTFOLIO_TRANSACTION_SELECT)
            .eq("owner_id", self._owner_id_str)
            .eq("portfolio_id", str(p_id))
            .eq("id", str(t_id))
            .execute()
        )
        if not res.data:
            return None
        return hydrate_portfolio_transaction(res.data[0], self._owner_id)

    def list_transactions(
        self,
        portfolio_id: UUID | str,
        account_id: Optional[UUID | str] = None,
    ) -> List[PortfolioTransaction]:
        """
        Lists all transactions for a portfolio across all pages, returning canonically sorted list:
        (effective_date, executed_at or UTC-min, recorded_at, id).
        """
        p_id = _normalize_uuid(portfolio_id, "portfolio_id")
        results: List[PortfolioTransaction] = []
        offset = 0

        while True:
            q = (
                self._client.table("portfolio_transactions")
                .select(PORTFOLIO_TRANSACTION_SELECT)
                .eq("owner_id", self._owner_id_str)
                .eq("portfolio_id", str(p_id))
            )
            if account_id is not None:
                a_id = _normalize_uuid(account_id, "account_id")
                q = q.eq("account_id", str(a_id))
            q = q.order("recorded_at", desc=False).order("id", desc=False)

            res = q.range(offset, offset + PAGE_SIZE - 1).execute()
            rows = res.data or []
            if not rows:
                break
            for r in rows:
                results.append(hydrate_portfolio_transaction(r, self._owner_id))

            offset += len(rows)

        utc_min = datetime.min.replace(tzinfo=timezone.utc)
        results.sort(
            key=lambda t: (
                t.effective_date,
                t.executed_at if t.executed_at is not None else utc_min,
                t.recorded_at,
                t.id,
            )
        )
        return results

    def lookup_external_identity(
        self,
        portfolio_id: UUID | str,
        account_id: UUID | str,
        external_source: str,
        external_reference: str,
    ) -> Optional[UUID]:
        """
        Invokes Migration 012/013 RPC lookup_portfolio_transaction_external_identity.
        Returns matching transaction UUID or None.
        """
        p_id = _normalize_uuid(portfolio_id, "portfolio_id")
        a_id = _normalize_uuid(account_id, "account_id")
        norm_source = normalize_external_source(external_source)
        norm_reference = normalize_external_reference(external_reference)

        params = {
            "p_owner_id": self._owner_id_str,
            "p_portfolio_id": str(p_id),
            "p_account_id": str(a_id),
            "p_external_source": norm_source,
            "p_external_reference": norm_reference,
        }

        res = self._client.rpc("lookup_portfolio_transaction_external_identity", params).execute()
        data = res.data
        if data is None:
            return None
        if isinstance(data, list):
            if not data:
                return None
            data = data[0]
        if isinstance(data, dict):
            data = data.get("lookup_portfolio_transaction_external_identity") or data.get("id")
        if not data:
            return None
        return UUID(str(data))

    def append_transaction(self, transaction: PortfolioTransaction) -> AppendResult:
        """
        Appends an immutable transaction to the ledger with complete preflight validation,
        system clock authority, and database-race safety.
        """
        if not isinstance(transaction, PortfolioTransaction):
            raise TypeError(
                f"Expected PortfolioTransaction instance, got {type(transaction).__name__}"
            )

        # 1. System Clock Authority for recorded_at
        system_recorded_at = self._get_system_time()
        tx = replace(transaction, recorded_at=system_recorded_at)

        # 2. Resolve parent Portfolio
        port = self.get_portfolio(tx.portfolio_id)
        if port is None:
            return AppendResult(
                status=AppendStatus.INVALID,
                transaction_id=tx.id,
                diagnostics=(f"Portfolio {tx.portfolio_id} does not exist under bound owner.",),
            )

        # 3. Resolve parent PortfolioAccount
        acc = self.get_portfolio_account(tx.portfolio_id, tx.account_id)
        if acc is None:
            return AppendResult(
                status=AppendStatus.INVALID,
                transaction_id=tx.id,
                diagnostics=(
                    f"PortfolioAccount {tx.account_id} does not exist in portfolio {tx.portfolio_id}.",
                ),
            )

        # 4. Resolve CashBucket if set
        bucket: Optional[CashBucket] = None
        if tx.cash_bucket_id is not None:
            bucket = self.get_cash_bucket(tx.portfolio_id, tx.cash_bucket_id)
            if bucket is None:
                return AppendResult(
                    status=AppendStatus.INVALID,
                    transaction_id=tx.id,
                    diagnostics=(
                        f"CashBucket {tx.cash_bucket_id} does not exist in portfolio {tx.portfolio_id}.",
                    ),
                )

        # 5. Cross-entity validation via canonical PortfolioLedgerValidator
        try:
            PortfolioLedgerValidator.validate_transaction_portfolio_consistency(
                tx,
                port,
                account=acc,
                cash_bucket=bucket,
            )
        except ValueError as err:
            return AppendResult(
                status=AppendStatus.INVALID,
                transaction_id=tx.id,
                diagnostics=(str(err),),
            )

        # 6. Physical ID Idempotency Precheck
        existing_by_id = self.get_transaction(tx.portfolio_id, tx.id)
        if existing_by_id is not None:
            if existing_by_id.economic_fingerprint() == tx.economic_fingerprint():
                return AppendResult(
                    status=AppendStatus.IDEMPOTENT_DUPLICATE,
                    transaction_id=existing_by_id.id,
                )
            return AppendResult(
                status=AppendStatus.CONFLICT,
                transaction_id=tx.id,
                diagnostics=(
                    f"Transaction with ID {tx.id} already exists with different economic fingerprint.",
                ),
            )

        # 7. External Identity Idempotency Precheck
        if tx.external_source is not None and tx.external_reference is not None:
            existing_ext_id = self.lookup_external_identity(
                tx.portfolio_id,
                tx.account_id,
                tx.external_source,
                tx.external_reference,
            )
            if existing_ext_id is not None:
                existing_ext = self.get_transaction(tx.portfolio_id, existing_ext_id)
                if existing_ext is not None:
                    if existing_ext.economic_fingerprint() == tx.economic_fingerprint():
                        return AppendResult(
                            status=AppendStatus.IDEMPOTENT_DUPLICATE,
                            transaction_id=existing_ext.id,
                        )
                    return AppendResult(
                        status=AppendStatus.CONFLICT,
                        transaction_id=tx.id,
                        diagnostics=(
                            f"External transaction ({tx.external_source}:{tx.external_reference}) "
                            f"already exists with different economic fingerprint.",
                        ),
                    )

        # 8. Reversal Preflight
        if tx.transaction_type == TransactionType.REVERSAL:
            target_id = tx.reverses_transaction_id
            if target_id is None:
                return AppendResult(
                    status=AppendStatus.INVALID,
                    transaction_id=tx.id,
                    diagnostics=("REVERSAL transaction must specify reverses_transaction_id.",),
                )

            target = self.get_transaction(tx.portfolio_id, target_id)
            if target is None:
                return AppendResult(
                    status=AppendStatus.INVALID,
                    transaction_id=tx.id,
                    diagnostics=(
                        f"Reversal target transaction {target_id} not found in portfolio {tx.portfolio_id}.",
                    ),
                )
            if target.account_id != tx.account_id:
                return AppendResult(
                    status=AppendStatus.INVALID,
                    transaction_id=tx.id,
                    diagnostics=(
                        f"Reversal account {tx.account_id} does not match target account {target.account_id}.",
                    ),
                )
            if target.transaction_type == TransactionType.REVERSAL:
                return AppendResult(
                    status=AppendStatus.INVALID,
                    transaction_id=tx.id,
                    diagnostics=(
                        f"Cannot reverse transaction {target_id} which is itself a REVERSAL.",
                    ),
                )

            # Check if target is already reversed
            rev_check = (
                self._client.table("portfolio_transactions")
                .select(PORTFOLIO_TRANSACTION_SELECT)
                .eq("owner_id", self._owner_id_str)
                .eq("portfolio_id", str(tx.portfolio_id))
                .eq("reverses_transaction_id", str(target_id))
                .execute()
            )
            if rev_check.data:
                existing_rev = hydrate_portfolio_transaction(rev_check.data[0], self._owner_id)
                return AppendResult(
                    status=AppendStatus.INVALID,
                    transaction_id=tx.id,
                    diagnostics=(
                        f"Target transaction {target_id} has already been reversed by {existing_rev.id}.",
                    ),
                )

        # 9. Perform INSERT
        row = serialize_portfolio_transaction(tx, self._owner_id)
        try:
            self._client.table("portfolio_transactions").insert(
                row, returning="minimal"
            ).execute()
        except APIError as e:
            if getattr(e, "code", None) == "23505":
                return self._resolve_23505_race(tx, e)
            raise

        # 10. Safe read back and verify
        persisted = self.get_transaction(tx.portfolio_id, tx.id)
        if persisted is None:
            raise RuntimeError(
                f"Inserted transaction {tx.id} could not be read back from persistence."
            )
        if persisted.economic_fingerprint() != tx.economic_fingerprint():
            raise RuntimeError(f"Persisted transaction {tx.id} economic fingerprint mismatch.")

        return AppendResult(status=AppendStatus.APPENDED, transaction_id=persisted.id)

    def _resolve_23505_race(
        self,
        tx: PortfolioTransaction,
        original_error: APIError,
    ) -> AppendResult:
        """
        Deterministically resolves concurrent SQLSTATE 23505 uniqueness violations.
        Order of resolution:
            A. Physical transaction ID lookup
            B. Normalized external identity lookup
            C. Reversal target lookup
            D. Re-raise original error if unexplained
        """
        # A. Physical ID lookup
        by_id = self.get_transaction(tx.portfolio_id, tx.id)
        if by_id is not None:
            if by_id.economic_fingerprint() == tx.economic_fingerprint():
                return AppendResult(
                    status=AppendStatus.IDEMPOTENT_DUPLICATE,
                    transaction_id=by_id.id,
                )
            return AppendResult(
                status=AppendStatus.CONFLICT,
                transaction_id=tx.id,
                diagnostics=(
                    f"Concurrent conflict: ID {tx.id} exists with different economic fingerprint.",
                ),
            )

        # B. External identity lookup
        if tx.external_source is not None and tx.external_reference is not None:
            ext_id = self.lookup_external_identity(
                tx.portfolio_id,
                tx.account_id,
                tx.external_source,
                tx.external_reference,
            )
            if ext_id is not None:
                by_ext = self.get_transaction(tx.portfolio_id, ext_id)
                if by_ext is not None:
                    if by_ext.economic_fingerprint() == tx.economic_fingerprint():
                        return AppendResult(
                            status=AppendStatus.IDEMPOTENT_DUPLICATE,
                            transaction_id=by_ext.id,
                        )
                    return AppendResult(
                        status=AppendStatus.CONFLICT,
                        transaction_id=tx.id,
                        diagnostics=(
                            f"Concurrent conflict: external identity ({tx.external_source}:{tx.external_reference}) "
                            f"exists with different economic fingerprint.",
                        ),
                    )

        # C. Reversal target lookup
        if tx.transaction_type == TransactionType.REVERSAL and tx.reverses_transaction_id is not None:
            rev_check = (
                self._client.table("portfolio_transactions")
                .select(PORTFOLIO_TRANSACTION_SELECT)
                .eq("owner_id", self._owner_id_str)
                .eq("portfolio_id", str(tx.portfolio_id))
                .eq("reverses_transaction_id", str(tx.reverses_transaction_id))
                .execute()
            )
            if rev_check.data:
                existing_rev = hydrate_portfolio_transaction(rev_check.data[0], self._owner_id)
                return AppendResult(
                    status=AppendStatus.INVALID,
                    transaction_id=tx.id,
                    diagnostics=(
                        f"Target transaction {tx.reverses_transaction_id} was concurrently reversed by {existing_rev.id}.",
                    ),
                )

        # D. Unexplained 23505 -> re-raise original APIError
        raise original_error

    def commit_import_binding_intent(
        self,
        intent: ImportLedgerBindingIntent,
    ) -> AppendResult:
        """
        Atomically commits an ImportLedgerBindingIntent to persistence.

        Phase 13Q Invariants:
            1. Atomic all-or-nothing execution: inserts candidate PortfolioTransaction
               and ImportLedgerBinding in a single RPC transaction.
            2. Idempotent replay: if the raw claim is already bound to a transaction
               with identical expected_plan_sha256 and economic_fingerprint, returns
               IDEMPOTENT_DUPLICATE with the existing transaction ID.
            3. Conflict detection: if the raw claim is already bound to a different
               plan SHA or different economic fingerprint, returns CONFLICT.
            4. External identity is NULL: import claims are NOT mapped to external_source/reference.
            5. Cash bucket is NULL: import transactions do not assign cash buckets.
            6. Reversals are forbidden: import transactions cannot be reversals.
            7. Safe readback & fingerprint verification on success / duplicate.
        """
        if not isinstance(intent, ImportLedgerBindingIntent):
            raise TypeError(
                f"intent must be an ImportLedgerBindingIntent instance, got {type(intent).__name__}: {intent!r}"
            )

        # 1. Preflight: Verify target portfolio and account exist under trusted owner context
        portfolio = self.get_portfolio(intent.portfolio_id)
        if portfolio is None:
            return AppendResult(
                status=AppendStatus.INVALID,
                transaction_id=uuid4(),
                diagnostics=(
                    f"Target portfolio {intent.portfolio_id} does not exist or is inaccessible under current owner.",
                ),
            )

        account = self.get_portfolio_account(intent.portfolio_id, intent.account_id)
        if account is None:
            return AppendResult(
                status=AppendStatus.INVALID,
                transaction_id=uuid4(),
                diagnostics=(
                    f"Target portfolio account {intent.account_id} does not exist or does not belong to portfolio {intent.portfolio_id}.",
                ),
            )

        # 2. Construct canonical candidate PortfolioTransaction from intent.plan
        plan = intent.plan
        candidate_tx = PortfolioTransaction(
            id=uuid4(),
            portfolio_id=intent.portfolio_id,
            account_id=intent.account_id,
            transaction_type=plan.transaction_type,
            effective_date=plan.effective_date,
            executed_at=plan.executed_at,
            recorded_at=self._get_system_time(),
            instrument_id=plan.instrument_id,
            quantity=plan.quantity,
            unit_price=plan.unit_price,
            trade_currency=plan.trade_currency,
            cash_amount=plan.cash_amount,
            cash_currency=plan.cash_currency,
            cash_bucket_id=None,
            from_currency=plan.from_currency,
            from_amount=plan.from_amount,
            to_currency=plan.to_currency,
            to_amount=plan.to_amount,
            external_source=None,
            external_reference=None,
            reverses_transaction_id=None,
            notes=None,
        )

        # 3. Cross-entity validation (consistency with portfolio & account)
        try:
            PortfolioLedgerValidator.validate_transaction_portfolio_consistency(
                candidate_tx,
                portfolio,
                account,
                None,
            )
        except ValueError as err:
            return AppendResult(
                status=AppendStatus.INVALID,
                transaction_id=candidate_tx.id,
                diagnostics=(str(err),),
            )

        # 4. Serialize transaction and binding using canonical codecs
        tx_payload = serialize_portfolio_transaction(candidate_tx, self._owner_id)
        binding_payload = serialize_import_ledger_binding(
            intent,
            transaction_id=candidate_tx.id,
            expected_owner_id=self._owner_id,
        )

        # 5. Call atomic commit RPC
        rpc_res = self._client.rpc(
            "commit_portfolio_import_claim",
            {
                "p_transaction": tx_payload,
                "p_binding": binding_payload,
            },
        ).execute()

        # 6. Parse and validate RPC return
        if not rpc_res.data or not isinstance(rpc_res.data, list) or len(rpc_res.data) != 1:
            raise RuntimeError(
                f"Malformed return from commit_portfolio_import_claim RPC: expected list with 1 dict, got {rpc_res.data!r}"
            )

        result_row = rpc_res.data[0]
        if not isinstance(result_row, dict):
            raise RuntimeError(
                f"Malformed result row from commit_portfolio_import_claim: expected dict, got {type(result_row).__name__}"
            )

        commit_status = result_row.get("commit_status")
        raw_tx_id = result_row.get("transaction_id")
        diagnostic = result_row.get("diagnostic")

        if not isinstance(commit_status, str):
            raise RuntimeError(f"Missing or non-string commit_status from RPC: {commit_status!r}")

        if raw_tx_id is None:
            raise RuntimeError(f"Missing transaction_id from commit_portfolio_import_claim RPC result.")

        if isinstance(raw_tx_id, UUID):
            res_tx_id = raw_tx_id
        elif isinstance(raw_tx_id, str):
            if not _CANONICAL_UUID_PATTERN.match(raw_tx_id):
                raise RuntimeError(f"Invalid transaction_id UUID string from RPC: {raw_tx_id!r}")
            try:
                res_tx_id = UUID(raw_tx_id)
            except Exception as e:
                raise RuntimeError(f"Invalid transaction_id UUID string from RPC: {raw_tx_id!r}") from e
        else:
            raise RuntimeError(f"Invalid transaction_id type from RPC: {type(raw_tx_id).__name__}")

        if diagnostic is not None and not isinstance(diagnostic, str):
            raise RuntimeError(f"Non-string diagnostic returned from RPC: {type(diagnostic).__name__}")

        # 7. Handle statuses
        if commit_status == "appended":
            persisted = self.get_transaction(candidate_tx.portfolio_id, res_tx_id)
            if persisted is None:
                raise RuntimeError(
                    f"Appended transaction {res_tx_id} could not be read back from persistence."
                )
            if persisted.economic_fingerprint() != candidate_tx.economic_fingerprint():
                raise RuntimeError(
                    f"Persisted transaction {res_tx_id} economic fingerprint mismatch."
                )
            return AppendResult(status=AppendStatus.APPENDED, transaction_id=persisted.id)

        elif commit_status == "idempotent_duplicate":
            persisted = self.get_transaction(candidate_tx.portfolio_id, res_tx_id)
            if persisted is None:
                raise RuntimeError(
                    f"Idempotent duplicate transaction {res_tx_id} could not be read back from persistence."
                )
            if persisted.economic_fingerprint() != candidate_tx.economic_fingerprint():
                raise RuntimeError(
                    f"Idempotent duplicate transaction {res_tx_id} economic fingerprint mismatch: "
                    f"existing={persisted.economic_fingerprint()} vs candidate={candidate_tx.economic_fingerprint()}"
                )
            return AppendResult(status=AppendStatus.IDEMPOTENT_DUPLICATE, transaction_id=persisted.id)

        elif commit_status == "conflict":
            return AppendResult(
                status=AppendStatus.CONFLICT,
                transaction_id=res_tx_id,
                diagnostics=(diagnostic,) if diagnostic else ("Import claim conflict.",),
            )

        else:
            raise RuntimeError(f"Unknown commit_status '{commit_status}' returned from commit_portfolio_import_claim RPC.")
