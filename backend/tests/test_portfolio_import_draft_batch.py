"""
backend/tests/test_portfolio_import_draft_batch.py
===================================================
Tests for Phase 13I: Immutable Economic Draft Batch Manifest & Complete READY-Coverage Integrity.

Zero network calls (pytest-socket enforced).
Pure in-memory domain evaluation using real Phase 13A-13H builders and models.

Test Matrix:
    1.  Basic Matrix (A-G)
    2.  READY Coverage Matrix (H-M)
    3.  Binding Matrix (N-Q)
    4.  Collection Matrix (R-Y)
    5.  Ordering Matrix (Z-AD)
    6.  Hash Matrix (AE-AM)
    7.  Identity Matrix (AN-AQ)
    8.  Mixed-Status Canonical Test
    9.  Economic Draft Object Preservation
    10. Surface Red-Team
    11. Canonical CSV Integration
"""

from __future__ import annotations

import dataclasses
from dataclasses import fields as dataclass_fields, FrozenInstanceError
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from typing import List
from uuid import uuid4

import pytest

from backend.engine.private.domain import Currency, TransactionType
from backend.engine.private.portfolio.import_assessment import (
    ImportAssessmentBatch,
    ImportAssessmentDiagnostic,
    ImportAssessmentStatus,
    build_import_assessment_batch,
    build_import_record_assessment,
)
from backend.engine.private.portfolio.import_draft import (
    ImportTransactionDraft,
    PortfolioImportDraftError,
    build_import_transaction_draft,
)
from backend.engine.private.portfolio.import_draft_batch import (
    ImportDraftBatchManifest,
    PortfolioImportDraftBatchError,
    build_import_draft_batch_manifest,
)
from backend.engine.private.portfolio.import_provenance import (
    build_import_file_provenance,
    build_import_record_provenance,
)
from backend.engine.private.portfolio.import_batch import build_import_batch_manifest
from backend.engine.private.portfolio.import_parsing import (
    ImportParsedField,
    build_parsed_import_record,
)
from backend.engine.private.portfolio.import_parsed_batch import (
    build_parsed_import_batch_manifest,
)
from backend.engine.private.portfolio.import_pipeline import build_import_staging_result
from backend.engine.private.portfolio.parsers import SentinaxCanonicalCsvParserV1


# ─────────────────────────────────────────────────────────────────────────────
# Helper Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _make_test_assessment_batch(
    statuses: List[ImportAssessmentStatus],
    portfolio_id=None,
    account_id=None,
    source_key: str = "sentinax_csv",
    parser_revision: int = 1,
) -> ImportAssessmentBatch:
    """Builds a real, verified ImportAssessmentBatch with specified statuses."""
    port_id = portfolio_id or uuid4()
    acc_id = account_id or uuid4()
    t = datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)

    file_prov = build_import_file_provenance(
        portfolio_id=port_id,
        account_id=acc_id,
        source_key=source_key,
        filename="test.csv",
        content=b"dummy_content",
        imported_at=t,
    )

    # Use the same raw bytes for both provenance and parsed record (SHA must match)
    raw_rows = [f"row_{i}".encode("utf-8") for i in range(len(statuses))]

    rec_provs = [
        build_import_record_provenance(
            file_provenance=file_prov,
            record_ordinal=i + 1,
            raw_record=raw_rows[i],
        )
        for i in range(len(statuses))
    ]

    raw_manifest = build_import_batch_manifest(
        file_provenance=file_prov,
        records=rec_provs,
    )

    parsed_records = [
        build_parsed_import_record(
            record_provenance=rec_provs[i],
            raw_record=raw_rows[i],
            parser_revision=parser_revision,
            fields=[
                ImportParsedField("symbol", f"TICKER_{i}"),
                ImportParsedField("quantity", "100"),
                ImportParsedField("price", "25.50"),
            ],
        )
        for i in range(len(statuses))
    ]

    parsed_manifest = build_parsed_import_batch_manifest(
        raw_manifest=raw_manifest,
        parser_revision=parser_revision,
        parsed_records=parsed_records,
    )

    assessments = []
    for i, status in enumerate(statuses):
        if status == ImportAssessmentStatus.READY:
            ass = build_import_record_assessment(parsed_records[i], status)
        else:
            diag = ImportAssessmentDiagnostic(
                code="diag_code", message="Diag message", field_key="symbol"
            )
            ass = build_import_record_assessment(parsed_records[i], status, [diag])
        assessments.append(ass)

    return build_import_assessment_batch(parsed_manifest, assessments)


def _make_empty_assessment_batch() -> ImportAssessmentBatch:
    """Builds a verified ImportAssessmentBatch with zero records."""
    port_id = uuid4()
    acc_id = uuid4()
    t = datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)

    file_prov = build_import_file_provenance(
        portfolio_id=port_id,
        account_id=acc_id,
        source_key="sentinax_csv",
        filename="empty.csv",
        content=b"empty",
        imported_at=t,
    )

    raw_manifest = build_import_batch_manifest(
        file_provenance=file_prov,
        records=[],
    )

    parsed_manifest = build_parsed_import_batch_manifest(
        raw_manifest=raw_manifest,
        parser_revision=1,
        parsed_records=[],
    )

    return build_import_assessment_batch(parsed_manifest, [])


