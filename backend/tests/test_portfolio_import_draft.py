"""
backend/tests/test_portfolio_import_draft.py
============================================
Tests for Phase 13H: Immutable Source-Neutral Economic Transaction Draft Contract.

Zero network calls (pytest-socket enforced).
Pure in-memory domain evaluation using real Phase 13A-13G builders and models.

Test Matrix:
    1. READY Gate Matrix (A-G)
    2. Transaction Type Matrix (H-K)
    3. Date / Time Matrix (L-R)
    4. Decimal Matrix (S-AB)
    5. Instrument Reference Matrix (AC-AJ)
    6. BUY / SELL Matrix (AK-AR)
    7. Cash Movement Matrix (AS-AY)
    8. Income / Fee Matrix (AZ-BG)
    9. FX Conversion Matrix (BH-BN)
    10. Hash & Canonicalization Matrix (BO-CA)
    11. Assessment-Binding Matrix (CB-CE)
    12. Surface Red-Team (Forbidden fields check)
    13. Canonical CSV Real Pipeline Integration
"""

from __future__ import annotations

from dataclasses import fields, FrozenInstanceError
from datetime import date, datetime, timezone, timedelta, tzinfo
from decimal import Decimal
from enum import Enum
import hashlib
import json
from typing import Optional
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
    _canonical_decimal_str,
    _canonical_datetime_str,
)
from backend.engine.private.portfolio.import_provenance import (
    build_import_file_provenance,
    build_import_record_provenance,
)
from backend.engine.private.portfolio.import_batch import (
    build_import_batch_manifest,
)
from backend.engine.private.portfolio.import_parsing import (
    ImportParsedField,
    build_parsed_import_record,
)
from backend.engine.private.portfolio.import_parsed_batch import (
    build_parsed_import_batch_manifest,
)
from backend.engine.private.portfolio.import_pipeline import (
    build_import_staging_result,
)
from backend.engine.private.portfolio.parsers import (
    SentinaxCanonicalCsvParserV1,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helper Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _make_test_assessment_batch(
    statuses: list[ImportAssessmentStatus] = [ImportAssessmentStatus.READY, ImportAssessmentStatus.UNRESOLVED],
) -> ImportAssessmentBatch:
    """Builds a real, verified ImportAssessmentBatch with specified statuses."""
    port_id = uuid4()
    acc_id = uuid4()
    t = datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)
    source_key = "sentinax_csv"
    parser_revision = 1

    file_prov = build_import_file_provenance(
        portfolio_id=port_id,
        account_id=acc_id,
        source_key=source_key,
        filename="test.csv",
        content=b"dummy_content",
        imported_at=t,
    )

    rec_provs = [
        build_import_record_provenance(
            file_provenance=file_prov,
            record_ordinal=i + 1,
            raw_record=f"row_{i}".encode("utf-8"),
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
            raw_record=f"row_{i}".encode("utf-8"),
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
            diag = ImportAssessmentDiagnostic(code="diag_code", message="Diag message", field_key="symbol")
            ass = build_import_record_assessment(parsed_records[i], status, [diag])
        assessments.append(ass)

    return build_import_assessment_batch(parsed_manifest, assessments)


# ─────────────────────────────────────────────────────────────────────────────
# 1. READY Gate Matrix (A-G)
# ─────────────────────────────────────────────────────────────────────────────

class TestReadyGateMatrix:
    """Verifies that only READY assessments can be drafted, and ordinal bounds are enforced."""

    def test_ready_assessment_permits_draft(self):
        """A: READY assessment permits draft creation."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        assert draft.record_ordinal == 1
        assert draft.assessment.status == ImportAssessmentStatus.READY
        assert draft.transaction_type == TransactionType.BUY

    def test_unresolved_assessment_rejected(self):
        """B: UNRESOLVED assessment fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.UNRESOLVED])
        with pytest.raises(PortfolioImportDraftError, match="Only records with READY assessment status"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.BUY,
                effective_date=date(2026, 8, 28),
                instrument_reference="AAPL",
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
            )

    def test_rejected_assessment_rejected(self):
        """C: REJECTED assessment fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.REJECTED])
        with pytest.raises(PortfolioImportDraftError, match="Only records with READY assessment status"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.BUY,
                effective_date=date(2026, 8, 28),
                instrument_reference="AAPL",
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
            )

    def test_ordinal_zero_rejected(self):
        """D: Ordinal 0 fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        with pytest.raises(PortfolioImportDraftError, match="record_ordinal must be"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=0,
                transaction_type=TransactionType.BUY,
                effective_date=date(2026, 8, 28),
                instrument_reference="AAPL",
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
            )

    def test_bool_ordinal_rejected(self):
        """E: Bool ordinal fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        with pytest.raises(PortfolioImportDraftError, match="record_ordinal must be an int"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=True,  # type: ignore
                transaction_type=TransactionType.BUY,
                effective_date=date(2026, 8, 28),
                instrument_reference="AAPL",
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
            )

    def test_ordinal_beyond_batch_rejected(self):
        """F: Ordinal beyond batch count fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        with pytest.raises(PortfolioImportDraftError, match="exceeds batch record count"):
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

    def test_wrong_assessment_batch_type_rejected(self):
        """G: Wrong assessment_batch type fails closed."""
        with pytest.raises(PortfolioImportDraftError, match="assessment_batch must be an ImportAssessmentBatch"):
            build_import_transaction_draft(
                assessment_batch={"not": "a batch"},  # type: ignore
                record_ordinal=1,
                transaction_type=TransactionType.BUY,
                effective_date=date(2026, 8, 28),
                instrument_reference="AAPL",
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Transaction Type Matrix (H-K)
# ─────────────────────────────────────────────────────────────────────────────

class TestTransactionTypeMatrix:
    """Verifies transaction type enum enforcement and REVERSAL rejection."""

    def test_actual_buy_enum_accepted(self):
        """H: Actual TransactionType.BUY is accepted."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        assert draft.transaction_type == TransactionType.BUY

    def test_string_transaction_type_rejected(self):
        """I: String 'buy' fails closed without coercion."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        with pytest.raises(PortfolioImportDraftError, match="transaction_type must be a TransactionType enum member"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type="BUY",  # type: ignore
                effective_date=date(2026, 8, 28),
                instrument_reference="AAPL",
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
            )

    def test_reversal_rejected(self):
        """J: TransactionType.REVERSAL is strictly rejected at pre-ledger stage."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        with pytest.raises(PortfolioImportDraftError, match="TransactionType.REVERSAL cannot be drafted"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.REVERSAL,
                effective_date=date(2026, 8, 28),
            )

    def test_arbitrary_enum_rejected(self):
        """K: Unrelated Enum fails closed."""
        class OtherEnum(Enum):
            BUY = "buy"

        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        with pytest.raises(PortfolioImportDraftError, match="transaction_type must be a TransactionType enum member"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=OtherEnum.BUY,  # type: ignore
                effective_date=date(2026, 8, 28),
                instrument_reference="AAPL",
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Date / Time Matrix (L-R)
# ─────────────────────────────────────────────────────────────────────────────

class TestDateTimeMatrix:
    """Verifies strict date typing and timezone-awareness rules."""

    def test_exact_date_accepted(self):
        """L: Exact date instance accepted."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        d = date(2026, 8, 28)
        draft = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=d,
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        assert draft.effective_date == d
        assert type(draft.effective_date) is date

    def test_datetime_passed_as_effective_date_rejected(self):
        """M: Datetime instance passed as effective_date fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        dt = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        with pytest.raises(PortfolioImportDraftError, match="effective_date must be strictly a built-in date instance"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.BUY,
                effective_date=dt,  # type: ignore
                instrument_reference="AAPL",
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
            )

    def test_executed_at_none_accepted(self):
        """N: executed_at=None accepted."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            executed_at=None,
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        assert draft.executed_at is None

    def test_aware_utc_accepted(self):
        """O: Aware UTC executed_at accepted."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        exec_t = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        draft = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            executed_at=exec_t,
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        assert draft.executed_at == exec_t

    def test_aware_plus_three_accepted_and_stored_unchanged(self):
        """P: Aware +03 executed_at stored in caller's timezone unchanged."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        tz3 = timezone(timedelta(hours=3))
        exec_t = datetime(2026, 8, 28, 13, 0, tzinfo=tz3)
        draft = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            executed_at=exec_t,
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        assert draft.executed_at == exec_t
        assert draft.executed_at.tzinfo == tz3

    def test_naive_datetime_rejected(self):
        """Q: Naive datetime fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        exec_t = datetime(2026, 8, 28, 13, 0)
        with pytest.raises(PortfolioImportDraftError, match="executed_at must be a timezone-aware datetime"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.BUY,
                effective_date=date(2026, 8, 28),
                executed_at=exec_t,
                instrument_reference="AAPL",
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
            )

    def test_null_utcoffset_timezone_rejected(self):
        """R: Timezone with utcoffset() -> None fails closed."""
        class NullOffsetTz(tzinfo):
            def utcoffset(self, dt):
                return None
            def tzname(self, dt):
                return "NULL"
            def dst(self, dt):
                return None

        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        exec_t = datetime(2026, 8, 28, 13, 0, tzinfo=NullOffsetTz())
        with pytest.raises(PortfolioImportDraftError, match="executed_at must be a timezone-aware datetime"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.BUY,
                effective_date=date(2026, 8, 28),
                executed_at=exec_t,
                instrument_reference="AAPL",
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Decimal Matrix (S-AB)
# ─────────────────────────────────────────────────────────────────────────────

class TestDecimalMatrix:
    """Verifies strict Decimal typing, finiteness, and positivity."""

    def test_positive_decimal_accepted(self):
        """S: Positive finite Decimal accepted."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_reference="AAPL",
            quantity=Decimal("100.5"),
            unit_price=Decimal("25.25"),
            trade_currency=Currency.USD,
        )
        assert draft.quantity == Decimal("100.5")
        assert draft.unit_price == Decimal("25.25")

    def test_non_decimal_types_rejected(self):
        """T-W: int, float, string, bool fail closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        bad_values = [
            10,            # T: int
            10.5,          # U: float
            "10.5",        # V: str
            True,          # W: bool
        ]
        for bad_val in bad_values:
            with pytest.raises(PortfolioImportDraftError, match="quantity must be a Decimal instance"):
                build_import_transaction_draft(
                    assessment_batch=batch,
                    record_ordinal=1,
                    transaction_type=TransactionType.BUY,
                    effective_date=date(2026, 8, 28),
                    instrument_reference="AAPL",
                    quantity=bad_val,  # type: ignore
                    unit_price=Decimal("25.25"),
                    trade_currency=Currency.USD,
                )

    def test_zero_and_negative_decimals_rejected(self):
        """X, Y: Zero and negative Decimals fail closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        for bad_dec in [Decimal("0"), Decimal("-0"), Decimal("-10.5")]:
            with pytest.raises(PortfolioImportDraftError, match="must be strictly positive"):
                build_import_transaction_draft(
                    assessment_batch=batch,
                    record_ordinal=1,
                    transaction_type=TransactionType.BUY,
                    effective_date=date(2026, 8, 28),
                    instrument_reference="AAPL",
                    quantity=bad_dec,
                    unit_price=Decimal("25.25"),
                    trade_currency=Currency.USD,
                )

    def test_non_finite_decimals_rejected(self):
        """Z-AB: NaN, Infinity, -Infinity fail closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        for non_finite in [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")]:
            with pytest.raises(PortfolioImportDraftError, match="must be a finite Decimal"):
                build_import_transaction_draft(
                    assessment_batch=batch,
                    record_ordinal=1,
                    transaction_type=TransactionType.BUY,
                    effective_date=date(2026, 8, 28),
                    instrument_reference="AAPL",
                    quantity=non_finite,
                    unit_price=Decimal("25.25"),
                    trade_currency=Currency.USD,
                )


# ─────────────────────────────────────────────────────────────────────────────
# 5. Instrument Reference Matrix (AC-AJ)
# ─────────────────────────────────────────────────────────────────────────────

class TestInstrumentReferenceMatrix:
    """Verifies instrument_reference syntax, length, and verbatim text preservation."""

    def test_valid_instrument_references_accepted(self):
        """AC-AF: Ticker, ISIN, Unicode, and exact spacing preserved verbatim."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        test_refs = [
            "AAPL",               # AC: Ordinary ticker
            "US0378331005",       # AD: ISIN
            "THYAO.IS",           # Compound
            "ALTIN.S1",           # Turkish commodity
            "  AAPL  ",           # AF: Exact whitespace preserved
            "Garantı_Portföy",    # AE: Unicode
        ]
        for ref in test_refs:
            draft = build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.BUY,
                effective_date=date(2026, 8, 28),
                instrument_reference=ref,
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
            )
            assert draft.instrument_reference == ref

    def test_invalid_instrument_references_rejected(self):
        """AG-AJ: Whitespace-only, empty, oversized (>256), and non-string fail closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        bad_refs = [
            "   \t\n",    # AG: Whitespace only
            "",            # AH: Empty
            "A" * 257,     # AI: >256 chars
            123,           # AJ: Non-string
            True,          # Bool
        ]
        for bad_ref in bad_refs:
            with pytest.raises(PortfolioImportDraftError):
                build_import_transaction_draft(
                    assessment_batch=batch,
                    record_ordinal=1,
                    transaction_type=TransactionType.BUY,
                    effective_date=date(2026, 8, 28),
                    instrument_reference=bad_ref,  # type: ignore
                    quantity=Decimal("10"),
                    unit_price=Decimal("150.00"),
                    trade_currency=Currency.USD,
                )


# ─────────────────────────────────────────────────────────────────────────────
# 6. BUY / SELL Matrix (AK-AR)
# ─────────────────────────────────────────────────────────────────────────────

class TestBuySellMatrix:
    """Verifies BUY/SELL trade field requirements and cross-family exclusions."""

    def test_valid_buy_and_sell_accepted(self):
        """AK, AL: Valid BUY and SELL drafts succeed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        for t_type in (TransactionType.BUY, TransactionType.SELL):
            draft = build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=t_type,
                effective_date=date(2026, 8, 28),
                instrument_reference="AAPL",
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
            )
            assert draft.transaction_type == t_type
            assert draft.cash_amount is None
            assert draft.from_amount is None

    def test_buy_missing_required_fields_rejected(self):
        """AM-AP: Missing instrument_reference, quantity, unit_price, or trade_currency fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        # AM: Missing instrument reference
        with pytest.raises(PortfolioImportDraftError, match="requires instrument_reference"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.BUY,
                effective_date=date(2026, 8, 28),
                instrument_reference=None,
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
            )

        # AN: Missing quantity
        with pytest.raises(PortfolioImportDraftError, match="requires quantity"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.BUY,
                effective_date=date(2026, 8, 28),
                instrument_reference="AAPL",
                quantity=None,
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
            )

        # AO: Missing unit price
        with pytest.raises(PortfolioImportDraftError, match="requires unit_price"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.BUY,
                effective_date=date(2026, 8, 28),
                instrument_reference="AAPL",
                quantity=Decimal("10"),
                unit_price=None,
                trade_currency=Currency.USD,
            )

        # AP: Missing trade currency
        with pytest.raises(PortfolioImportDraftError, match="requires trade_currency"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.BUY,
                effective_date=date(2026, 8, 28),
                instrument_reference="AAPL",
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=None,
            )

    def test_buy_with_cash_or_fx_fields_rejected(self):
        """AQ, AR: BUY carrying simple cash or FX fields fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        # AQ: Simple cash
        with pytest.raises(PortfolioImportDraftError, match="must not specify cash fields"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.BUY,
                effective_date=date(2026, 8, 28),
                instrument_reference="AAPL",
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
                cash_amount=Decimal("1500.00"),
                cash_currency=Currency.USD,
            )

        # AR: FX fields
        with pytest.raises(PortfolioImportDraftError, match="must not specify FX fields"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.BUY,
                effective_date=date(2026, 8, 28),
                instrument_reference="AAPL",
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
                from_currency=Currency.USD,
                from_amount=Decimal("1500.00"),
                to_currency=Currency.TRY,
                to_amount=Decimal("50000.00"),
            )


