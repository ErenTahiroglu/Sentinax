"""
backend/tests/test_portfolio_import_atomic_commit_repository.py
===============================================================
Comprehensive tests for Phase 13Q:
Atomic Import Claim + Ledger Transaction Commit RPC & Owner-Bound Repository Execution.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import inspect
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import (
    Currency,
    PortfolioMode,
    TransactionType,
)
from backend.engine.private.portfolio.import_assessment import (
    ImportAssessmentStatus,
    build_import_assessment_batch,
    build_import_record_assessment,
)
from backend.engine.private.portfolio.import_batch import (
    build_import_batch_manifest,
)
from backend.engine.private.portfolio.import_commit import (
    ImportLedgerBindingIntent,
    build_import_ledger_binding_intent,
)
from backend.engine.private.portfolio.import_draft import (
    build_import_transaction_draft,
)
from backend.engine.private.portfolio.import_instrument_resolution import (
    ImportInstrumentResolutionStatus,
    build_import_instrument_resolution,
)
from backend.engine.private.portfolio.import_materialization import (
    build_import_ledger_transaction_plan,
)
from backend.engine.private.portfolio.import_parsing import (
    ImportParsedField,
    build_parsed_import_record,
)
from backend.engine.private.portfolio.import_parsed_batch import (
    build_parsed_import_batch_manifest,
)
from backend.engine.private.portfolio.import_provenance import (
    build_import_file_provenance,
    build_import_record_provenance,
)
from backend.engine.private.portfolio.ledger import AppendResult, AppendStatus
from backend.engine.private.portfolio.models import (
    Portfolio,
    PortfolioAccount,
    PortfolioTransaction,
)
from backend.engine.private.portfolio.persistence import (
    serialize_portfolio,
    serialize_portfolio_account,
    serialize_portfolio_transaction,
)
from backend.engine.private.portfolio.repository import PortfolioRepository


# ─────────────────────────────────────────────────────────────────────────────
# Factory Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_test_binding_intent(
    portfolio_id: Optional[UUID] = None,
    account_id: Optional[UUID] = None,
    ordinal: int = 1,
    transaction_type: TransactionType = TransactionType.BUY,
    effective_date: date = date(2026, 8, 28),
    instrument_id: Optional[UUID] = None,
    source_key: str = "sentinax_csv",
    filename: str = "test.csv",
    imported_at: Optional[datetime] = None,
    raw_content: bytes = b"dummy_content",
    resolver_key: str = "mock",
    resolver_revision: int = 1,
    **extra_kwargs: Any,
) -> ImportLedgerBindingIntent:
    port_id = portfolio_id or uuid4()
    acc_id = account_id or uuid4()
    inst_id = instrument_id or (uuid4() if transaction_type == TransactionType.BUY else None)
    t = imported_at or datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)

    file_prov = build_import_file_provenance(
        portfolio_id=port_id,
        account_id=acc_id,
        source_key=source_key,
        filename=filename,
        content=raw_content,
        imported_at=t,
    )

    records = []
    parsed_records = []
    assessments = []
    for ord_idx in range(1, ordinal + 1):
        raw_r = f"raw_row_{ord_idx}".encode("utf-8")
        rp = build_import_record_provenance(
            file_provenance=file_prov,
            record_ordinal=ord_idx,
            raw_record=raw_r,
        )
        records.append(rp)
        pr = build_parsed_import_record(
            record_provenance=rp,
            raw_record=raw_r,
            parser_revision=1,
            fields=[
                ImportParsedField("symbol", "AAPL"),
                ImportParsedField("quantity", "10"),
                ImportParsedField("price", "150.00"),
            ],
        )
        parsed_records.append(pr)
        ass = build_import_record_assessment(
            parsed_record=pr,
            status=ImportAssessmentStatus.READY,
        )
        assessments.append(ass)

    raw_manifest = build_import_batch_manifest(
        file_provenance=file_prov,
        records=records,
    )

    parsed_manifest = build_parsed_import_batch_manifest(
        raw_manifest=raw_manifest,
        parser_revision=1,
        parsed_records=parsed_records,
    )

    ass_batch = build_import_assessment_batch(
        parsed_manifest=parsed_manifest,
        assessments=assessments,
    )

    draft_kwargs: Dict[str, Any] = {
        "assessment_batch": ass_batch,
        "record_ordinal": ordinal,
        "transaction_type": transaction_type,
        "effective_date": effective_date,
    }

    if transaction_type == TransactionType.BUY:
        draft_kwargs.update({
            "instrument_reference": "AAPL",
            "quantity": Decimal("10"),
            "unit_price": Decimal("150.00"),
            "trade_currency": Currency.USD,
        })
    elif transaction_type == TransactionType.CASH_DEPOSIT:
        draft_kwargs.update({
            "cash_amount": Decimal("500.00"),
            "cash_currency": Currency.TRY,
        })
    elif transaction_type == TransactionType.FX_CONVERSION:
        draft_kwargs.update({
            "from_currency": Currency.USD,
            "from_amount": Decimal("100.00"),
            "to_currency": Currency.TRY,
            "to_amount": Decimal("3400.00"),
        })
    elif transaction_type == TransactionType.DIVIDEND:
        draft_kwargs.update({
            "cash_amount": Decimal("50.00"),
            "cash_currency": Currency.USD,
        })
        if instrument_id is not None:
            draft_kwargs["instrument_reference"] = "AAPL"

    draft_kwargs.update(extra_kwargs)
    draft = build_import_transaction_draft(**draft_kwargs)

    res_status = (
        ImportInstrumentResolutionStatus.RESOLVED
        if (transaction_type == TransactionType.BUY or (transaction_type == TransactionType.DIVIDEND and instrument_id is not None))
        else ImportInstrumentResolutionStatus.NOT_REQUIRED
    )

    res = build_import_instrument_resolution(
        draft=draft,
        status=res_status,
        resolution_as_of_date=effective_date,
        resolver_key=resolver_key if res_status == ImportInstrumentResolutionStatus.RESOLVED else None,
        resolver_revision=resolver_revision if res_status == ImportInstrumentResolutionStatus.RESOLVED else None,
        instrument_id=inst_id if res_status == ImportInstrumentResolutionStatus.RESOLVED else None,
    )

    plan = build_import_ledger_transaction_plan(res)
    return build_import_ledger_binding_intent(plan)


# ─────────────────────────────────────────────────────────────────────────────
# In-Memory Mock Supabase / PostgREST Client for Atomic Import Commit
# ─────────────────────────────────────────────────────────────────────────────

class MockQueryResult:
    def __init__(self, data: Any, count: Optional[int] = None):
        self.data = data
        self.count = count


class MockQueryBuilder:
    def __init__(self, table_name: str, client_store: MockSupabaseClient):
        self.table_name = table_name
        self.client_store = client_store
        self.eq_filters: Dict[str, Any] = {}
        self.is_filters: Dict[str, Any] = {}
        self.insert_payload: Optional[Any] = None
        self.returning_mode: Optional[str] = None

    def select(self, projection: str) -> MockQueryBuilder:
        return self

    def eq(self, column: str, value: Any) -> MockQueryBuilder:
        self.eq_filters[column] = value
        return self

    def is_(self, column: str, value: Any) -> MockQueryBuilder:
        self.is_filters[column] = value
        return self

    def order(self, column: str, desc: bool = False, **kwargs: Any) -> MockQueryBuilder:
        return self

    def range(self, start: int, end: int) -> MockQueryBuilder:
        return self

    def insert(self, json_data: Any, returning: str = "representation") -> MockQueryBuilder:
        self.insert_payload = json_data
        self.returning_mode = returning
        self.client_store.recorded_table_inserts.append((self.table_name, json_data))
        return self

    def execute(self) -> MockQueryResult:
        if self.insert_payload is not None:
            row = deepcopy(self.insert_payload)
            table_rows = self.client_store.tables.setdefault(self.table_name, [])
            table_rows.append(row)
            return MockQueryResult(data=[] if self.returning_mode == "minimal" else [row])

        table_rows = self.client_store.tables.get(self.table_name, [])
        filtered = []
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
        return MockQueryResult(data=filtered, count=len(filtered))


_UNSET = object()


class MockRpcBuilder:
    def __init__(self, fn_name: str, params: Dict[str, Any], client_store: MockSupabaseClient):
        self.fn_name = fn_name
        self.params = params
        self.client_store = client_store

    def execute(self) -> MockQueryResult:
        self.client_store.recorded_rpcs.append((self.fn_name, self.params))

        if self.client_store.next_rpc_error is not None:
            err = self.client_store.next_rpc_error
            self.client_store.next_rpc_error = None
            raise err

        if self.client_store.next_rpc_result is not _UNSET:
            res = self.client_store.next_rpc_result
            self.client_store.next_rpc_result = _UNSET
            return MockQueryResult(data=res)

        if self.fn_name == "commit_portfolio_import_claim":
            p_tx = self.params.get("p_transaction", {})
            p_b = self.params.get("p_binding", {})

            # Check if matching claim exists
            claims = self.client_store.tables.get("portfolio_import_claim_bindings", [])
            for b in claims:
                if (
                    str(b.get("owner_id")) == str(p_b.get("owner_id"))
                    and str(b.get("portfolio_id")) == str(p_b.get("portfolio_id"))
                    and str(b.get("account_id")) == str(p_b.get("account_id"))
                    and str(b.get("source_key")) == str(p_b.get("source_key"))
                    and str(b.get("file_content_sha256")) == str(p_b.get("file_content_sha256"))
                    and int(b.get("record_ordinal")) == int(p_b.get("record_ordinal"))
                    and str(b.get("record_sha256")) == str(p_b.get("record_sha256"))
                ):
                    existing_tx_id = b["transaction_id"]
                    # Find transaction
                    txs = self.client_store.tables.get("portfolio_transactions", [])
                    matching_tx = next((t for t in txs if str(t.get("id")) == str(existing_tx_id)), None)
                    existing_fp = matching_tx.get("economic_fingerprint") if matching_tx else None

                    if b.get("expected_plan_sha256") == p_b.get("expected_plan_sha256") and existing_fp == p_tx.get("economic_fingerprint"):
                        return MockQueryResult(data=[{
                            "commit_status": "idempotent_duplicate",
                            "transaction_id": existing_tx_id,
                            "diagnostic": "Existing claim matches plan SHA and economic fingerprint."
                        }])
                    else:
                        return MockQueryResult(data=[{
                            "commit_status": "conflict",
                            "transaction_id": existing_tx_id,
                            "diagnostic": "Existing claim has different plan SHA or economic fingerprint."
                        }])

            # If not found: insert into both tables
            tx_row = deepcopy(p_tx)
            self.client_store.tables.setdefault("portfolio_transactions", []).append(tx_row)

            b_row = deepcopy(p_b)
            b_row["bound_at"] = "2026-08-28T16:00:00+00:00"
            self.client_store.tables.setdefault("portfolio_import_claim_bindings", []).append(b_row)

            return MockQueryResult(data=[{
                "commit_status": "appended",
                "transaction_id": p_tx["id"],
                "diagnostic": "Transaction and claim binding appended successfully."
            }])

        return MockQueryResult(data=None)


class MockSupabaseClient:
    def __init__(self):
        self.tables: Dict[str, List[Dict[str, Any]]] = {}
        self.recorded_table_inserts: List[Any] = []
        self.recorded_rpcs: List[Any] = []
        self.next_rpc_result: Any = _UNSET
        self.next_rpc_error: Optional[Exception] = None

    def table(self, table_name: str) -> MockQueryBuilder:
        return MockQueryBuilder(table_name, self)

    def rpc(self, fn_name: str, params: Dict[str, Any]) -> MockRpcBuilder:
        return MockRpcBuilder(fn_name, params, self)


# ─────────────────────────────────────────────────────────────────────────────
# Test Fixtures & Setup
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def test_context():
    owner_id = uuid4()
    client = MockSupabaseClient()
    repo_clock = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)
    repo = PortfolioRepository(client=client, owner_id=owner_id, clock=lambda: repo_clock)

    port_id = uuid4()
    acc_id = uuid4()

    # Seed parent portfolio and account in mock store
    port = Portfolio(
        id=port_id,
        mode=PortfolioMode.MY_PORTFOLIO,
        name="Main Portfolio",
        base_currency=Currency.USD,
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    acc = PortfolioAccount(
        id=acc_id,
        portfolio_id=port_id,
        name="Main Account",
        base_currency=Currency.USD,
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    client.tables["portfolios"] = [serialize_portfolio(port, owner_id)]
    client.tables["portfolio_accounts"] = [serialize_portfolio_account(acc, owner_id)]
    client.tables["portfolio_transactions"] = []
    client.tables["portfolio_import_claim_bindings"] = []

    return {
        "owner_id": owner_id,
        "client": client,
        "repo": repo,
        "port_id": port_id,
        "acc_id": acc_id,
        "repo_clock": repo_clock,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. Primary Execution & Event Types (Matrix A-D)
# ─────────────────────────────────────────────────────────────────────────────

class TestAtomicCommitExecution:
    """Matrix A-D: Core atomic commit execution across transaction types."""

    def test_commit_valid_buy_intent(self, test_context):
        """Sections 60, 65, 66: Valid BUY intent appends atomically and verifies readback."""
        repo = test_context["repo"]
        client = test_context["client"]
        port_id = test_context["port_id"]
        acc_id = test_context["acc_id"]
        owner_id = test_context["owner_id"]

        intent = _make_test_binding_intent(
            portfolio_id=port_id,
            account_id=acc_id,
            transaction_type=TransactionType.BUY,
        )

        res = repo.commit_import_binding_intent(intent)

        assert res.status == AppendStatus.APPENDED
        assert res.transaction_id is not None
        assert len(client.recorded_rpcs) == 1
        fn_name, params = client.recorded_rpcs[0]
        assert fn_name == "commit_portfolio_import_claim"

        # Verify transaction payload
        p_tx = params["p_transaction"]
        assert p_tx["id"] == str(res.transaction_id)
        assert p_tx["owner_id"] == str(owner_id)
        assert p_tx["portfolio_id"] == str(port_id)
        assert p_tx["account_id"] == str(acc_id)
        assert p_tx["transaction_type"] == "buy"
        assert p_tx["quantity"] == "10"
        assert p_tx["unit_price"] == "150.00"
        assert p_tx["trade_currency"] == "USD"
        assert p_tx["external_source"] is None
        assert p_tx["external_reference"] is None
        assert p_tx["cash_bucket_id"] is None
        assert p_tx["notes"] is None

        # Verify binding payload
        p_b = params["p_binding"]
        assert p_b["transaction_id"] == str(res.transaction_id)
        assert p_b["owner_id"] == str(owner_id)
        assert p_b["portfolio_id"] == str(port_id)
        assert p_b["account_id"] == str(acc_id)
        assert p_b["source_key"] == "sentinax_csv"
        assert p_b["record_ordinal"] == 1
        assert "bound_at" not in p_b

    def test_commit_valid_cash_deposit_intent(self, test_context):
        """Section 61: CASH_DEPOSIT intent commits without instrument or cash bucket."""
        repo = test_context["repo"]
        port_id = test_context["port_id"]
        acc_id = test_context["acc_id"]

        intent = _make_test_binding_intent(
            portfolio_id=port_id,
            account_id=acc_id,
            transaction_type=TransactionType.CASH_DEPOSIT,
        )

        res = repo.commit_import_binding_intent(intent)

        assert res.status == AppendStatus.APPENDED
        tx = repo.get_transaction(port_id, res.transaction_id)
        assert tx is not None
        assert tx.transaction_type == TransactionType.CASH_DEPOSIT
        assert tx.instrument_id is None
        assert tx.cash_amount == Decimal("500.00")
        assert tx.cash_currency == Currency.TRY
        assert tx.cash_bucket_id is None

    def test_commit_valid_fx_conversion_intent(self, test_context):
        """Section 62: FX_CONVERSION intent preserves both legs."""
        repo = test_context["repo"]
        port_id = test_context["port_id"]
        acc_id = test_context["acc_id"]

        intent = _make_test_binding_intent(
            portfolio_id=port_id,
            account_id=acc_id,
            transaction_type=TransactionType.FX_CONVERSION,
        )

        res = repo.commit_import_binding_intent(intent)

        assert res.status == AppendStatus.APPENDED
        tx = repo.get_transaction(port_id, res.transaction_id)
        assert tx is not None
        assert tx.transaction_type == TransactionType.FX_CONVERSION
        assert tx.from_currency == Currency.USD
        assert tx.from_amount == Decimal("100.00")
        assert tx.to_currency == Currency.TRY
        assert tx.to_amount == Decimal("3400.00")
        assert tx.cash_bucket_id is None

    def test_commit_dividend_referenced_and_unreferenced(self, test_context):
        """Section 63: DIVIDEND with and without instrument."""
        repo = test_context["repo"]
        port_id = test_context["port_id"]
        acc_id = test_context["acc_id"]

        # Referenced DIVIDEND
        inst_id = uuid4()
        intent_ref = _make_test_binding_intent(
            portfolio_id=port_id,
            account_id=acc_id,
            transaction_type=TransactionType.DIVIDEND,
            instrument_id=inst_id,
            ordinal=1,
        )
        res_ref = repo.commit_import_binding_intent(intent_ref)
        assert res_ref.status == AppendStatus.APPENDED
        tx_ref = repo.get_transaction(port_id, res_ref.transaction_id)
        assert tx_ref.instrument_id == inst_id

        # Unreferenced DIVIDEND
        intent_unref = _make_test_binding_intent(
            portfolio_id=port_id,
            account_id=acc_id,
            transaction_type=TransactionType.DIVIDEND,
            instrument_id=None,
            ordinal=2,
        )
        res_unref = repo.commit_import_binding_intent(intent_unref)
        assert res_unref.status == AppendStatus.APPENDED
        tx_unref = repo.get_transaction(port_id, res_unref.transaction_id)
        assert tx_unref.instrument_id is None


# ─────────────────────────────────────────────────────────────────────────────
# 2. Clock, UUID, and Idempotency Semantics (Sections 64, 67, 68, 69)
# ─────────────────────────────────────────────────────────────────────────────

class TestClockAndIdempotency:
    """Sections 64, 67, 68, 69: Recorded_at clock, replay duplicate, and conflict."""

    def test_recorded_at_uses_repository_system_clock(self, test_context):
        """Section 64: Candidate transaction recorded_at comes from repo clock, not imported_at."""
        repo = test_context["repo"]
        port_id = test_context["port_id"]
        acc_id = test_context["acc_id"]
        repo_clock = test_context["repo_clock"]

        imported_at = datetime(2020, 1, 1, 12, 0, tzinfo=timezone.utc)
        intent = _make_test_binding_intent(
            portfolio_id=port_id,
            account_id=acc_id,
            imported_at=imported_at,
        )

        res = repo.commit_import_binding_intent(intent)
        assert res.status == AppendStatus.APPENDED

        tx = repo.get_transaction(port_id, res.transaction_id)
        assert tx.recorded_at == repo_clock
        assert tx.recorded_at != imported_at

    def test_idempotent_replay_returns_existing_transaction_id(self, test_context):
        """Section 67: Exact replay of same claim returns IDEMPOTENT_DUPLICATE with existing ID."""
        repo = test_context["repo"]
        port_id = test_context["port_id"]
        acc_id = test_context["acc_id"]

        intent = _make_test_binding_intent(portfolio_id=port_id, account_id=acc_id)

        res1 = repo.commit_import_binding_intent(intent)
        assert res1.status == AppendStatus.APPENDED

        res2 = repo.commit_import_binding_intent(intent)
        assert res2.status == AppendStatus.IDEMPOTENT_DUPLICATE
        assert res2.transaction_id == res1.transaction_id

    def test_idempotent_readback_fingerprint_mismatch_fails_closed(self, test_context):
        """Section 68: If RPC claims idempotent duplicate but readback fingerprint mismatches, fail closed."""
        repo = test_context["repo"]
        client = test_context["client"]
        port_id = test_context["port_id"]
        acc_id = test_context["acc_id"]

        intent = _make_test_binding_intent(portfolio_id=port_id, account_id=acc_id)
        res1 = repo.commit_import_binding_intent(intent)
        assert res1.status == AppendStatus.APPENDED

        # Corrupt the stored transaction's quantity
        client.tables["portfolio_transactions"][0]["quantity"] = "999.00"
        client.tables["portfolio_transactions"][0]["economic_fingerprint"] = "0" * 64

        # Next call returns idempotent_duplicate pointing to the corrupted tx
        client.next_rpc_result = [{
            "commit_status": "idempotent_duplicate",
            "transaction_id": str(res1.transaction_id),
            "diagnostic": "corrupted",
        }]

        with pytest.raises(Exception):
            repo.commit_import_binding_intent(intent)

    def test_same_claim_changed_plan_returns_conflict(self, test_context):
        """Section 69: Same raw claim but changed plan returns CONFLICT without append."""
        repo = test_context["repo"]
        port_id = test_context["port_id"]
        acc_id = test_context["acc_id"]

        # First claim with price 150.00
        intent1 = _make_test_binding_intent(
            portfolio_id=port_id,
            account_id=acc_id,
            raw_content=b"file_v1",
            ordinal=1,
            unit_price=Decimal("150.00"),
        )
        res1 = repo.commit_import_binding_intent(intent1)
        assert res1.status == AppendStatus.APPENDED

        # Second claim with identical raw claim identity but price 200.00 (different plan SHA)
        intent2 = _make_test_binding_intent(
            portfolio_id=port_id,
            account_id=acc_id,
            raw_content=b"file_v1",
            ordinal=1,
            unit_price=Decimal("200.00"),
        )
        res2 = repo.commit_import_binding_intent(intent2)
        assert res2.status == AppendStatus.CONFLICT
        assert res2.transaction_id == res1.transaction_id


# ─────────────────────────────────────────────────────────────────────────────
# 3. Fail-Closed Validation & Error Handling (Sections 70-72)
# ─────────────────────────────────────────────────────────────────────────────

class TestFailClosedValidation:
    """Sections 70-72: Preflight validation and malformed RPC returns."""

    def test_missing_parent_portfolio_returns_invalid(self, test_context):
        """Section 71: Missing portfolio returns AppendStatus.INVALID, RPC not called."""
        repo = test_context["repo"]
        client = test_context["client"]
        acc_id = test_context["acc_id"]

        missing_port_id = uuid4()
        intent = _make_test_binding_intent(portfolio_id=missing_port_id, account_id=acc_id)

        res = repo.commit_import_binding_intent(intent)
        assert res.status == AppendStatus.INVALID
        assert len(client.recorded_rpcs) == 0

    def test_missing_parent_account_returns_invalid(self, test_context):
        """Section 71: Missing account returns AppendStatus.INVALID, RPC not called."""
        repo = test_context["repo"]
        client = test_context["client"]
        port_id = test_context["port_id"]

        missing_acc_id = uuid4()
        intent = _make_test_binding_intent(portfolio_id=port_id, account_id=missing_acc_id)

        res = repo.commit_import_binding_intent(intent)
        assert res.status == AppendStatus.INVALID
        assert len(client.recorded_rpcs) == 0

    def test_wrong_input_type_rejected(self, test_context):
        """Section 33: Non-intent input rejected before RPC."""
        repo = test_context["repo"]
        with pytest.raises(TypeError, match="ImportLedgerBindingIntent"):
            repo.commit_import_binding_intent("not_an_intent")  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad_res", [
        None,
        [],
        [{"commit_status": "unknown", "transaction_id": str(uuid4())}],
        [{"commit_status": "appended"}],  # missing transaction_id
        [{"commit_status": "appended", "transaction_id": "not-a-uuid"}],
        [{"commit_status": "appended", "transaction_id": str(uuid4())}, {"commit_status": "appended"}],
    ])
    def test_malformed_rpc_results_fail_closed(self, test_context, bad_res):
        """Section 70: Malformed RPC return shapes raise RuntimeError."""
        repo = test_context["repo"]
        client = test_context["client"]
        port_id = test_context["port_id"]
        acc_id = test_context["acc_id"]

        intent = _make_test_binding_intent(portfolio_id=port_id, account_id=acc_id)
        client.next_rpc_result = bad_res

        with pytest.raises(RuntimeError):
            repo.commit_import_binding_intent(intent)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Static Source Inspection Tests (Sections 73-79)
# ─────────────────────────────────────────────────────────────────────────────

class TestStaticSourceIntegrity:
    """Sections 73-79: Verify architectural constraints in repository implementation."""

    def test_no_non_atomic_table_writes_in_commit_import_binding_intent(self):
        """Section 73: commit_import_binding_intent contains zero table inserts."""
        src = inspect.getsource(PortfolioRepository.commit_import_binding_intent)
        assert '.table("portfolio_transactions").insert' not in src
        assert '.table("portfolio_import_claim_bindings").insert' not in src

    def test_no_append_transaction_call(self):
        """Section 74: commit_import_binding_intent does not call append_transaction."""
        src = inspect.getsource(PortfolioRepository.commit_import_binding_intent)
        assert 'append_transaction' not in src

    def test_no_external_identity_lookup_call(self):
        """Section 75: commit_import_binding_intent does not call lookup_external_identity."""
        src = inspect.getsource(PortfolioRepository.commit_import_binding_intent)
        assert 'lookup_external_identity' not in src

    def test_no_clock_calls_in_method(self):
        """Section 77: commit_import_binding_intent uses self._get_system_time() and no datetime.now."""
        src = inspect.getsource(PortfolioRepository.commit_import_binding_intent)
        assert 'datetime.now' not in src
        assert 'datetime.utcnow' not in src
        assert 'date.today' not in src
        assert 'self._get_system_time()' in src

    def test_uses_portfolio_transaction_economic_fingerprint(self):
        """Section 78: Uses PortfolioTransaction.economic_fingerprint()."""
        src = inspect.getsource(PortfolioRepository.commit_import_binding_intent)
        assert 'economic_fingerprint()' in src