def _make_buy_draft(
    assessment_batch: ImportAssessmentBatch,
    record_ordinal: int,
    instrument: str = "AAPL",
    qty: str = "10",
    price: str = "150.00",
) -> ImportTransactionDraft:
    return build_import_transaction_draft(
        assessment_batch=assessment_batch,
        record_ordinal=record_ordinal,
        transaction_type=TransactionType.BUY,
        effective_date=date(2026, 8, 28),
        instrument_reference=instrument,
        quantity=Decimal(qty),
        unit_price=Decimal(price),
        trade_currency=Currency.USD,
    )


def _make_cash_deposit_draft(
    assessment_batch: ImportAssessmentBatch,
    record_ordinal: int,
    amount: str = "500.00",
) -> ImportTransactionDraft:
    return build_import_transaction_draft(
        assessment_batch=assessment_batch,
        record_ordinal=record_ordinal,
        transaction_type=TransactionType.CASH_DEPOSIT,
        effective_date=date(2026, 8, 28),
        cash_amount=Decimal(amount),
        cash_currency=Currency.USD,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Basic Matrix (A-G)
# ─────────────────────────────────────────────────────────────────────────────

class TestBasicMatrix:
    """A-G: Basic structural validity."""

    def test_A_empty_assessment_empty_drafts_valid(self):
        """A: Empty assessment batch + empty drafts is valid."""
        batch = _make_empty_assessment_batch()
        manifest = build_import_draft_batch_manifest(batch, [])
        assert manifest.draft_count == 0
        assert manifest.record_count == 0
        assert manifest.drafts == ()

    def test_B_non_empty_zero_ready_empty_drafts_valid(self):
        """B: Non-empty batch with all UNRESOLVED/REJECTED + empty drafts is valid."""
        batch = _make_test_assessment_batch([
            ImportAssessmentStatus.UNRESOLVED,
            ImportAssessmentStatus.REJECTED,
        ])
        manifest = build_import_draft_batch_manifest(batch, [])
        assert manifest.draft_count == 0
        assert manifest.ready_count == 0
        assert manifest.unresolved_count == 1
        assert manifest.rejected_count == 1

    def test_C_one_ready_one_draft_valid(self):
        """C: One READY record + one matching draft is valid."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        manifest = build_import_draft_batch_manifest(batch, [draft])
        assert manifest.draft_count == 1
        assert manifest.drafts == (draft,)

    def test_D_multiple_ready_complete_drafts_valid(self):
        """D: Multiple READY records + complete drafts is valid."""
        batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(batch, 1)
        d2 = _make_buy_draft(batch, 2, instrument="MSFT")
        d3 = _make_buy_draft(batch, 3, instrument="TSLA")
        manifest = build_import_draft_batch_manifest(batch, [d1, d2, d3])
        assert manifest.draft_count == 3
        assert manifest.ready_count == 3

    def test_E_mixed_statuses_drafts_only_for_ready_valid(self):
        """E: Mixed READY/UNRESOLVED/REJECTED with drafts only for READY is valid."""
        batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.UNRESOLVED,
            ImportAssessmentStatus.REJECTED,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(batch, 1)
        d4 = _make_cash_deposit_draft(batch, 4)
        manifest = build_import_draft_batch_manifest(batch, [d1, d4])
        assert manifest.draft_count == 2
        assert manifest.ready_count == 2

    def test_F_draft_count_exact(self):
        """F: draft_count equals assessment_batch.ready_count."""
        batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.UNRESOLVED,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(batch, 1)
        d3 = _make_buy_draft(batch, 3, instrument="GOOG")
        manifest = build_import_draft_batch_manifest(batch, [d1, d3])
        assert manifest.draft_count == manifest.assessment_batch.ready_count

    def test_G_frozen_mutation_rejected(self):
        """G: Mutation on frozen ImportDraftBatchManifest raises FrozenInstanceError."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        manifest = build_import_draft_batch_manifest(batch, [draft])
        with pytest.raises((FrozenInstanceError, dataclasses.FrozenInstanceError, TypeError, AttributeError)):
            manifest.draft_manifest_sha256 = "x" * 64  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 2. READY Coverage Matrix (H-M)
# ─────────────────────────────────────────────────────────────────────────────

class TestReadyCoverageMatrix:
    """H-M: Coverage enforcement."""

    def test_H_ready_ordinal_omitted_rejected(self):
        """H: Omitting a READY ordinal fails closed."""
        batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(batch, 1)
        # d2 omitted
        with pytest.raises(PortfolioImportDraftBatchError, match="missing drafts for READY record ordinals"):
            build_import_draft_batch_manifest(batch, [d1])

    def test_I_two_ready_one_draft_rejected(self):
        """I: Two READY ordinals with only one draft fails closed."""
        batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(batch, 1)
        d2 = _make_buy_draft(batch, 2, instrument="MSFT")
        # d3 omitted
        with pytest.raises(PortfolioImportDraftBatchError, match="missing drafts"):
            build_import_draft_batch_manifest(batch, [d1, d2])

    def test_J_duplicate_draft_ordinal_rejected(self):
        """J: Duplicate draft record_ordinal fails closed explicitly."""
        batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1a = _make_buy_draft(batch, 1)
        d1b = _make_buy_draft(batch, 1, qty="5")  # same ordinal, different economics
        with pytest.raises(PortfolioImportDraftBatchError, match="Duplicate draft record_ordinal"):
            build_import_draft_batch_manifest(batch, [d1a, d1b])

    def test_K_draft_for_unresolved_ordinal_rejected(self):
        """K: Draft for UNRESOLVED ordinal fails closed."""
        batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.UNRESOLVED,
        ])
        d1 = _make_buy_draft(batch, 1)
        # Cannot build draft for ordinal 2 (UNRESOLVED) — Phase 13H already gates this.
        with pytest.raises(PortfolioImportDraftError, match="Only records with READY assessment status"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=2,
                transaction_type=TransactionType.BUY,
                effective_date=date(2026, 8, 28),
                instrument_reference="AAPL",
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
            )

    def test_L_draft_for_rejected_ordinal_rejected(self):
        """L: Draft for REJECTED ordinal fails closed."""
        batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.REJECTED,
        ])
        with pytest.raises(PortfolioImportDraftError, match="Only records with READY assessment status"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=2,
                transaction_type=TransactionType.BUY,
                effective_date=date(2026, 8, 28),
                instrument_reference="AAPL",
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
            )

    def test_M_exact_ready_ordinal_set_accepted(self):
        """M: Providing exactly the READY ordinal set succeeds."""
        batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.UNRESOLVED,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(batch, 1)
        d3 = _make_buy_draft(batch, 3, instrument="GOOG")
        manifest = build_import_draft_batch_manifest(batch, [d1, d3])
        assert {d.record_ordinal for d in manifest.drafts} == {1, 3}


