"""
backend/tests/test_portfolio_import_instrument_resolution.py
============================================================
Tests for Phase 13J: Immutable PIT-Safe Instrument Resolution Outcome & Complete Draft-Coverage Manifest.

Zero network calls (pytest-socket enforced).
Pure in-memory domain evaluation using real Phase 13A-13I builders and models.
"""

from __future__ import annotations

import dataclasses
from dataclasses import fields as dataclass_fields, FrozenInstanceError
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import inspect
import json
from typing import List
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
from backend.engine.private.portfolio.import_batch import build_import_batch_manifest
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
    ImportInstrumentResolutionDiagnostic,
    ImportInstrumentResolutionStatus,
    PortfolioImportInstrumentResolutionError,
    build_import_instrument_resolution,
    build_import_instrument_resolution_batch,
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


def _make_empty_draft_batch() -> ImportDraftBatchManifest:
    """Builds a verified ImportDraftBatchManifest with zero drafts."""
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
    raw_manifest = build_import_batch_manifest(file_provenance=file_prov, records=[])
    parsed_manifest = build_parsed_import_batch_manifest(raw_manifest=raw_manifest, parser_revision=1, parsed_records=[])
    ass_batch = build_import_assessment_batch(parsed_manifest, [])
    return build_import_draft_batch_manifest(ass_batch, [])


def _make_buy_draft(
    assessment_batch: ImportAssessmentBatch,
    record_ordinal: int,
    instrument: str = "AAPL",
    qty: str = "10",
    price: str = "150.00",
    eff_date: date = date(2026, 8, 28),
) -> ImportTransactionDraft:
    return build_import_transaction_draft(
        assessment_batch=assessment_batch,
        record_ordinal=record_ordinal,
        transaction_type=TransactionType.BUY,
        effective_date=eff_date,
        instrument_reference=instrument,
        quantity=Decimal(qty),
        unit_price=Decimal(price),
        trade_currency=Currency.USD,
    )


def _make_sell_draft(
    assessment_batch: ImportAssessmentBatch,
    record_ordinal: int,
    instrument: str = "MSFT",
    qty: str = "5",
    price: str = "300.00",
    eff_date: date = date(2026, 8, 28),
) -> ImportTransactionDraft:
    return build_import_transaction_draft(
        assessment_batch=assessment_batch,
        record_ordinal=record_ordinal,
        transaction_type=TransactionType.SELL,
        effective_date=eff_date,
        instrument_reference=instrument,
        quantity=Decimal(qty),
        unit_price=Decimal(price),
        trade_currency=Currency.USD,
    )


def _make_cash_deposit_draft(
    assessment_batch: ImportAssessmentBatch,
    record_ordinal: int,
    amount: str = "500.00",
    eff_date: date = date(2026, 8, 28),
) -> ImportTransactionDraft:
    return build_import_transaction_draft(
        assessment_batch=assessment_batch,
        record_ordinal=record_ordinal,
        transaction_type=TransactionType.CASH_DEPOSIT,
        effective_date=eff_date,
        cash_amount=Decimal(amount),
        cash_currency=Currency.USD,
    )


def _make_cash_withdrawal_draft(
    assessment_batch: ImportAssessmentBatch,
    record_ordinal: int,
    amount: str = "200.00",
    eff_date: date = date(2026, 8, 28),
) -> ImportTransactionDraft:
    return build_import_transaction_draft(
        assessment_batch=assessment_batch,
        record_ordinal=record_ordinal,
        transaction_type=TransactionType.CASH_WITHDRAWAL,
        effective_date=eff_date,
        cash_amount=Decimal(amount),
        cash_currency=Currency.USD,
    )


def _make_dividend_draft(
    assessment_batch: ImportAssessmentBatch,
    record_ordinal: int,
    amount: str = "50.00",
    instrument: str | None = "AAPL",
    eff_date: date = date(2026, 8, 28),
) -> ImportTransactionDraft:
    return build_import_transaction_draft(
        assessment_batch=assessment_batch,
        record_ordinal=record_ordinal,
        transaction_type=TransactionType.DIVIDEND,
        effective_date=eff_date,
        instrument_reference=instrument,
        cash_amount=Decimal(amount),
        cash_currency=Currency.USD,
    )


def _make_fx_draft(
    assessment_batch: ImportAssessmentBatch,
    record_ordinal: int,
    from_amt: str = "100.00",
    to_amt: str = "3200.00",
    eff_date: date = date(2026, 8, 28),
) -> ImportTransactionDraft:
    return build_import_transaction_draft(
        assessment_batch=assessment_batch,
        record_ordinal=record_ordinal,
        transaction_type=TransactionType.FX_CONVERSION,
        effective_date=eff_date,
        from_currency=Currency.USD,
        from_amount=Decimal(from_amt),
        to_currency=Currency.TRY,
        to_amount=Decimal(to_amt),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Diagnostic Matrix (A-I)
# ─────────────────────────────────────────────────────────────────────────────

class TestDiagnosticMatrix:
    """A-I: Diagnostic grammar and immutability."""

    def test_A_valid_diagnostic_accepted(self):
        """A: Valid diagnostic code and message accepted."""
        diag = ImportInstrumentResolutionDiagnostic(
            code="instrument_not_found",
            message="No canonical instrument found for AAPL",
        )
        assert diag.code == "instrument_not_found"
        assert diag.message == "No canonical instrument found for AAPL"

    def test_B_uppercase_code_rejected(self):
        """B: Uppercase diagnostic code fails closed."""
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="Diagnostic code must match"):
            ImportInstrumentResolutionDiagnostic(code="InstrumentNotFound", message="msg")

    def test_C_hyphen_code_rejected(self):
        """C: Hyphenated diagnostic code fails closed."""
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="Diagnostic code must match"):
            ImportInstrumentResolutionDiagnostic(code="instrument-not-found", message="msg")

    def test_D_newline_suffixed_code_rejected(self):
        """D: Newline-suffixed diagnostic code fails closed."""
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="Diagnostic code must match"):
            ImportInstrumentResolutionDiagnostic(code="instrument_not_found\n", message="msg")

    def test_E_unicode_code_rejected(self):
        """E: Unicode non-ASCII letters in code fail closed."""
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="Diagnostic code must match"):
            ImportInstrumentResolutionDiagnostic(code="enstrüman_yok", message="msg")

    def test_F_empty_message_rejected(self):
        """F: Empty message fails closed."""
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="length must be between 1 and 2048"):
            ImportInstrumentResolutionDiagnostic(code="valid_code", message="")

    def test_G_whitespace_only_message_rejected(self):
        """G: Whitespace-only message fails closed."""
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="whitespace-only"):
            ImportInstrumentResolutionDiagnostic(code="valid_code", message="   \t\n")

    def test_H_message_exceeding_2048_rejected(self):
        """H: Message exceeding 2048 characters fails closed."""
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="length must be between 1 and 2048"):
            ImportInstrumentResolutionDiagnostic(code="valid_code", message="x" * 2049)

    def test_I_frozen_mutation_rejected(self):
        """I: Direct mutation of frozen diagnostic raises error."""
        diag = ImportInstrumentResolutionDiagnostic(code="valid_code", message="msg")
        with pytest.raises((FrozenInstanceError, dataclasses.FrozenInstanceError, TypeError, AttributeError)):
            diag.code = "other"  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 2. Resolver Metadata Matrix (J-Q)
# ─────────────────────────────────────────────────────────────────────────────

class TestResolverMetadataMatrix:
    """J-Q: Resolver key/revision pairing and lexical contract."""

    def test_J_valid_resolver_key_revision_accepted(self):
        """J: Valid resolver key and revision >= 1 accepted."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        res = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_identity",
            resolver_revision=1,
            instrument_id=uuid4(),
        )
        assert res.resolver_key == "sentinax_identity"
        assert res.resolver_revision == 1

    def test_K_invalid_uppercase_resolver_key_rejected(self):
        """K: Uppercase resolver key outside grammar fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="resolver_key must match"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="SentinaxIdentity",
                resolver_revision=1,
                instrument_id=uuid4(),
            )

    def test_L_whitespace_resolver_key_rejected(self):
        """L: Whitespace in resolver key fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="resolver_key must match"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax identity",
                resolver_revision=1,
                instrument_id=uuid4(),
            )

    def test_M_revision_bool_rejected(self):
        """M: Bool as revision fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="resolver_revision must be an int"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=True,  # type: ignore
                instrument_id=uuid4(),
            )

    def test_N_revision_zero_rejected(self):
        """N: Revision 0 fails closed (must be >= 1)."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="resolver_revision must be >= 1"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=0,
                instrument_id=uuid4(),
            )

    def test_O_key_without_revision_rejected(self):
        """O: Resolver key without revision fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="Resolver metadata must be all-or-none"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=None,
                instrument_id=uuid4(),
            )

    def test_P_revision_without_key_rejected(self):
        """P: Revision without key fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="Resolver metadata must be all-or-none"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key=None,
                resolver_revision=1,
                instrument_id=uuid4(),
            )

    def test_Q_not_required_with_resolver_metadata_rejected(self):
        """Q: NOT_REQUIRED status with resolver metadata fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="resolver_key and resolver_revision must be None for NOT_REQUIRED"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 3. PIT Date Matrix (R-V)