# ─────────────────────────────────────────────────────────────────────────────
# 7. Cash Movement Matrix (AS-AY)
# ─────────────────────────────────────────────────────────────────────────────

class TestCashMovementMatrix:
    """Verifies CASH_DEPOSIT and CASH_WITHDRAWAL requirements and exclusions."""

    def test_valid_cash_deposit_and_withdrawal_accepted(self):
        """AS, AT: Valid CASH_DEPOSIT and CASH_WITHDRAWAL succeed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        for t_type in (TransactionType.CASH_DEPOSIT, TransactionType.CASH_WITHDRAWAL):
            draft = build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=t_type,
                effective_date=date(2026, 8, 28),
                cash_amount=Decimal("5000.00"),
                cash_currency=Currency.TRY,
            )
            assert draft.transaction_type == t_type
            assert draft.cash_amount == Decimal("5000.00")
            assert draft.cash_currency == Currency.TRY

    def test_cash_movement_with_instrument_or_trade_fields_rejected(self):
        """AU, AX, AY: Cash movement carrying instrument_reference, trade, or FX fields fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        # AU: instrument_reference
        with pytest.raises(PortfolioImportDraftError, match="must not specify instrument_reference"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.CASH_DEPOSIT,
                effective_date=date(2026, 8, 28),
                instrument_reference="AAPL",
                cash_amount=Decimal("5000.00"),
                cash_currency=Currency.TRY,
            )

        # AX: trade fields (quantity)
        with pytest.raises(PortfolioImportDraftError, match="must not specify trade fields"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.CASH_DEPOSIT,
                effective_date=date(2026, 8, 28),
                quantity=Decimal("10"),
                cash_amount=Decimal("5000.00"),
                cash_currency=Currency.TRY,
            )

        # AY: FX fields
        with pytest.raises(PortfolioImportDraftError, match="must not specify FX fields"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.CASH_DEPOSIT,
                effective_date=date(2026, 8, 28),
                cash_amount=Decimal("5000.00"),
                cash_currency=Currency.TRY,
                from_currency=Currency.USD,
                from_amount=Decimal("100"),
            )