# ─────────────────────────────────────────────────────────────────────────────
# 3. Binding Matrix (N-Q)
# ─────────────────────────────────────────────────────────────────────────────

class TestBindingMatrix:
    """N-Q: Assessment batch binding enforcement."""

    def test_N_draft_bound_to_exact_batch_accepted(self):
        """N: Draft bound to the exact same assessment batch object is accepted."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        assert draft.assessment_batch is batch
        manifest = build_import_draft_batch_manifest(batch, [draft])
        assert manifest.assessment_batch is batch

    def test_O_draft_from_different_batch_rejected(self):
        """O: Draft from a different assessment batch fails closed."""
        batch1 = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        batch2 = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft_for_batch2 = _make_buy_draft(batch2, 1)
        # draft_for_batch2.assessment_batch is batch2, not batch1
        with pytest.raises(PortfolioImportDraftBatchError, match="different assessment batch"):
            build_import_draft_batch_manifest(batch1, [draft_for_batch2])

    def test_P_same_ordinal_economics_foreign_batch_rejected(self):
        """P: Same ordinal/economics from foreign batch fails closed."""
        batch1 = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        batch2 = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        # Build draft against batch2 - even if ordinal matches, binding must fail
        draft_foreign = _make_buy_draft(batch2, 1)
        with pytest.raises(PortfolioImportDraftBatchError, match="different assessment batch"):
            build_import_draft_batch_manifest(batch1, [draft_foreign])

    def test_Q_old_draft_rejected_after_status_change(self):
        """Q: Draft from old batch rejected when status changes in a rebuilt batch."""
        # Build batch1 with ordinal 1 READY
        statuses = [ImportAssessmentStatus.READY, ImportAssessmentStatus.READY]
        batch_old = _make_test_assessment_batch(statuses)
        draft_old = _make_buy_draft(batch_old, 1)

        # "Rebuild" batch as a separate batch object (status change scenario)
        # The new batch is different from the old batch (different object)
        batch_new = _make_test_assessment_batch([
            ImportAssessmentStatus.UNRESOLVED,  # ordinal 1 now UNRESOLVED
            ImportAssessmentStatus.READY,
        ])

        # The draft was built against batch_old, so it will be rejected
        with pytest.raises(PortfolioImportDraftBatchError, match="different assessment batch"):
            build_import_draft_batch_manifest(batch_new, [draft_old])


# ─────────────────────────────────────────────────────────────────────────────
# 4. Collection Matrix (R-Y)
# ─────────────────────────────────────────────────────────────────────────────

class TestCollectionMatrix:
    """R-Y: Input collection type enforcement."""

    def test_R_builder_list_accepted(self):
        """R: Builder accepts a list of drafts."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        manifest = build_import_draft_batch_manifest(batch, [draft])
        assert manifest.draft_count == 1

    def test_S_builder_tuple_accepted(self):
        """S: Builder accepts a tuple of drafts."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        manifest = build_import_draft_batch_manifest(batch, (draft,))
        assert manifest.draft_count == 1

    def test_T_generator_rejected(self):
        """T: Generator input fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        gen = (d for d in [draft])
        with pytest.raises(PortfolioImportDraftBatchError, match="list or tuple"):
            build_import_draft_batch_manifest(batch, gen)  # type: ignore

    def test_U_set_rejected(self):
        """U: Set input fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportDraftBatchError, match="list or tuple"):
            build_import_draft_batch_manifest(batch, {draft})  # type: ignore

    def test_V_dict_rejected(self):
        """V: Dict input fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        with pytest.raises(PortfolioImportDraftBatchError, match="list or tuple"):
            build_import_draft_batch_manifest(batch, {1: "value"})  # type: ignore

    def test_W_str_rejected(self):
        """W: String input fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        with pytest.raises(PortfolioImportDraftBatchError, match="list or tuple"):
            build_import_draft_batch_manifest(batch, "draft")  # type: ignore

    def test_X_bytes_rejected(self):
        """X: Bytes input fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        with pytest.raises(PortfolioImportDraftBatchError, match="list or tuple"):
            build_import_draft_batch_manifest(batch, b"draft")  # type: ignore

    def test_Y_direct_constructor_list_rejected(self):
        """Y: Direct constructor with list (not tuple) fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        manifest = build_import_draft_batch_manifest(batch, [draft])
        sha = manifest.draft_manifest_sha256
        with pytest.raises(PortfolioImportDraftBatchError):
            ImportDraftBatchManifest(
                assessment_batch=batch,
                drafts=[draft],  # type: ignore — must be tuple
                draft_manifest_sha256=sha,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 5. Ordering Matrix (Z-AD)
# ─────────────────────────────────────────────────────────────────────────────

class TestOrderingMatrix:
    """Z-AD: Canonical ordering and invariance."""

    def _make_three_ready_batch_and_drafts(self):
        batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(batch, 1)
        d2 = _make_buy_draft(batch, 2, instrument="MSFT")
        d3 = _make_buy_draft(batch, 3, instrument="TSLA")
        return batch, d1, d2, d3

    def test_Z_shuffled_builder_drafts_canonicalized(self):
        """Z: Shuffled builder input produces canonically sorted tuple."""
        batch, d1, d2, d3 = self._make_three_ready_batch_and_drafts()
        manifest = build_import_draft_batch_manifest(batch, [d3, d1, d2])
        assert manifest.drafts[0].record_ordinal == 1
        assert manifest.drafts[1].record_ordinal == 2
        assert manifest.drafts[2].record_ordinal == 3

    def test_AA_ordered_shuffled_same_tuple(self):
        """AA: Ordered and shuffled inputs produce identical tuple."""
        batch, d1, d2, d3 = self._make_three_ready_batch_and_drafts()
        m_ordered = build_import_draft_batch_manifest(batch, [d1, d2, d3])
        m_shuffled = build_import_draft_batch_manifest(batch, [d3, d1, d2])
        assert m_ordered.drafts == m_shuffled.drafts

    def test_AB_ordered_shuffled_same_sha(self):
        """AB: Ordered and shuffled inputs produce identical draft_manifest_sha256."""
        batch, d1, d2, d3 = self._make_three_ready_batch_and_drafts()
        m_ordered = build_import_draft_batch_manifest(batch, [d1, d2, d3])
        m_shuffled = build_import_draft_batch_manifest(batch, [d2, d3, d1])
        assert m_ordered.draft_manifest_sha256 == m_shuffled.draft_manifest_sha256

    def test_AC_ordered_shuffled_same_identity(self):
        """AC: Ordered and shuffled inputs produce identical draft_manifest_identity."""
        batch, d1, d2, d3 = self._make_three_ready_batch_and_drafts()
        m_ordered = build_import_draft_batch_manifest(batch, [d1, d2, d3])
        m_shuffled = build_import_draft_batch_manifest(batch, [d3, d2, d1])
        assert m_ordered.draft_manifest_identity == m_shuffled.draft_manifest_identity

    def test_AD_unsorted_direct_tuple_rejected(self):
        """AD: Direct constructor with unsorted (descending) drafts tuple fails closed."""
        batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(batch, 1)
        d2 = _make_buy_draft(batch, 2, instrument="MSFT")
        # Get the valid SHA for sorted tuple
        m = build_import_draft_batch_manifest(batch, [d1, d2])
        # Now try to create directly with unsorted tuple (d2, d1)
        with pytest.raises(PortfolioImportDraftBatchError, match="not sorted"):
            ImportDraftBatchManifest(
                assessment_batch=batch,
                drafts=(d2, d1),
                draft_manifest_sha256=m.draft_manifest_sha256,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 6. Hash Matrix (AE-AM)
# ─────────────────────────────────────────────────────────────────────────────

class TestHashMatrix:
    """AE-AM: Canonical JSON/hash correctness and sensitivity."""

    def test_AE_independent_canonical_json_hash_matches(self):
        """AE: Independently computed canonical hash matches stored draft_manifest_sha256."""
        batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(batch, 1)
        d2 = _make_buy_draft(batch, 2, instrument="MSFT")
        manifest = build_import_draft_batch_manifest(batch, [d1, d2])

        # Recompute preimage independently
        preimage = [
            str(batch.portfolio_id),
            str(batch.account_id),
            batch.source_key,
            batch.file_content_sha256,
            batch.raw_manifest_sha256,
            batch.parser_revision,
            batch.parsed_manifest_sha256,
            batch.assessment_manifest_sha256,
            [
                [
                    d.record_ordinal,
                    d.assessment.parsed_record.parsed_sha256,
                    d.draft_sha256,
                ]
                for d in manifest.drafts
            ],
        ]
        encoded = json.dumps(preimage, ensure_ascii=True, separators=(",", ":"))
        expected_sha = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        assert manifest.draft_manifest_sha256 == expected_sha

    def test_AF_repeated_build_deterministic(self):
        """AF: Building the same batch twice produces identical SHA."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        d = _make_buy_draft(batch, 1)
        m1 = build_import_draft_batch_manifest(batch, [d])
        m2 = build_import_draft_batch_manifest(batch, [d])
        assert m1.draft_manifest_sha256 == m2.draft_manifest_sha256

    def test_AG_economic_draft_change_changes_manifest_sha(self):
        """AG: Changing one draft's economics changes the draft_manifest_sha256."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        d_v1 = _make_buy_draft(batch, 1, qty="10")
        d_v2 = _make_buy_draft(batch, 1, qty="20")  # different quantity
        m1 = build_import_draft_batch_manifest(batch, [d_v1])
        m2 = build_import_draft_batch_manifest(batch, [d_v2])
        assert m1.draft_manifest_sha256 != m2.draft_manifest_sha256

    def test_AH_transaction_type_change_changes_sha(self):
        """AH: Different valid transaction type/economics changes manifest SHA."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        d_buy = _make_buy_draft(batch, 1)
        d_sell = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.SELL,
            effective_date=date(2026, 8, 28),
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        m1 = build_import_draft_batch_manifest(batch, [d_buy])
        m2 = build_import_draft_batch_manifest(batch, [d_sell])
        assert m1.draft_manifest_sha256 != m2.draft_manifest_sha256

    def test_AI_assessment_manifest_change_changes_sha(self):
        """AI: Different assessment batch (different assessment_manifest_sha256) produces different SHA."""
        batch1 = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        batch2 = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        d1 = _make_buy_draft(batch1, 1)
        d2 = _make_buy_draft(batch2, 1)
        m1 = build_import_draft_batch_manifest(batch1, [d1])
        m2 = build_import_draft_batch_manifest(batch2, [d2])
        # Different batches → different assessment_manifest_sha256 → different draft_manifest_sha256
        assert m1.draft_manifest_sha256 != m2.draft_manifest_sha256

    def test_AJ_valid_format_fake_digest_rejected(self):
        """AJ: A valid-looking but incorrect hex digest is rejected by direct constructor."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        fake_sha = "a" * 64  # valid format, wrong value
        with pytest.raises(PortfolioImportDraftBatchError, match="digest mismatch"):
            ImportDraftBatchManifest(
                assessment_batch=batch,
                drafts=(draft,),
                draft_manifest_sha256=fake_sha,
            )

    def test_AK_uppercase_digest_rejected(self):
        """AK: Uppercase SHA hex string fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        valid_manifest = build_import_draft_batch_manifest(batch, [draft])
        upper_sha = valid_manifest.draft_manifest_sha256.upper()
        with pytest.raises(PortfolioImportDraftBatchError, match="lowercase hex"):
            ImportDraftBatchManifest(
                assessment_batch=batch,
                drafts=(draft,),
                draft_manifest_sha256=upper_sha,
            )

    def test_AL_digest_with_trailing_newline_rejected(self):
        """AL: digest + trailing newline fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        valid_manifest = build_import_draft_batch_manifest(batch, [draft])
        with pytest.raises(PortfolioImportDraftBatchError, match="lowercase hex"):
            ImportDraftBatchManifest(
                assessment_batch=batch,
                drafts=(draft,),
                draft_manifest_sha256=valid_manifest.draft_manifest_sha256 + "\n",
            )

    def test_AM_digest_with_crlf_rejected(self):
        """AM: digest + CRLF fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        valid_manifest = build_import_draft_batch_manifest(batch, [draft])
        with pytest.raises(PortfolioImportDraftBatchError, match="lowercase hex"):
            ImportDraftBatchManifest(
                assessment_batch=batch,
                drafts=(draft,),
                draft_manifest_sha256=valid_manifest.draft_manifest_sha256 + "\r\n",
            )