# ─────────────────────────────────────────────────────────────────────────────

class TestPITDateMatrix:
    """R-V: Point-In-Time date invariants."""

    def test_R_exact_draft_effective_date_accepted(self):
        """R: Exact draft effective_date is accepted."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1, eff_date=date(2026, 5, 15))
        res = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=date(2026, 5, 15),
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=uuid4(),
        )
        assert res.resolution_as_of_date == date(2026, 5, 15)

    def test_S_different_date_rejected(self):
        """S: Date differing from draft.effective_date fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1, eff_date=date(2026, 5, 15))
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="must equal draft.effective_date"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=date(2026, 5, 16),
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
            )

    def test_T_datetime_as_date_rejected(self):
        """T: datetime instance passed as resolution date fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1, eff_date=date(2026, 5, 15))
        dt = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="strictly a datetime.date instance"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=dt,  # type: ignore
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
            )

    def test_U_date_today_fallback_does_not_exist(self):
        """U: Builder requires resolution_as_of_date as positional argument with no default."""
        sig = inspect.signature(build_import_instrument_resolution)
        param = sig.parameters["resolution_as_of_date"]
        assert param.default is inspect.Parameter.empty

    def test_V_repeated_construction_deterministic(self):
        """V: Construction is purely deterministic across runs."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        inst_id = uuid4()
        r1 = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=inst_id,
        )
        r2 = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=inst_id,
        )
        assert r1.resolution_sha256 == r2.resolution_sha256


# ─────────────────────────────────────────────────────────────────────────────
# 4. NOT_REQUIRED Matrix (W-AE)
# ─────────────────────────────────────────────────────────────────────────────

class TestNotRequiredMatrix:
    """W-AE: NOT_REQUIRED status eligibility and field contract."""

    def test_W_cash_deposit_not_required_accepted(self):
        """W: CASH_DEPOSIT with NOT_REQUIRED is accepted."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(batch, 1)
        res = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
            resolution_as_of_date=draft.effective_date,
        )
        assert res.status == ImportInstrumentResolutionStatus.NOT_REQUIRED

    def test_X_cash_withdrawal_not_required_accepted(self):
        """X: CASH_WITHDRAWAL with NOT_REQUIRED is accepted."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_withdrawal_draft(batch, 1)
        res = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
            resolution_as_of_date=draft.effective_date,
        )
        assert res.status == ImportInstrumentResolutionStatus.NOT_REQUIRED

    def test_Y_fx_conversion_not_required_accepted(self):
        """Y: FX_CONVERSION with NOT_REQUIRED is accepted."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_fx_draft(batch, 1)
        res = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
            resolution_as_of_date=draft.effective_date,
        )
        assert res.status == ImportInstrumentResolutionStatus.NOT_REQUIRED

    def test_Z_dividend_without_reference_not_required_accepted(self):
        """Z: DIVIDEND without instrument_reference with NOT_REQUIRED is accepted."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_dividend_draft(batch, 1, instrument=None)
        res = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
            resolution_as_of_date=draft.effective_date,
        )
        assert res.status == ImportInstrumentResolutionStatus.NOT_REQUIRED

    def test_AA_buy_not_required_rejected(self):
        """AA: BUY draft with NOT_REQUIRED status fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="NOT_REQUIRED status is invalid"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
                resolution_as_of_date=draft.effective_date,
            )

    def test_AB_sell_not_required_rejected(self):
        """AB: SELL draft with NOT_REQUIRED status fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_sell_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="NOT_REQUIRED status is invalid"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
                resolution_as_of_date=draft.effective_date,
            )

    def test_AC_event_with_instrument_ref_not_required_rejected(self):
        """AC: DIVIDEND with instrument_reference cannot be NOT_REQUIRED."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_dividend_draft(batch, 1, instrument="AAPL")
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="NOT_REQUIRED status is invalid"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
                resolution_as_of_date=draft.effective_date,
            )

    def test_AD_not_required_carrying_uuid_rejected(self):
        """AD: NOT_REQUIRED carrying instrument_id fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="instrument_id must be None for NOT_REQUIRED"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
                resolution_as_of_date=draft.effective_date,
                instrument_id=uuid4(),
            )

    def test_AE_not_required_carrying_diagnostics_rejected(self):
        """AE: NOT_REQUIRED carrying diagnostics fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(batch, 1)
        diag = ImportInstrumentResolutionDiagnostic(code="diag_code", message="msg")
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="diagnostics must be empty for NOT_REQUIRED"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
                resolution_as_of_date=draft.effective_date,
                diagnostics=[diag],
            )


# ─────────────────────────────────────────────────────────────────────────────
# 5. RESOLVED Matrix (AF-AM)
# ─────────────────────────────────────────────────────────────────────────────

class TestResolvedMatrix:
    """AF-AM: RESOLVED status eligibility and constraints."""

    def test_AF_buy_reference_resolved_accepted(self):
        """AF: BUY + reference + one UUID RESOLVED is accepted."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        inst_id = uuid4()
        res = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=inst_id,
        )
        assert res.instrument_id == inst_id
        assert res.status == ImportInstrumentResolutionStatus.RESOLVED

    def test_AG_sell_resolved_accepted(self):
        """AG: SELL + reference + one UUID RESOLVED is accepted."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_sell_draft(batch, 1)
        inst_id = uuid4()
        res = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=inst_id,
        )
        assert res.instrument_id == inst_id

    def test_AH_dividend_with_reference_resolved_accepted(self):
        """AH: DIVIDEND with reference RESOLVED is accepted."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_dividend_draft(batch, 1, instrument="AAPL")
        inst_id = uuid4()
        res = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=inst_id,
        )
        assert res.instrument_id == inst_id

    def test_AI_resolved_without_reference_rejected(self):
        """AI: CASH_DEPOSIT (no instrument_reference) marked RESOLVED fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="RESOLVED status requires draft to have an instrument_reference"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
            )

    def test_AJ_resolved_without_resolver_metadata_rejected(self):
        """AJ: RESOLVED without resolver_key fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="RESOLVED status requires resolver_key and resolver_revision"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=draft.effective_date,
                instrument_id=uuid4(),
            )

    def test_AK_resolved_without_instrument_id_rejected(self):
        """AK: RESOLVED with instrument_id=None fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="RESOLVED status requires an authoritative UUID instrument_id"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=None,
            )

    def test_AL_resolved_with_candidates_rejected(self):
        """AL: RESOLVED carrying candidate UUIDs fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="RESOLVED status must not carry candidate_instrument_ids"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
                candidate_instrument_ids=[uuid4()],
            )

    def test_AM_resolved_with_diagnostics_rejected(self):
        """AM: RESOLVED carrying diagnostics fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        diag = ImportInstrumentResolutionDiagnostic(code="some_diag", message="msg")
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="RESOLVED status must not carry diagnostics"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
                diagnostics=[diag],
            )


# ─────────────────────────────────────────────────────────────────────────────
# 6. UNRESOLVED Matrix (AN-AR)
# ─────────────────────────────────────────────────────────────────────────────

class TestUnresolvedMatrix:
    """AN-AR: UNRESOLVED status eligibility and constraints."""

    def test_AN_reference_resolver_diagnostic_accepted(self):
        """AN: UNRESOLVED with reference, resolver metadata, and diagnostic is accepted."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        diag = ImportInstrumentResolutionDiagnostic(code="instrument_not_found", message="Unknown ticker")
        res = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.UNRESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            diagnostics=[diag],
        )
        assert res.status == ImportInstrumentResolutionStatus.UNRESOLVED
        assert len(res.diagnostics) == 1

    def test_AO_unresolved_without_reference_rejected(self):
        """AO: CASH_DEPOSIT (no reference) marked UNRESOLVED fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(batch, 1)
        diag = ImportInstrumentResolutionDiagnostic(code="diag_code", message="msg")
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="UNRESOLVED status requires draft to have an instrument_reference"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.UNRESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                diagnostics=[diag],
            )

    def test_AP_unresolved_without_diagnostic_rejected(self):
        """AP: UNRESOLVED with zero diagnostics fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="UNRESOLVED status requires at least one diagnostic"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.UNRESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                diagnostics=[],
            )

    def test_AQ_unresolved_carrying_instrument_id_rejected(self):
        """AQ: UNRESOLVED carrying final instrument_id fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        diag = ImportInstrumentResolutionDiagnostic(code="instrument_not_found", message="msg")
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="UNRESOLVED status must not carry an instrument_id"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.UNRESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
                diagnostics=[diag],
            )

    def test_AR_unresolved_carrying_candidates_rejected(self):
        """AR: UNRESOLVED carrying candidates fails closed (must use AMBIGUOUS)."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        diag = ImportInstrumentResolutionDiagnostic(code="instrument_not_found", message="msg")
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="UNRESOLVED status must not carry candidate_instrument_ids"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.UNRESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                candidate_instrument_ids=[uuid4(), uuid4()],
                diagnostics=[diag],
            )


