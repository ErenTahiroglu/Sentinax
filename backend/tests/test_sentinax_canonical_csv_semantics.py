"""
backend/tests/test_sentinax_canonical_csv_semantics.py
======================================================
Tests for Phase 13L: Sentinax Canonical CSV v1 Semantic Interpreter to Assessment & Economic Draft Batch.

Zero network calls (pytest-socket enforced).
Uses real SentinaxCanonicalCsvParserV1 and Phase 13A-13I staging builders.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional
from uuid import uuid4

import pytest

from backend.engine.private.domain import Currency, TransactionType
from backend.engine.private.portfolio.import_assessment import (
    ImportAssessmentBatch,
    ImportAssessmentStatus,
    build_import_assessment_batch,
    build_import_record_assessment,
)
from backend.engine.private.portfolio.import_batch import build_import_batch_manifest
from backend.engine.private.portfolio.import_draft import (
    ImportTransactionDraft,
    PortfolioImportDraftError,
    build_import_transaction_draft,
)
from backend.engine.private.portfolio.import_draft_batch import ImportDraftBatchManifest
from backend.engine.private.portfolio.import_parsed_batch import (
    ParsedImportBatchManifest,
    build_parsed_import_batch_manifest,
)
from backend.engine.private.portfolio.import_parsing import (
    ImportParsedField,
    ParsedImportRecord,
    build_parsed_import_record,
)
from backend.engine.private.portfolio.import_provenance import (
    build_import_file_provenance,
    build_import_record_provenance,
)
from backend.engine.private.portfolio.parsers.sentinax_csv import (
    SentinaxCanonicalCsvParserV1,
)
from backend.engine.private.portfolio.parsers.sentinax_csv_semantics import (
    SentinaxCanonicalCsvSemanticError,
    SentinaxCanonicalCsvSemanticInterpreterV1,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test Helpers
# ─────────────────────────────────────────────────────────────────────────────

CANONICAL_HEADERS = (
    "transaction_type,effective_date,executed_at,instrument_reference,"
    "quantity,unit_price,trade_currency,cash_amount,cash_currency,"
    "from_currency,from_amount,to_currency,to_amount"
)


from backend.engine.private.portfolio.import_pipeline import build_import_staging_result


def _parse_csv_bytes(csv_bytes: bytes) -> ParsedImportBatchManifest:
    """Helper to run real Phase 13F parser over CSV bytes and build verified ParsedImportBatchManifest."""
    staging = build_import_staging_result(
        portfolio_id=uuid4(),
        account_id=uuid4(),
        filename="test.csv",
        content=csv_bytes,
        imported_at=datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc),
        parser=SentinaxCanonicalCsvParserV1(),
    )
    return staging.parsed_manifest


def _make_single_row_csv(
    tx_type: str = "buy",
    eff_date: str = "2026-08-28",
    exec_at: str = "2026-08-28T10:15:30+00:00",
    inst_ref: str = "AAPL",
    qty: str = "10",
    price: str = "150.00",
    trade_curr: str = "USD",
    cash_amt: str = "",
    cash_curr: str = "",
    from_curr: str = "",
    from_amt: str = "",
    to_curr: str = "",
    to_amt: str = "",
) -> bytes:
    """Builds a single-row canonical CSV string in bytes."""
    row = f"{tx_type},{eff_date},{exec_at},{inst_ref},{qty},{price},{trade_curr},{cash_amt},{cash_curr},{from_curr},{from_amt},{to_curr},{to_amt}"
    return f"{CANONICAL_HEADERS}\n{row}\n".encode("utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Schema Matrix (A-G)
# ─────────────────────────────────────────────────────────────────────────────

class TestSchemaMatrix:
    """A-G: Batch-level schema verification."""

    def test_A_exact_13_field_schema_accepted(self):
        """A: Exact 13-field schema successfully interpreted."""
        csv_bytes = _make_single_row_csv()
        parsed = _parse_csv_bytes(csv_bytes)
        interpreter = SentinaxCanonicalCsvSemanticInterpreterV1()
        manifest = interpreter.interpret(parsed)
        assert isinstance(manifest, ImportDraftBatchManifest)
        assert manifest.draft_count == 1
        assert manifest.assessment_batch.ready_count == 1

    def test_B_arbitrary_original_column_order_accepted(self):
        """B: Header with permuted column order decodes into exact 13 keys and is accepted."""
        header = (
            "effective_date,transaction_type,executed_at,instrument_reference,"
            "quantity,unit_price,trade_currency,cash_amount,cash_currency,"
            "from_currency,from_amount,to_currency,to_amount"
        )
        row = (
            "2026-08-28,buy,2026-08-28T10:15:30+00:00,AAPL,"
            "10,150.00,USD,,,,,,"
        )
        csv_bytes = f"{header}\n{row}\n".encode("utf-8")
        parsed = _parse_csv_bytes(csv_bytes)
        interpreter = SentinaxCanonicalCsvSemanticInterpreterV1()
        manifest = interpreter.interpret(parsed)
        assert manifest.draft_count == 1

    def test_C_missing_one_semantic_field_raises_batch_error(self):
        """C: Missing one required column in parsed record fails at schema level."""
        csv_bytes = _make_single_row_csv()
        parsed = _parse_csv_bytes(csv_bytes)
        # Omit 'to_amount' from record fields
        rec = parsed.parsed_records[0]
        fields_12 = tuple(f for f in rec.fields if f.field_key != "to_amount")
        rec_12 = build_parsed_import_record(
            record_provenance=rec.record_provenance,
            raw_record=csv_bytes.splitlines()[1],
            parser_revision=1,
            fields=fields_12,
        )
        parsed_12 = build_parsed_import_batch_manifest(parsed.raw_manifest, 1, [rec_12])
        interpreter = SentinaxCanonicalCsvSemanticInterpreterV1()
        with pytest.raises(SentinaxCanonicalCsvSemanticError, match="exact 13 canonical field keys"):
            interpreter.interpret(parsed_12)

    def test_D_extra_field_raises_batch_error(self):
        """D: Extra unexpected column in parsed record fails at schema level."""
        csv_bytes = _make_single_row_csv()
        parsed = _parse_csv_bytes(csv_bytes)
        rec = parsed.parsed_records[0]
        fields_14 = tuple(sorted(list(rec.fields) + [ImportParsedField("notes", "extra")], key=lambda f: f.field_key))
        rec_14 = build_parsed_import_record(
            record_provenance=rec.record_provenance,
            raw_record=csv_bytes.splitlines()[1],
            parser_revision=1,
            fields=fields_14,
        )
        parsed_14 = build_parsed_import_batch_manifest(parsed.raw_manifest, 1, [rec_14])
        interpreter = SentinaxCanonicalCsvSemanticInterpreterV1()
        with pytest.raises(SentinaxCanonicalCsvSemanticError, match="exact 13 canonical field keys"):
            interpreter.interpret(parsed_14)

    def test_E_wrong_source_key_raises_batch_error(self):
        """E: Parsed manifest with mismatched source_key fails closed."""
        class OtherBrokerParser(SentinaxCanonicalCsvParserV1):
            @property
            def source_key(self) -> str:
                return "other_broker"

        staging = build_import_staging_result(
            portfolio_id=uuid4(),
            account_id=uuid4(),
            filename="test.csv",
            content=_make_single_row_csv(),
            imported_at=datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc),
            parser=OtherBrokerParser(),
        )
        interpreter = SentinaxCanonicalCsvSemanticInterpreterV1()
        with pytest.raises(SentinaxCanonicalCsvSemanticError, match="source_key"):
            interpreter.interpret(staging.parsed_manifest)

    def test_F_wrong_parser_revision_raises_batch_error(self):
        """F: Parsed manifest with parser_revision != 1 fails closed."""
        class Revision2Parser(SentinaxCanonicalCsvParserV1):
            @property
            def parser_revision(self) -> int:
                return 2

        staging = build_import_staging_result(
            portfolio_id=uuid4(),
            account_id=uuid4(),
            filename="test.csv",
            content=_make_single_row_csv(),
            imported_at=datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc),
            parser=Revision2Parser(),
        )
        interpreter = SentinaxCanonicalCsvSemanticInterpreterV1()
        with pytest.raises(SentinaxCanonicalCsvSemanticError, match="parser_revision"):
            interpreter.interpret(staging.parsed_manifest)

    def test_G_empty_parsed_batch_valid(self):
        """G: Empty CSV with valid header yields empty draft batch."""
        csv_bytes = f"{CANONICAL_HEADERS}\n".encode("utf-8")
        parsed = _parse_csv_bytes(csv_bytes)
        interpreter = SentinaxCanonicalCsvSemanticInterpreterV1()
        manifest = interpreter.interpret(parsed)
        assert manifest.draft_count == 0
        assert manifest.assessment_batch.record_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# 2. Transaction Type Matrix (H-T)
# ─────────────────────────────────────────────────────────────────────────────

class TestTransactionTypeMatrix:
    """H-T: Transaction type mapping and rejection."""

    def test_H_buy_accepted(self):
        csv = _make_single_row_csv(tx_type="buy")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].transaction_type == TransactionType.BUY

    def test_I_sell_accepted(self):
        csv = _make_single_row_csv(tx_type="sell")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].transaction_type == TransactionType.SELL

    def test_J_cash_deposit_accepted(self):
        csv = _make_single_row_csv(
            tx_type="cash_deposit", inst_ref="", qty="", price="", trade_curr="",
            cash_amt="500.00", cash_curr="USD"
        )
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].transaction_type == TransactionType.CASH_DEPOSIT

    def test_K_cash_withdrawal_accepted(self):
        csv = _make_single_row_csv(
            tx_type="cash_withdrawal", inst_ref="", qty="", price="", trade_curr="",
            cash_amt="200.00", cash_curr="USD"
        )
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].transaction_type == TransactionType.CASH_WITHDRAWAL

    def test_L_dividend_accepted(self):
        csv = _make_single_row_csv(
            tx_type="dividend", inst_ref="AAPL", qty="", price="", trade_curr="",
            cash_amt="50.00", cash_curr="USD"
        )
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].transaction_type == TransactionType.DIVIDEND

    def test_M_interest_accepted(self):
        csv = _make_single_row_csv(
            tx_type="interest", inst_ref="", qty="", price="", trade_curr="",
            cash_amt="12.50", cash_curr="USD"
        )
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].transaction_type == TransactionType.INTEREST

    def test_N_fee_accepted(self):
        csv = _make_single_row_csv(
            tx_type="fee", inst_ref="", qty="", price="", trade_curr="",
            cash_amt="5.00", cash_curr="USD"
        )
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].transaction_type == TransactionType.FEE

    def test_O_tax_withholding_accepted(self):
        csv = _make_single_row_csv(
            tx_type="tax_withholding", inst_ref="", qty="", price="", trade_curr="",
            cash_amt="7.50", cash_curr="USD"
        )
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].transaction_type == TransactionType.TAX_WITHHOLDING

    def test_P_fx_conversion_accepted(self):
        csv = _make_single_row_csv(
            tx_type="fx_conversion", inst_ref="", qty="", price="", trade_curr="",
            from_curr="USD", from_amt="100.00", to_curr="TRY", to_amt="3200.00"
        )
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].transaction_type == TransactionType.FX_CONVERSION

    def test_Q_reversal_rejected_row_level(self):
        csv = _make_single_row_csv(tx_type="reversal")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        ass = m.assessment_batch.assessments[0]
        assert ass.status == ImportAssessmentStatus.REJECTED
        assert ass.diagnostics[0].code == "invalid_transaction_type"

    def test_R_BUY_uppercase_rejected(self):
        csv = _make_single_row_csv(tx_type="BUY")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_transaction_type"

    def test_S_trailing_space_buy_rejected(self):
        csv = _make_single_row_csv(tx_type="buy ")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_transaction_type"

    def test_T_empty_transaction_type_rejected(self):
        csv = _make_single_row_csv(tx_type="")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_transaction_type"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Effective Date Matrix (U-AB)
# ─────────────────────────────────────────────────────────────────────────────

class TestEffectiveDateMatrix:
    """U-AB: Effective date parsing and format rules."""

    def test_U_canonical_date_accepted(self):
        csv = _make_single_row_csv(eff_date="2026-08-28")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].effective_date == date(2026, 8, 28)

    def test_V_leap_date_accepted(self):
        csv = _make_single_row_csv(eff_date="2024-02-29")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].effective_date == date(2024, 2, 29)

    def test_W_invalid_calendar_date_rejected(self):
        csv = _make_single_row_csv(eff_date="2026-02-29")  # not a leap year
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_effective_date"

    def test_X_non_zero_padded_date_rejected(self):
        csv = _make_single_row_csv(eff_date="2026-8-28")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_effective_date"

    def test_Y_slash_format_rejected(self):
        csv = _make_single_row_csv(eff_date="28/08/2026")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_effective_date"

    def test_Z_trailing_space_rejected(self):
        csv = _make_single_row_csv(eff_date="2026-08-28 ")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_effective_date"

    def test_AA_empty_date_rejected(self):
        csv = _make_single_row_csv(eff_date="")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_effective_date"

    def test_AB_datetime_text_rejected(self):
        csv = _make_single_row_csv(eff_date="2026-08-28T00:00:00")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_effective_date"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Executed_at Matrix (AC-AK)
# ─────────────────────────────────────────────────────────────────────────────

class TestExecutedAtMatrix:
    """AC-AK: Executed_at timestamp parsing and timezone awareness."""

    def test_AC_empty_executed_at_maps_to_none(self):
        csv = _make_single_row_csv(exec_at="")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].executed_at is None

    def test_AD_utc_offset_accepted(self):
        csv = _make_single_row_csv(exec_at="2026-08-28T10:15:30+00:00")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].executed_at == datetime(2026, 8, 28, 10, 15, 30, tzinfo=timezone.utc)

    def test_AE_positive_offset_accepted(self):
        csv = _make_single_row_csv(exec_at="2026-08-28T13:15:30+03:00")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].executed_at is not None
        assert m.drafts[0].executed_at.utcoffset().total_seconds() == 3 * 3600

    def test_AF_microseconds_accepted(self):
        csv = _make_single_row_csv(exec_at="2026-08-28T10:15:30.123456+00:00")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].executed_at.microsecond == 123456

    def test_AG_naive_datetime_rejected(self):
        csv = _make_single_row_csv(exec_at="2026-08-28T10:15:30")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_executed_at"

    def test_AH_z_suffix_rejected(self):
        csv = _make_single_row_csv(exec_at="2026-08-28T10:15:30Z")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_executed_at"

    def test_AI_space_separator_rejected(self):
        csv = _make_single_row_csv(exec_at="2026-08-28 10:15:30+00:00")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_executed_at"

    def test_AJ_missing_seconds_rejected(self):
        csv = _make_single_row_csv(exec_at="2026-08-28T10:15+00:00")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_executed_at"

    def test_AK_trailing_whitespace_rejected(self):
        csv = _make_single_row_csv(exec_at="2026-08-28T10:15:30+00:00 ")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_executed_at"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Decimal Matrix (AL-BA)
# ─────────────────────────────────────────────────────────────────────────────

class TestDecimalMatrix:
    """AL-BA: Decimal lexical parsing and precision preservation."""

    def test_AL_empty_decimal_maps_to_none(self):
        csv = _make_single_row_csv(
            tx_type="cash_deposit", inst_ref="", qty="", price="", trade_curr="",
            cash_amt="100", cash_curr="USD"
        )
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].quantity is None

    def test_AM_zero_lexical_parse_accepted(self):
        # 0 parses lexically, but will fail Phase 13H economic positivity for BUY quantity
        csv = _make_single_row_csv(qty="0")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        # Fails at economic contract stage, not lexical decimal syntax stage!
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_economic_contract"

    def test_AN_integer_decimal_accepted(self):
        csv = _make_single_row_csv(qty="10")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].quantity == Decimal("10")

    def test_AO_fractional_decimal_accepted(self):
        csv = _make_single_row_csv(qty="10.505")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].quantity == Decimal("10.505")

    def test_AP_arbitrary_precision_preserved(self):
        prec_str = "0.123456789012345678901234567890"
        csv = _make_single_row_csv(qty=prec_str)
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].quantity == Decimal(prec_str)

    def test_AQ_leading_zero_integer_rejected(self):
        csv = _make_single_row_csv(qty="01")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_decimal"

    def test_AR_plus_sign_rejected(self):
        csv = _make_single_row_csv(qty="+1")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_decimal"

    def test_AS_minus_sign_rejected(self):
        csv = _make_single_row_csv(qty="-1")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_decimal"

    def test_AT_exponent_rejected(self):
        csv = _make_single_row_csv(qty="1e3")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_decimal"

    def test_AU_comma_thousands_rejected(self):
        csv = _make_single_row_csv(qty='"1,000"')
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_decimal"

    def test_AV_leading_whitespace_rejected(self):
        csv = _make_single_row_csv(qty=" 10")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_decimal"

    def test_AW_trailing_whitespace_rejected(self):
        csv = _make_single_row_csv(qty="10 ")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_decimal"

    def test_AX_missing_leading_zero_rejected(self):
        csv = _make_single_row_csv(qty=".5")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_decimal"

    def test_AY_trailing_dot_rejected(self):
        csv = _make_single_row_csv(qty="1.")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_decimal"

    def test_AZ_nan_rejected(self):
        csv = _make_single_row_csv(qty="NaN")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_decimal"

    def test_BA_infinity_rejected(self):
        csv = _make_single_row_csv(qty="Infinity")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_decimal"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Currency Matrix (BB-BH)
# ─────────────────────────────────────────────────────────────────────────────

class TestCurrencyMatrix:
    """BB-BH: Currency code parsing and validation."""

    @pytest.mark.parametrize("curr", ["TRY", "USD", "EUR", "GBP", "XAU", "XAG"])
    def test_BB_canonical_currencies_accepted(self, curr: str):
        csv = _make_single_row_csv(trade_curr=curr)
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].trade_currency == Currency[curr]

    def test_BC_empty_currency_maps_to_none(self):
        csv = _make_single_row_csv(
            tx_type="cash_deposit", inst_ref="", qty="", price="", trade_curr="",
            cash_amt="100", cash_curr="USD"
        )
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].trade_currency is None

    def test_BD_lowercase_currency_rejected(self):
        csv = _make_single_row_csv(trade_curr="usd")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_currency"

    def test_BE_alias_tl_rejected(self):
        csv = _make_single_row_csv(trade_curr="TL")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_currency"

    def test_BF_symbol_dollar_rejected(self):
        csv = _make_single_row_csv(trade_curr="$")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_currency"

    def test_BG_trailing_whitespace_currency_rejected(self):
        csv = _make_single_row_csv(trade_curr="USD ")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_currency"

    def test_BH_unsupported_currency_rejected(self):
        csv = _make_single_row_csv(trade_curr="JPY")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_currency"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Instrument Reference Matrix (BI-BN)
# ─────────────────────────────────────────────────────────────────────────────

class TestInstrumentReferenceMatrix:
    """BI-BN: Instrument reference preservation and whitespace handling."""

    def test_BI_empty_instrument_reference_maps_to_none(self):
        csv = _make_single_row_csv(
            tx_type="cash_deposit", inst_ref="", qty="", price="", trade_curr="",
            cash_amt="100", cash_curr="USD"
        )
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].instrument_reference is None

    def test_BJ_aapl_preserved(self):
        csv = _make_single_row_csv(inst_ref="AAPL")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].instrument_reference == "AAPL"

    def test_BK_altin_s1_preserved(self):
        csv = _make_single_row_csv(inst_ref="ALTIN.S1")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].instrument_reference == "ALTIN.S1"

    def test_BL_unicode_reference_preserved(self):
        csv = _make_single_row_csv(inst_ref="İŞCTR")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].instrument_reference == "İŞCTR"

    def test_BM_leading_trailing_spaces_preserved(self):
        raw_ref = "  AAPL  "
        csv = _make_single_row_csv(inst_ref=raw_ref)
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.drafts[0].instrument_reference == raw_ref

    def test_BN_whitespace_only_reference_rejected_by_phase13h(self):
        # A whitespace-only string passes lexical parse (it's not empty string ""),
        # but fails Phase 13H economic contract (instrument reference cannot be whitespace only)
        csv = _make_single_row_csv(inst_ref="   ")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        ass = m.assessment_batch.assessments[0]
        assert ass.status == ImportAssessmentStatus.REJECTED
        assert ass.diagnostics[0].code == "invalid_economic_contract"


# ─────────────────────────────────────────────────────────────────────────────
# 8. Lexical Multi-Diagnostic Matrix (Section 61)
# ─────────────────────────────────────────────────────────────────────────────

class TestLexicalMultiDiagnosticMatrix:
    """Section 61: Multiple lexical failures on a single row."""

    def test_multiple_lexical_failures_all_reported(self):
        """Four lexical errors on one row are all captured in canonical order."""
        csv = _make_single_row_csv(
            tx_type="invalid_type",
            eff_date="2026-8-28",
            qty="invalid_qty",
            trade_curr="invalid_curr",
        )
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        ass = m.assessment_batch.assessments[0]
        assert ass.status == ImportAssessmentStatus.REJECTED
        assert len(ass.diagnostics) == 4
        codes = {d.code for d in ass.diagnostics}
        assert codes == {
            "invalid_transaction_type",
            "invalid_effective_date",
            "invalid_decimal",
            "invalid_currency",
        }


# ─────────────────────────────────────────────────────────────────────────────
# 9. Economic Contract Matrix (BO-BY) (Section 62)
# ─────────────────────────────────────────────────────────────────────────────

class TestEconomicContractMatrix:
    """BO-BY: Phase 13H economic validation pass."""

    def test_BO_buy_full_valid_trade_family_ready(self):
        csv = _make_single_row_csv(tx_type="buy")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 1

    def test_BP_buy_missing_quantity_rejected(self):
        csv = _make_single_row_csv(tx_type="buy", qty="")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        ass = m.assessment_batch.assessments[0]
        assert ass.status == ImportAssessmentStatus.REJECTED
        assert ass.diagnostics[0].code == "invalid_economic_contract"
        assert ass.diagnostics[0].field_key is None

    def test_BQ_buy_with_cash_field_rejected(self):
        csv = _make_single_row_csv(tx_type="buy", cash_amt="100.00", cash_curr="USD")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_economic_contract"

    def test_BR_cash_deposit_with_amount_currency_ready(self):
        csv = _make_single_row_csv(
            tx_type="cash_deposit", inst_ref="", qty="", price="", trade_curr="",
            cash_amt="500.00", cash_curr="USD"
        )
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 1

    def test_BS_cash_deposit_with_instrument_reference_rejected(self):
        csv = _make_single_row_csv(
            tx_type="cash_deposit", inst_ref="AAPL", qty="", price="", trade_curr="",
            cash_amt="500.00", cash_curr="USD"
        )
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_economic_contract"

    def test_BT_dividend_without_instrument_ready(self):
        csv = _make_single_row_csv(
            tx_type="dividend", inst_ref="", qty="", price="", trade_curr="",
            cash_amt="50.00", cash_curr="USD"
        )
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 1

    def test_BU_dividend_with_instrument_ready(self):
        csv = _make_single_row_csv(
            tx_type="dividend", inst_ref="AAPL", qty="", price="", trade_curr="",
            cash_amt="50.00", cash_curr="USD"
        )
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 1

    def test_BV_fx_conversion_distinct_currencies_ready(self):
        csv = _make_single_row_csv(
            tx_type="fx_conversion", inst_ref="", qty="", price="", trade_curr="",
            from_curr="USD", from_amt="100.00", to_curr="TRY", to_amt="3200.00"
        )
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 1

    def test_BW_fx_conversion_same_currencies_rejected(self):
        csv = _make_single_row_csv(
            tx_type="fx_conversion", inst_ref="", qty="", price="", trade_curr="",
            from_curr="USD", from_amt="100.00", to_curr="USD", to_amt="100.00"
        )
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_economic_contract"

    def test_BX_fx_conversion_missing_to_amount_rejected(self):
        csv = _make_single_row_csv(
            tx_type="fx_conversion", inst_ref="", qty="", price="", trade_curr="",
            from_curr="USD", from_amt="100.00", to_curr="TRY", to_amt=""
        )
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        assert m.draft_count == 0
        assert m.assessment_batch.assessments[0].diagnostics[0].code == "invalid_economic_contract"


# ─────────────────────────────────────────────────────────────────────────────
# 10. Rejection Diagnostic Matrix (BZ-CD) (Section 63)
# ─────────────────────────────────────────────────────────────────────────────

class TestRejectionDiagnosticMatrix:
    """BZ-CD: Diagnostic field binding and generic messaging."""

    def test_BZ_lexical_diagnostics_bound_to_field(self):
        csv = _make_single_row_csv(qty="bad_qty")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        diag = m.assessment_batch.assessments[0].diagnostics[0]
        assert diag.field_key == "quantity"

    def test_CA_economic_diagnostic_has_none_field_key(self):
        csv = _make_single_row_csv(tx_type="buy", qty="")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        diag = m.assessment_batch.assessments[0].diagnostics[0]
        assert diag.field_key is None

    def test_CB_economic_diagnostic_message_exact_and_generic(self):
        csv = _make_single_row_csv(tx_type="buy", qty="")
        m = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        diag = m.assessment_batch.assessments[0].diagnostics[0]
        assert diag.message == "Parsed row violates the canonical economic transaction draft contract."


# ─────────────────────────────────────────────────────────────────────────────
# 11. Mixed Batch Matrix (Section 64)
# ─────────────────────────────────────────────────────────────────────────────

class TestMixedBatchMatrix:
    """Section 64: Five-row mixed scenario."""

    def test_five_row_mixed_scenario(self):
        """
        1 valid BUY
        2 invalid decimal (qty="bad")
        3 valid CASH_DEPOSIT
        4 economically contradictory FX (same currencies)
        5 valid DIVIDEND
        """
        rows = [
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,MSFT,bad_qty,300.00,USD,,,,,,",
            "cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,500.00,USD,,,,",
            "fx_conversion,2026-08-28,2026-08-28T10:15:30+00:00,,,,,,,USD,100.00,USD,100.00",
            "dividend,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,,,,50.00,USD,,,,",
        ]
        csv_bytes = f"{CANONICAL_HEADERS}\n" + "\n".join(rows) + "\n"
        parsed = _parse_csv_bytes(csv_bytes.encode("utf-8"))

        manifest = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(parsed)

        assert manifest.assessment_batch.record_count == 5
        assert manifest.assessment_batch.ready_count == 3
        assert manifest.assessment_batch.rejected_count == 2
        assert manifest.assessment_batch.unresolved_count == 0

        assert manifest.draft_count == 3
        assert [d.record_ordinal for d in manifest.drafts] == [1, 3, 5]

        # Statuses:
        assert manifest.assessment_batch.assessments[0].status == ImportAssessmentStatus.READY
        assert manifest.assessment_batch.assessments[1].status == ImportAssessmentStatus.REJECTED
        assert manifest.assessment_batch.assessments[2].status == ImportAssessmentStatus.READY
        assert manifest.assessment_batch.assessments[3].status == ImportAssessmentStatus.REJECTED
        assert manifest.assessment_batch.assessments[4].status == ImportAssessmentStatus.READY


# ─────────────────────────────────────────────────────────────────────────────
# 12. Regressions & Invariants (Sections 65-74)
# ─────────────────────────────────────────────────────────────────────────────

class TestRegressionsAndInvariants:
    """Sections 65-74: Structural, precision, and architectural regressions."""

    def test_provisional_batch_discarded_and_final_drafts_bound_to_final_batch(self):
        """Section 65: All final drafts are bound to the final assessment batch."""
        csv = _make_single_row_csv()
        manifest = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        for draft in manifest.drafts:
            assert draft.assessment_batch is manifest.assessment_batch

    def test_economic_validation_uses_real_build_import_transaction_draft(self, monkeypatch):
        """Section 66: Economic contract validation delegates to build_import_transaction_draft."""
        import backend.engine.private.portfolio.parsers.sentinax_csv_semantics as mod

        call_count = 0
        real_builder = mod.build_import_transaction_draft

        def spy_builder(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return real_builder(*args, **kwargs)

        monkeypatch.setattr(mod, "build_import_transaction_draft", spy_builder)

        csv = _make_single_row_csv()
        manifest = SentinaxCanonicalCsvSemanticInterpreterV1().interpret(_parse_csv_bytes(csv))
        # 1 call in pass 2 (provisional) + 1 call in pass 3 (final) = 2 calls
        assert call_count == 2
        assert manifest.draft_count == 1

    def test_deterministic_interpretation(self):
        """Section 70: Repeated interpretations produce identical manifest SHAs."""
        csv = _make_single_row_csv()
        parsed = _parse_csv_bytes(csv)
        interpreter = SentinaxCanonicalCsvSemanticInterpreterV1()
        m1 = interpreter.interpret(parsed)
        m2 = interpreter.interpret(parsed)
        assert m1.draft_manifest_sha256 == m2.draft_manifest_sha256
        assert m1.assessment_batch.assessment_manifest_sha256 == m2.assessment_batch.assessment_manifest_sha256

    def test_statelessness(self):
        """Section 72: Repeated calls on same interpreter instance are independent."""
        interpreter = SentinaxCanonicalCsvSemanticInterpreterV1()
        csv1 = _make_single_row_csv(inst_ref="AAPL")
        csv2 = _make_single_row_csv(inst_ref="MSFT")
        m1 = interpreter.interpret(_parse_csv_bytes(csv1))
        m2 = interpreter.interpret(_parse_csv_bytes(csv2))
        assert m1.drafts[0].instrument_reference == "AAPL"
        assert m2.drafts[0].instrument_reference == "MSFT"


# ─────────────────────────────────────────────────────────────────────────────
# 13. Phase 13L.1 Fixed-Metadata Immutability Hardening
# ─────────────────────────────────────────────────────────────────────────────

class TestFixedMetadataImmutabilityHardening:
    """Tests for Phase 13L.1 fixed metadata immutability and bypass prevention."""

    def test_metadata_attributes_immutable(self):
        """Section 10: Mutating source_key, parser_revision, or semantic_revision raises AttributeError."""
        interpreter = SentinaxCanonicalCsvSemanticInterpreterV1()

        with pytest.raises(AttributeError):
            interpreter.source_key = "evil_source"  # type: ignore

        with pytest.raises(AttributeError):
            interpreter.parser_revision = 999  # type: ignore

        with pytest.raises(AttributeError):
            interpreter.semantic_revision = 999  # type: ignore

        # Verify values remain unchanged
        assert interpreter.source_key == "sentinax_csv"
        assert interpreter.parser_revision == 1
        assert interpreter.semantic_revision == 1

    def test_slots_prevents_instance_dict(self):
        """Section 8: Interpreter has __slots__ = () and no mutable __dict__."""
        interpreter = SentinaxCanonicalCsvSemanticInterpreterV1()
        assert not hasattr(interpreter, "__dict__")

    def test_foreign_source_bypass_red_team(self):
        """Section 11: Foreign source cannot bypass interpreter gate."""
        interpreter = SentinaxCanonicalCsvSemanticInterpreterV1()

        class ForeignSourceParser(SentinaxCanonicalCsvParserV1):
            @property
            def source_key(self) -> str:
                return "foreign_source"

        staging = build_import_staging_result(
            portfolio_id=uuid4(),
            account_id=uuid4(),
            filename="test.csv",
            content=_make_single_row_csv(),
            imported_at=datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc),
            parser=ForeignSourceParser(),
        )

        with pytest.raises(AttributeError):
            interpreter.source_key = "foreign_source"  # type: ignore

        with pytest.raises(SentinaxCanonicalCsvSemanticError, match="source_key"):
            interpreter.interpret(staging.parsed_manifest)

    def test_parser_revision_2_bypass_red_team(self):
        """Section 12: Parser revision 2 cannot bypass interpreter gate."""
        interpreter = SentinaxCanonicalCsvSemanticInterpreterV1()

        class Revision2Parser(SentinaxCanonicalCsvParserV1):
            @property
            def parser_revision(self) -> int:
                return 2

        staging = build_import_staging_result(
            portfolio_id=uuid4(),
            account_id=uuid4(),
            filename="test.csv",
            content=_make_single_row_csv(),
            imported_at=datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc),
            parser=Revision2Parser(),
        )

        with pytest.raises(AttributeError):
            interpreter.parser_revision = 2  # type: ignore

        with pytest.raises(SentinaxCanonicalCsvSemanticError, match="parser_revision"):
            interpreter.interpret(staging.parsed_manifest)

    def test_class_contract_defaults(self):
        """Section 13: Newly created interpreter always has exact fixed metadata."""
        interpreter = SentinaxCanonicalCsvSemanticInterpreterV1()
        assert interpreter.source_key == "sentinax_csv"
        assert interpreter.parser_revision == 1
        assert interpreter.semantic_revision == 1