# ─────────────────────────────────────────────────────────────────────────────
# 7. Identity Matrix (AN-AQ)
# ─────────────────────────────────────────────────────────────────────────────

class TestIdentityMatrix:
    """AN-AQ: draft_manifest_identity composition."""

    def test_AN_draft_manifest_identity_deterministic(self):
        """AN: draft_manifest_identity is deterministic for the same inputs."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        m1 = build_import_draft_batch_manifest(batch, [draft])
        m2 = build_import_draft_batch_manifest(batch, [draft])
        assert m1.draft_manifest_identity == m2.draft_manifest_identity

    def test_AO_identity_extends_assessment_manifest_identity(self):
        """AO: draft_manifest_identity starts with assessment_manifest_identity."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        manifest = build_import_draft_batch_manifest(batch, [draft])
        identity = manifest.draft_manifest_identity
        ass_identity = batch.assessment_manifest_identity
        assert identity[: len(ass_identity)] == ass_identity
        assert identity[-1] == manifest.draft_manifest_sha256

    def test_AP_no_uuid_generation(self):
        """AP: draft_manifest_identity contains no freshly generated UUID."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        m1 = build_import_draft_batch_manifest(batch, [draft])
        m2 = build_import_draft_batch_manifest(batch, [draft])
        # UUIDs in identity come from batch (portfolio_id, account_id), not freshly generated
        assert m1.draft_manifest_identity == m2.draft_manifest_identity

    def test_AQ_draft_manifest_identity_not_ledger_external_identity(self):
        """AQ: draft_manifest_identity is staging-only; no external_reference or idempotency key."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        manifest = build_import_draft_batch_manifest(batch, [draft])
        # Identity is a plain tuple of existing domain values, not a ledger key
        identity = manifest.draft_manifest_identity
        assert isinstance(identity, tuple)
        # Last element is the draft_manifest_sha256 (a hex string, not a UUID)
        assert isinstance(identity[-1], str)
        assert len(identity[-1]) == 64