# ─────────────────────────────────────────────────────────────────────────────
# 7. AMBIGUOUS Matrix (AS-BA)
# ─────────────────────────────────────────────────────────────────────────────

class TestAmbiguousMatrix:
    """AS-BA: AMBIGUOUS status eligibility and candidate handling."""

    def test_AS_two_candidate_uuids_accepted(self):
        """AS: AMBIGUOUS with two candidates accepted."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        u1, u2 = uuid4(), uuid4()
        diag = ImportInstrumentResolutionDiagnostic(code="ambiguous_reference", message="Multiple matches")
        res = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.AMBIGUOUS,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            candidate_instrument_ids=[u1, u2],
            diagnostics=[diag],
        )
        assert res.status == ImportInstrumentResolutionStatus.AMBIGUOUS
        assert len(res.candidate_instrument_ids) == 2

    def test_AT_three_candidate_uuids_accepted(self):
        """AT: AMBIGUOUS with three candidates accepted."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        u1, u2, u3 = uuid4(), uuid4(), uuid4()
        diag = ImportInstrumentResolutionDiagnostic(code="ambiguous_reference", message="3 matches")
        res = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.AMBIGUOUS,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            candidate_instrument_ids=[u1, u2, u3],
            diagnostics=[diag],
        )
        assert len(res.candidate_instrument_ids) == 3

    def test_AU_zero_candidates_rejected(self):
        """AU: AMBIGUOUS with zero candidates fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        diag = ImportInstrumentResolutionDiagnostic(code="ambiguous_reference", message="None")
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="AMBIGUOUS status requires at least two candidate_instrument_ids"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.AMBIGUOUS,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                candidate_instrument_ids=[],
                diagnostics=[diag],
            )

    def test_AV_one_candidate_rejected(self):
        """AV: AMBIGUOUS with exactly 1 candidate fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        diag = ImportInstrumentResolutionDiagnostic(code="ambiguous_reference", message="1 match")
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="AMBIGUOUS status requires at least two candidate_instrument_ids"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.AMBIGUOUS,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                candidate_instrument_ids=[uuid4()],
                diagnostics=[diag],
            )

    def test_AW_duplicate_candidates_rejected(self):
        """AW: Duplicate candidate UUID fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        u = uuid4()
        diag = ImportInstrumentResolutionDiagnostic(code="ambiguous_reference", message="dup")
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="Duplicate candidate UUID"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.AMBIGUOUS,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                candidate_instrument_ids=[u, u],
                diagnostics=[diag],
            )

    def test_AX_unsorted_direct_candidate_tuple_rejected(self):
        """AX: Direct constructor with unsorted candidate tuple fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        u1 = UUID("00000000-0000-0000-0000-000000000001")
        u2 = UUID("00000000-0000-0000-0000-000000000002")
        diag = ImportInstrumentResolutionDiagnostic(code="ambiguous_reference", message="msg")
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="not canonically sorted"):
            ImportInstrumentResolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.AMBIGUOUS,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                candidate_instrument_ids=(u2, u1),  # unsorted
                diagnostics=(diag,),
                resolution_sha256="a" * 64,
            )

    def test_AY_builder_shuffled_candidates_canonicalized(self):
        """AY: Builder canonicalizes shuffled candidate inputs."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        u1 = UUID("00000000-0000-0000-0000-000000000001")
        u2 = UUID("00000000-0000-0000-0000-000000000002")
        diag = ImportInstrumentResolutionDiagnostic(code="ambiguous_reference", message="msg")
        r1 = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.AMBIGUOUS,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            candidate_instrument_ids=[u1, u2],
            diagnostics=[diag],
        )
        r2 = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.AMBIGUOUS,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            candidate_instrument_ids=[u2, u1],
            diagnostics=[diag],
        )
        assert r1.candidate_instrument_ids == (u1, u2)
        assert r2.candidate_instrument_ids == (u1, u2)
        assert r1.resolution_sha256 == r2.resolution_sha256

    def test_AZ_ambiguous_carrying_final_instrument_id_rejected(self):
        """AZ: AMBIGUOUS carrying final instrument_id fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        diag = ImportInstrumentResolutionDiagnostic(code="ambiguous_reference", message="msg")
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="AMBIGUOUS status must not carry a final instrument_id"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.AMBIGUOUS,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
                candidate_instrument_ids=[uuid4(), uuid4()],
                diagnostics=[diag],
            )

    def test_BA_ambiguous_without_diagnostic_rejected(self):
        """BA: AMBIGUOUS without diagnostics fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="AMBIGUOUS status requires at least one diagnostic"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.AMBIGUOUS,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                candidate_instrument_ids=[uuid4(), uuid4()],
                diagnostics=[],
            )


# ─────────────────────────────────────────────────────────────────────────────
# 8. Diagnostic Collection Matrix (BB-BG)
# ─────────────────────────────────────────────────────────────────────────────

class TestDiagnosticCollectionMatrix:
    """BB-BG: Diagnostic collection type and ordering."""

    def test_BB_builder_list_accepted(self):
        """BB: Builder accepts list of diagnostics."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        d = ImportInstrumentResolutionDiagnostic(code="code_a", message="msg")
        res = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.UNRESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            diagnostics=[d],
        )
        assert res.diagnostics == (d,)

    def test_BC_builder_tuple_accepted(self):
        """BC: Builder accepts tuple of diagnostics."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        d = ImportInstrumentResolutionDiagnostic(code="code_a", message="msg")
        res = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.UNRESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            diagnostics=(d,),
        )
        assert res.diagnostics == (d,)

    def test_BD_generator_rejected(self):
        """BD: Generator diagnostics fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        d = ImportInstrumentResolutionDiagnostic(code="code_a", message="msg")
        gen = (x for x in [d])
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="diagnostics must be a list or tuple"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.UNRESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                diagnostics=gen,  # type: ignore
            )

    def test_BE_duplicate_diagnostic_code_rejected(self):
        """BE: Duplicate diagnostic code fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        d1 = ImportInstrumentResolutionDiagnostic(code="code_dup", message="msg1")
        d2 = ImportInstrumentResolutionDiagnostic(code="code_dup", message="msg2")
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="Duplicate diagnostic code detected"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.UNRESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                diagnostics=[d1, d2],
            )

    def test_BF_shuffled_builder_diagnostics_canonicalized(self):
        """BF: Shuffled diagnostics are canonicalized by (code, message)."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        d1 = ImportInstrumentResolutionDiagnostic(code="code_a", message="msg")
        d2 = ImportInstrumentResolutionDiagnostic(code="code_b", message="msg")
        r1 = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.UNRESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            diagnostics=[d1, d2],
        )
        r2 = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.UNRESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            diagnostics=[d2, d1],
        )
        assert r1.diagnostics == (d1, d2)
        assert r2.diagnostics == (d1, d2)
        assert r1.resolution_sha256 == r2.resolution_sha256

    def test_BG_unsorted_direct_diagnostic_tuple_rejected(self):
        """BG: Unsorted diagnostics in direct constructor fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        d1 = ImportInstrumentResolutionDiagnostic(code="code_a", message="msg")
        d2 = ImportInstrumentResolutionDiagnostic(code="code_b", message="msg")
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="not canonically sorted"):
            ImportInstrumentResolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.UNRESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                diagnostics=(d2, d1),  # unsorted
                resolution_sha256="a" * 64,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 9. Resolution Hash Matrix (BH-BR)
# ─────────────────────────────────────────────────────────────────────────────

class TestResolutionHashMatrix:
    """BH-BR: Resolution digest computation and sensitivity."""

    def test_BH_independent_canonical_json_hash_matches(self):
        """BH: Independently computed JSON SHA matches model digest."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        inst_id = uuid4()
        res = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=inst_id,
        )
        preimage = [
            draft.draft_sha256,
            draft.record_ordinal,
            draft.instrument_reference,
            draft.effective_date.isoformat(),
            ImportInstrumentResolutionStatus.RESOLVED.value,
            "sentinax_id",
            1,
            str(inst_id),
            [],
            [],
        ]
        raw_json = json.dumps(preimage, ensure_ascii=True, separators=(",", ":"))
        expected_sha = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
        assert res.resolution_sha256 == expected_sha

    def test_BI_repeated_identical_construction_deterministic(self):
        """BI: Repeated construction produces identical hash."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(batch, 1)
        r1 = build_import_instrument_resolution(draft, ImportInstrumentResolutionStatus.NOT_REQUIRED, draft.effective_date)
        r2 = build_import_instrument_resolution(draft, ImportInstrumentResolutionStatus.NOT_REQUIRED, draft.effective_date)
        assert r1.resolution_sha256 == r2.resolution_sha256

    def test_BJ_status_change_changes_hash(self):
        """BJ: Different resolution status produces different hash."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        diag = ImportInstrumentResolutionDiagnostic(code="diag_code", message="msg")
        r_resolved = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=uuid4(),
        )
        r_unresolved = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.UNRESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            diagnostics=[diag],
        )
        assert r_resolved.resolution_sha256 != r_unresolved.resolution_sha256

    def test_BK_resolver_revision_change_changes_hash(self):
        """BK: Resolver revision change produces different hash."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        inst_id = uuid4()
        r1 = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=inst_id,
        )
        r2 = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=2,
            instrument_id=inst_id,
        )
        assert r1.resolution_sha256 != r2.resolution_sha256

    def test_BL_instrument_uuid_change_changes_hash(self):
        """BL: Instrument UUID change produces different hash."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        r1 = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=uuid4(),
        )
        r2 = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=uuid4(),
        )
        assert r1.resolution_sha256 != r2.resolution_sha256

    def test_BM_candidate_order_through_builder_does_not_change_hash(self):
        """BM: Shuffled candidates through builder produce identical hash."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        u1, u2 = uuid4(), uuid4()
        diag = ImportInstrumentResolutionDiagnostic(code="diag_code", message="msg")
        r1 = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.AMBIGUOUS,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            candidate_instrument_ids=[u1, u2],
            diagnostics=[diag],
        )
        r2 = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.AMBIGUOUS,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            candidate_instrument_ids=[u2, u1],
            diagnostics=[diag],
        )
        assert r1.resolution_sha256 == r2.resolution_sha256

    def test_BN_diagnostic_change_changes_hash(self):
        """BN: Diagnostic code/message change produces different hash."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        d1 = ImportInstrumentResolutionDiagnostic(code="diag_a", message="msg1")
        d2 = ImportInstrumentResolutionDiagnostic(code="diag_b", message="msg2")
        r1 = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.UNRESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            diagnostics=[d1],
        )
        r2 = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.UNRESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            diagnostics=[d2],
        )
        assert r1.resolution_sha256 != r2.resolution_sha256

    def test_BO_pit_date_cannot_vary_from_economic_date(self):
        """BO: Passing a varied PIT date fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1, eff_date=date(2026, 8, 28))
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="must equal draft.effective_date"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=date(2026, 8, 29),
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
            )

    def test_BP_fake_valid_format_hash_rejected(self):
        """BP: 64-hex fake hash rejected in direct constructor."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="digest mismatch"):
            ImportInstrumentResolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
                resolution_as_of_date=draft.effective_date,
                resolution_sha256="f" * 64,
            )

    def test_BQ_uppercase_hash_rejected(self):
        """BQ: Uppercase hash rejected in direct constructor."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="lowercase hex"):
            ImportInstrumentResolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
                resolution_as_of_date=draft.effective_date,
                resolution_sha256="A" * 64,
            )

    def test_BR_hash_with_newline_rejected(self):
        """BR: Hash with trailing newline rejected."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="lowercase hex"):
            ImportInstrumentResolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
                resolution_as_of_date=draft.effective_date,
                resolution_sha256="a" * 64 + "\n",
            )


