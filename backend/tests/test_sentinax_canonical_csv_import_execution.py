# backend/tests/test_sentinax_canonical_csv_import_execution.py
"""
Test suite for Phase 13S: End-to-End Canonical CSV Import Execution Orchestrator & Execution Result.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import inspect
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import Currency, PortfolioMode, TransactionType
from backend.engine.private.portfolio.import_assessment import (
    ImportAssessmentStatus,
    build_import_assessment_batch,
)
from backend.engine.private.portfolio.import_batch import (
    build_import_batch_manifest,
)
from backend.engine.private.portfolio.import_batch_commit import (
    ImportBatchCommitResult,
    ImportBatchCommitStatus,
    ImportBatchItemCommitStatus,
)
from backend.engine.private.portfolio.import_commit import (
    ImportLedgerBindingBatch,
    build_import_ledger_binding_batch,
)
from backend.engine.private.portfolio.import_draft_batch import (
    build_import_draft_batch_manifest,
)
from backend.engine.private.portfolio.import_instrument_resolution import (
    ImportInstrumentResolutionBatch,
    ImportInstrumentResolutionStatus,
    build_import_instrument_resolution_batch,
)
from backend.engine.private.portfolio.import_instrument_resolver import (
    PortfolioImportInstrumentResolver,
)
from backend.engine.private.portfolio.import_materialization import (
    ImportLedgerMaterializationBatch,
    build_import_ledger_materialization_batch,
)
from backend.engine.private.portfolio.import_parsed_batch import (
    build_parsed_import_batch_manifest,
)
from backend.engine.private.portfolio.import_provenance import (
    build_import_file_provenance,
)
from backend.engine.private.portfolio.models import (
    Portfolio,
    PortfolioAccount,
)
from backend.engine.private.portfolio.parsers import (
    SentinaxCanonicalCsvError,
)
from backend.engine.private.portfolio.parsers.sentinax_csv_semantics import (
    SentinaxCanonicalCsvSemanticError,
)
from backend.engine.private.portfolio.persistence import (
    serialize_portfolio,
    serialize_portfolio_account,
)
from backend.engine.private.portfolio.repository import PortfolioRepository
from backend.engine.private.portfolio.sentinax_csv_import import (
    run_sentinax_canonical_csv_import_v1,
)
import backend.engine.private.portfolio.sentinax_csv_import_execution as exec_module
from backend.engine.private.portfolio.sentinax_csv_import_execution import (
    SentinaxCanonicalCsvImportExecutionResult,
    SentinaxCanonicalCsvImportExecutionStatus,
    execute_sentinax_canonical_csv_import_v1,
)


CANONICAL_HEADERS = (
    "transaction_type,effective_date,executed_at,instrument_reference,"
    "quantity,unit_price,trade_currency,cash_amount,cash_currency,"
    "from_currency,from_amount,to_currency,to_amount"
)


def _make_csv(rows: Sequence[str]) -> bytes:
    content = f"{CANONICAL_HEADERS}\n" + "\n".join(rows) + "\n"
    return content.encode("utf-8")


class MockInstrumentResolver(PortfolioImportInstrumentResolver):
    """Configurable mock instrument resolver."""
    def __init__(
        self,
        mapping: Optional[Dict[Tuple[str, date], Sequence[UUID]]] = None,
        resolver_key: str = "test_resolver",
        resolver_revision: int = 1,
        exception_on_ref: Optional[str] = None,
    ) -> None:
        self._mapping = mapping or {}
        self._resolver_key = resolver_key
        self._resolver_revision = resolver_revision
        self._exception_on_ref = exception_on_ref
        self.invocations: List[Tuple[str, date]] = []

    @property
    def resolver_key(self) -> str:
        return self._resolver_key

    @property
    def resolver_revision(self) -> int:
        return self._resolver_revision

    def resolve_candidates(
        self,
        instrument_reference: str,
        as_of_date: date,
    ) -> Sequence[UUID]:
        self.invocations.append((instrument_reference, as_of_date))
        if self._exception_on_ref and instrument_reference == self._exception_on_ref:
            raise RuntimeError(f"Resolver exploded for {instrument_reference}")
        return self._mapping.get((instrument_reference, as_of_date), ())


# ─────────────────────────────────────────────────────────────────────────────
# Mock Supabase Transport Infrastructure
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

            claims = self.client_store.tables.setdefault("portfolio_import_claim_bindings", [])
            txs = self.client_store.tables.setdefault("portfolio_transactions", [])

            for item in p_items:
                p_tx = item.get("transaction", {})
                p_b = item.get("binding", {})

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
                        return MockQueryResult(data=[{
                            "batch_status": "conflict",
                            "transaction_ids": [],
                            "item_statuses": [],
                            "conflict_record_ordinal": int(p_b.get("record_ordinal")),
                            "conflict_transaction_id": str(existing_tx_id),
                            "diagnostic": f"Existing claim conflict on record {p_b.get('record_ordinal')}.",
                        }])
                else:
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


@pytest.fixture
def repo_context():
    owner_id = uuid4()
    port_id = uuid4()
    acc_id = uuid4()

    client = MockSupabaseClient()
    portfolio = Portfolio(
        id=port_id,
        owner_id=owner_id,
        name="Test Portfolio",
        base_currency=Currency.USD,
        mode=PortfolioMode.MY_PORTFOLIO,
        created_at=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
    )
    account = PortfolioAccount(
        id=acc_id,
        portfolio_id=port_id,
        name="Test Account",
        base_currency=Currency.USD,
        created_at=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
    )
    client.tables["portfolios"] = [serialize_portfolio(portfolio, owner_id=owner_id)]
    client.tables["portfolio_accounts"] = [serialize_portfolio_account(account, owner_id=owner_id)]

    repo = PortfolioRepository(client=client, owner_id=owner_id)
    return {
        "owner_id": owner_id,
        "portfolio_id": port_id,
        "account_id": acc_id,
        "client": client,
        "repo": repo,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 55. Result Contract & Direct Constructor Tamper Tests (A-I)
# ─────────────────────────────────────────────────────────────────────────────

class TestResultContractAndDirectConstructor:
    """Sections 55 & 12: Direct constructor validation & tamper rejection."""

    def test_a_resolution_blocked_valid_direct_result(self):
        """A. RESOLUTION_BLOCKED valid direct result."""
        port_id = uuid4()
        acc_id = uuid4()
        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,UNKNOWN_SYM,10,150.00,USD,,,,,,",
        ])
        resolver = MockInstrumentResolver(mapping={})
        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="test.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )
        assert not res_batch.is_fully_resolved

        result = SentinaxCanonicalCsvImportExecutionResult(
            status=SentinaxCanonicalCsvImportExecutionStatus.RESOLUTION_BLOCKED,
            resolution_batch=res_batch,
            materialization_batch=None,
            binding_batch=None,
            commit_result=None,
        )
        assert result.status == SentinaxCanonicalCsvImportExecutionStatus.RESOLUTION_BLOCKED
        assert result.materialization_batch is None
        assert result.binding_batch is None
        assert result.commit_result is None

    def test_b_blocked_result_with_materialization_or_commit_supplied_rejected(self):
        """B. blocked result with materialization or commit supplied -> reject."""
        port_id = uuid4()
        acc_id = uuid4()
        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,UNKNOWN_SYM,10,150.00,USD,,,,,,",
        ])
        resolver = MockInstrumentResolver(mapping={})
        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="test.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )

        with pytest.raises(ValueError, match="RESOLUTION_BLOCKED result must have commit_result=None"):
            SentinaxCanonicalCsvImportExecutionResult(
                status=SentinaxCanonicalCsvImportExecutionStatus.RESOLUTION_BLOCKED,
                resolution_batch=res_batch,
                commit_result=ImportBatchCommitResult(status=ImportBatchCommitStatus.NOOP),
            )

    def test_c_fully_resolved_appended_missing_materialization_rejected(self):
        """C. fully resolved APPENDED result with missing materialization -> reject."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
        ])
        resolver = MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id]})
        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="test.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )
        assert res_batch.is_fully_resolved

        with pytest.raises(ValueError, match="APPENDED result requires non-None materialization_batch"):
            SentinaxCanonicalCsvImportExecutionResult(
                status=SentinaxCanonicalCsvImportExecutionStatus.APPENDED,
                resolution_batch=res_batch,
                materialization_batch=None,
                binding_batch=None,
                commit_result=ImportBatchCommitResult(
                    status=ImportBatchCommitStatus.APPENDED,
                    transaction_ids=(uuid4(),),
                    item_statuses=(ImportBatchItemCommitStatus.APPENDED,),
                ),
            )

    def test_d_materialization_bound_to_different_resolution_rejected(self):
        """D. materialization bound to different resolution -> reject."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id1 = uuid4()
        inst_id2 = uuid4()
        eff_date = date(2026, 8, 28)

        res_batch1 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="file1.csv",
            content=_make_csv(["buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"]),
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id1]}),
        )
        res_batch2 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="file2.csv",
            content=_make_csv(["buy,2026-08-28,2026-08-28T10:15:30+00:00,MSFT,5,300.00,USD,,,,,,"]),
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(mapping={("MSFT", eff_date): [inst_id2]}),
        )

        mat_batch2 = build_import_ledger_materialization_batch(res_batch2)
        bind_batch2 = build_import_ledger_binding_batch(mat_batch2)

        with pytest.raises(ValueError, match="materialization_batch.resolution_batch does not match resolution_batch"):
            SentinaxCanonicalCsvImportExecutionResult(
                status=SentinaxCanonicalCsvImportExecutionStatus.APPENDED,
                resolution_batch=res_batch1,  # mismatch!
                materialization_batch=mat_batch2,
                binding_batch=bind_batch2,
                commit_result=ImportBatchCommitResult(
                    status=ImportBatchCommitStatus.APPENDED,
                    transaction_ids=(uuid4(),),
                    item_statuses=(ImportBatchItemCommitStatus.APPENDED,),
                ),
            )

    def test_e_binding_bound_to_different_materialization_rejected(self):
        """E. binding bound to different materialization -> reject."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id1 = uuid4()
        inst_id2 = uuid4()
        eff_date = date(2026, 8, 28)

        res_batch1 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="file1.csv",
            content=_make_csv(["buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"]),
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id1]}),
        )
        res_batch2 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="file2.csv",
            content=_make_csv(["buy,2026-08-28,2026-08-28T10:15:30+00:00,MSFT,5,300.00,USD,,,,,,"]),
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(mapping={("MSFT", eff_date): [inst_id2]}),
        )

        mat_batch1 = build_import_ledger_materialization_batch(res_batch1)
        mat_batch2 = build_import_ledger_materialization_batch(res_batch2)
        bind_batch2 = build_import_ledger_binding_batch(mat_batch2)

        with pytest.raises(ValueError, match="binding_batch.materialization_batch does not match materialization_batch"):
            SentinaxCanonicalCsvImportExecutionResult(
                status=SentinaxCanonicalCsvImportExecutionStatus.APPENDED,
                resolution_batch=res_batch1,
                materialization_batch=mat_batch1,
                binding_batch=bind_batch2,  # mismatch!
                commit_result=ImportBatchCommitResult(
                    status=ImportBatchCommitStatus.APPENDED,
                    transaction_ids=(uuid4(),),
                    item_statuses=(ImportBatchItemCommitStatus.APPENDED,),
                ),
            )

    def test_f_top_status_disagrees_with_commit_status_rejected(self):
        """F. top status disagrees with commit status -> reject."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)

        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="test.csv",
            content=_make_csv(["buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"]),
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id]}),
        )
        mat_batch = build_import_ledger_materialization_batch(res_batch)
        bind_batch = build_import_ledger_binding_batch(mat_batch)

        with pytest.raises(ValueError, match="Top-level status APPENDED does not match commit_result status NOOP"):
            SentinaxCanonicalCsvImportExecutionResult(
                status=SentinaxCanonicalCsvImportExecutionStatus.APPENDED,
                resolution_batch=res_batch,
                materialization_batch=mat_batch,
                binding_batch=bind_batch,
                commit_result=ImportBatchCommitResult(status=ImportBatchCommitStatus.NOOP),
            )

    def test_g_noop_direct_result_exact(self):
        """G. NOOP direct result exact."""
        port_id = uuid4()
        acc_id = uuid4()
        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="empty.csv",
            content=f"{CANONICAL_HEADERS}\n".encode("utf-8"),
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(),
        )
        mat_batch = build_import_ledger_materialization_batch(res_batch)
        bind_batch = build_import_ledger_binding_batch(mat_batch)
        commit_res = ImportBatchCommitResult(status=ImportBatchCommitStatus.NOOP)

        res = SentinaxCanonicalCsvImportExecutionResult(
            status=SentinaxCanonicalCsvImportExecutionStatus.NOOP,
            resolution_batch=res_batch,
            materialization_batch=mat_batch,
            binding_batch=bind_batch,
            commit_result=commit_res,
        )
        assert res.status == SentinaxCanonicalCsvImportExecutionStatus.NOOP
        assert res.transaction_ids == ()

    def test_h_conflict_direct_result_exact(self):
        """H. CONFLICT direct result exact."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)

        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="test.csv",
            content=_make_csv(["buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"]),
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id]}),
        )
        mat_batch = build_import_ledger_materialization_batch(res_batch)
        bind_batch = build_import_ledger_binding_batch(mat_batch)
        conf_tx_id = uuid4()
        commit_res = ImportBatchCommitResult(
            status=ImportBatchCommitStatus.CONFLICT,
            problem_record_ordinal=1,
            conflict_transaction_id=conf_tx_id,
            diagnostics=("Existing claim conflict on record 1.",),
        )

        res = SentinaxCanonicalCsvImportExecutionResult(
            status=SentinaxCanonicalCsvImportExecutionStatus.CONFLICT,
            resolution_batch=res_batch,
            materialization_batch=mat_batch,
            binding_batch=bind_batch,
            commit_result=commit_res,
        )
        assert res.status == SentinaxCanonicalCsvImportExecutionStatus.CONFLICT
        assert res.commit_result.conflict_transaction_id == conf_tx_id
        assert res.transaction_ids == ()

    def test_i_invalid_direct_result_exact(self):
        """I. INVALID direct result exact."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)

        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="test.csv",
            content=_make_csv(["buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"]),
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id]}),
        )
        mat_batch = build_import_ledger_materialization_batch(res_batch)
        bind_batch = build_import_ledger_binding_batch(mat_batch)
        commit_res = ImportBatchCommitResult(
            status=ImportBatchCommitStatus.INVALID,
            problem_record_ordinal=1,
            diagnostics=("Target account does not exist.",),
        )

        res = SentinaxCanonicalCsvImportExecutionResult(
            status=SentinaxCanonicalCsvImportExecutionStatus.INVALID,
            resolution_batch=res_batch,
            materialization_batch=mat_batch,
            binding_batch=bind_batch,
            commit_result=commit_res,
        )
        assert res.status == SentinaxCanonicalCsvImportExecutionStatus.INVALID
        assert res.commit_result.problem_record_ordinal == 1
        assert res.transaction_ids == ()

    def test_17_nonempty_noop_tamper_rejected(self):
        """17. Non-empty binding batch with NOOP commit result -> reject."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)

        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="test.csv",
            content=_make_csv(["buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"]),
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id]}),
        )
        mat_batch = build_import_ledger_materialization_batch(res_batch)
        bind_batch = build_import_ledger_binding_batch(mat_batch)
        assert bind_batch.intent_count == 1

        with pytest.raises(ValueError, match="NOOP commit_result requires binding_batch.intent_count == 0"):
            SentinaxCanonicalCsvImportExecutionResult(
                status=SentinaxCanonicalCsvImportExecutionStatus.NOOP,
                resolution_batch=res_batch,
                materialization_batch=mat_batch,
                binding_batch=bind_batch,
                commit_result=ImportBatchCommitResult(status=ImportBatchCommitStatus.NOOP),
            )

    def test_18_success_count_too_small_rejected(self):
        """18. Binding batch intent_count = 2 with APPENDED commit result with 1 item -> reject."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)

        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="test.csv",
            content=_make_csv([
                "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
                "cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,1000.00,USD,,,,",
            ]),
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id]}),
        )
        mat_batch = build_import_ledger_materialization_batch(res_batch)
        bind_batch = build_import_ledger_binding_batch(mat_batch)
        assert bind_batch.intent_count == 2

        with pytest.raises(ValueError, match="transaction_ids count 1 does not match binding_batch.intent_count 2"):
            SentinaxCanonicalCsvImportExecutionResult(
                status=SentinaxCanonicalCsvImportExecutionStatus.APPENDED,
                resolution_batch=res_batch,
                materialization_batch=mat_batch,
                binding_batch=bind_batch,
                commit_result=ImportBatchCommitResult(
                    status=ImportBatchCommitStatus.APPENDED,
                    transaction_ids=(uuid4(),),
                    item_statuses=(ImportBatchItemCommitStatus.APPENDED,),
                ),
            )

    def test_19_success_count_too_large_rejected(self):
        """19. Binding batch intent_count = 1 with APPENDED commit result with 2 items -> reject."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)

        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="test.csv",
            content=_make_csv(["buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"]),
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id]}),
        )
        mat_batch = build_import_ledger_materialization_batch(res_batch)
        bind_batch = build_import_ledger_binding_batch(mat_batch)
        assert bind_batch.intent_count == 1

        with pytest.raises(ValueError, match="transaction_ids count 2 does not match binding_batch.intent_count 1"):
            SentinaxCanonicalCsvImportExecutionResult(
                status=SentinaxCanonicalCsvImportExecutionStatus.APPENDED,
                resolution_batch=res_batch,
                materialization_batch=mat_batch,
                binding_batch=bind_batch,
                commit_result=ImportBatchCommitResult(
                    status=ImportBatchCommitStatus.APPENDED,
                    transaction_ids=(uuid4(), uuid4()),
                    item_statuses=(ImportBatchItemCommitStatus.APPENDED, ImportBatchItemCommitStatus.APPENDED),
                ),
            )

    def test_20_idempotent_count_tamper_rejected(self):
        """20. Binding batch intent_count = 2 with IDEMPOTENT_DUPLICATE commit result with 1 item -> reject."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)

        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="test.csv",
            content=_make_csv([
                "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
                "cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,1000.00,USD,,,,",
            ]),
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id]}),
        )
        mat_batch = build_import_ledger_materialization_batch(res_batch)
        bind_batch = build_import_ledger_binding_batch(mat_batch)
        assert bind_batch.intent_count == 2

        with pytest.raises(ValueError, match="transaction_ids count 1 does not match binding_batch.intent_count 2"):
            SentinaxCanonicalCsvImportExecutionResult(
                status=SentinaxCanonicalCsvImportExecutionStatus.IDEMPOTENT_DUPLICATE,
                resolution_batch=res_batch,
                materialization_batch=mat_batch,
                binding_batch=bind_batch,
                commit_result=ImportBatchCommitResult(
                    status=ImportBatchCommitStatus.IDEMPOTENT_DUPLICATE,
                    transaction_ids=(uuid4(),),
                    item_statuses=(ImportBatchItemCommitStatus.IDEMPOTENT_DUPLICATE,),
                ),
            )

    def test_21_empty_binding_plus_appended_rejected(self):
        """21. Empty binding batch with APPENDED commit result -> reject."""
        port_id = uuid4()
        acc_id = uuid4()
        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="empty.csv",
            content=f"{CANONICAL_HEADERS}\n".encode("utf-8"),
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(),
        )
        mat_batch = build_import_ledger_materialization_batch(res_batch)
        bind_batch = build_import_ledger_binding_batch(mat_batch)
        assert bind_batch.intent_count == 0

        with pytest.raises(ValueError, match="APPENDED commit_result requires binding_batch.intent_count > 0"):
            SentinaxCanonicalCsvImportExecutionResult(
                status=SentinaxCanonicalCsvImportExecutionStatus.APPENDED,
                resolution_batch=res_batch,
                materialization_batch=mat_batch,
                binding_batch=bind_batch,
                commit_result=ImportBatchCommitResult(
                    status=ImportBatchCommitStatus.APPENDED,
                    transaction_ids=(uuid4(),),
                    item_statuses=(ImportBatchItemCommitStatus.APPENDED,),
                ),
            )

    def test_22_empty_binding_plus_idempotent_rejected(self):
        """22. Empty binding batch with IDEMPOTENT_DUPLICATE commit result -> reject."""
        port_id = uuid4()
        acc_id = uuid4()
        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="empty.csv",
            content=f"{CANONICAL_HEADERS}\n".encode("utf-8"),
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(),
        )
        mat_batch = build_import_ledger_materialization_batch(res_batch)
        bind_batch = build_import_ledger_binding_batch(mat_batch)
        assert bind_batch.intent_count == 0

        with pytest.raises(ValueError, match="IDEMPOTENT_DUPLICATE commit_result requires binding_batch.intent_count > 0"):
            SentinaxCanonicalCsvImportExecutionResult(
                status=SentinaxCanonicalCsvImportExecutionStatus.IDEMPOTENT_DUPLICATE,
                resolution_batch=res_batch,
                materialization_batch=mat_batch,
                binding_batch=bind_batch,
                commit_result=ImportBatchCommitResult(
                    status=ImportBatchCommitStatus.IDEMPOTENT_DUPLICATE,
                    transaction_ids=(uuid4(),),
                    item_statuses=(ImportBatchItemCommitStatus.IDEMPOTENT_DUPLICATE,),
                ),
            )

    def test_23_empty_binding_plus_conflict_rejected(self):
        """23. Empty binding batch with CONFLICT commit result -> reject."""
        port_id = uuid4()
        acc_id = uuid4()
        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="empty.csv",
            content=f"{CANONICAL_HEADERS}\n".encode("utf-8"),
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(),
        )
        mat_batch = build_import_ledger_materialization_batch(res_batch)
        bind_batch = build_import_ledger_binding_batch(mat_batch)
        assert bind_batch.intent_count == 0

        with pytest.raises(ValueError, match="CONFLICT commit_result requires binding_batch.intent_count > 0"):
            SentinaxCanonicalCsvImportExecutionResult(
                status=SentinaxCanonicalCsvImportExecutionStatus.CONFLICT,
                resolution_batch=res_batch,
                materialization_batch=mat_batch,
                binding_batch=bind_batch,
                commit_result=ImportBatchCommitResult(
                    status=ImportBatchCommitStatus.CONFLICT,
                    problem_record_ordinal=1,
                    conflict_transaction_id=uuid4(),
                    diagnostics=("Conflict",),
                ),
            )

    def test_24_empty_binding_plus_invalid_rejected(self):
        """24. Empty binding batch with INVALID commit result -> reject."""
        port_id = uuid4()
        acc_id = uuid4()
        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="empty.csv",
            content=f"{CANONICAL_HEADERS}\n".encode("utf-8"),
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(),
        )
        mat_batch = build_import_ledger_materialization_batch(res_batch)
        bind_batch = build_import_ledger_binding_batch(mat_batch)
        assert bind_batch.intent_count == 0

        with pytest.raises(ValueError, match="INVALID commit_result requires binding_batch.intent_count > 0"):
            SentinaxCanonicalCsvImportExecutionResult(
                status=SentinaxCanonicalCsvImportExecutionStatus.INVALID,
                resolution_batch=res_batch,
                materialization_batch=mat_batch,
                binding_batch=bind_batch,
                commit_result=ImportBatchCommitResult(
                    status=ImportBatchCommitStatus.INVALID,
                    problem_record_ordinal=1,
                    diagnostics=("Invalid",),
                ),
            )

    def test_25_conflict_ordinal_tamper_rejected(self):
        """25. Binding ordinals 1,3 with CONFLICT problem_record_ordinal 2 -> reject."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)

        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="mixed.csv",
            content=_make_csv([
                "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
                "buy,2026-08-28,2026-08-28T10:15:30+00:00,BAD,bad_qty,150.00,USD,,,,,,",
                "cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,1000.00,USD,,,,",
            ]),
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id]}),
        )
        mat_batch = build_import_ledger_materialization_batch(res_batch)
        bind_batch = build_import_ledger_binding_batch(mat_batch)
        assert tuple(i.record_ordinal for i in bind_batch.intents) == (1, 3)

        with pytest.raises(ValueError, match="problem_record_ordinal 2 is not present in binding_batch intent ordinals"):
            SentinaxCanonicalCsvImportExecutionResult(
                status=SentinaxCanonicalCsvImportExecutionStatus.CONFLICT,
                resolution_batch=res_batch,
                materialization_batch=mat_batch,
                binding_batch=bind_batch,
                commit_result=ImportBatchCommitResult(
                    status=ImportBatchCommitStatus.CONFLICT,
                    problem_record_ordinal=2,
                    conflict_transaction_id=uuid4(),
                    diagnostics=("Conflict",),
                ),
            )

    def test_26_invalid_ordinal_tamper_rejected(self):
        """26. Binding ordinals 1,3 with INVALID problem_record_ordinal 2 -> reject."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)

        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="mixed.csv",
            content=_make_csv([
                "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
                "buy,2026-08-28,2026-08-28T10:15:30+00:00,BAD,bad_qty,150.00,USD,,,,,,",
                "cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,1000.00,USD,,,,",
            ]),
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id]}),
        )
        mat_batch = build_import_ledger_materialization_batch(res_batch)
        bind_batch = build_import_ledger_binding_batch(mat_batch)
        assert tuple(i.record_ordinal for i in bind_batch.intents) == (1, 3)

        with pytest.raises(ValueError, match="problem_record_ordinal 2 is not present in binding_batch intent ordinals"):
            SentinaxCanonicalCsvImportExecutionResult(
                status=SentinaxCanonicalCsvImportExecutionStatus.INVALID,
                resolution_batch=res_batch,
                materialization_batch=mat_batch,
                binding_batch=bind_batch,
                commit_result=ImportBatchCommitResult(
                    status=ImportBatchCommitStatus.INVALID,
                    problem_record_ordinal=2,
                    diagnostics=("Invalid",),
                ),
            )

    def test_27_valid_conflict_ordinal_accepted(self):
        """27. Binding ordinals 1,3 with CONFLICT problem_record_ordinal 3 -> accepted."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)

        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="mixed.csv",
            content=_make_csv([
                "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
                "buy,2026-08-28,2026-08-28T10:15:30+00:00,BAD,bad_qty,150.00,USD,,,,,,",
                "cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,1000.00,USD,,,,",
            ]),
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id]}),
        )
        mat_batch = build_import_ledger_materialization_batch(res_batch)
        bind_batch = build_import_ledger_binding_batch(mat_batch)
        conf_tx_id = uuid4()

        result = SentinaxCanonicalCsvImportExecutionResult(
            status=SentinaxCanonicalCsvImportExecutionStatus.CONFLICT,
            resolution_batch=res_batch,
            materialization_batch=mat_batch,
            binding_batch=bind_batch,
            commit_result=ImportBatchCommitResult(
                status=ImportBatchCommitStatus.CONFLICT,
                problem_record_ordinal=3,
                conflict_transaction_id=conf_tx_id,
                diagnostics=("Conflict on record 3",),
            ),
        )
        assert result.status == SentinaxCanonicalCsvImportExecutionStatus.CONFLICT
        assert result.commit_result.problem_record_ordinal == 3

    def test_28_valid_invalid_ordinal_accepted(self):
        """28. Binding ordinals 1,3 with INVALID problem_record_ordinal 1 -> accepted."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)

        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="mixed.csv",
            content=_make_csv([
                "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
                "buy,2026-08-28,2026-08-28T10:15:30+00:00,BAD,bad_qty,150.00,USD,,,,,,",
                "cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,1000.00,USD,,,,",
            ]),
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id]}),
        )
        mat_batch = build_import_ledger_materialization_batch(res_batch)
        bind_batch = build_import_ledger_binding_batch(mat_batch)

        result = SentinaxCanonicalCsvImportExecutionResult(
            status=SentinaxCanonicalCsvImportExecutionStatus.INVALID,
            resolution_batch=res_batch,
            materialization_batch=mat_batch,
            binding_batch=bind_batch,
            commit_result=ImportBatchCommitResult(
                status=ImportBatchCommitStatus.INVALID,
                problem_record_ordinal=1,
                diagnostics=("Invalid on record 1",),
            ),
        )
        assert result.status == SentinaxCanonicalCsvImportExecutionStatus.INVALID
        assert result.commit_result.problem_record_ordinal == 1


# ─────────────────────────────────────────────────────────────────────────────
# 56. Count Property Tests (J-P)
# ─────────────────────────────────────────────────────────────────────────────

class TestCountProperties:
    """Section 56: Delegated read-only count properties."""

    def test_count_properties_exact(self, repo_context):
        """J-P: source_record_count, ready, rejected, resolution, unresolved, ambiguous, binding counts."""
        port_id = repo_context["portfolio_id"]
        acc_id = repo_context["account_id"]
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)

        # 3 rows: 1 valid/resolved, 1 semantic rejected, 1 valid/resolved
        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,MSFT,bad_qty,300.00,USD,,,,,,",
            "cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,1000.00,USD,,,,",
        ])
        resolver = MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id]})

        result = execute_sentinax_canonical_csv_import_v1(
            repository=repo_context["repo"],
            portfolio_id=port_id,
            account_id=acc_id,
            filename="mixed.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )

        assert result.source_record_count == 3
        assert result.ready_record_count == 2
        assert result.rejected_record_count == 1
        assert result.resolution_count == 2
        assert result.unresolved_resolution_count == 0
        assert result.ambiguous_resolution_count == 0
        assert result.binding_intent_count == 2


# ─────────────────────────────────────────────────────────────────────────────
# 57. Ordinal Property Tests (Q-S)
# ─────────────────────────────────────────────────────────────────────────────

class TestOrdinalProperties:
    """Section 57: Ordinal properties and preserved ordering."""

    def test_rejected_and_blocked_ordinals_and_no_renumbering(self, repo_context):
        """Q-S: rejected ordinals, blocked ordinals, no renumbering."""
        port_id = repo_context["portfolio_id"]
        acc_id = repo_context["account_id"]
        inst_id1 = uuid4()
        eff_date = date(2026, 8, 28)

        # 4 rows: 1 READY/RESOLVED, 2 REJECTED, 3 READY/UNRESOLVED, 4 REJECTED
        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,BAD1,bad_qty,100,USD,,,,,,",
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,UNKNOWN_TICKER,5,50.00,USD,,,,,,",
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,BAD2,10,bad_price,USD,,,,,,",
        ])
        resolver = MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id1]})

        result = execute_sentinax_canonical_csv_import_v1(
            repository=repo_context["repo"],
            portfolio_id=port_id,
            account_id=acc_id,
            filename="mixed_ordinals.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )

        assert result.status == SentinaxCanonicalCsvImportExecutionStatus.RESOLUTION_BLOCKED
        assert result.source_record_count == 4
        assert result.ready_record_count == 2
        assert result.rejected_record_count == 2
        assert result.rejected_record_ordinals == (2, 4)
        assert result.blocked_resolution_ordinals == (3,)
        assert result.binding_intent_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# 58-59. Resolution Blocked Tests (UNRESOLVED & AMBIGUOUS)
# ─────────────────────────────────────────────────────────────────────────────

class TestResolutionBlockedExecution:
    """Sections 58-59: Fail-closed zero-write gate on UNRESOLVED and AMBIGUOUS outcomes."""

    def test_resolution_blocked_unresolved(self, repo_context):
        """58. One RESOLVED, one UNRESOLVED -> RESOLUTION_BLOCKED and 0 repo writes."""
        port_id = repo_context["portfolio_id"]
        acc_id = repo_context["account_id"]
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,UNKNOWN_SYM,20,50.00,USD,,,,,,",
        ])
        resolver = MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id]})

        result = execute_sentinax_canonical_csv_import_v1(
            repository=repo_context["repo"],
            portfolio_id=port_id,
            account_id=acc_id,
            filename="unresolved.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )

        assert result.status == SentinaxCanonicalCsvImportExecutionStatus.RESOLUTION_BLOCKED
        assert result.unresolved_resolution_count == 1
        assert result.ambiguous_resolution_count == 0
        assert result.materialization_batch is None
        assert result.binding_batch is None
        assert result.commit_result is None
        assert len(repo_context["client"].recorded_rpcs) == 0

    def test_resolution_blocked_ambiguous(self, repo_context):
        """59. One RESOLVED, one AMBIGUOUS -> RESOLUTION_BLOCKED and 0 repo writes."""
        port_id = repo_context["portfolio_id"]
        acc_id = repo_context["account_id"]
        inst_id1 = uuid4()
        cand_id1 = uuid4()
        cand_id2 = uuid4()
        eff_date = date(2026, 8, 28)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AMBIG_SYM,20,50.00,USD,,,,,,",
        ])
        resolver = MockInstrumentResolver(mapping={
            ("AAPL", eff_date): [inst_id1],
            ("AMBIG_SYM", eff_date): [cand_id1, cand_id2],
        })

        result = execute_sentinax_canonical_csv_import_v1(
            repository=repo_context["repo"],
            portfolio_id=port_id,
            account_id=acc_id,
            filename="ambiguous.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )

        assert result.status == SentinaxCanonicalCsvImportExecutionStatus.RESOLUTION_BLOCKED
        assert result.unresolved_resolution_count == 0
        assert result.ambiguous_resolution_count == 1
        assert result.materialization_batch is None
        assert result.binding_batch is None
        assert result.commit_result is None
        assert len(repo_context["client"].recorded_rpcs) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 60-63. Fully Executed Outcomes (APPENDED, IDEMPOTENT, CONFLICT, INVALID)
# ─────────────────────────────────────────────────────────────────────────────

class TestExecutionOutcomes:
    """Sections 60-63: Full stage chain with mapped commit statuses."""

    def test_60_fully_resolved_appended(self, repo_context):
        """60. Fully resolved valid CSV -> APPENDED with valid transaction IDs."""
        port_id = repo_context["portfolio_id"]
        acc_id = repo_context["account_id"]
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
        ])
        resolver = MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id]})

        result = execute_sentinax_canonical_csv_import_v1(
            repository=repo_context["repo"],
            portfolio_id=port_id,
            account_id=acc_id,
            filename="valid.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )

        assert result.status == SentinaxCanonicalCsvImportExecutionStatus.APPENDED
        assert result.resolution_batch.is_fully_resolved
        assert result.materialization_batch is not None
        assert result.binding_batch is not None
        assert result.commit_result is not None
        assert len(result.transaction_ids) == 1
        assert len(repo_context["client"].recorded_rpcs) == 1

    def test_61_full_replay_idempotent_duplicate(self, repo_context):
        """61. Exact full replay -> IDEMPOTENT_DUPLICATE."""
        port_id = repo_context["portfolio_id"]
        acc_id = repo_context["account_id"]
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
        ])
        resolver = MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id]})

        # Run 1: Appends
        res1 = execute_sentinax_canonical_csv_import_v1(
            repository=repo_context["repo"],
            portfolio_id=port_id,
            account_id=acc_id,
            filename="replay.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )
        assert res1.status == SentinaxCanonicalCsvImportExecutionStatus.APPENDED

        # Run 2: Replay with different filename and imported_at -> IDEMPOTENT_DUPLICATE
        res2 = execute_sentinax_canonical_csv_import_v1(
            repository=repo_context["repo"],
            portfolio_id=port_id,
            account_id=acc_id,
            filename="replay_diff_name.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 16, 00, tzinfo=timezone.utc),
            resolver=resolver,
        )
        assert res2.status == SentinaxCanonicalCsvImportExecutionStatus.IDEMPOTENT_DUPLICATE
        assert res2.transaction_ids == res1.transaction_ids

    def test_62_conflict_result(self, repo_context):
        """62. Changed interpretation on existing claim -> CONFLICT."""
        port_id = repo_context["portfolio_id"]
        acc_id = repo_context["account_id"]
        inst_id1 = uuid4()
        inst_id2 = uuid4()
        eff_date = date(2026, 8, 28)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
        ])

        # Run 1: Initial import
        resolver1 = MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id1]})
        res1 = execute_sentinax_canonical_csv_import_v1(
            repository=repo_context["repo"],
            portfolio_id=port_id,
            account_id=acc_id,
            filename="conflict.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver1,
        )
        assert res1.status == SentinaxCanonicalCsvImportExecutionStatus.APPENDED

        # Run 2: Same raw CSV bytes, but resolver resolves to different instrument -> CONFLICT
        resolver2 = MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id2]})
        res2 = execute_sentinax_canonical_csv_import_v1(
            repository=repo_context["repo"],
            portfolio_id=port_id,
            account_id=acc_id,
            filename="conflict.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver2,
        )
        assert res2.status == SentinaxCanonicalCsvImportExecutionStatus.CONFLICT
        assert res2.commit_result.conflict_transaction_id == res1.transaction_ids[0]
        assert res2.commit_result.problem_record_ordinal == 1
        assert res2.transaction_ids == ()

    def test_63_invalid_target_result(self, repo_context):
        """63. Missing account target -> INVALID."""
        port_id = repo_context["portfolio_id"]
        bad_acc_id = uuid4()  # Does not exist in repository tables
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
        ])
        resolver = MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id]})

        result = execute_sentinax_canonical_csv_import_v1(
            repository=repo_context["repo"],
            portfolio_id=port_id,
            account_id=bad_acc_id,
            filename="invalid.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )

        assert result.status == SentinaxCanonicalCsvImportExecutionStatus.INVALID
        assert result.commit_result.problem_record_ordinal == 1
        assert result.transaction_ids == ()


# ─────────────────────────────────────────────────────────────────────────────
# 64-67. Special File Topologies (All-Rejected, Mixed, Empty)
# ─────────────────────────────────────────────────────────────────────────────

class TestSpecialFileTopologies:
    """Sections 64-67: All-rejected, mixed rejected+valid, mixed rejected+blocked, empty files."""

    def test_64_all_rejected_batch_produces_noop(self, repo_context):
        """64. All rows semantically rejected -> NOOP."""
        port_id = repo_context["portfolio_id"]
        acc_id = repo_context["account_id"]

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,bad_qty,150.00,USD,,,,,,",
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,MSFT,10,bad_price,USD,,,,,,",
        ])
        resolver = MockInstrumentResolver()

        result = execute_sentinax_canonical_csv_import_v1(
            repository=repo_context["repo"],
            portfolio_id=port_id,
            account_id=acc_id,
            filename="all_rejected.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )

        assert result.status == SentinaxCanonicalCsvImportExecutionStatus.NOOP
        assert result.source_record_count == 2
        assert result.ready_record_count == 0
        assert result.rejected_record_count == 2
        assert result.resolution_count == 0
        assert result.binding_intent_count == 0
        assert result.transaction_ids == ()
        assert len(repo_context["client"].recorded_rpcs) == 0

    def test_65_mixed_rejected_plus_append(self, repo_context):
        """65. 1 valid, 2 rejected, 3 valid -> APPENDED with binding ordinals (1, 3)."""
        port_id = repo_context["portfolio_id"]
        acc_id = repo_context["account_id"]
        inst_id1 = uuid4()
        inst_id3 = uuid4()
        eff_date = date(2026, 8, 28)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,REJ_SYM,bad_qty,100,USD,,,,,,",
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,MSFT,5,300.00,USD,,,,,,",
        ])
        resolver = MockInstrumentResolver(mapping={
            ("AAPL", eff_date): [inst_id1],
            ("MSFT", eff_date): [inst_id3],
        })

        result = execute_sentinax_canonical_csv_import_v1(
            repository=repo_context["repo"],
            portfolio_id=port_id,
            account_id=acc_id,
            filename="mixed.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )

        assert result.status == SentinaxCanonicalCsvImportExecutionStatus.APPENDED
        assert result.source_record_count == 3
        assert result.ready_record_count == 2
        assert result.rejected_record_count == 1
        assert result.rejected_record_ordinals == (2,)
        assert result.binding_intent_count == 2
        assert tuple(i.record_ordinal for i in result.binding_batch.intents) == (1, 3)
        assert len(result.transaction_ids) == 2

    def test_66_mixed_rejected_plus_blocked(self, repo_context):
        """66. 1 valid/resolved, 2 rejected, 3 valid/ambiguous -> RESOLUTION_BLOCKED and 0 writes."""
        port_id = repo_context["portfolio_id"]
        acc_id = repo_context["account_id"]
        inst_id1 = uuid4()
        cand1 = uuid4()
        cand2 = uuid4()
        eff_date = date(2026, 8, 28)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,REJ_SYM,bad_qty,100,USD,,,,,,",
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AMBIG_SYM,5,300.00,USD,,,,,,",
        ])
        resolver = MockInstrumentResolver(mapping={
            ("AAPL", eff_date): [inst_id1],
            ("AMBIG_SYM", eff_date): [cand1, cand2],
        })

        result = execute_sentinax_canonical_csv_import_v1(
            repository=repo_context["repo"],
            portfolio_id=port_id,
            account_id=acc_id,
            filename="mixed_blocked.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )

        assert result.status == SentinaxCanonicalCsvImportExecutionStatus.RESOLUTION_BLOCKED
        assert result.source_record_count == 3
        assert result.ready_record_count == 2
        assert result.rejected_record_count == 1
        assert result.rejected_record_ordinals == (2,)
        assert result.blocked_resolution_ordinals == (3,)
        assert len(repo_context["client"].recorded_rpcs) == 0

    def test_67_empty_header_only_batch(self, repo_context):
        """67. Header-only batch -> NOOP."""
        port_id = repo_context["portfolio_id"]
        acc_id = repo_context["account_id"]

        csv_bytes = f"{CANONICAL_HEADERS}\n".encode("utf-8")
        resolver = MockInstrumentResolver()

        result = execute_sentinax_canonical_csv_import_v1(
            repository=repo_context["repo"],
            portfolio_id=port_id,
            account_id=acc_id,
            filename="header_only.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )

        assert result.status == SentinaxCanonicalCsvImportExecutionStatus.NOOP
        assert result.source_record_count == 0
        assert result.ready_record_count == 0
        assert result.binding_intent_count == 0
        assert result.transaction_ids == ()
        assert len(repo_context["client"].recorded_rpcs) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 68-71. Exception Propagation Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestExceptionPropagation:
    """Sections 68-71: Lower-layer exceptions propagate unchanged."""

    def test_68_parser_exception_propagates(self, repo_context):
        """68. Malformed CSV syntax propagates SentinaxCanonicalCsvError."""
        # Unclosed quote violates CSV physical line syntax
        bad_csv_bytes = b'buy,"unclosed quote,2026-08-28\n'
        with pytest.raises(SentinaxCanonicalCsvError):
            execute_sentinax_canonical_csv_import_v1(
                repository=repo_context["repo"],
                portfolio_id=repo_context["portfolio_id"],
                account_id=repo_context["account_id"],
                filename="bad_syntax.csv",
                content=bad_csv_bytes,
                imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
                resolver=MockInstrumentResolver(),
            )

    def test_69_semantic_exception_propagates(self, repo_context):
        """69. Format-level semantic exception propagates SentinaxCanonicalCsvSemanticError."""
        bad_header_content = b"col1,col2\n1,2\n"
        with pytest.raises(SentinaxCanonicalCsvSemanticError):
            execute_sentinax_canonical_csv_import_v1(
                repository=repo_context["repo"],
                portfolio_id=repo_context["portfolio_id"],
                account_id=repo_context["account_id"],
                filename="bad_schema.csv",
                content=bad_header_content,
                imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
                resolver=MockInstrumentResolver(),
            )

    def test_70_resolver_exception_propagates(self, repo_context):
        """70. Resolver exception propagates unchanged."""
        port_id = repo_context["portfolio_id"]
        acc_id = repo_context["account_id"]
        eff_date = date(2026, 8, 28)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,BOOM,10,150.00,USD,,,,,,",
        ])
        resolver = MockInstrumentResolver(
            mapping={},
            exception_on_ref="BOOM",
        )

        with pytest.raises(RuntimeError, match="Resolver exploded for BOOM"):
            execute_sentinax_canonical_csv_import_v1(
                repository=repo_context["repo"],
                portfolio_id=port_id,
                account_id=acc_id,
                filename="boom.csv",
                content=csv_bytes,
                imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
                resolver=resolver,
            )

    def test_71_database_exception_propagates(self, repo_context):
        """71. Repository / database exception propagates unchanged."""
        port_id = repo_context["portfolio_id"]
        acc_id = repo_context["account_id"]
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
        ])
        resolver = MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id]})

        repo_context["client"].next_rpc_error = ConnectionError("Database network failed")

        with pytest.raises(ConnectionError, match="Database network failed"):
            execute_sentinax_canonical_csv_import_v1(
                repository=repo_context["repo"],
                portfolio_id=port_id,
                account_id=acc_id,
                filename="dberr.csv",
                content=csv_bytes,
                imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
                resolver=resolver,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 72-81. Static Invariant & Purity Inspection Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestStaticPurityAndInvariants:
    """Sections 72-81: Static AST and source code inspection tests."""

    def test_72_73_exact_reuse_and_no_direct_parser_instantiation(self):
        """72-73: Calls run_sentinax_canonical_csv_import_v1, no direct parser/interpreter instantiation."""
        src = inspect.getsource(exec_module)
        tree = ast.parse(src)

        called_funcs = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called_funcs.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called_funcs.add(node.func.attr)

        assert "run_sentinax_canonical_csv_import_v1" in called_funcs
        assert "build_import_ledger_materialization_batch" in called_funcs
        assert "build_import_ledger_binding_batch" in called_funcs
        assert "commit_import_binding_batch" in called_funcs

        assert "SentinaxCanonicalCsvParserV1" not in called_funcs
        assert "SentinaxCanonicalCsvSemanticInterpreterV1" not in called_funcs

    def test_74_77_no_lower_level_duplication_and_no_single_commit(self):
        """74, 77: No Decimal parsing, currency parsing, transaction construction, commit_import_binding_intent."""
        src = inspect.getsource(exec_module)
        assert "commit_import_binding_intent" not in src
        assert "append_transaction" not in src
        assert "Decimal(" not in src
        assert "PortfolioTransaction" not in src
        assert "external_source" not in src
        assert "external_reference" not in src
        assert "cash_bucket_id" not in src

    def test_75_no_write_on_block(self, repo_context, monkeypatch):
        """75. Spy repo: resolution blocked -> zero commit_import_binding_batch, commit_import_binding_intent, append_transaction calls."""
        port_id = repo_context["portfolio_id"]
        acc_id = repo_context["account_id"]
        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,UNKNOWN_TICKER,10,150.00,USD,,,,,,",
        ])
        resolver = MockInstrumentResolver(mapping={})

        commit_batch_calls = []
        commit_intent_calls = []
        append_tx_calls = []

        repo = repo_context["repo"]
        orig_batch = repo.commit_import_binding_batch
        orig_intent = repo.commit_import_binding_intent
        orig_append = repo.append_transaction

        def spy_batch(*args, **kwargs):
            commit_batch_calls.append(args)
            return orig_batch(*args, **kwargs)

        def spy_intent(*args, **kwargs):
            commit_intent_calls.append(args)
            return orig_intent(*args, **kwargs)

        def spy_append(*args, **kwargs):
            append_tx_calls.append(args)
            return orig_append(*args, **kwargs)

        monkeypatch.setattr(repo, "commit_import_binding_batch", spy_batch)
        monkeypatch.setattr(repo, "commit_import_binding_intent", spy_intent)
        monkeypatch.setattr(repo, "append_transaction", spy_append)

        res = execute_sentinax_canonical_csv_import_v1(
            repository=repo,
            portfolio_id=port_id,
            account_id=acc_id,
            filename="blocked.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )

        assert res.status == SentinaxCanonicalCsvImportExecutionStatus.RESOLUTION_BLOCKED
        assert len(commit_batch_calls) == 0
        assert len(commit_intent_calls) == 0
        assert len(append_tx_calls) == 0

    def test_76_one_batch_commit(self, repo_context, monkeypatch):
        """76. Fully resolved non-empty file -> commit_import_binding_batch called exactly once."""
        port_id = repo_context["portfolio_id"]
        acc_id = repo_context["account_id"]
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
        ])
        resolver = MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id]})

        commit_batch_calls = []
        repo = repo_context["repo"]
        orig_batch = repo.commit_import_binding_batch

        def spy_batch(*args, **kwargs):
            commit_batch_calls.append(args)
            return orig_batch(*args, **kwargs)

        monkeypatch.setattr(repo, "commit_import_binding_batch", spy_batch)

        res = execute_sentinax_canonical_csv_import_v1(
            repository=repo,
            portfolio_id=port_id,
            account_id=acc_id,
            filename="single.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )

        assert res.status == SentinaxCanonicalCsvImportExecutionStatus.APPENDED
        assert len(commit_batch_calls) == 1

    def test_78_no_clock(self):
        """78. Zero clock calls in execution module."""
        src = inspect.getsource(exec_module)
        assert "datetime.now" not in src
        assert "datetime.utcnow" not in src
        assert "date.today" not in src
        assert "_get_system_time" not in src

    def test_79_no_uuid_generation(self):
        """79. Zero UUID generation in execution module."""
        src = inspect.getsource(exec_module)
        assert "uuid4(" not in src
        assert "uuid5(" not in src

    def test_80_no_hashlib(self):
        """80. Zero hash calculations in execution module."""
        src = inspect.getsource(exec_module)
        assert "hashlib" not in src
        assert "sha256(" not in src

    def test_81_no_db_transport(self):
        """81. Zero direct DB transport calls."""
        src = inspect.getsource(exec_module)
        assert ".rpc(" not in src
        assert ".table(" not in src
        assert "PostgREST" not in src
        assert "Supabase" not in src

    def test_29_no_mutable_status_map_in_production_source(self):
        """29. Verify no mutable _COMMIT_STATUS_MAP exists in production module."""
        src = inspect.getsource(exec_module)
        assert "_COMMIT_STATUS_MAP" not in src
        assert not hasattr(exec_module, "_COMMIT_STATUS_MAP")

        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assert not target.id.endswith("_MAP"), f"Found forbidden map constant: {target.id}"

    def test_30_status_conversion_strictness(self):
        """30. Verify exact fail-closed status conversion for all 5 Phase 13R statuses."""
        from backend.engine.private.portfolio.sentinax_csv_import_execution import (
            _execution_status_from_commit_status,
        )
        assert _execution_status_from_commit_status(ImportBatchCommitStatus.NOOP) == SentinaxCanonicalCsvImportExecutionStatus.NOOP
        assert _execution_status_from_commit_status(ImportBatchCommitStatus.APPENDED) == SentinaxCanonicalCsvImportExecutionStatus.APPENDED
        assert _execution_status_from_commit_status(ImportBatchCommitStatus.IDEMPOTENT_DUPLICATE) == SentinaxCanonicalCsvImportExecutionStatus.IDEMPOTENT_DUPLICATE
        assert _execution_status_from_commit_status(ImportBatchCommitStatus.CONFLICT) == SentinaxCanonicalCsvImportExecutionStatus.CONFLICT
        assert _execution_status_from_commit_status(ImportBatchCommitStatus.INVALID) == SentinaxCanonicalCsvImportExecutionStatus.INVALID

        with pytest.raises(TypeError):
            _execution_status_from_commit_status("appended")  # type: ignore

        with pytest.raises(TypeError):
            _execution_status_from_commit_status(None)  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 82-84. End-to-End Pipeline Scenarios
# ─────────────────────────────────────────────────────────────────────────────

class TestEndToEndPipelineScenarios:
    """Sections 82-84: Comprehensive end-to-end integration workflows."""

    def test_82_representative_multi_event_csv(self, repo_context):
        """82. BUY, REJECTED, CASH_DEPOSIT, DIVIDEND, FX_CONVERSION end-to-end."""
        port_id = repo_context["portfolio_id"]
        acc_id = repo_context["account_id"]
        inst_aapl = uuid4()
        eff_date = date(2026, 8, 28)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,INVALID,bad_qty,100,USD,,,,,,",
            "cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,1000.00,USD,,,,",
            "dividend,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,,,,50.00,USD,,,,",
            "fx_conversion,2026-08-28,2026-08-28T10:15:30+00:00,,,,,,,USD,1000.00,EUR,850.00",
        ])
        resolver = MockInstrumentResolver(mapping={
            ("AAPL", eff_date): [inst_aapl],
        })

        result = execute_sentinax_canonical_csv_import_v1(
            repository=repo_context["repo"],
            portfolio_id=port_id,
            account_id=acc_id,
            filename="full_pipeline.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )

        assert result.status == SentinaxCanonicalCsvImportExecutionStatus.APPENDED
        assert result.source_record_count == 5
        assert result.ready_record_count == 4
        assert result.rejected_record_count == 1
        assert result.rejected_record_ordinals == (2,)
        assert result.resolution_count == 4
        assert result.resolution_batch.is_fully_resolved
        assert result.materialization_batch.plan_count == 4
        assert result.binding_batch.intent_count == 4
        assert tuple(i.record_ordinal for i in result.binding_batch.intents) == (1, 3, 4, 5)
        assert len(result.transaction_ids) == 4

    def test_83_84_replay_and_conflict_end_to_end(self, repo_context):
        """83-84: Replay produces IDEMPOTENT_DUPLICATE; changed resolution produces CONFLICT."""
        port_id = repo_context["portfolio_id"]
        acc_id = repo_context["account_id"]
        inst_id_orig = uuid4()
        inst_id_mod = uuid4()
        eff_date = date(2026, 8, 28)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
            "cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,500.00,USD,,,,",
        ])

        # Step 1: Initial import
        res1 = execute_sentinax_canonical_csv_import_v1(
            repository=repo_context["repo"],
            portfolio_id=port_id,
            account_id=acc_id,
            filename="source_file_v1.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id_orig]}),
        )
        assert res1.status == SentinaxCanonicalCsvImportExecutionStatus.APPENDED
        assert len(res1.transaction_ids) == 2

        # Step 2: Exact replay with different filename and imported_at -> IDEMPOTENT_DUPLICATE
        res2 = execute_sentinax_canonical_csv_import_v1(
            repository=repo_context["repo"],
            portfolio_id=port_id,
            account_id=acc_id,
            filename="source_file_v2_replayed.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id_orig]}),
        )
        assert res2.status == SentinaxCanonicalCsvImportExecutionStatus.IDEMPOTENT_DUPLICATE
        assert res2.transaction_ids == res1.transaction_ids

        # Step 3: Conflict when instrument resolution changes
        res3 = execute_sentinax_canonical_csv_import_v1(
            repository=repo_context["repo"],
            portfolio_id=port_id,
            account_id=acc_id,
            filename="source_file_v3_conflict.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc),
            resolver=MockInstrumentResolver(mapping={("AAPL", eff_date): [inst_id_mod]}),
        )
        assert res3.status == SentinaxCanonicalCsvImportExecutionStatus.CONFLICT
        assert res3.commit_result.conflict_transaction_id == res1.transaction_ids[0]
        assert res3.commit_result.problem_record_ordinal == 1
        assert res3.transaction_ids == ()