# ─────────────────────────────────────────────────────────────────────────────
# 8. Mixed-Status Canonical Test
# ─────────────────────────────────────────────────────────────────────────────

class TestMixedStatusCanonical:
    """Canonical mixed-status scenario from Phase 13I spec."""

    def test_mixed_status_canonical(self):
        """
        Four assessments: 1 READY, 2 UNRESOLVED, 3 REJECTED, 4 READY.
        Drafts for ordinals 1 and 4 only.
        Verify all required properties.
        """
        batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,        # ordinal 1
            ImportAssessmentStatus.UNRESOLVED,   # ordinal 2
            ImportAssessmentStatus.REJECTED,     # ordinal 3
            ImportAssessmentStatus.READY,        # ordinal 4
        ])

        d1 = _make_buy_draft(batch, 1)
        d4 = _make_cash_deposit_draft(batch, 4)

        manifest = build_import_draft_batch_manifest(batch, [d1, d4])

        assert manifest.draft_count == 2
        assert manifest.ready_count == 2
        assert manifest.unresolved_count == 1
        assert manifest.rejected_count == 1
        assert manifest.record_count == 4

        # Canonical ordinals
        ordinals = tuple(d.record_ordinal for d in manifest.drafts)
        assert ordinals == (1, 4)


# ─────────────────────────────────────────────────────────────────────────────
# 9. Economic Draft Object Preservation
# ─────────────────────────────────────────────────────────────────────────────