# ─────────────────────────────────────────────────────────────────────────────
# 10. Record Identity Matrix (BS-BV)
# ─────────────────────────────────────────────────────────────────────────────

class TestRecordIdentityMatrix:
    """BS-BV: Resolution record staging identity properties."""

    def test_BS_resolution_identity_deterministic(self):
        """BS: resolution_identity is deterministic across constructions."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(batch, 1)
        r1 = build_import_instrument_resolution(draft, ImportInstrumentResolutionStatus.NOT_REQUIRED, draft.effective_date)
        r2 = build_import_instrument_resolution(draft, ImportInstrumentResolutionStatus.NOT_REQUIRED, draft.effective_date)
        assert r1.resolution_identity == r2.resolution_identity

    def test_BT_resolution_identity_extends_exact_draft_identity(self):
        """BT: resolution_identity is (*draft.draft_identity, resolution_sha256)."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(batch, 1)
        res = build_import_instrument_resolution(draft, ImportInstrumentResolutionStatus.NOT_REQUIRED, draft.effective_date)
        assert res.resolution_identity == (*draft.draft_identity, res.resolution_sha256)

    def test_BU_no_random_uuid_generated(self):
        """BU: resolution_identity contains only cryptographic hashes and ordinal."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(batch, 1)
        res = build_import_instrument_resolution(draft, ImportInstrumentResolutionStatus.NOT_REQUIRED, draft.effective_date)
        # draft_identity is (assessment_manifest_sha256, record_ordinal, draft_sha256)
        ass_sha, ord_val, draft_sha, res_sha = res.resolution_identity
        assert isinstance(ass_sha, str) and len(ass_sha) == 64
        assert ord_val == 1
        assert isinstance(draft_sha, str) and len(draft_sha) == 64
        assert isinstance(res_sha, str) and len(res_sha) == 64

    def test_BV_no_object_memory_address_affects_identity(self):
        """BV: Object memory address does not affect staging identity."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(batch, 1)
        r1 = build_import_instrument_resolution(draft, ImportInstrumentResolutionStatus.NOT_REQUIRED, draft.effective_date)
        r2 = build_import_instrument_resolution(draft, ImportInstrumentResolutionStatus.NOT_REQUIRED, draft.effective_date)
        assert id(r1) != id(r2)
        assert r1.resolution_identity == r2.resolution_identity


# ─────────────────────────────────────────────────────────────────────────────
# 11. Batch Coverage Matrix (BW-CD)
# ─────────────────────────────────────────────────────────────────────────────