# ─────────────────────────────────────────────────────────────────────────────
# 8. Income / Fee Matrix (AZ-BG)
# ─────────────────────────────────────────────────────────────────────────────

class TestIncomeFeeMatrix:
    """Verifies DIVIDEND, INTEREST, FEE, and TAX_WITHHOLDING rules."""

    def test_valid_income_and_fees(self):
        """AZ-BD: DIVIDEND (with/without instrument), INTEREST, FEE, TAX_WITHHOLDING succeed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        # AZ: DIVIDEND without instrument
        d1 = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.DIVIDEND,
            effective_date=date(2026, 8, 28),
            cash_amount=Decimal("120.50"),
            cash_currency=Currency.USD,
        )
        assert d1.instrument_reference is None

        # BA: DIVIDEND with instrument
        d2 = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.DIVIDEND,
            effective_date=date(2026, 8, 28),
            instrument_reference="AAPL",
            cash_amount=Decimal("120.50"),
            cash_currency=Currency.USD,
        )
        assert d2.instrument_reference == "AAPL"

        # BB-BD: INTEREST, FEE, TAX_WITHHOLDING
        for t_type in (TransactionType.INTEREST, TransactionType.FEE, TransactionType.TAX_WITHHOLDING):
            d = build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=t_type,
                effective_date=date(2026, 8, 28),
                cash_amount=Decimal("15.00"),
                cash_currency=Currency.USD,
            )
            assert d.transaction_type == t_type

    def test_income_missing_cash_or_carrying_trade_fields_rejected(self):
        """BE-BG: Missing cash_amount or carrying trade/FX fields fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        # BE: Missing cash_amount
        with pytest.raises(PortfolioImportDraftError, match="requires cash_amount"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.DIVIDEND,
                effective_date=date(2026, 8, 28),
                cash_currency=Currency.USD,
            )

        # BF: Carrying trade fields (quantity)
        with pytest.raises(PortfolioImportDraftError, match="must not specify trade fields"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.DIVIDEND,
                effective_date=date(2026, 8, 28),
                cash_amount=Decimal("100"),
                cash_currency=Currency.USD,
                quantity=Decimal("5"),
            )