class TestDraftObjectPreservation:
    """Builder must preserve original draft objects (not reconstruct copies)."""

    def test_draft_object_identity_preserved_after_sort(self):
        """Builder preserves original ImportTransactionDraft instances after sorting."""
        batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(batch, 1)
        d2 = _make_buy_draft(batch, 2, instrument="MSFT")
        d3 = _make_buy_draft(batch, 3, instrument="TSLA")

        # Submit in reversed order
        manifest = build_import_draft_batch_manifest(batch, [d3, d1, d2])

        # Verify sorted by ordinal AND same object instances
        assert manifest.drafts[0] is d1
        assert manifest.drafts[1] is d2
        assert manifest.drafts[2] is d3


# ─────────────────────────────────────────────────────────────────────────────
# 10. Surface Red-Team
# ─────────────────────────────────────────────────────────────────────────────

class TestSurfaceRedTeam:
    """Verify ImportDraftBatchManifest contains ONLY the three allowed stored fields."""

    def test_only_three_stored_fields(self):
        """ImportDraftBatchManifest has exactly 3 dataclass fields."""
        field_names = {f.name for f in dataclass_fields(ImportDraftBatchManifest)}
        assert field_names == {"assessment_batch", "drafts", "draft_manifest_sha256"}

    def test_no_portfolio_id_stored_duplicate(self):
        """portfolio_id is not a stored dataclass field."""
        field_names = {f.name for f in dataclass_fields(ImportDraftBatchManifest)}
        assert "portfolio_id" not in field_names

    def test_no_account_id_stored_duplicate(self):
        """account_id is not a stored dataclass field."""
        field_names = {f.name for f in dataclass_fields(ImportDraftBatchManifest)}
        assert "account_id" not in field_names

    def test_no_source_key_stored_duplicate(self):
        """source_key is not a stored dataclass field."""
        field_names = {f.name for f in dataclass_fields(ImportDraftBatchManifest)}
        assert "source_key" not in field_names

    def test_no_instrument_ids(self):
        """No instrument_id field stored in manifest."""
        field_names = {f.name for f in dataclass_fields(ImportDraftBatchManifest)}
        assert "instrument_id" not in field_names

    def test_no_external_reference(self):
        """No external_reference or external_source field stored."""
        field_names = {f.name for f in dataclass_fields(ImportDraftBatchManifest)}
        assert "external_reference" not in field_names
        assert "external_source" not in field_names

    def test_no_portfolio_transaction(self):
        """No PortfolioTransaction import in the draft_batch module."""
        import backend.engine.private.portfolio.import_draft_batch as module
        # The module should not import PortfolioTransaction
        assert "PortfolioTransaction" not in (
            getattr(module, "__file__", "") or ""
        ) or True  # file path check not useful; check module imports directly
        # Verify: PortfolioTransaction is not accessible from the draft_batch module namespace
        assert not hasattr(module, "PortfolioTransaction")

    def test_no_instrument_resolver(self):
        """No instrument resolver import in the module."""
        import backend.engine.private.portfolio.import_draft_batch as module
        import inspect
        src = inspect.getsource(module)
        assert "instrument_resolver" not in src.lower()
        assert "InstrumentResolver" not in src

    def test_derived_counts_delegate_to_assessment_batch(self):
        """Derived count properties delegate to assessment_batch, not stored locally."""
        batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.UNRESOLVED,
            ImportAssessmentStatus.REJECTED,
        ])
        d1 = _make_buy_draft(batch, 1)
        manifest = build_import_draft_batch_manifest(batch, [d1])
        # Derived properties mirror assessment_batch values
        assert manifest.record_count == batch.record_count
        assert manifest.ready_count == batch.ready_count
        assert manifest.unresolved_count == batch.unresolved_count
        assert manifest.rejected_count == batch.rejected_count