class TestBatchCoverageMatrix:
    """BW-CD: Batch-level coverage and draft correspondence."""

    def test_BW_empty_draft_batch_empty_resolutions_valid(self):
        """BW: Empty draft batch with empty resolutions is valid."""
        draft_manifest = _make_empty_draft_batch()
        batch = build_import_instrument_resolution_batch(draft_manifest, [])
        assert batch.resolution_count == 0
        assert batch.resolutions == ()
        assert batch.is_fully_resolved is True

    def test_BX_one_draft_one_outcome_valid(self):
        """BX: Single draft with corresponding single outcome is valid."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])

        res = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=uuid4(),
        )
        batch = build_import_instrument_resolution_batch(draft_manifest, [res])
        assert batch.resolution_count == 1
        assert batch.resolved_count == 1
        assert batch.is_fully_resolved is True

    def test_BY_multi_draft_complete_outcomes_valid(self):
        """BY: Multiple drafts with complete outcomes accepted."""
        ass_batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(ass_batch, 1)
        d2 = _make_cash_deposit_draft(ass_batch, 2)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        r1 = build_import_instrument_resolution(
            draft=d1,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=d1.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=uuid4(),
        )
        r2 = build_import_instrument_resolution(
            draft=d2,
            status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
            resolution_as_of_date=d2.effective_date,
        )
        batch = build_import_instrument_resolution_batch(draft_manifest, [r1, r2])
        assert batch.resolution_count == 2
        assert batch.resolved_count == 1
        assert batch.not_required_count == 1
        assert batch.is_fully_resolved is True

    def test_BZ_omitted_resolution_rejected(self):
        """BZ: Omission of one draft resolution fails closed."""
        ass_batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(ass_batch, 1)
        d2 = _make_cash_deposit_draft(ass_batch, 2)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        r1 = build_import_instrument_resolution(
            draft=d1,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=d1.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=uuid4(),
        )
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="Resolution count mismatch"):
            build_import_instrument_resolution_batch(draft_manifest, [r1])

    def test_CA_extra_resolution_rejected(self):
        """CA: Extra resolution outcome fails closed."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        d1 = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1])

        r1 = build_import_instrument_resolution(
            draft=d1,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=d1.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=uuid4(),
        )
        # Second resolution for ordinal 1
        r1_dup = build_import_instrument_resolution(
            draft=d1,
            status=ImportInstrumentResolutionStatus.UNRESOLVED,
            resolution_as_of_date=d1.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            diagnostics=[ImportInstrumentResolutionDiagnostic("code_a", "msg")],
        )
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="Duplicate resolution record_ordinal"):
            build_import_instrument_resolution_batch(draft_manifest, [r1, r1_dup])

    def test_CB_duplicate_draft_ordinal_resolution_rejected(self):
        """CB: Two resolutions for the same record ordinal fail closed."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        d1 = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1])

        r1 = build_import_instrument_resolution(
            draft=d1,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=d1.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=uuid4(),
        )
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="Duplicate resolution record_ordinal"):
            build_import_instrument_resolution_batch(draft_manifest, [r1, r1])

    def test_CC_foreign_semantic_draft_rejected(self):
        """CC: Resolution built against foreign draft fails closed."""
        ass_batch_1 = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        ass_batch_2 = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        d1 = _make_buy_draft(ass_batch_1, 1)
        d_foreign = _make_buy_draft(ass_batch_2, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch_1, [d1])

        r_foreign = build_import_instrument_resolution(
            draft=d_foreign,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=d_foreign.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=uuid4(),
        )
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="not semantically bound"):
            build_import_instrument_resolution_batch(draft_manifest, [r_foreign])

    def test_CD_same_ordinal_different_draft_economics_rejected(self):
        """CD: Resolution with different draft economics fails closed."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        d1 = _make_buy_draft(ass_batch, 1, qty="10")
        d1_alt = _make_buy_draft(ass_batch, 1, qty="20")
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1])

        r_alt = build_import_instrument_resolution(
            draft=d1_alt,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=d1_alt.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=uuid4(),
        )
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="not semantically bound"):
            build_import_instrument_resolution_batch(draft_manifest, [r_alt])


# ─────────────────────────────────────────────────────────────────────────────
# 12. Batch Ordering Matrix (CE-CI)
# ─────────────────────────────────────────────────────────────────────────────

