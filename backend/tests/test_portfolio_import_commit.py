"""
Unit and integration test suite for Phase 13O:
Immutable Import-Commit Claim & Ledger-Binding Intent Contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import inspect
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import Currency, TransactionType
from backend.engine.private.portfolio.import_assessment import (
    ImportAssessmentBatch,
    ImportAssessmentDiagnostic,
    ImportAssessmentStatus,
    build_import_assessment_batch,
    build_import_record_assessment,
)
from backend.engine.private.portfolio.import_batch import (
    ImportBatchManifest,
    build_import_batch_manifest,
)
from backend.engine.private.portfolio.import_commit import (
    ImportLedgerBindingBatch,
    ImportLedgerBindingIntent,
    PortfolioImportCommitError,
    build_import_ledger_binding_batch,
    build_import_ledger_binding_intent,
)
from backend.engine.private.portfolio.import_draft import (
    ImportTransactionDraft,
    build_import_transaction_draft,
)
from backend.engine.private.portfolio.import_draft_batch import (
    ImportDraftBatchManifest,
    build_import_draft_batch_manifest,
)
from backend.engine.private.portfolio.import_instrument_resolution import (
    ImportInstrumentResolution,
    ImportInstrumentResolutionBatch,
    ImportInstrumentResolutionStatus,
    build_import_instrument_resolution,
    build_import_instrument_resolution_batch,
)
from backend.engine.private.portfolio.import_materialization import (
    ImportLedgerMaterializationBatch,
    ImportLedgerTransactionPlan,
    build_import_ledger_materialization_batch,
    build_import_ledger_transaction_plan,
)
from backend.engine.private.portfolio.import_parsing import (
    ImportParsedField,
    ParsedImportRecord,
    build_parsed_import_record,
)
from backend.engine.private.portfolio.import_parsed_batch import (
    ParsedImportBatchManifest,
    build_parsed_import_batch_manifest,
)
from backend.engine.private.portfolio.import_provenance import (
    ImportFileProvenance,
    ImportRecordProvenance,
    build_import_file_provenance,
    build_import_record_provenance,
)
from backend.engine.private.portfolio.sentinax_csv_import import (
    run_sentinax_canonical_csv_import_v1,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test Fixtures & Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_test_provenance_and_plan(
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
) -> Tuple[ImportRecordProvenance, ImportLedgerTransactionPlan]:
    port_id = portfolio_id or uuid4()
    acc_id = account_id or uuid4()
    inst_id = instrument_id or uuid4()
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

    rec_prov = records[ordinal - 1]
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
    elif transaction_type == TransactionType.DIVIDEND:
        draft_kwargs.update({
            "instrument_reference": "AAPL",
            "cash_amount": Decimal("25.00"),
            "cash_currency": Currency.USD,
        })
    elif transaction_type == TransactionType.FX_CONVERSION:
        draft_kwargs.update({
            "from_currency": Currency.USD,
            "from_amount": Decimal("100.00"),
            "to_currency": Currency.TRY,
            "to_amount": Decimal("3400.00"),
        })

    draft = build_import_transaction_draft(**draft_kwargs)

    res_status = (
        ImportInstrumentResolutionStatus.RESOLVED
        if transaction_type in (TransactionType.BUY, TransactionType.DIVIDEND)
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
    return rec_prov, plan


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


# ─────────────────────────────────────────────────────────────────────────────
# 1. Basic Record Tests (Section 54)
# ─────────────────────────────────────────────────────────────────────────────

class TestBasicRecordIntents:
    """Section 54: Basic binding intent builder across transaction types."""

    def test_resolved_buy_produces_valid_intent(self):
        """Matrix A: Valid RESOLVED BUY plan -> valid binding intent."""
        rec_prov, plan = _make_test_provenance_and_plan(transaction_type=TransactionType.BUY)
        intent = build_import_ledger_binding_intent(plan)

        assert intent.plan == plan
        assert intent.portfolio_id == plan.portfolio_id
        assert intent.account_id == plan.account_id
        assert intent.source_key == "sentinax_csv"
        assert intent.record_ordinal == 1
        assert intent.record_sha256 == rec_prov.record_sha256
        assert intent.file_content_sha256 == rec_prov.file_identity[3]
        assert intent.expected_plan_sha256 == plan.plan_sha256
        assert intent.claim_identity == rec_prov.record_identity

    def test_not_required_cash_deposit_produces_valid_intent(self):
        """Matrix B: Valid NOT_REQUIRED CASH_DEPOSIT plan -> valid binding intent."""
        rec_prov, plan = _make_test_provenance_and_plan(transaction_type=TransactionType.CASH_DEPOSIT)
        intent = build_import_ledger_binding_intent(plan)

        assert intent.claim_identity == rec_prov.record_identity
        assert intent.expected_plan_sha256 == plan.plan_sha256

    def test_referenced_dividend_produces_valid_intent(self):
        """Matrix C: Referenced DIVIDEND plan -> valid binding intent."""
        rec_prov, plan = _make_test_provenance_and_plan(transaction_type=TransactionType.DIVIDEND)
        intent = build_import_ledger_binding_intent(plan)

        assert intent.claim_identity == rec_prov.record_identity
        assert intent.expected_plan_sha256 == plan.plan_sha256

    def test_fx_conversion_produces_valid_intent(self):
        """Matrix D: FX_CONVERSION plan -> valid binding intent."""
        rec_prov, plan = _make_test_provenance_and_plan(transaction_type=TransactionType.FX_CONVERSION)
        intent = build_import_ledger_binding_intent(plan)

        assert intent.claim_identity == rec_prov.record_identity
        assert intent.expected_plan_sha256 == plan.plan_sha256


# ─────────────────────────────────────────────────────────────────────────────
# 2. Exact Claim & Tamper Tests (Sections 55-56)
# ─────────────────────────────────────────────────────────────────────────────

class TestExactClaimAndDirectTamper:
    """Sections 55-56: Exact claim fields and direct constructor tamper rejection."""

    def test_exact_claim_fields_match_record_identity(self):
        """Matrix E-L: Claim tuple equals underlying record_identity."""
        rec_prov, plan = _make_test_provenance_and_plan()
        intent = build_import_ledger_binding_intent(plan)

        port_id, acc_id, src_key, file_sha, ordinal, rec_sha = intent.claim_identity

        assert port_id == rec_prov.file_identity[0]
        assert acc_id == rec_prov.file_identity[1]
        assert src_key == rec_prov.file_identity[2]
        assert file_sha == rec_prov.file_identity[3]
        assert ordinal == rec_prov.record_ordinal
        assert rec_sha == rec_prov.record_sha256
        assert intent.expected_plan_sha256 == plan.plan_sha256
        assert intent.interpreted_claim_identity == (*intent.claim_identity, plan.plan_sha256)

    def test_tampered_portfolio_id_rejected(self):
        """Matrix M: tampered portfolio_id rejected."""
        rec_prov, plan = _make_test_provenance_and_plan()
        other_port = uuid4()

        with pytest.raises(PortfolioImportCommitError, match="portfolio_id"):
            ImportLedgerBindingIntent(
                plan=plan,
                portfolio_id=other_port,
                account_id=plan.account_id,
                source_key=rec_prov.file_identity[2],
                file_content_sha256=rec_prov.file_identity[3],
                record_ordinal=rec_prov.record_ordinal,
                record_sha256=rec_prov.record_sha256,
                expected_plan_sha256=plan.plan_sha256,
            )

    def test_tampered_account_id_rejected(self):
        """Matrix N: tampered account_id rejected."""
        rec_prov, plan = _make_test_provenance_and_plan()
        other_acc = uuid4()

        with pytest.raises(PortfolioImportCommitError, match="account_id"):
            ImportLedgerBindingIntent(
                plan=plan,
                portfolio_id=plan.portfolio_id,
                account_id=other_acc,
                source_key=rec_prov.file_identity[2],
                file_content_sha256=rec_prov.file_identity[3],
                record_ordinal=rec_prov.record_ordinal,
                record_sha256=rec_prov.record_sha256,
                expected_plan_sha256=plan.plan_sha256,
            )

    def test_tampered_source_key_rejected(self):
        """Matrix O: tampered source_key rejected."""
        rec_prov, plan = _make_test_provenance_and_plan()

        with pytest.raises(PortfolioImportCommitError, match="source_key"):
            ImportLedgerBindingIntent(
                plan=plan,
                portfolio_id=plan.portfolio_id,
                account_id=plan.account_id,
                source_key="other_csv",
                file_content_sha256=rec_prov.file_identity[3],
                record_ordinal=rec_prov.record_ordinal,
                record_sha256=rec_prov.record_sha256,
                expected_plan_sha256=plan.plan_sha256,
            )

    def test_tampered_file_content_sha_rejected(self):
        """Matrix P: tampered file_content_sha256 rejected."""
        rec_prov, plan = _make_test_provenance_and_plan()

        with pytest.raises(PortfolioImportCommitError, match="file_content_sha256"):
            ImportLedgerBindingIntent(
                plan=plan,
                portfolio_id=plan.portfolio_id,
                account_id=plan.account_id,
                source_key=rec_prov.file_identity[2],
                file_content_sha256="0" * 64,
                record_ordinal=rec_prov.record_ordinal,
                record_sha256=rec_prov.record_sha256,
                expected_plan_sha256=plan.plan_sha256,
            )

    def test_tampered_record_ordinal_rejected(self):
        """Matrix Q: tampered record_ordinal rejected."""
        rec_prov, plan = _make_test_provenance_and_plan(ordinal=1)

        with pytest.raises(PortfolioImportCommitError, match="record_ordinal"):
            ImportLedgerBindingIntent(
                plan=plan,
                portfolio_id=plan.portfolio_id,
                account_id=plan.account_id,
                source_key=rec_prov.file_identity[2],
                file_content_sha256=rec_prov.file_identity[3],
                record_ordinal=999,
                record_sha256=rec_prov.record_sha256,
                expected_plan_sha256=plan.plan_sha256,
            )

    def test_tampered_record_sha_rejected(self):
        """Matrix R: tampered record_sha256 rejected."""
        rec_prov, plan = _make_test_provenance_and_plan()

        with pytest.raises(PortfolioImportCommitError, match="record_sha256"):
            ImportLedgerBindingIntent(
                plan=plan,
                portfolio_id=plan.portfolio_id,
                account_id=plan.account_id,
                source_key=rec_prov.file_identity[2],
                file_content_sha256=rec_prov.file_identity[3],
                record_ordinal=rec_prov.record_ordinal,
                record_sha256="0" * 64,
                expected_plan_sha256=plan.plan_sha256,
            )

    def test_tampered_expected_plan_sha_rejected(self):
        """Matrix S: tampered expected_plan_sha256 rejected."""
        rec_prov, plan = _make_test_provenance_and_plan()

        with pytest.raises(PortfolioImportCommitError, match="expected_plan_sha256"):
            ImportLedgerBindingIntent(
                plan=plan,
                portfolio_id=plan.portfolio_id,
                account_id=plan.account_id,
                source_key=rec_prov.file_identity[2],
                file_content_sha256=rec_prov.file_identity[3],
                record_ordinal=rec_prov.record_ordinal,
                record_sha256=rec_prov.record_sha256,
                expected_plan_sha256="f" * 64,
            )

    def test_builder_input_type_validation(self):
        """Matrix T-V: Non-plan input rejected, builder accepts only plan."""
        with pytest.raises(PortfolioImportCommitError, match="ImportLedgerTransactionPlan"):
            build_import_ledger_binding_intent("not_a_plan")  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────────
# 3. Invariance & Sensitivity Tests (Sections 58-59)
# ─────────────────────────────────────────────────────────────────────────────

class TestInvarianceAndSensitivity:
    """Sections 58-59: Claim identity invariance and sensitivity rules."""

    def test_filename_and_imported_at_invariance(self):
        """Matrix W & X & Y: Filename and imported_at change produces identical claim identity."""
        port_id = uuid4()
        acc_id = uuid4()
        content = b"same_exact_content"

        rec1, plan1 = _make_test_provenance_and_plan(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="file1.csv",
            imported_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
            raw_content=content,
        )

        rec2, plan2 = _make_test_provenance_and_plan(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="file2_different.csv",
            imported_at=datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc),
            raw_content=content,
        )

        intent1 = build_import_ledger_binding_intent(plan1)
        intent2 = build_import_ledger_binding_intent(plan2)

        assert intent1.claim_identity == intent2.claim_identity

    def test_portfolio_sensitivity(self):
        """Matrix Z: Different portfolio produces distinct claim identity."""
        content = b"same_content"
        _, plan1 = _make_test_provenance_and_plan(portfolio_id=uuid4(), raw_content=content)
        _, plan2 = _make_test_provenance_and_plan(portfolio_id=uuid4(), raw_content=content)

        intent1 = build_import_ledger_binding_intent(plan1)
        intent2 = build_import_ledger_binding_intent(plan2)

        assert intent1.claim_identity != intent2.claim_identity

    def test_account_sensitivity(self):
        """Matrix AA: Different account produces distinct claim identity."""
        port_id = uuid4()
        content = b"same_content"
        _, plan1 = _make_test_provenance_and_plan(portfolio_id=port_id, account_id=uuid4(), raw_content=content)
        _, plan2 = _make_test_provenance_and_plan(portfolio_id=port_id, account_id=uuid4(), raw_content=content)

        intent1 = build_import_ledger_binding_intent(plan1)
        intent2 = build_import_ledger_binding_intent(plan2)

        assert intent1.claim_identity != intent2.claim_identity

    def test_source_key_sensitivity(self):
        """Matrix AB: Different source_key produces distinct claim identity."""
        port_id = uuid4()
        acc_id = uuid4()
        content = b"same_content"
        _, plan1 = _make_test_provenance_and_plan(portfolio_id=port_id, account_id=acc_id, source_key="src_a", raw_content=content)
        _, plan2 = _make_test_provenance_and_plan(portfolio_id=port_id, account_id=acc_id, source_key="src_b", raw_content=content)

        intent1 = build_import_ledger_binding_intent(plan1)
        intent2 = build_import_ledger_binding_intent(plan2)

        assert intent1.claim_identity != intent2.claim_identity

    def test_content_sensitivity(self):
        """Matrix AC: Different file content produces distinct claim identity."""
        port_id = uuid4()
        acc_id = uuid4()
        _, plan1 = _make_test_provenance_and_plan(portfolio_id=port_id, account_id=acc_id, raw_content=b"content_1")
        _, plan2 = _make_test_provenance_and_plan(portfolio_id=port_id, account_id=acc_id, raw_content=b"content_2")

        intent1 = build_import_ledger_binding_intent(plan1)
        intent2 = build_import_ledger_binding_intent(plan2)

        assert intent1.claim_identity != intent2.claim_identity


# ─────────────────────────────────────────────────────────────────────────────
# 4. Plan vs Claim Separation & Conflict Mechanics (Section 60)
# ─────────────────────────────────────────────────────────────────────────────

class TestPlanVsClaimSeparation:
    """Section 60: Proves claim identity is independent of semantic/resolver interpretation."""

    def test_same_claim_with_different_resolver_revision(self):
        """
        Matrix AE-AH: Changing resolver revision alters plan_sha256
        while keeping raw claim_identity identical.
        """
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        content = b"same_file_bytes"

        # Plan 1 with resolver revision 1
        _, plan1 = _make_test_provenance_and_plan(
            portfolio_id=port_id,
            account_id=acc_id,
            instrument_id=inst_id,
            raw_content=content,
            resolver_revision=1,
        )

        # Plan 2 with resolver revision 2 (different resolution SHA -> different plan SHA)
        _, plan2 = _make_test_provenance_and_plan(
            portfolio_id=port_id,
            account_id=acc_id,
            instrument_id=inst_id,
            raw_content=content,
            resolver_revision=2,
        )

        intent1 = build_import_ledger_binding_intent(plan1)
        intent2 = build_import_ledger_binding_intent(plan2)

        # Raw claim identity is identical
        assert intent1.claim_identity == intent2.claim_identity

        # Plan interpretation changed
        assert intent1.expected_plan_sha256 != intent2.expected_plan_sha256
        assert intent1.interpreted_claim_identity != intent2.interpreted_claim_identity


# ─────────────────────────────────────────────────────────────────────────────
# 5. Batch Layer Matrix (Section 61)
# ─────────────────────────────────────────────────────────────────────────────

class TestBatchLayerMatrix:
    """Section 61: Batch intent binding, canonical ordering, and duplicate detection."""

    def test_empty_materialization_batch_produces_empty_binding_batch(self):
        """Matrix AI: Empty materialization batch -> empty binding batch."""
        port_id = uuid4()
        acc_id = uuid4()
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        file_prov = build_import_file_provenance(
            portfolio_id=port_id,
            account_id=acc_id,
            source_key="sentinax_csv",
            filename="empty.csv",
            content=b"header\n",
            imported_at=imported_at,
        )
        raw_manifest = build_import_batch_manifest(file_prov, [])
        parsed_manifest = build_parsed_import_batch_manifest(raw_manifest, 1, [])
        ass_batch = build_import_assessment_batch(parsed_manifest, [])
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [])
        res_batch = build_import_instrument_resolution_batch(draft_manifest, [])
        mat_batch = build_import_ledger_materialization_batch(res_batch)

        binding_batch = build_import_ledger_binding_batch(mat_batch)

        assert binding_batch.intent_count == 0
        assert binding_batch.intents == ()

    def test_multi_plan_binding_batch_order_and_completeness(self):
        """Matrix AJ-AN: Multi-plan batch produces complete intents in canonical order."""
        _, plan1 = _make_test_provenance_and_plan(ordinal=1, transaction_type=TransactionType.BUY)
        _, plan2 = _make_test_provenance_and_plan(ordinal=2, transaction_type=TransactionType.CASH_DEPOSIT)

        # Manually assemble valid ImportLedgerMaterializationBatch
        # or use Sentinax CSV path
        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
            "cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,500.00,USD,,,,",
        ])

        resolver = MockInstrumentResolver(
            mapping={("AAPL", date(2026, 8, 28)): [uuid4()]}
        )

        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=uuid4(),
            account_id=uuid4(),
            filename="trades.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )

        mat_batch = build_import_ledger_materialization_batch(res_batch)
        binding_batch = build_import_ledger_binding_batch(mat_batch)

        assert binding_batch.intent_count == 2
        assert len(binding_batch.intents) == 2
        assert binding_batch.intents[0].record_ordinal == 1
        assert binding_batch.intents[1].record_ordinal == 2

    def test_missing_intent_rejected_in_direct_constructor(self):
        """Matrix AO: Missing intent in direct batch constructor rejected."""
        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
            "cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,500.00,USD,,,,",
        ])
        resolver = MockInstrumentResolver(mapping={("AAPL", date(2026, 8, 28)): [uuid4()]})
        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=uuid4(),
            account_id=uuid4(),
            filename="trades.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )
        mat_batch = build_import_ledger_materialization_batch(res_batch)
        intent1 = build_import_ledger_binding_intent(mat_batch.plans[0])

        with pytest.raises(PortfolioImportCommitError, match="intents count"):
            ImportLedgerBindingBatch(
                materialization_batch=mat_batch,
                intents=(intent1,),
            )

    def test_extra_intent_rejected_in_direct_constructor(self):
        """Matrix AP: Extra intent in direct batch constructor rejected."""
        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
        ])
        resolver = MockInstrumentResolver(mapping={("AAPL", date(2026, 8, 28)): [uuid4()]})
        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=uuid4(),
            account_id=uuid4(),
            filename="trades.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )
        mat_batch = build_import_ledger_materialization_batch(res_batch)
        intent1 = build_import_ledger_binding_intent(mat_batch.plans[0])

        with pytest.raises(PortfolioImportCommitError, match="intents count"):
            ImportLedgerBindingBatch(
                materialization_batch=mat_batch,
                intents=(intent1, intent1),
            )

    def test_shuffled_direct_tuple_rejected(self):
        """Matrix AR: Shuffled / unsorted direct intent tuple rejected."""
        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
            "cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,500.00,USD,,,,",
        ])
        resolver = MockInstrumentResolver(mapping={("AAPL", date(2026, 8, 28)): [uuid4()]})
        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=uuid4(),
            account_id=uuid4(),
            filename="trades.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )
        mat_batch = build_import_ledger_materialization_batch(res_batch)
        intent1 = build_import_ledger_binding_intent(mat_batch.plans[0])
        intent2 = build_import_ledger_binding_intent(mat_batch.plans[1])

        with pytest.raises(PortfolioImportCommitError, match="not semantically bound|record_ordinal ascending"):
            ImportLedgerBindingBatch(
                materialization_batch=mat_batch,
                intents=(intent2, intent1),
            )

    def test_semantic_plan_equality_accepted(self):
        """Matrix AN: Reconstructed intent with semantically equal plan accepted in batch."""
        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
        ])
        resolver = MockInstrumentResolver(mapping={("AAPL", date(2026, 8, 28)): [uuid4()]})
        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=uuid4(),
            account_id=uuid4(),
            filename="trades.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )
        mat_batch = build_import_ledger_materialization_batch(res_batch)
        orig_plan = mat_batch.plans[0]

        # Reconstructed equal plan
        reconstructed_plan = ImportLedgerTransactionPlan(
            resolution=orig_plan.resolution,
            portfolio_id=orig_plan.portfolio_id,
            account_id=orig_plan.account_id,
            transaction_type=orig_plan.transaction_type,
            effective_date=orig_plan.effective_date,
            executed_at=orig_plan.executed_at,
            instrument_id=orig_plan.instrument_id,
            quantity=orig_plan.quantity,
            unit_price=orig_plan.unit_price,
            trade_currency=orig_plan.trade_currency,
            cash_amount=orig_plan.cash_amount,
            cash_currency=orig_plan.cash_currency,
            from_currency=orig_plan.from_currency,
            from_amount=orig_plan.from_amount,
            to_currency=orig_plan.to_currency,
            to_amount=orig_plan.to_amount,
            plan_sha256=orig_plan.plan_sha256,
        )
        assert reconstructed_plan is not orig_plan
        assert reconstructed_plan == orig_plan

        intent = build_import_ledger_binding_intent(reconstructed_plan)
        batch = ImportLedgerBindingBatch(
            materialization_batch=mat_batch,
            intents=(intent,),
        )
        assert batch.intent_count == 1

    def test_duplicate_claim_identity_rejected_in_batch(self):
        """Matrix AQ: Duplicate claim_identity in direct batch constructor rejected."""
        _, plan = _make_test_provenance_and_plan(ordinal=1)
        intent = build_import_ledger_binding_intent(plan)

        # Mock a materialization batch with 2 plans
        # Direct constructor with duplicated intent (same claim identity)
        class DummyMaterializationBatch:
            plan_count = 2
            plans = (plan, plan)

        with pytest.raises(PortfolioImportCommitError):
            ImportLedgerBindingBatch(
                materialization_batch=DummyMaterializationBatch(),  # type: ignore[arg-type]
                intents=(intent, intent),
            )


# ─────────────────────────────────────────────────────────────────────────────
# 6. End-to-End Integration & Reimport (Sections 62-64)
# ─────────────────────────────────────────────────────────────────────────────

class TestEndToEndCommitIntegration:
    """Sections 62-64: Full pipeline integration through Sentinax Canonical CSV."""

    def test_full_pipeline_with_rejected_row_preserves_gaps(self):
        """
        Section 62: Real Canonical CSV with BUY (1), REJECTED (2), CASH (3), DIVIDEND (4), FX (5).
        Expected binding intents for ordinals 1, 3, 4, 5 (zero intent for rejected row 2).
        """
        port_id = uuid4()
        acc_id = uuid4()
        aapl_id = uuid4()
        msft_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,BAD_ROW,not_a_num,150.00,USD,,,,,,",  # REJECTED row (ordinal 2)
            "cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,500.00,USD,,,,",          # ordinal 3
            "dividend,2026-08-28,2026-08-28T10:15:30+00:00,MSFT,,,,25.00,USD,,,,",          # ordinal 4
            "fx_conversion,2026-08-28,2026-08-28T10:15:30+00:00,,,,,,,USD,100.00,TRY,3400.00", # ordinal 5
        ])

        resolver = MockInstrumentResolver(
            mapping={
                ("AAPL", eff_date): [aapl_id],
                ("MSFT", eff_date): [msft_id],
            }
        )

        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="mixed.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        mat_batch = build_import_ledger_materialization_batch(res_batch)
        binding_batch = build_import_ledger_binding_batch(mat_batch)

        assert binding_batch.intent_count == 4
        ordinals = [i.record_ordinal for i in binding_batch.intents]
        assert ordinals == [1, 3, 4, 5]

    def test_same_file_reimport_identical_claims(self):
        """Section 63: Same file imported twice produces identical claim identities per row."""
        port_id = uuid4()
        acc_id = uuid4()
        aapl_id = uuid4()
        eff_date = date(2026, 8, 28)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
        ])

        resolver = MockInstrumentResolver(
            mapping={("AAPL", eff_date): [aapl_id]}
        )

        # Run 1: file1.csv at t1
        res1 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="file1.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
            resolver=resolver,
        )
        mat1 = build_import_ledger_materialization_batch(res1)
        batch1 = build_import_ledger_binding_batch(mat1)

        # Run 2: file2.csv at t2
        res2 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="file2_renamed.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 19, 30, tzinfo=timezone.utc),
            resolver=resolver,
        )
        mat2 = build_import_ledger_materialization_batch(res2)
        batch2 = build_import_ledger_binding_batch(mat2)

        assert batch1.intents[0].claim_identity == batch2.intents[0].claim_identity
        assert batch1.intents[0].expected_plan_sha256 == batch2.intents[0].expected_plan_sha256

    def test_same_claim_changed_interpretation_conflict_case(self):
        """
        Section 64: Same raw import provenance with different resolver candidate yields
        same claim identity but different expected_plan_sha256.
        """
        port_id = uuid4()
        acc_id = uuid4()
        eff_date = date(2026, 8, 28)
        inst_id_v1 = uuid4()
        inst_id_v2 = uuid4()

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
        ])

        # Version 1 resolver
        resolver_v1 = MockInstrumentResolver(
            mapping={("AAPL", eff_date): [inst_id_v1]},
            resolver_revision=1,
        )
        res1 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
            resolver=resolver_v1,
        )
        mat1 = build_import_ledger_materialization_batch(res1)
        batch1 = build_import_ledger_binding_batch(mat1)

        # Version 2 resolver (different instrument UUID)
        resolver_v2 = MockInstrumentResolver(
            mapping={("AAPL", eff_date): [inst_id_v2]},
            resolver_revision=2,
        )
        res2 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
            resolver=resolver_v2,
        )
        mat2 = build_import_ledger_materialization_batch(res2)
        batch2 = build_import_ledger_binding_batch(mat2)

        # Claim identity is identical!
        assert batch1.intents[0].claim_identity == batch2.intents[0].claim_identity

        # Plan interpretation is different!
        assert batch1.intents[0].expected_plan_sha256 != batch2.intents[0].expected_plan_sha256


# ─────────────────────────────────────────────────────────────────────────────
# 7. Static / Source Inspection Tests (Sections 65-69)
# ─────────────────────────────────────────────────────────────────────────────

class TestSourceInspection:
    """Sections 65-69: Verifies zero forbidden symbols, imports, or authority violations."""

    def test_no_hashlib_imported(self):
        """Section 65: Production module does not import hashlib."""
        import backend.engine.private.portfolio.import_commit as commit_mod
        src = inspect.getsource(commit_mod)
        assert "import hashlib" not in src
        assert "from hashlib" not in src

    def test_no_uuid_generation(self):
        """Section 66: Production module contains no uuid4 or uuid5."""
        import backend.engine.private.portfolio.import_commit as commit_mod
        src = inspect.getsource(commit_mod)
        assert "uuid4" not in src
        assert "uuid5" not in src

    def test_no_current_time_calls(self):
        """Section 67: Production module contains no datetime.now, utcnow, date.today."""
        import backend.engine.private.portfolio.import_commit as commit_mod
        src = inspect.getsource(commit_mod)
        assert "datetime.now" not in src
        assert "datetime.utcnow" not in src
        assert "date.today" not in src
        assert "utcnow" not in src

    def test_no_ledger_identity_or_idempotency_fields(self):
        """Section 68: Dataclass has no external_source, external_reference, or idempotency_key."""
        fields = [f.name for f in ImportLedgerBindingIntent.__dataclass_fields__.values()]
        assert "external_source" not in fields
        assert "external_reference" not in fields
        assert "idempotency_key" not in fields
        assert "transaction_id" not in fields
        assert "recorded_at" not in fields
        assert "cash_bucket_id" not in fields

    def test_no_ledger_transaction_or_repository_imports(self):
        """Section 69: Production module does not import PortfolioTransaction or PortfolioRepository."""
        import backend.engine.private.portfolio.import_commit as commit_mod
        src = inspect.getsource(commit_mod)
        assert "PortfolioTransaction" not in src
        assert "PortfolioRepository" not in src
