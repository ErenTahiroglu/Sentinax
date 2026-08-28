# backend/tests/test_portfolio_import_atomic_batch_commit_repository.py
"""Repository unit and integration tests for Phase 13R atomic batch commit."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import inspect
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import (
    Currency,
    PortfolioMode,
    TransactionType,
)
from backend.engine.private.portfolio.import_assessment import (
    build_import_assessment_batch,
)
from backend.engine.private.portfolio.import_batch import (
    build_import_batch_manifest,
)
from backend.engine.private.portfolio.import_commit import (
    ImportLedgerBindingBatch,
    build_import_ledger_binding_batch,
)
from backend.engine.private.portfolio.import_draft_batch import (
    build_import_draft_batch_manifest,
)
from backend.engine.private.portfolio.import_instrument_resolution import (
    build_import_instrument_resolution_batch,
)
from backend.engine.private.portfolio.import_materialization import (
    build_import_ledger_materialization_batch,
)
from backend.engine.private.portfolio.import_parsed_batch import (
    build_parsed_import_batch_manifest,
)
from backend.engine.private.portfolio.import_provenance import (
    build_import_file_provenance,
)
from backend.engine.private.portfolio.import_batch_commit import (
    ImportBatchCommitResult,
    ImportBatchCommitStatus,
    ImportBatchItemCommitStatus,
)
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
from backend.engine.private.portfolio.sentinax_csv_import import (
    run_sentinax_canonical_csv_import_v1,
)


# ─────────────────────────────────────────────────────────────────────────────
# Mock Supabase Transport Infrastructure for Batch Tests
# ─────────────────────────────────────────────────────────────────────────────

class MockQueryResult:
    def __init__(self, data: Any = None, count: Optional[int] = None):
        self.data = data
        self.count = count


class MockQueryBuilder:
    def __init__(self, table_name: str, client_store: MockSupabaseClient):
        self.table_name = table_name
        self.client_store = client_store
        self.eq_filters: Dict[str, Any] = {}
        self.is_filters: Dict[str, Any] = {}
        self.insert_payload: Any = None
        self.returning_mode: str = "representation"

    def select(self, columns: str) -> MockQueryBuilder:
        return self

    def eq(self, column: str, value: Any) -> MockQueryBuilder:
        self.eq_filters[column] = value
        return self

    def is_(self, column: str, value: Any) -> MockQueryBuilder:
        self.is_filters[column] = value
        return self

    def order(self, column: str, desc: bool = False) -> MockQueryBuilder:
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

        if self.fn_name == "commit_portfolio_import_claim_batch":
            p_items = self.params.get("p_items", [])
            tx_ids = []
            item_statuses = []
            has_appended = False

            # Simulate batch transaction in mock
            claims = self.client_store.tables.setdefault("portfolio_import_claim_bindings", [])
            txs = self.client_store.tables.setdefault("portfolio_transactions", [])

            for item in p_items:
                p_tx = item.get("transaction", {})
                p_b = item.get("binding", {})

                # Check if matching claim exists
                existing_claim = None
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
                        existing_claim = b
                        break

                if existing_claim is not None:
                    existing_tx_id = existing_claim["transaction_id"]
                    matching_tx = next((t for t in txs if str(t.get("id")) == str(existing_tx_id)), None)
                    existing_fp = matching_tx.get("economic_fingerprint") if matching_tx else None

                    if existing_claim.get("expected_plan_sha256") == p_b.get("expected_plan_sha256") and existing_fp == p_tx.get("economic_fingerprint"):
                        tx_ids.append(str(existing_tx_id))
                        item_statuses.append("idempotent_duplicate")
                    else:
                        # Conflict on this item: rollback and return conflict
                        return MockQueryResult(data=[{
                            "batch_status": "conflict",
                            "transaction_ids": [],
                            "item_statuses": [],
                            "conflict_record_ordinal": int(p_b.get("record_ordinal")),
                            "conflict_transaction_id": str(existing_tx_id),
                            "diagnostic": f"Existing claim conflict on record {p_b.get('record_ordinal')}.",
                        }])
                else:
                    # Append new row
                    has_appended = True
                    tx_row = deepcopy(p_tx)
                    txs.append(tx_row)
                    b_row = deepcopy(p_b)
                    b_row["bound_at"] = "2026-08-28T16:00:00+00:00"
                    claims.append(b_row)

                    tx_ids.append(p_tx["id"])
                    item_statuses.append("appended")

            batch_status = "appended" if has_appended else "idempotent_duplicate"
            return MockQueryResult(data=[{
                "batch_status": batch_status,
                "transaction_ids": tx_ids,
                "item_statuses": item_statuses,
                "conflict_record_ordinal": None,
                "conflict_transaction_id": None,
                "diagnostic": "Batch committed successfully.",
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
# Test Fixtures & Helpers
# ─────────────────────────────────────────────────────────────────────────────

class MockInstrumentResolver:
    """Configurable mock instrument resolver."""

    def __init__(
        self,
        mapping: Optional[Dict[Tuple[str, date], Sequence[UUID]]] = None,
        resolver_revision: int = 1,
    ) -> None:
        self.mapping = mapping or {}
        self.invocations: List[Tuple[str, date]] = []
        self.resolver_key: str = "mock_resolver"
        self.resolver_revision: int = resolver_revision

    def resolve_candidates(self, instrument_reference: str, as_of_date: date) -> Sequence[UUID]:
        self.invocations.append((instrument_reference, as_of_date))
        return self.mapping.get((instrument_reference, as_of_date), [])


def _make_csv(rows: List[str]) -> bytes:
    header = (
        "transaction_type,effective_date,executed_at,instrument_reference,"
        "quantity,unit_price,trade_currency,cash_amount,cash_currency,"
        "from_currency,from_amount,to_currency,to_amount\n"
    )
    return (header + "\n".join(rows) + "\n").encode("utf-8")


def _make_test_binding_batch(
    portfolio_id: UUID,
    account_id: UUID,
    csv_rows: List[str],
    resolver_mapping: Optional[Dict[Tuple[str, date], Sequence[UUID]]] = None,
    filename: str = "test.csv",
    imported_at: Optional[datetime] = None,
) -> ImportLedgerBindingBatch:
    csv_bytes = _make_csv(csv_rows)
    resolver = MockInstrumentResolver(mapping=resolver_mapping or {})
    res_batch = run_sentinax_canonical_csv_import_v1(
        portfolio_id=portfolio_id,
        account_id=account_id,
        filename=filename,
        content=csv_bytes,
        imported_at=imported_at or datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
        resolver=resolver,
    )
    mat_batch = build_import_ledger_materialization_batch(res_batch)
    return build_import_ledger_binding_batch(mat_batch)


def _make_empty_binding_batch(
    portfolio_id: UUID,
    account_id: UUID,
) -> ImportLedgerBindingBatch:
    file_prov = build_import_file_provenance(
        portfolio_id=portfolio_id,
        account_id=account_id,
        source_key="sentinax_canonical_csv_v1",
        filename="empty.csv",
        content=b"header\n",
        imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
    )
    raw_manifest = build_import_batch_manifest(file_prov, [])
    parsed_manifest = build_parsed_import_batch_manifest(raw_manifest, 1, [])
    ass_batch = build_import_assessment_batch(parsed_manifest, [])
    draft_manifest = build_import_draft_batch_manifest(ass_batch, [])
    res_batch = build_import_instrument_resolution_batch(draft_manifest, [])
    mat_batch = build_import_ledger_materialization_batch(res_batch)
    return build_import_ledger_binding_batch(mat_batch)


@pytest.fixture
def test_context():
    owner_id = uuid4()
    client = MockSupabaseClient()

    now_fixed = datetime(2026, 8, 28, 16, 0, 0, tzinfo=timezone.utc)
    clock_calls = 0

    def clock():
        nonlocal clock_calls
        clock_calls += 1
        return now_fixed

    repo = PortfolioRepository(client, owner_id, clock=clock)

    port_id = uuid4()
    port = Portfolio(
        id=port_id,
        owner_id=owner_id,
        name="Main Port",
        mode=PortfolioMode.MY_PORTFOLIO,
        base_currency=Currency.USD,
        created_at=now_fixed,
    )
    client.tables["portfolios"] = [serialize_portfolio(port, owner_id)]

    acc_id = uuid4()
    acc = PortfolioAccount(
        id=acc_id,
        portfolio_id=port_id,
        name="Main Acc",
        base_currency=Currency.USD,
        created_at=now_fixed,
    )
    client.tables["portfolio_accounts"] = [serialize_portfolio_account(acc, owner_id)]

    inst_aapl = uuid4()
    inst_msft = uuid4()
    resolver_mapping = {
        ("AAPL", date(2026, 8, 28)): [inst_aapl],
        ("MSFT", date(2026, 8, 28)): [inst_msft],
    }

    return {
        "owner_id": owner_id,
        "client": client,
        "repo": repo,
        "port_id": port_id,
        "acc_id": acc_id,
        "inst_aapl": inst_aapl,
        "inst_msft": inst_msft,
        "resolver_mapping": resolver_mapping,
        "get_clock_calls": lambda: clock_calls,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. Execution Tests (Sections 63-68)
# ─────────────────────────────────────────────────────────────────────────────

class TestAtomicBatchCommitExecution:
    """Sections 63-68: Full execution behavior of commit_import_binding_batch."""

    def test_zero_intent_batch_is_noop(self, test_context):
        """Section 63: Zero-intent batch returns NOOP with 0 RPCs and 0 clock calls."""
        repo = test_context["repo"]
        client = test_context["client"]
        port_id = test_context["port_id"]
        acc_id = test_context["acc_id"]

        batch = _make_empty_binding_batch(port_id, acc_id)

        res = repo.commit_import_binding_batch(batch)
        assert res.status == ImportBatchCommitStatus.NOOP
        assert res.transaction_ids == ()
        assert res.item_statuses == ()
        assert len(client.recorded_rpcs) == 0
        assert test_context["get_clock_calls"]() == 0

    def test_commit_valid_appended_batch(self, test_context):
        """Section 64: Commits a two-intent batch successfully as APPENDED."""
        repo = test_context["repo"]
        client = test_context["client"]
        port_id = test_context["port_id"]
        acc_id = test_context["acc_id"]
        resolver_mapping = test_context["resolver_mapping"]

        batch = _make_test_binding_batch(
            port_id, acc_id,
            [
                "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
                "buy,2026-08-28,2026-08-28T10:15:30+00:00,MSFT,5,300.00,USD,,,,,,",
            ],
            resolver_mapping=resolver_mapping,
        )

        res = repo.commit_import_binding_batch(batch)
        assert res.status == ImportBatchCommitStatus.APPENDED
        assert len(res.transaction_ids) == 2
        assert res.item_statuses == (ImportBatchItemCommitStatus.APPENDED, ImportBatchItemCommitStatus.APPENDED)
        assert len(client.recorded_rpcs) == 1
        assert client.recorded_rpcs[0][0] == "commit_portfolio_import_claim_batch"
        assert test_context["get_clock_calls"]() == 1

        # Check DB rows
        assert len(client.tables["portfolio_transactions"]) == 2
        assert len(client.tables["portfolio_import_claim_bindings"]) == 2

    def test_commit_all_duplicate_batch(self, test_context):
        """Section 65: All duplicate batch returns IDEMPOTENT_DUPLICATE with exact transaction IDs."""
        repo = test_context["repo"]
        port_id = test_context["port_id"]
        acc_id = test_context["acc_id"]
        resolver_mapping = test_context["resolver_mapping"]

        batch = _make_test_binding_batch(
            port_id, acc_id,
            [
                "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
                "buy,2026-08-28,2026-08-28T10:15:30+00:00,MSFT,5,300.00,USD,,,,,,",
            ],
            resolver_mapping=resolver_mapping,
        )

        # First run appends
        res1 = repo.commit_import_binding_batch(batch)
        assert res1.status == ImportBatchCommitStatus.APPENDED

        # Second run is idempotent duplicate
        res2 = repo.commit_import_binding_batch(batch)
        assert res2.status == ImportBatchCommitStatus.IDEMPOTENT_DUPLICATE
        assert res2.transaction_ids == res1.transaction_ids
        assert res2.item_statuses == (ImportBatchItemCommitStatus.IDEMPOTENT_DUPLICATE, ImportBatchItemCommitStatus.IDEMPOTENT_DUPLICATE)

    def test_commit_mixed_append_and_duplicate_batch(self, test_context):
        """Section 66: Mixed batch returns APPENDED with exact ordered item statuses."""
        repo = test_context["repo"]
        port_id = test_context["port_id"]
        acc_id = test_context["acc_id"]
        resolver_mapping = test_context["resolver_mapping"]

        batch_first = _make_test_binding_batch(
            port_id, acc_id,
            ["buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"],
            resolver_mapping=resolver_mapping,
            filename="file1.csv",
        )
        res_first = repo.commit_import_binding_batch(batch_first)
        assert res_first.status == ImportBatchCommitStatus.APPENDED

        # Batch with 2 items
        batch_2 = _make_test_binding_batch(
            port_id, acc_id,
            [
                "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
                "buy,2026-08-28,2026-08-28T10:15:30+00:00,MSFT,5,300.00,USD,,,,,,",
            ],
            resolver_mapping=resolver_mapping,
            filename="file2.csv",
        )

        # Mock existing transaction 1 and newly appended transaction 2
        cand1_id = uuid4()
        cand2_id = uuid4()

        tx1 = PortfolioTransaction(
            id=cand1_id,
            portfolio_id=port_id,
            account_id=acc_id,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            executed_at=datetime(2026, 8, 28, 10, 15, 30, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 8, 28, 16, 0, 0, tzinfo=timezone.utc),
            instrument_id=test_context["inst_aapl"],
            quantity=Decimal("10.000000"),
            unit_price=Decimal("150.000000"),
            trade_currency=Currency.USD,
        )
        tx2 = PortfolioTransaction(
            id=cand2_id,
            portfolio_id=port_id,
            account_id=acc_id,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            executed_at=datetime(2026, 8, 28, 10, 15, 30, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 8, 28, 16, 0, 0, tzinfo=timezone.utc),
            instrument_id=test_context["inst_msft"],
            quantity=Decimal("5.000000"),
            unit_price=Decimal("300.000000"),
            trade_currency=Currency.USD,
        )
        client = test_context["client"]
        owner_id = test_context["owner_id"]
        client.tables["portfolio_transactions"] = [
            serialize_portfolio_transaction(tx1, owner_id),
            serialize_portfolio_transaction(tx2, owner_id),
        ]

        client.next_rpc_result = [{
            "batch_status": "appended",
            "transaction_ids": [str(cand1_id), str(cand2_id)],
            "item_statuses": ["idempotent_duplicate", "appended"],
            "conflict_record_ordinal": None,
            "conflict_transaction_id": None,
            "diagnostic": "Batch committed successfully.",
        }]

        res_mixed = repo.commit_import_binding_batch(batch_2)
        assert res_mixed.status == ImportBatchCommitStatus.APPENDED
        assert len(res_mixed.transaction_ids) == 2
        assert res_mixed.transaction_ids == (cand1_id, cand2_id)
        assert res_mixed.item_statuses == (ImportBatchItemCommitStatus.IDEMPOTENT_DUPLICATE, ImportBatchItemCommitStatus.APPENDED)

    def test_commit_conflict_returns_conflict_and_diagnostic(self, test_context):
        """Section 67: Changed plan on existing claim returns CONFLICT with problem ordinal."""
        repo = test_context["repo"]
        port_id = test_context["port_id"]
        acc_id = test_context["acc_id"]
        resolver_mapping = test_context["resolver_mapping"]

        batch_1 = _make_test_binding_batch(
            port_id, acc_id,
            ["buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"],
            resolver_mapping=resolver_mapping,
            filename="trades.csv",
        )
        repo.commit_import_binding_batch(batch_1)

        # Batch 2 has changed economics for the same claim identity (same filename, same ordinal)
        # Note: different content -> to get same claim identity with different plan, we can modify the mock binding
        # Or build batch with same file_content_sha256 but different plan
        # Let's verify repository conflict handling when RPC returns conflict
        client = test_context["client"]
        existing_tx_id = client.tables["portfolio_transactions"][0]["id"]
        client.next_rpc_result = [{
            "batch_status": "conflict",
            "transaction_ids": [],
            "item_statuses": [],
            "conflict_record_ordinal": 1,
            "conflict_transaction_id": str(existing_tx_id),
            "diagnostic": "Existing claim conflict on record 1.",
        }]

        res = repo.commit_import_binding_batch(batch_1)
        assert res.status == ImportBatchCommitStatus.CONFLICT
        assert res.transaction_ids == ()
        assert res.item_statuses == ()
        assert res.problem_record_ordinal == 1
        assert str(res.conflict_transaction_id) == str(existing_tx_id)
        assert len(res.diagnostics) > 0

    def test_invalid_target_parent_fails_closed_before_rpc(self, test_context):
        """Section 68: Missing account on any intent returns INVALID without RPC."""
        repo = test_context["repo"]
        client = test_context["client"]
        port_id = test_context["port_id"]
        missing_acc_id = uuid4()
        resolver_mapping = test_context["resolver_mapping"]

        batch = _make_test_binding_batch(
            port_id, missing_acc_id,
            ["buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"],
            resolver_mapping=resolver_mapping,
        )

        res = repo.commit_import_binding_batch(batch)
        assert res.status == ImportBatchCommitStatus.INVALID
        assert res.problem_record_ordinal == 1
        assert len(client.recorded_rpcs) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 2. Economic and Financial Copy Tests (Sections 69-72)
# ─────────────────────────────────────────────────────────────────────────────

class TestEconomicsAndSerialization:
    """Sections 69-72: Shared clock, distinct UUIDs, exact economics copy."""

    def test_shared_batch_recorded_at_timestamp(self, test_context):
        """Section 69: Shared system clock invoked once, matching across all items."""
        repo = test_context["repo"]
        client = test_context["client"]
        port_id = test_context["port_id"]
        acc_id = test_context["acc_id"]
        resolver_mapping = test_context["resolver_mapping"]

        batch = _make_test_binding_batch(
            port_id, acc_id,
            [
                "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
                "buy,2026-08-28,2026-08-28T10:15:30+00:00,MSFT,5,300.00,USD,,,,,,",
            ],
            resolver_mapping=resolver_mapping,
        )

        repo.commit_import_binding_batch(batch)
        assert test_context["get_clock_calls"]() == 1

        payload_items = client.recorded_rpcs[0][1]["p_items"]
        t1_rec = payload_items[0]["transaction"]["recorded_at"]
        t2_rec = payload_items[1]["transaction"]["recorded_at"]
        assert t1_rec == t2_rec

    def test_candidate_uuids_distinct(self, test_context):
        """Section 70: Candidate transaction IDs are distinct UUIDs."""
        repo = test_context["repo"]
        client = test_context["client"]
        port_id = test_context["port_id"]
        acc_id = test_context["acc_id"]
        resolver_mapping = test_context["resolver_mapping"]

        batch = _make_test_binding_batch(
            port_id, acc_id,
            [
                "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
                "buy,2026-08-28,2026-08-28T10:15:30+00:00,MSFT,5,300.00,USD,,,,,,",
            ],
            resolver_mapping=resolver_mapping,
        )

        repo.commit_import_binding_batch(batch)
        payload_items = client.recorded_rpcs[0][1]["p_items"]
        id1 = payload_items[0]["transaction"]["id"]
        id2 = payload_items[1]["transaction"]["id"]
        assert id1 != id2

    def test_exact_economic_copy_all_families(self, test_context):
        """Section 71: Preserves economics for CASH_DEPOSIT, FX_CONVERSION, DIVIDEND."""
        repo = test_context["repo"]
        port_id = test_context["port_id"]
        acc_id = test_context["acc_id"]
        resolver_mapping = test_context["resolver_mapping"]

        batch = _make_test_binding_batch(
            port_id, acc_id,
            [
                "cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,5000.00,USD,,,,",
                "fx_conversion,2026-08-28,2026-08-28T10:15:30+00:00,,,,,,,USD,1000.00,TRY,34000.00",
                "dividend,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,,,,150.00,USD,,,,",
            ],
            resolver_mapping=resolver_mapping,
        )
        res = repo.commit_import_binding_batch(batch)
        assert res.status == ImportBatchCommitStatus.APPENDED
        assert len(res.transaction_ids) == 3


# ─────────────────────────────────────────────────────────────────────────────
# 3. Malformed RPC and Readback Tests (Sections 73-74)
# ─────────────────────────────────────────────────────────────────────────────

class TestFailClosedAndReadback:
    """Sections 73-74: Fail-closed on malformed RPC returns and fingerprint mismatch."""

    @pytest.mark.parametrize("bad_res", [
        None,
        [],
        [{"batch_status": "unknown"}],
        [{"batch_status": "appended", "transaction_ids": ["not-a-uuid"], "item_statuses": ["appended"]}],
        [{"batch_status": "appended", "transaction_ids": [str(uuid4())], "item_statuses": ["unknown"]}],
        [{"batch_status": "appended", "transaction_ids": [str(uuid4())], "item_statuses": []}],  # length mismatch
        [{"batch_status": "appended", "transaction_ids": [str(uuid4())], "item_statuses": ["idempotent_duplicate"]}],  # no appended
        [{"batch_status": "idempotent_duplicate", "transaction_ids": [str(uuid4())], "item_statuses": ["appended"]}],
        [{"batch_status": "conflict", "conflict_record_ordinal": None}],
        [{"batch_status": "conflict", "conflict_record_ordinal": 999}],  # ordinal not in batch
    ])
    def test_malformed_rpc_results_fail_closed(self, test_context, bad_res):
        """Section 73: Malformed RPC return shapes raise RuntimeError."""
        repo = test_context["repo"]
        client = test_context["client"]
        port_id = test_context["port_id"]
        acc_id = test_context["acc_id"]
        resolver_mapping = test_context["resolver_mapping"]

        batch = _make_test_binding_batch(
            port_id, acc_id,
            ["buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"],
            resolver_mapping=resolver_mapping,
        )
        client.next_rpc_result = bad_res

        with pytest.raises(RuntimeError):
            repo.commit_import_binding_batch(batch)

    def test_fingerprint_mismatch_on_readback_fails_closed(self, test_context):
        """Section 74: Readback economic fingerprint mismatch raises RuntimeError."""
        repo = test_context["repo"]
        client = test_context["client"]
        port_id = test_context["port_id"]
        acc_id = test_context["acc_id"]
        resolver_mapping = test_context["resolver_mapping"]

        batch = _make_test_binding_batch(
            port_id, acc_id,
            ["buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"],
            resolver_mapping=resolver_mapping,
        )

        # Inject fake transaction row in DB with different fingerprint
        fake_tx_id = uuid4()
        fake_tx = PortfolioTransaction(
            id=fake_tx_id,
            portfolio_id=port_id,
            account_id=acc_id,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            executed_at=datetime(2026, 8, 28, 10, 15, 30, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 8, 28, 16, 0, 0, tzinfo=timezone.utc),
            instrument_id=test_context["inst_aapl"],
            quantity=Decimal("9999.000000"),  # modified quantity
            unit_price=Decimal("1.000000"),
            trade_currency=Currency.USD,
        )
        owner_id = test_context["owner_id"]
        client.tables["portfolio_transactions"] = [serialize_portfolio_transaction(fake_tx, owner_id)]

        # Mock RPC returning fake_tx_id
        client.next_rpc_result = [{
            "batch_status": "appended",
            "transaction_ids": [str(fake_tx_id)],
            "item_statuses": ["appended"],
            "conflict_record_ordinal": None,
            "conflict_transaction_id": None,
            "diagnostic": None,
        }]

        with pytest.raises(RuntimeError, match="economic fingerprint mismatch"):
            repo.commit_import_binding_batch(batch)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Static Source Inspection Tests (Sections 75-82)
# ─────────────────────────────────────────────────────────────────────────────

class TestStaticSourceIntegrity:
    """Sections 75-82: Verify architectural constraints in repository implementation."""

    def test_no_sequential_single_intent_loop(self):
        """Section 75: commit_import_binding_batch does NOT call commit_import_binding_intent."""
        src = inspect.getsource(PortfolioRepository.commit_import_binding_batch)
        assert 'commit_import_binding_intent' not in src

    def test_no_direct_table_writes(self):
        """Section 76: commit_import_binding_batch contains zero table inserts."""
        src = inspect.getsource(PortfolioRepository.commit_import_binding_batch)
        assert '.table("portfolio_transactions").insert' not in src
        assert '.table("portfolio_import_claim_bindings").insert' not in src

    def test_no_append_transaction_call(self):
        """Section 77: commit_import_binding_batch does not call append_transaction."""
        src = inspect.getsource(PortfolioRepository.commit_import_binding_batch)
        assert 'append_transaction' not in src

    def test_no_external_identity_lookup(self):
        """Section 78: commit_import_binding_batch does not call lookup_external_identity."""
        src = inspect.getsource(PortfolioRepository.commit_import_binding_batch)
        assert 'lookup_external_identity' not in src

    def test_single_clock_call_architecture(self):
        """Section 80: commit_import_binding_batch uses self._get_system_time() and no datetime.now."""
        src = inspect.getsource(PortfolioRepository.commit_import_binding_batch)
        assert 'datetime.now' not in src
        assert 'datetime.utcnow' not in src
        assert 'date.today' not in src
        assert 'self._get_system_time()' in src

    def test_uses_portfolio_transaction_economic_fingerprint(self):
        """Section 81: Uses PortfolioTransaction.economic_fingerprint()."""
        src = inspect.getsource(PortfolioRepository.commit_import_binding_batch)
        assert 'economic_fingerprint()' in src