# ─────────────────────────────────────────────────────────────────────────────
# 9. FX Conversion Matrix (BH-BN)
# ─────────────────────────────────────────────────────────────────────────────

class TestFxConversionMatrix:
    """Verifies FX_CONVERSION pair requirements and exclusions."""

    def test_valid_fx_conversion_accepted(self):
        """BH: Valid FX_CONVERSION succeeds."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.FX_CONVERSION,
            effective_date=date(2026, 8, 28),
            from_currency=Currency.USD,
            from_amount=Decimal("1000.00"),
            to_currency=Currency.TRY,
            to_amount=Decimal("34000.00"),
        )
        assert draft.transaction_type == TransactionType.FX_CONVERSION
        assert draft.from_currency == Currency.USD
        assert draft.to_currency == Currency.TRY

    def test_fx_conversion_same_currency_rejected(self):
        """BI: from_currency == to_currency fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        with pytest.raises(PortfolioImportDraftError, match="from_currency and to_currency must differ"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.FX_CONVERSION,
                effective_date=date(2026, 8, 28),
                from_currency=Currency.USD,
                from_amount=Decimal("1000.00"),
                to_currency=Currency.USD,
                to_amount=Decimal("1000.00"),
            )

    def test_fx_conversion_missing_amounts_or_carrying_other_fields_rejected(self):
        """BJ-BN: Missing amounts or carrying simple cash, instrument, or trade fields fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        # BJ: Missing from_amount
        with pytest.raises(PortfolioImportDraftError, match="requires from_amount"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.FX_CONVERSION,
                effective_date=date(2026, 8, 28),
                from_currency=Currency.USD,
                to_currency=Currency.TRY,
                to_amount=Decimal("34000.00"),
            )

        # BL: Simple cash fields
        with pytest.raises(PortfolioImportDraftError, match="must not specify simple cash fields"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.FX_CONVERSION,
                effective_date=date(2026, 8, 28),
                from_currency=Currency.USD,
                from_amount=Decimal("1000.00"),
                to_currency=Currency.TRY,
                to_amount=Decimal("34000.00"),
                cash_amount=Decimal("1000.00"),
            )

        # BM: instrument_reference
        with pytest.raises(PortfolioImportDraftError, match="must not specify instrument_reference"):
            build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.FX_CONVERSION,
                effective_date=date(2026, 8, 28),
                from_currency=Currency.USD,
                from_amount=Decimal("1000.00"),
                to_currency=Currency.TRY,
                to_amount=Decimal("34000.00"),
                instrument_reference="USDTRY",
            )


# ─────────────────────────────────────────────────────────────────────────────
# 10. Hash & Canonicalization Matrix (BO-CA)
# ─────────────────────────────────────────────────────────────────────────────

class TestHashAndCanonicalizationMatrix:
    """Verifies preimage calculation, Decimal canonicalization, timezone instant equivalence, and digest checks."""

    def test_independent_preimage_hash_matches(self):
        """BO: Independent JSON preimage computation matches draft_sha256."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        exec_t = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        d_date = date(2026, 8, 28)

        draft = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=d_date,
            executed_at=exec_t,
            instrument_reference="AAPL",
            quantity=Decimal("10.5"),
            unit_price=Decimal("150.25"),
            trade_currency=Currency.USD,
        )

        expected_preimage = [
            batch.assessment_manifest_sha256,
            1,
            batch.assessments[0].parsed_record.parsed_sha256,
            "buy",
            "2026-08-28",
            "2026-08-28T10:00:00+00:00",
            "AAPL",
            "10.5",
            "150.25",
            "USD",
            None,
            None,
            None,
            None,
            None,
            None,
        ]
        json_bytes = json.dumps(expected_preimage, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        expected_sha = hashlib.sha256(json_bytes).hexdigest()

        assert draft.draft_sha256 == expected_sha

    def test_repeated_identical_build_deterministic(self):
        """BP: Repeated identical builds produce identical hash."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
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
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        assert d1.draft_sha256 == d2.draft_sha256

    def test_equivalent_decimal_representations_same_hash(self):
        """BQ: Numerically equal Decimals produce identical hash."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        decimal_variants = [
            Decimal("1"),
            Decimal("1.0"),
            Decimal("1.00"),
            Decimal("1E+0"),
        ]
        hashes = set()
        for d in decimal_variants:
            draft = build_import_transaction_draft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.BUY,
                effective_date=date(2026, 8, 28),
                instrument_reference="AAPL",
                quantity=d,
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
            )
            hashes.add(draft.draft_sha256)
        assert len(hashes) == 1

    def test_economically_equivalent_executed_at_same_hash(self):
        """BR: Chronologically equal instants in different timezones produce identical hash."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        t_utc = datetime(2026, 8, 28, 10, 0, 0, tzinfo=timezone.utc)
        t_plus3 = datetime(2026, 8, 28, 13, 0, 0, tzinfo=timezone(timedelta(hours=3)))

        d1 = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            executed_at=t_utc,
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        d2 = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            executed_at=t_plus3,
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        assert d1.draft_sha256 == d2.draft_sha256

    def test_microsecond_difference_changes_hash(self):
        """BS: Microsecond difference alters hash."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        t1 = datetime(2026, 8, 28, 10, 0, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 28, 10, 0, 0, 1, tzinfo=timezone.utc)

        d1 = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            executed_at=t1,
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        d2 = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            executed_at=t2,
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        assert d1.draft_sha256 != d2.draft_sha256

    def test_attribute_changes_alter_hash(self):
        """BT-BW: Changing transaction_type, instrument_reference, quantity, or currency alters hash."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        base = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )

        # BT: Type change
        mod_type = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.SELL,
            effective_date=date(2026, 8, 28),
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        assert base.draft_sha256 != mod_type.draft_sha256

        # BU: Instrument ref change (whitespace)
        mod_ref = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_reference="AAPL ",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        assert base.draft_sha256 != mod_ref.draft_sha256

        # BV: Quantity change
        mod_qty = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_reference="AAPL",
            quantity=Decimal("11"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        assert base.draft_sha256 != mod_qty.draft_sha256

        # BW: Currency change
        mod_curr = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.EUR,
        )
        assert base.draft_sha256 != mod_curr.draft_sha256

    def test_direct_constructor_invalid_hashes_rejected(self):
        """BY-CA: Wrong hash, uppercase, and trailing newline fail closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        # BY: Wrong digest
        with pytest.raises(PortfolioImportDraftError, match="draft_sha256 digest mismatch"):
            ImportTransactionDraft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.BUY,
                effective_date=date(2026, 8, 28),
                instrument_reference="AAPL",
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
                draft_sha256="0" * 64,
            )

        # BZ: Uppercase
        with pytest.raises(PortfolioImportDraftError, match="64-character lowercase hex"):
            ImportTransactionDraft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.BUY,
                effective_date=date(2026, 8, 28),
                instrument_reference="AAPL",
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
                draft_sha256="A" * 64,
            )

        # CA: Trailing newline
        with pytest.raises(PortfolioImportDraftError, match="64-character lowercase hex"):
            ImportTransactionDraft(
                assessment_batch=batch,
                record_ordinal=1,
                transaction_type=TransactionType.BUY,
                effective_date=date(2026, 8, 28),
                instrument_reference="AAPL",
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
                draft_sha256=("0" * 64) + "\n",
            )