# ─────────────────────────────────────────────────────────────────────────────
# 11. Canonical CSV Integration
# ─────────────────────────────────────────────────────────────────────────────

class TestCanonicalCsvIntegration:
    """
    Real pipeline integration: SentinaxCanonicalCsvParserV1 → build_import_staging_result
    → assessment builders → draft builders → Phase 13I manifest.
    Two parsed rows, both READY, both drafted explicitly.
    """

    CSV_BYTES = (
        b"transaction_type,effective_date,symbol,quantity,unit_price,currency\n"
        b"BUY,2026-08-28,AAPL,10,150.00,USD\n"
        b"BUY,2026-08-28,MSFT,5,320.00,USD\n"
    )

    def _build_staging(self):
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        parser = SentinaxCanonicalCsvParserV1()
        return build_import_staging_result(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="test_batch.csv",
            content=self.CSV_BYTES,
            imported_at=t,
            parser=parser,
        )

    def _build_full_manifest(self):
        staging = self._build_staging()
        parsed_manifest = staging.parsed_manifest

        assessments = []
        for rec in parsed_manifest.parsed_records:
            ass = build_import_record_assessment(rec, ImportAssessmentStatus.READY)
            assessments.append(ass)

        assessment_batch = build_import_assessment_batch(parsed_manifest, assessments)

        d1 = build_import_transaction_draft(
            assessment_batch=assessment_batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        d2 = build_import_transaction_draft(
            assessment_batch=assessment_batch,
            record_ordinal=2,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_reference="MSFT",
            quantity=Decimal("5"),
            unit_price=Decimal("320.00"),
            trade_currency=Currency.USD,
        )

        return build_import_draft_batch_manifest(assessment_batch, [d1, d2])

    def test_canonical_csv_two_rows_both_ready(self):
        """Two CSV rows, both READY, both drafted — manifest valid."""
        manifest = self._build_full_manifest()
        assert manifest.draft_count == 2
        assert manifest.ready_count == 2

    def test_canonical_csv_exact_ready_coverage(self):
        """Exact READY coverage: ordinals {1, 2}."""
        manifest = self._build_full_manifest()
        ordinals = {d.record_ordinal for d in manifest.drafts}
        assert ordinals == {1, 2}

    def test_canonical_csv_deterministic_ordering(self):
        """Drafts in manifest are sorted by record_ordinal ascending."""
        manifest = self._build_full_manifest()
        ords = [d.record_ordinal for d in manifest.drafts]
        assert ords == sorted(ords)

    def test_canonical_csv_deterministic_digest(self):
        """Building the manifest twice yields identical draft_manifest_sha256."""
        m1 = self._build_full_manifest()
        m2 = self._build_full_manifest()
        # Note: staging creates new file_provenance each call (new UUIDs) so SHAs differ.
        # We verify determinism within a single build by rebuilding with same batch.
        staging = self._build_staging()
        parsed_manifest = staging.parsed_manifest
        assessments = [
            build_import_record_assessment(rec, ImportAssessmentStatus.READY)
            for rec in parsed_manifest.parsed_records
        ]
        batch = build_import_assessment_batch(parsed_manifest, assessments)
        d1 = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        d2 = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=2,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_reference="MSFT",
            quantity=Decimal("5"),
            unit_price=Decimal("320.00"),
            trade_currency=Currency.USD,
        )
        ma = build_import_draft_batch_manifest(batch, [d1, d2])
        mb = build_import_draft_batch_manifest(batch, [d2, d1])  # shuffled
        assert ma.draft_manifest_sha256 == mb.draft_manifest_sha256

    def test_canonical_csv_no_ledger_object_created(self):
        """No PortfolioTransaction is created during manifest construction."""
        from backend.engine.private.portfolio import PortfolioTransaction
        manifest = self._build_full_manifest()
        # Confirm drafts are ImportTransactionDraft, not PortfolioTransaction
        for d in manifest.drafts:
            assert isinstance(d, ImportTransactionDraft)
            assert not isinstance(d, PortfolioTransaction)

    def test_canonical_csv_no_parsed_text_inference(self):
        """Phase 13I does not inspect or re-interpret parsed field text."""
        manifest = self._build_full_manifest()
        # Verify parsed fields exist on assessments but manifest doesn't derive new economics
        for d in manifest.drafts:
            # instrument_reference was explicitly supplied by the test, not inferred
            assert d.instrument_reference in ("AAPL", "MSFT")
            # Manifest drafts list exactly what was explicitly built
            assert d.transaction_type == TransactionType.BUY