class TestBatchOrderingMatrix:
    """CE-CI: Canonical batch ordering invariants."""

    def test_CE_shuffled_builder_input_canonicalized(self):
        """CE: Shuffled builder input is canonicalized by draft.record_ordinal."""
        ass_batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(ass_batch, 1)
        d2 = _make_cash_deposit_draft(ass_batch, 2)
        d3 = _make_sell_draft(ass_batch, 3)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2, d3])

        r1 = build_import_instrument_resolution(d1, ImportInstrumentResolutionStatus.RESOLVED, d1.effective_date, "sentinax_id", 1, uuid4())
        r2 = build_import_instrument_resolution(d2, ImportInstrumentResolutionStatus.NOT_REQUIRED, d2.effective_date)
        r3 = build_import_instrument_resolution(d3, ImportInstrumentResolutionStatus.RESOLVED, d3.effective_date, "sentinax_id", 1, uuid4())

        b1 = build_import_instrument_resolution_batch(draft_manifest, [r1, r2, r3])
        b2 = build_import_instrument_resolution_batch(draft_manifest, [r3, r1, r2])
        assert [r.draft.record_ordinal for r in b1.resolutions] == [1, 2, 3]
        assert [r.draft.record_ordinal for r in b2.resolutions] == [1, 2, 3]

    def test_CF_ordered_shuffled_same_tuple(self):
        """CF: Ordered and shuffled inputs produce identical resolution tuple."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY, ImportAssessmentStatus.READY])
        d1 = _make_buy_draft(ass_batch, 1)
        d2 = _make_cash_deposit_draft(ass_batch, 2)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        r1 = build_import_instrument_resolution(d1, ImportInstrumentResolutionStatus.RESOLVED, d1.effective_date, "sentinax_id", 1, uuid4())
        r2 = build_import_instrument_resolution(d2, ImportInstrumentResolutionStatus.NOT_REQUIRED, d2.effective_date)

        b1 = build_import_instrument_resolution_batch(draft_manifest, [r1, r2])
        b2 = build_import_instrument_resolution_batch(draft_manifest, [r2, r1])
        assert b1.resolutions == b2.resolutions

    def test_CG_ordered_shuffled_same_manifest_sha(self):
        """CG: Ordered and shuffled inputs produce identical manifest SHA."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY, ImportAssessmentStatus.READY])
        d1 = _make_buy_draft(ass_batch, 1)
        d2 = _make_cash_deposit_draft(ass_batch, 2)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        r1 = build_import_instrument_resolution(d1, ImportInstrumentResolutionStatus.RESOLVED, d1.effective_date, "sentinax_id", 1, uuid4())
        r2 = build_import_instrument_resolution(d2, ImportInstrumentResolutionStatus.NOT_REQUIRED, d2.effective_date)

        b1 = build_import_instrument_resolution_batch(draft_manifest, [r1, r2])
        b2 = build_import_instrument_resolution_batch(draft_manifest, [r2, r1])
        assert b1.resolution_manifest_sha256 == b2.resolution_manifest_sha256

    def test_CH_ordered_shuffled_same_manifest_identity(self):
        """CH: Ordered and shuffled inputs produce identical manifest identity."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY, ImportAssessmentStatus.READY])
        d1 = _make_buy_draft(ass_batch, 1)
        d2 = _make_cash_deposit_draft(ass_batch, 2)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        r1 = build_import_instrument_resolution(d1, ImportInstrumentResolutionStatus.RESOLVED, d1.effective_date, "sentinax_id", 1, uuid4())
        r2 = build_import_instrument_resolution(d2, ImportInstrumentResolutionStatus.NOT_REQUIRED, d2.effective_date)

        b1 = build_import_instrument_resolution_batch(draft_manifest, [r1, r2])
        b2 = build_import_instrument_resolution_batch(draft_manifest, [r2, r1])
        assert b1.resolution_manifest_identity == b2.resolution_manifest_identity

    def test_CI_unsorted_direct_tuple_rejected(self):
        """CI: Unsorted resolutions in direct constructor fail closed."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY, ImportAssessmentStatus.READY])
        d1 = _make_buy_draft(ass_batch, 1)
        d2 = _make_cash_deposit_draft(ass_batch, 2)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        r1 = build_import_instrument_resolution(d1, ImportInstrumentResolutionStatus.RESOLVED, d1.effective_date, "sentinax_id", 1, uuid4())
        r2 = build_import_instrument_resolution(d2, ImportInstrumentResolutionStatus.NOT_REQUIRED, d2.effective_date)

        with pytest.raises(PortfolioImportInstrumentResolutionError):
            ImportInstrumentResolutionBatch(
                draft_manifest=draft_manifest,
                resolutions=(r2, r1),  # unsorted
                resolution_manifest_sha256="a" * 64,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 13. Batch Hash Matrix (CJ-CQ)
# ─────────────────────────────────────────────────────────────────────────────

class TestBatchHashMatrix:
    """CJ-CQ: Resolution batch manifest digest computation and sensitivity."""

    def test_CJ_independent_manifest_hash_matches(self):
        """CJ: Independently computed manifest preimage JSON SHA matches."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])

        inst_id = uuid4()
        res = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=inst_id,
        )
        batch = build_import_instrument_resolution_batch(draft_manifest, [res])

        ass_b = draft_manifest.assessment_batch
        preimage = [
            str(ass_b.portfolio_id),
            str(ass_b.account_id),
            ass_b.source_key,
            ass_b.file_content_sha256,
            ass_b.raw_manifest_sha256,
            ass_b.parser_revision,
            ass_b.parsed_manifest_sha256,
            ass_b.assessment_manifest_sha256,
            draft_manifest.draft_manifest_sha256,
            [
                [
                    res.draft.record_ordinal,
                    res.draft.draft_sha256,
                    res.resolution_sha256,
                ]
            ],
        ]
        raw_json = json.dumps(preimage, ensure_ascii=True, separators=(",", ":"))
        expected_sha = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
        assert batch.resolution_manifest_sha256 == expected_sha

    def test_CK_repeated_build_deterministic(self):
        """CK: Repeated manifest build produces identical digest."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        res = build_import_instrument_resolution(draft, ImportInstrumentResolutionStatus.NOT_REQUIRED, draft.effective_date)

        b1 = build_import_instrument_resolution_batch(draft_manifest, [res])
        b2 = build_import_instrument_resolution_batch(draft_manifest, [res])
        assert b1.resolution_manifest_sha256 == b2.resolution_manifest_sha256

    def test_CL_status_change_changes_manifest_sha(self):
        """CL: Status change in one resolution changes manifest SHA."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])

        r_res = build_import_instrument_resolution(draft, ImportInstrumentResolutionStatus.RESOLVED, draft.effective_date, "sentinax_id", 1, uuid4())
        r_unres = build_import_instrument_resolution(
            draft,
            ImportInstrumentResolutionStatus.UNRESOLVED,
            draft.effective_date,
            "sentinax_id",
            1,
            diagnostics=[ImportInstrumentResolutionDiagnostic("not_found", "msg")],
        )

        b1 = build_import_instrument_resolution_batch(draft_manifest, [r_res])
        b2 = build_import_instrument_resolution_batch(draft_manifest, [r_unres])
        assert b1.resolution_manifest_sha256 != b2.resolution_manifest_sha256

    def test_CM_selected_instrument_uuid_change_changes_manifest_sha(self):
        """CM: Changing the selected instrument UUID changes manifest SHA."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])

        r1 = build_import_instrument_resolution(draft, ImportInstrumentResolutionStatus.RESOLVED, draft.effective_date, "sentinax_id", 1, uuid4())
        r2 = build_import_instrument_resolution(draft, ImportInstrumentResolutionStatus.RESOLVED, draft.effective_date, "sentinax_id", 1, uuid4())

        b1 = build_import_instrument_resolution_batch(draft_manifest, [r1])
        b2 = build_import_instrument_resolution_batch(draft_manifest, [r2])
        assert b1.resolution_manifest_sha256 != b2.resolution_manifest_sha256

    def test_CN_draft_manifest_change_changes_manifest_sha(self):
        """CN: Changing underlying draft manifest changes resolution manifest SHA."""
        ass_batch_1 = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        ass_batch_2 = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        d1 = _make_cash_deposit_draft(ass_batch_1, 1)
        d2 = _make_cash_deposit_draft(ass_batch_2, 1)
        m1 = build_import_draft_batch_manifest(ass_batch_1, [d1])
        m2 = build_import_draft_batch_manifest(ass_batch_2, [d2])

        r1 = build_import_instrument_resolution(d1, ImportInstrumentResolutionStatus.NOT_REQUIRED, d1.effective_date)
        r2 = build_import_instrument_resolution(d2, ImportInstrumentResolutionStatus.NOT_REQUIRED, d2.effective_date)

        b1 = build_import_instrument_resolution_batch(m1, [r1])
        b2 = build_import_instrument_resolution_batch(m2, [r2])
        assert b1.resolution_manifest_sha256 != b2.resolution_manifest_sha256

    def test_CO_fake_manifest_hash_rejected(self):
        """CO: Plausible fake manifest hash rejected in direct constructor."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        res = build_import_instrument_resolution(draft, ImportInstrumentResolutionStatus.NOT_REQUIRED, draft.effective_date)

        with pytest.raises(PortfolioImportInstrumentResolutionError, match="digest mismatch"):
            ImportInstrumentResolutionBatch(
                draft_manifest=draft_manifest,
                resolutions=(res,),
                resolution_manifest_sha256="b" * 64,
            )

    def test_CP_uppercase_manifest_hash_rejected(self):
        """CP: Uppercase manifest hash rejected in direct constructor."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        res = build_import_instrument_resolution(draft, ImportInstrumentResolutionStatus.NOT_REQUIRED, draft.effective_date)

        with pytest.raises(PortfolioImportInstrumentResolutionError, match="lowercase hex"):
            ImportInstrumentResolutionBatch(
                draft_manifest=draft_manifest,
                resolutions=(res,),
                resolution_manifest_sha256="B" * 64,
            )

    def test_CQ_newline_manifest_hash_rejected(self):
        """CQ: Manifest hash with newline rejected."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        res = build_import_instrument_resolution(draft, ImportInstrumentResolutionStatus.NOT_REQUIRED, draft.effective_date)

        with pytest.raises(PortfolioImportInstrumentResolutionError, match="lowercase hex"):
            ImportInstrumentResolutionBatch(
                draft_manifest=draft_manifest,
                resolutions=(res,),
                resolution_manifest_sha256="b" * 64 + "\n",
            )


# ─────────────────────────────────────────────────────────────────────────────
# 14. Status Count Matrix (CR-CZ)
# ─────────────────────────────────────────────────────────────────────────────

class TestStatusCountMatrix:
    """CR-CZ: Derived count properties and is_fully_resolved readiness."""

    def test_CR_not_required_count_exact(self):
        """CR: not_required_count accurately counts NOT_REQUIRED."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        d1 = _make_cash_deposit_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1])
        r1 = build_import_instrument_resolution(d1, ImportInstrumentResolutionStatus.NOT_REQUIRED, d1.effective_date)
        batch = build_import_instrument_resolution_batch(draft_manifest, [r1])
        assert batch.not_required_count == 1
        assert batch.resolved_count == 0

    def test_CS_resolved_count_exact(self):
        """CS: resolved_count accurately counts RESOLVED."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        d1 = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1])
        r1 = build_import_instrument_resolution(d1, ImportInstrumentResolutionStatus.RESOLVED, d1.effective_date, "sentinax_id", 1, uuid4())
        batch = build_import_instrument_resolution_batch(draft_manifest, [r1])
        assert batch.resolved_count == 1
        assert batch.not_required_count == 0

    def test_CT_unresolved_count_exact(self):
        """CT: unresolved_count accurately counts UNRESOLVED."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        d1 = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1])
        diag = ImportInstrumentResolutionDiagnostic("not_found", "msg")
        r1 = build_import_instrument_resolution(d1, ImportInstrumentResolutionStatus.UNRESOLVED, d1.effective_date, "sentinax_id", 1, diagnostics=[diag])
        batch = build_import_instrument_resolution_batch(draft_manifest, [r1])
        assert batch.unresolved_count == 1

    def test_CU_ambiguous_count_exact(self):
        """CU: ambiguous_count accurately counts AMBIGUOUS."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        d1 = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1])
        diag = ImportInstrumentResolutionDiagnostic("ambig", "msg")
        r1 = build_import_instrument_resolution(
            d1,
            ImportInstrumentResolutionStatus.AMBIGUOUS,
            d1.effective_date,
            "sentinax_id",
            1,
            candidate_instrument_ids=[uuid4(), uuid4()],
            diagnostics=[diag],
        )
        batch = build_import_instrument_resolution_batch(draft_manifest, [r1])
        assert batch.ambiguous_count == 1

    def test_CV_counts_sum_to_resolution_count(self):
        """CV: not_required + resolved + unresolved + ambiguous == resolution_count."""
        ass_batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(ass_batch, 1)
        d2 = _make_cash_deposit_draft(ass_batch, 2)
        d3 = _make_buy_draft(ass_batch, 3, instrument="GOOG")
        d4 = _make_buy_draft(ass_batch, 4, instrument="AMZN")
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2, d3, d4])

        diag = ImportInstrumentResolutionDiagnostic("diag_code", "msg")
        r1 = build_import_instrument_resolution(d1, ImportInstrumentResolutionStatus.RESOLVED, d1.effective_date, "sentinax_id", 1, uuid4())
        r2 = build_import_instrument_resolution(d2, ImportInstrumentResolutionStatus.NOT_REQUIRED, d2.effective_date)
        r3 = build_import_instrument_resolution(d3, ImportInstrumentResolutionStatus.UNRESOLVED, d3.effective_date, "sentinax_id", 1, diagnostics=[diag])
        r4 = build_import_instrument_resolution(d4, ImportInstrumentResolutionStatus.AMBIGUOUS, d4.effective_date, "sentinax_id", 1, candidate_instrument_ids=[uuid4(), uuid4()], diagnostics=[diag])

        batch = build_import_instrument_resolution_batch(draft_manifest, [r1, r2, r3, r4])
        assert batch.resolution_count == 4
        assert (
            batch.not_required_count
            + batch.resolved_count
            + batch.unresolved_count
            + batch.ambiguous_count
        ) == batch.resolution_count

    def test_CW_resolution_count_equals_draft_count(self):
        """CW: resolution_count equals draft_manifest.draft_count."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        d1 = _make_cash_deposit_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1])
        r1 = build_import_instrument_resolution(d1, ImportInstrumentResolutionStatus.NOT_REQUIRED, d1.effective_date)
        batch = build_import_instrument_resolution_batch(draft_manifest, [r1])
        assert batch.resolution_count == draft_manifest.draft_count

    def test_CX_fully_resolved_true_for_resolved_and_not_required(self):
        """CX: is_fully_resolved is True when all outcomes are RESOLVED or NOT_REQUIRED."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY, ImportAssessmentStatus.READY])
        d1 = _make_buy_draft(ass_batch, 1)
        d2 = _make_cash_deposit_draft(ass_batch, 2)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        r1 = build_import_instrument_resolution(d1, ImportInstrumentResolutionStatus.RESOLVED, d1.effective_date, "sentinax_id", 1, uuid4())
        r2 = build_import_instrument_resolution(d2, ImportInstrumentResolutionStatus.NOT_REQUIRED, d2.effective_date)
        batch = build_import_instrument_resolution_batch(draft_manifest, [r1, r2])
        assert batch.is_fully_resolved is True

    def test_CY_fully_resolved_false_with_unresolved(self):
        """CY: is_fully_resolved is False when an UNRESOLVED draft exists."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        d1 = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1])
        diag = ImportInstrumentResolutionDiagnostic("not_found", "msg")
        r1 = build_import_instrument_resolution(d1, ImportInstrumentResolutionStatus.UNRESOLVED, d1.effective_date, "sentinax_id", 1, diagnostics=[diag])
        batch = build_import_instrument_resolution_batch(draft_manifest, [r1])
        assert batch.is_fully_resolved is False

    def test_CZ_fully_resolved_false_with_ambiguous(self):
        """CZ: is_fully_resolved is False when an AMBIGUOUS draft exists."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        d1 = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1])
        diag = ImportInstrumentResolutionDiagnostic("ambig", "msg")
        r1 = build_import_instrument_resolution(
            d1,
            ImportInstrumentResolutionStatus.AMBIGUOUS,
            d1.effective_date,
            "sentinax_id",
            1,
            candidate_instrument_ids=[uuid4(), uuid4()],
            diagnostics=[diag],
        )
        batch = build_import_instrument_resolution_batch(draft_manifest, [r1])
        assert batch.is_fully_resolved is False


# ─────────────────────────────────────────────────────────────────────────────
# 15. Mixed Integration Test (Section 71)
# ─────────────────────────────────────────────────────────────────────────────

class TestMixedIntegration:
    """Section 71: Four-draft mixed scenario integration test."""

    def test_four_draft_mixed_scenario(self):
        """
        Mixed integration:
        1. BUY with instrument_reference -> RESOLVED -> UUID A
        2. CASH_DEPOSIT -> NOT_REQUIRED
        3. DIVIDEND with instrument_reference -> AMBIGUOUS -> UUID B / UUID C
        4. FX_CONVERSION -> NOT_REQUIRED
        """
        ass_batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(ass_batch, 1, instrument="AAPL")
        d2 = _make_cash_deposit_draft(ass_batch, 2, amount="1000.00")
        d3 = _make_dividend_draft(ass_batch, 3, amount="50.00", instrument="MSFT")
        d4 = _make_fx_draft(ass_batch, 4, from_amt="100.00", to_amt="3200.00")

        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2, d3, d4])

        uuid_a = uuid4()
        uuid_b = UUID("00000000-0000-0000-0000-000000000001")
        uuid_c = UUID("00000000-0000-0000-0000-000000000002")

        r1 = build_import_instrument_resolution(
            draft=d1,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=d1.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=uuid_a,
        )
        r2 = build_import_instrument_resolution(
            draft=d2,
            status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
            resolution_as_of_date=d2.effective_date,
        )
        r3 = build_import_instrument_resolution(
            draft=d3,
            status=ImportInstrumentResolutionStatus.AMBIGUOUS,
            resolution_as_of_date=d3.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            candidate_instrument_ids=[uuid_b, uuid_c],
            diagnostics=[ImportInstrumentResolutionDiagnostic("ambiguous_reference", "Multiple matches")],
        )
        r4 = build_import_instrument_resolution(
            draft=d4,
            status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
            resolution_as_of_date=d4.effective_date,
        )

        batch = build_import_instrument_resolution_batch(draft_manifest, [r1, r2, r3, r4])

        assert batch.resolution_count == 4
        assert batch.resolved_count == 1
        assert batch.not_required_count == 2
        assert batch.ambiguous_count == 1
        assert batch.unresolved_count == 0
        assert batch.is_fully_resolved is False


# ─────────────────────────────────────────────────────────────────────────────
# 16. Semantic Equality Reconstruction Test (Section 72)
# ─────────────────────────────────────────────────────────────────────────────

class TestSemanticEqualityReconstruction:
    """Section 72: Reconstructed draft equality binding."""

    def test_reconstructed_draft_accepted_by_semantic_equality(self):
        """
        Prove: Draft A == Draft B and Draft A is not Draft B.
        A resolution built against A is accepted in a batch with manifest containing equal B.
        """
        ass_batch_A = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        # Reconstruct semantically equal assessment batch B
        ass_batch_B = build_import_assessment_batch(
            ass_batch_A.parsed_manifest,
            list(ass_batch_A.assessments),
        )
        assert ass_batch_A == ass_batch_B
        assert ass_batch_A is not ass_batch_B

        draft_A = _make_buy_draft(ass_batch_A, 1)
        draft_B = _make_buy_draft(ass_batch_B, 1)
        assert draft_A == draft_B
        assert draft_A is not draft_B

        # Build draft manifest using batch B
        draft_manifest_B = build_import_draft_batch_manifest(ass_batch_B, [draft_B])

        # Build resolution outcome using draft A
        res_A = build_import_instrument_resolution(
            draft=draft_A,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=draft_A.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=uuid4(),
        )

        # Batch built with manifest B and resolution A must be accepted due to draft semantic equality
        batch = build_import_instrument_resolution_batch(draft_manifest_B, [res_A])
        assert batch.resolution_count == 1
        assert batch.resolutions[0] is res_A


# ─────────────────────────────────────────────────────────────────────────────
# 17. Surface Red-Team (Section 73)
# ─────────────────────────────────────────────────────────────────────────────

class TestSurfaceRedTeam:
    """Section 73: Verify field surface and module isolation."""

    def test_record_resolution_field_surface(self):
        """ImportInstrumentResolution contains only authorized fields."""
        field_names = {f.name for f in dataclass_fields(ImportInstrumentResolution)}
        expected = {
            "draft",
            "status",
            "resolution_as_of_date",
            "resolver_key",
            "resolver_revision",
            "instrument_id",
            "candidate_instrument_ids",
            "diagnostics",
            "resolution_sha256",
        }
        assert field_names == expected

    def test_batch_resolution_field_surface(self):
        """ImportInstrumentResolutionBatch contains only authorized fields."""
        field_names = {f.name for f in dataclass_fields(ImportInstrumentResolutionBatch)}
        expected = {
            "draft_manifest",
            "resolutions",
            "resolution_manifest_sha256",
        }
        assert field_names == expected

    def test_no_forbidden_dependencies(self):
        """Module does not import forbidden services or ledger models."""
        import backend.engine.private.portfolio.import_instrument_resolution as module
        assert not hasattr(module, "PortfolioTransaction")
        assert not hasattr(module, "InstrumentResolverService")
        assert not hasattr(module, "CashBucket")


# ─────────────────────────────────────────────────────────────────────────────
# 18. Final Red-Team Scenarios (Section 78)
# ─────────────────────────────────────────────────────────────────────────────

class TestFinalRedTeam:
    """Section 78: Comprehensive failure-injection red-team scenarios."""

    def test_buy_marked_not_required_rejected(self):
        """BUY marked NOT_REQUIRED fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="NOT_REQUIRED status is invalid"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
                resolution_as_of_date=draft.effective_date,
            )

    def test_cash_deposit_falsely_resolved_rejected(self):
        """CASH_DEPOSIT marked RESOLVED fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="RESOLVED status requires draft to have an instrument_reference"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
            )

    def test_instrument_reference_preserved_verbatim(self):
        """Draft instrument_reference is preserved verbatim without normalization."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1, instrument="  aapl.is_verbatim  ")
        res = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=draft.effective_date,
            resolver_key="sentinax_id",
            resolver_revision=1,
            instrument_id=uuid4(),
        )
        assert res.draft.instrument_reference == "  aapl.is_verbatim  "

    def test_current_date_used_instead_of_effective_date_rejected(self):
        """Using current date instead of draft.effective_date fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1, eff_date=date(2025, 1, 1))
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="must equal draft.effective_date"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=date.today(),
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
            )

    def test_ambiguous_with_one_candidate_rejected(self):
        """AMBIGUOUS with only one candidate rejected."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        diag = ImportInstrumentResolutionDiagnostic("ambig", "msg")
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="at least two candidate_instrument_ids"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.AMBIGUOUS,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                candidate_instrument_ids=[uuid4()],
                diagnostics=[diag],
            )

    def test_unresolved_with_candidates_rejected(self):
        """UNRESOLVED with candidates rejected."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        diag = ImportInstrumentResolutionDiagnostic("not_found", "msg")
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="must not carry candidate_instrument_ids"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.UNRESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                candidate_instrument_ids=[uuid4()],
                diagnostics=[diag],
            )

    def test_resolved_without_uuid_rejected(self):
        """RESOLVED without instrument UUID rejected."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="requires an authoritative UUID"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=None,
            )

    def test_resolver_metadata_absent_on_attempted_resolution_rejected(self):
        """Attempted resolution without resolver metadata rejected."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="requires resolver_key and resolver_revision"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key=None,
                resolver_revision=None,
                instrument_id=uuid4(),
            )


# ─────────────────────────────────────────────────────────────────────────────
# 19. Phase 13J.1 Builder Prevalidation & Error Domain Hardening
# ─────────────────────────────────────────────────────────────────────────────

class TestBuilderPrevalidationHardening:
    """Tests for Phase 13J.1 builder prevalidation and fail-closed error domain."""

    def test_malformed_draft_raises_domain_error_not_attribute_error(self):
        """Malformed draft object raises PortfolioImportInstrumentResolutionError, not AttributeError."""
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="draft must be an ImportTransactionDraft instance"):
            build_import_instrument_resolution(
                draft="not-a-draft",  # type: ignore
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=date(2026, 8, 28),
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
            )

    def test_none_draft_raises_domain_error(self):
        """None draft object raises PortfolioImportInstrumentResolutionError."""
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="draft must be an ImportTransactionDraft instance"):
            build_import_instrument_resolution(
                draft=None,  # type: ignore
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=date(2026, 8, 28),
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
            )

    def test_malformed_status_raises_domain_error_not_attribute_error(self):
        """String status raises PortfolioImportInstrumentResolutionError, not AttributeError."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="status must be an ImportInstrumentResolutionStatus enum member"):
            build_import_instrument_resolution(
                draft=draft,
                status="resolved",  # type: ignore
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
            )

    def test_none_status_raises_domain_error(self):
        """None status raises PortfolioImportInstrumentResolutionError."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="status must be an ImportInstrumentResolutionStatus enum member"):
            build_import_instrument_resolution(
                draft=draft,
                status=None,  # type: ignore
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
            )

    def test_datetime_as_date_raises_domain_error(self):
        """datetime instance as resolution_as_of_date raises PortfolioImportInstrumentResolutionError."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        dt = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="strictly a datetime.date instance"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=dt,  # type: ignore
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
            )

    def test_string_date_raises_domain_error_not_attribute_error(self):
        """String as resolution_as_of_date raises PortfolioImportInstrumentResolutionError, not AttributeError."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="strictly a datetime.date instance"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date="2026-08-28",  # type: ignore
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
            )

    def test_none_date_raises_domain_error(self):
        """None as resolution_as_of_date raises PortfolioImportInstrumentResolutionError."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="strictly a datetime.date instance"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=None,  # type: ignore
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
            )

    def test_resolver_metadata_pairing_in_builder(self):
        """Resolver key/revision half-presence raises PortfolioImportInstrumentResolutionError in builder."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="Resolver metadata must be all-or-none"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=None,
                instrument_id=uuid4(),
            )
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="Resolver metadata must be all-or-none"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key=None,
                resolver_revision=1,
                instrument_id=uuid4(),
            )

    def test_malformed_instrument_uuid_in_builder(self):
        """String passed as instrument_id raises PortfolioImportInstrumentResolutionError."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="requires an authoritative UUID"):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id="not-a-uuid",  # type: ignore
            )

    def test_status_contract_matrix_in_builder(self):
        """Verify invalid status combinations fail in Phase 13J error domain."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        buy_draft = _make_buy_draft(batch, 1)
        cash_draft = _make_cash_deposit_draft(batch, 1)

        # A. NOT_REQUIRED with resolver metadata
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="resolver_key and resolver_revision must be None for NOT_REQUIRED"):
            build_import_instrument_resolution(
                draft=cash_draft,
                status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
                resolution_as_of_date=cash_draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
            )

        # B. RESOLVED without instrument UUID
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="requires an authoritative UUID"):
            build_import_instrument_resolution(
                draft=buy_draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=buy_draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=None,
            )

        # C. UNRESOLVED without diagnostic
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="UNRESOLVED status requires at least one diagnostic"):
            build_import_instrument_resolution(
                draft=buy_draft,
                status=ImportInstrumentResolutionStatus.UNRESOLVED,
                resolution_as_of_date=buy_draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                diagnostics=[],
            )

        # D. AMBIGUOUS with only one candidate
        diag = ImportInstrumentResolutionDiagnostic("ambig", "msg")
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="at least two candidate_instrument_ids"):
            build_import_instrument_resolution(
                draft=buy_draft,
                status=ImportInstrumentResolutionStatus.AMBIGUOUS,
                resolution_as_of_date=buy_draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                candidate_instrument_ids=[uuid4()],
                diagnostics=[diag],
            )

        # E. Cash draft marked RESOLVED
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="RESOLVED status requires draft to have an instrument_reference"):
            build_import_instrument_resolution(
                draft=cash_draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=cash_draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
            )

        # F. BUY marked NOT_REQUIRED
        with pytest.raises(PortfolioImportInstrumentResolutionError, match="NOT_REQUIRED status is invalid"):
            build_import_instrument_resolution(
                draft=buy_draft,
                status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
                resolution_as_of_date=buy_draft.effective_date,
            )

    def test_hash_helper_not_reached_on_malformed_input(self, monkeypatch):
        """Monkeypatch _compute_resolution_sha256 to prove it is not called when validation fails."""
        import backend.engine.private.portfolio.import_instrument_resolution as module

        class SentinelHashException(Exception):
            pass

        def exploding_hash(*args, **kwargs):
            raise SentinelHashException("Hash function should not have been reached!")

        monkeypatch.setattr(module, "_compute_resolution_sha256", exploding_hash)

        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(batch, 1)

        # Test 1: Malformed draft
        with pytest.raises(PortfolioImportInstrumentResolutionError):
            build_import_instrument_resolution(
                draft="bad-draft",  # type: ignore
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date=date(2026, 8, 28),
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
            )

        # Test 2: Malformed status
        with pytest.raises(PortfolioImportInstrumentResolutionError):
            build_import_instrument_resolution(
                draft=draft,
                status="invalid_status",  # type: ignore
                resolution_as_of_date=draft.effective_date,
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
            )

        # Test 3: Malformed date
        with pytest.raises(PortfolioImportInstrumentResolutionError):
            build_import_instrument_resolution(
                draft=draft,
                status=ImportInstrumentResolutionStatus.RESOLVED,
                resolution_as_of_date="2026-08-28",  # type: ignore
                resolver_key="sentinax_id",
                resolver_revision=1,
                instrument_id=uuid4(),
            )