# ─────────────────────────────────────────────────────────────────────────────
# 11. Assessment-Binding Matrix (CB-CE)
# ─────────────────────────────────────────────────────────────────────────────

class TestAssessmentBindingMatrix:
    """Verifies derived assessment property and draft_identity coupling."""

    def test_derived_assessment_exact_object_from_batch(self):
        """CB: draft.assessment returns exact object from assessment_batch."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        assert draft.assessment is batch.assessments[0]

    def test_draft_identity_structure(self):
        """CC: draft_identity is (assessment_manifest_sha256, record_ordinal, draft_sha256)."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        assert draft.draft_identity == (
            batch.assessment_manifest_sha256,
            1,
            draft.draft_sha256,
        )

    def test_different_batch_produces_different_draft_sha(self):
        """CD: Identical record economics drafted against different batches produce different draft_sha256."""
        batch1 = _make_test_assessment_batch([ImportAssessmentStatus.READY, ImportAssessmentStatus.UNRESOLVED])
        batch2 = _make_test_assessment_batch([ImportAssessmentStatus.READY, ImportAssessmentStatus.READY])

        d1 = build_import_transaction_draft(
            assessment_batch=batch1,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        d2 = build_import_transaction_draft(
            assessment_batch=batch2,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        assert d1.draft_sha256 != d2.draft_sha256


# ─────────────────────────────────────────────────────────────────────────────
# 12. Surface Red-Team (Forbidden Fields Check)
# ─────────────────────────────────────────────────────────────────────────────

class TestSurfaceRedTeam:
    """Verifies that ImportTransactionDraft contains zero ledger lifecycle or database state."""

    def test_no_forbidden_fields_in_draft_model(self):
        """48: Confirms draft model has only declared domain fields."""
        draft_fields = {f.name for f in fields(ImportTransactionDraft)}
        forbidden_fields = {
            "id",
            "transaction_id",
            "recorded_at",
            "instrument_id",
            "external_source",
            "external_reference",
            "idempotency_key",
            "cash_bucket_id",
            "reverses_transaction_id",
            "raw_record",
            "raw_bytes",
            "parser",
            "repository",
        }
        intersection = draft_fields.intersection(forbidden_fields)
        assert len(intersection) == 0, f"Found forbidden fields in draft model: {intersection}"

    def test_draft_immutability(self):
        """33: Frozen dataclass mutation fails closed."""
        batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        with pytest.raises(FrozenInstanceError):
            draft.quantity = Decimal("20")  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 13. Canonical CSV Real Pipeline Integration
# ─────────────────────────────────────────────────────────────────────────────

class TestCanonicalCsvRealPipelineIntegration:
    """Verifies real Canonical CSV ingestion through assessment to economic draft creation."""

    def test_canonical_csv_to_economic_draft(self):
        """49: Real Canonical CSV parser -> staging pipeline -> assessment batch -> draft."""
        parser = SentinaxCanonicalCsvParserV1()
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime(2026, 8, 28, 13, 30, tzinfo=timezone.utc)

        # 1-row CSV
        csv_content = b"symbol,quantity,price\nAAPL,10,150.25\n"

        staging_result = build_import_staging_result(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=csv_content,
            imported_at=t,
            parser=parser,
        )

        assert staging_result.parsed_manifest.record_count == 1
        p0 = staging_result.parsed_manifest.parsed_records[0]

        # Explicit Phase 13G assessment
        ass0 = build_import_record_assessment(p0, ImportAssessmentStatus.READY)
        batch = build_import_assessment_batch(
            parsed_manifest=staging_result.parsed_manifest,
            assessments=[ass0],
        )

        # Explicit economic draft
        draft = build_import_transaction_draft(
            assessment_batch=batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=date(2026, 8, 28),
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.25"),
            trade_currency=Currency.USD,
        )

        assert draft.record_ordinal == 1
        assert draft.assessment.status == ImportAssessmentStatus.READY
        assert draft.instrument_reference == "AAPL"
        assert draft.quantity == Decimal("10")
        assert draft.unit_price == Decimal("150.25")
        assert draft.trade_currency == Currency.USD
        assert len(draft.draft_sha256) == 64