# ─────────────────────────────────────────────────────────────────────────────
# 12. Zero-READY Batch (from spec section 16)
# ─────────────────────────────────────────────────────────────────────────────

class TestZeroReadyBatch:
    """Zero-READY batch produces deterministic empty manifest."""

    def test_zero_ready_batch_deterministic_sha(self):
        """Zero-READY batch: drafts == (), SHA is deterministic."""
        batch = _make_test_assessment_batch([
            ImportAssessmentStatus.UNRESOLVED,
            ImportAssessmentStatus.REJECTED,
        ])
        m1 = build_import_draft_batch_manifest(batch, [])
        m2 = build_import_draft_batch_manifest(batch, [])
        assert m1.draft_manifest_sha256 == m2.draft_manifest_sha256
        assert m1.drafts == ()

    def test_empty_assessment_batch_deterministic_sha(self):
        """Empty assessment batch: drafts == (), SHA is deterministic."""
        batch = _make_empty_assessment_batch()
        m1 = build_import_draft_batch_manifest(batch, [])
        m2 = build_import_draft_batch_manifest(batch, [])
        assert m1.draft_manifest_sha256 == m2.draft_manifest_sha256


# ─────────────────────────────────────────────────────────────────────────────
# 13. Final Red-Team Scenarios
# ─────────────────────────────────────────────────────────────────────────────

class TestFinalRedTeam:
    """Attempts to break READY-coverage, binding, and integrity guarantees."""

    def test_ready_record_omitted_rejected(self):
        """READY record omitted from drafts fails closed."""
        batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportDraftBatchError, match="missing drafts"):
            build_import_draft_batch_manifest(batch, [d1])

    def test_ready_record_duplicated_rejected(self):
        """Duplicate READY record draft fails closed."""
        batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(batch, 1)
        d1_dup = _make_buy_draft(batch, 1, qty="20")  # same ordinal
        with pytest.raises(PortfolioImportDraftBatchError, match="Duplicate"):
            build_import_draft_batch_manifest(batch, [d1, d1_dup])

    def test_foreign_assessment_batch_draft_rejected(self):
        """Draft from completely different batch fails closed."""
        batch_target = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        batch_foreign = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        foreign_draft = _make_buy_draft(batch_foreign, 1)
        with pytest.raises(PortfolioImportDraftBatchError, match="different assessment batch"):
            build_import_draft_batch_manifest(batch_target, [foreign_draft])

    def test_shuffled_input_does_not_change_identity(self):
        """Shuffled draft input produces identical identity (input order invariance)."""
        batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(batch, 1)
        d2 = _make_buy_draft(batch, 2, instrument="MSFT")
        d3 = _make_buy_draft(batch, 3, instrument="TSLA")
        m1 = build_import_draft_batch_manifest(batch, [d1, d2, d3])
        m2 = build_import_draft_batch_manifest(batch, [d3, d2, d1])
        assert m1.draft_manifest_identity == m2.draft_manifest_identity

    def test_valid_fake_digest_rejected(self):
        """A plausible 64-char hex digest that doesn't match computed value is rejected."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        fake = "b" * 64
        with pytest.raises(PortfolioImportDraftBatchError):
            ImportDraftBatchManifest(
                assessment_batch=batch,
                drafts=(draft,),
                draft_manifest_sha256=fake,
            )

    def test_one_to_many_drafts_slip_through_rejected(self):
        """Attempting to supply two drafts for the same READY ordinal fails."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        d1a = _make_buy_draft(batch, 1, qty="10")
        d1b = _make_buy_draft(batch, 1, qty="20")
        with pytest.raises(PortfolioImportDraftBatchError, match="Duplicate"):
            build_import_draft_batch_manifest(batch, [d1a, d1b])

    def test_instrument_reference_preserved_verbatim_not_resolved(self):
        """instrument_reference is preserved verbatim without UUID resolution."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1, instrument="TICKER:XYZ/SPECIAL")
        manifest = build_import_draft_batch_manifest(batch, [draft])
        assert manifest.drafts[0].instrument_reference == "TICKER:XYZ/SPECIAL"
