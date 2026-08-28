"""
backend/tests/test_sentinax_canonical_csv_parser.py
===================================================
Tests for Phase 13F: Sentinax Canonical CSV v1 Source Parser Adapter.

Zero network calls (pytest-socket enforced).
Pure in-memory domain evaluation testing parser directly and through the Phase 13E pipeline.

Test Matrix:
    1. Basic Parser Contract (source_key, parser_revision, header-only, single/multi-row, trailing newlines, tuple type, determinism)
    2. Byte Input & Encoding Defense (str, bytearray, memoryview, empty bytes, invalid UTF-8, BOM, NUL, >50 MiB)
    3. Newline & Physical Line Handling (LF, CRLF, mixed LF/CRLF rejection, bare CR rejection, blank lines, multiline quotes)
    4. Header Contract & Key Grammar (Single, multi, duplicate, uppercase, spaces, empty, digits, Unicode, max length, >128 cols)
    5. Row Structure & Cardinality (Exact columns, missing columns, extra columns, all-empty cells, empty string field values)
    6. CSV Quoting & Syntax (Quoted commas, escaped quotes, unclosed quotes, malformed syntax, raw byte differentiation)
    7. Whitespace Preservation (Leading, trailing, whitespace-only, quoted whitespace without stripping)
    8. Raw Byte Slice Authority (Exact source byte slices minus LF/CRLF, no re-encoding, UTF-8 non-ASCII preserved, quote syntax preserved)
    9. Ordering & Duplicate Preservation (Source order preserved, duplicate rows preserved without deduplication)
    10. Defensive Limits (MAX_COLUMNS, MAX_RECORD_BYTES, MAX_DATA_RECORDS)
    11. Phase 13E Pipeline Integration (Header-only, multi-row, metadata flow, SHA bindings, deterministic manifests)
    12. LF vs. CRLF Pipeline Sensitivity (File SHA, file identity, raw manifest identity, parsed manifest identity)
    13. Financial Semantic Separation (No TransactionType, Decimal, currency, or instrument resolution)
"""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
import hashlib
from typing import Sequence, Tuple
from uuid import uuid4

import pytest

from backend.engine.private.portfolio.import_parsing import ImportParsedField
from backend.engine.private.portfolio.import_pipeline import (
    ExtractedImportRecord,
    ImportStagingResult,
    build_import_staging_result,
)
from backend.engine.private.portfolio.parsers import (
    SentinaxCanonicalCsvError,
    SentinaxCanonicalCsvParserV1,
)
from backend.engine.private.portfolio.parsers.sentinax_csv import (
    MAX_COLUMNS,
    MAX_CONTENT_BYTES,
    MAX_DATA_RECORDS,
    MAX_RECORD_BYTES,
    _scan_physical_lines,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Basic Parser Contract Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBasicParserContract:
    """Verifies baseline properties, source key, revision, and valid canonical CSV extractions."""

    def test_source_key_and_parser_revision(self):
        """A, B: Parser exposes fixed source_key='sentinax_csv' and parser_revision=1."""
        parser = SentinaxCanonicalCsvParserV1()
        assert parser.source_key == "sentinax_csv"
        assert parser.parser_revision == 1

    def test_valid_lf_header_only(self):
        """C: Valid LF header-only file returns empty tuple."""
        parser = SentinaxCanonicalCsvParserV1()
        records = parser.extract_records(b"symbol,quantity\n")
        assert records == ()
        assert isinstance(records, tuple)

    def test_valid_crlf_header_only(self):
        """D: Valid CRLF header-only file returns empty tuple."""
        parser = SentinaxCanonicalCsvParserV1()
        records = parser.extract_records(b"symbol,quantity\r\n")
        assert records == ()
        assert isinstance(records, tuple)

    def test_one_data_row(self):
        """E: Single data row returns tuple with 1 ExtractedImportRecord."""
        parser = SentinaxCanonicalCsvParserV1()
        content = b"symbol,quantity\nAAPL,10\n"
        records = parser.extract_records(content)

        assert len(records) == 1
        assert isinstance(records[0], ExtractedImportRecord)
        assert records[0].raw_record == b"AAPL,10"
        assert records[0].fields == (
            ImportParsedField("quantity", "10"),
            ImportParsedField("symbol", "AAPL"),
        )

    def test_multiple_rows(self):
        """F: Multiple data rows return exact record sequence in order."""
        parser = SentinaxCanonicalCsvParserV1()
        content = b"symbol,quantity\nAAPL,10\nMSFT,20\nGOOG,30\n"
        records = parser.extract_records(content)

        assert len(records) == 3
        assert records[0].raw_record == b"AAPL,10"
        assert records[1].raw_record == b"MSFT,20"
        assert records[2].raw_record == b"GOOG,30"

    def test_final_newline_optional(self):
        """G: Final line without trailing newline produces identical records."""
        parser = SentinaxCanonicalCsvParserV1()
        with_nl = parser.extract_records(b"symbol,quantity\nAAPL,10\n")
        without_nl = parser.extract_records(b"symbol,quantity\nAAPL,10")

        assert len(with_nl) == 1
        assert len(without_nl) == 1
        assert with_nl[0].raw_record == without_nl[0].raw_record
        assert with_nl[0].fields == without_nl[0].fields

    def test_output_is_tuple(self):
        """H: Output is strictly an immutable tuple."""
        parser = SentinaxCanonicalCsvParserV1()
        records = parser.extract_records(b"symbol\nAAPL\n")
        assert type(records) is tuple

    def test_deterministic_repeated_parse(self):
        """I: Repeated calls with identical content return identical results."""
        parser = SentinaxCanonicalCsvParserV1()
        content = b"symbol,price\nAAPL,150.25\n"
        r1 = parser.extract_records(content)
        r2 = parser.extract_records(content)
        assert r1 == r2


# ─────────────────────────────────────────────────────────────────────────────
# 2. Byte Input & Encoding Defense Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestByteInputAndEncodingDefense:
    """Verifies strict immutable bytes, size limit, UTF-8 strictness, BOM, and NUL defenses."""

    def test_non_bytes_input_rejected(self):
        """J, K, L: String, bytearray, memoryview, and other types are rejected."""
        parser = SentinaxCanonicalCsvParserV1()

        for bad_input in (
            "symbol,qty\nAAPL,10\n",      # J: str
            bytearray(b"symbol,qty\n"),     # K: bytearray
            memoryview(b"symbol,qty\n"),    # L: memoryview
            123,
            None,
            object(),
        ):
            with pytest.raises(SentinaxCanonicalCsvError, match="immutable bytes"):
                parser.extract_records(bad_input)  # type: ignore

    def test_empty_bytes_rejected(self):
        """M: Empty bytes payload is rejected."""
        parser = SentinaxCanonicalCsvParserV1()
        with pytest.raises(SentinaxCanonicalCsvError, match="content must not be empty"):
            parser.extract_records(b"")

    def test_invalid_utf8_rejected(self):
        """N: Invalid UTF-8 byte sequences fail closed."""
        parser = SentinaxCanonicalCsvParserV1()
        bad_utf8 = b"symbol,name\nAAPL,\xff\xfe\xfd\n"
        with pytest.raises(SentinaxCanonicalCsvError, match="Malformed UTF-8"):
            parser.extract_records(bad_utf8)

    def test_utf8_bom_rejected(self):
        """O: UTF-8 BOM prefix is rejected (never silently stripped)."""
        parser = SentinaxCanonicalCsvParserV1()
        bom_content = b"\xef\xbb\xbfsymbol,quantity\nAAPL,10\n"
        with pytest.raises(SentinaxCanonicalCsvError, match="UTF-8 BOM is not allowed"):
            parser.extract_records(bom_content)

    def test_nul_byte_rejected(self):
        """P: NUL byte (\x00) anywhere in content is rejected."""
        parser = SentinaxCanonicalCsvParserV1()
        nul_content = b"symbol,quantity\nAA\x00PL,10\n"
        with pytest.raises(SentinaxCanonicalCsvError, match="NUL bytes are not allowed"):
            parser.extract_records(nul_content)

    def test_content_exceeding_max_bytes_rejected(self):
        """Q: Content larger than MAX_CONTENT_BYTES (50 MiB) fails closed."""
        parser = SentinaxCanonicalCsvParserV1()
        oversized = b"a\n" + b"x" * (MAX_CONTENT_BYTES + 1)
        with pytest.raises(SentinaxCanonicalCsvError, match="exceeds maximum size limit"):
            parser.extract_records(oversized)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Newline & Physical Line Handling Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestNewlineAndPhysicalLineHandling:
    """Verifies LF/CRLF uniform support, mixed newline rejection, bare CR rejection, and blank line rejection."""

    def test_all_lf_accepted(self):
        """R: File with consistent LF line terminators succeeds."""
        parser = SentinaxCanonicalCsvParserV1()
        records = parser.extract_records(b"symbol,quantity\nAAPL,10\nMSFT,20\n")
        assert len(records) == 2

    def test_all_crlf_accepted(self):
        """S: File with consistent CRLF line terminators succeeds."""
        parser = SentinaxCanonicalCsvParserV1()
        records = parser.extract_records(b"symbol,quantity\r\nAAPL,10\r\nMSFT,20\r\n")
        assert len(records) == 2

    def test_mixed_lf_and_crlf_rejected(self):
        """T: Mixed LF and CRLF in the same file fails closed."""
        parser = SentinaxCanonicalCsvParserV1()
        mixed_content = b"symbol,quantity\r\nAAPL,10\nMSFT,20\r\n"
        with pytest.raises(SentinaxCanonicalCsvError, match="Mixed newline styles"):
            parser.extract_records(mixed_content)

    def test_bare_cr_rejected(self):
        """U: Bare CR (\\r not followed by \\n) fails closed."""
        parser = SentinaxCanonicalCsvParserV1()
        bare_cr = b"symbol,quantity\rAAPL,10\n"
        with pytest.raises(SentinaxCanonicalCsvError, match="Bare CR"):
            parser.extract_records(bare_cr)

    def test_blank_physical_middle_row_rejected(self):
        """V: Blank physical line in the middle fails closed."""
        parser = SentinaxCanonicalCsvParserV1()
        blank_middle = b"symbol,quantity\n\nAAPL,10\n"
        with pytest.raises(SentinaxCanonicalCsvError, match="Blank physical line at row 2"):
            parser.extract_records(blank_middle)

    def test_multiple_trailing_newlines_rejected(self):
        """V2: Multiple trailing newlines create blank lines and fail closed."""
        parser = SentinaxCanonicalCsvParserV1()
        double_trailing = b"symbol,quantity\nAAPL,10\n\n"
        with pytest.raises(SentinaxCanonicalCsvError, match="Blank physical line"):
            parser.extract_records(double_trailing)

    def test_single_trailing_newline_does_not_create_blank_record(self):
        """W: Single trailing newline produces exact data records without trailing blank row."""
        parser = SentinaxCanonicalCsvParserV1()
        content = b"symbol\nAAPL\n"
        records = parser.extract_records(content)
        assert len(records) == 1
        assert records[0].raw_record == b"AAPL"

    def test_multiline_quoted_field_rejected(self):
        """X: Quoted field opened on one line and closed on another fails closed."""
        parser = SentinaxCanonicalCsvParserV1()
        # "AAPL\nline2" splits physically at newline
        multiline_quoted = b'symbol,notes\n"AAPL\nsecond_line",10\n'
        with pytest.raises(SentinaxCanonicalCsvError, match="Malformed CSV"):
            parser.extract_records(multiline_quoted)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Header Contract & Key Grammar Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestHeaderContractAndKeyGrammar:
    """Verifies strict Phase 13C field-key grammar matching, uniqueness, column limits, and rejection of invalid headers."""

    def test_single_field_header_accepted(self):
        """Y: 1-column header is accepted."""
        parser = SentinaxCanonicalCsvParserV1()
        records = parser.extract_records(b"symbol\nAAPL\n")
        assert len(records) == 1

    def test_multiple_headers_accepted(self):
        """Z: Multi-column header is accepted."""
        parser = SentinaxCanonicalCsvParserV1()
        records = parser.extract_records(b"symbol,trade_date,gross_amount\nAAPL,2026-08-28,100\n")
        assert len(records) == 1

    def test_duplicate_header_rejected(self):
        """AA: Duplicate header key fails closed."""
        parser = SentinaxCanonicalCsvParserV1()
        with pytest.raises(SentinaxCanonicalCsvError, match="Duplicate header key detected: 'symbol'"):
            parser.extract_records(b"symbol,quantity,symbol\nAAPL,10,AAPL\n")

    def test_uppercase_header_rejected(self):
        """AB: Uppercase in header key fails closed (no silent lowercasing)."""
        parser = SentinaxCanonicalCsvParserV1()
        with pytest.raises(SentinaxCanonicalCsvError, match="Invalid header column key"):
            parser.extract_records(b"Symbol,quantity\nAAPL,10\n")

    def test_spaced_header_rejected(self):
        """AC: Leading/trailing space in header key fails closed (no stripping)."""
        parser = SentinaxCanonicalCsvParserV1()
        with pytest.raises(SentinaxCanonicalCsvError, match="Invalid header column key"):
            parser.extract_records(b" symbol,quantity\nAAPL,10\n")

        with pytest.raises(SentinaxCanonicalCsvError, match="Invalid header column key"):
            parser.extract_records(b"symbol ,quantity\nAAPL,10\n")

    def test_empty_header_rejected(self):
        """AD: Empty column header fails closed."""
        parser = SentinaxCanonicalCsvParserV1()
        with pytest.raises(SentinaxCanonicalCsvError, match="Invalid header column key"):
            parser.extract_records(b",quantity\nAAPL,10\n")

    def test_digit_first_header_rejected(self):
        """AE: Digit-first header key fails closed."""
        parser = SentinaxCanonicalCsvParserV1()
        with pytest.raises(SentinaxCanonicalCsvError, match="Invalid header column key"):
            parser.extract_records(b"1symbol,quantity\nAAPL,10\n")

    def test_unicode_header_rejected(self):
        """AF: Non-ASCII Unicode in header key fails closed."""
        parser = SentinaxCanonicalCsvParserV1()
        with pytest.raises(SentinaxCanonicalCsvError, match="Invalid header column key"):
            parser.extract_records("tutar₺,adet\n100,10\n".encode("utf-8"))

    def test_max_length_header_key_accepted(self):
        """AG: 64-char valid header key is accepted."""
        parser = SentinaxCanonicalCsvParserV1()
        key_64 = "a" * 64
        content = f"{key_64}\nval\n".encode("utf-8")
        records = parser.extract_records(content)
        assert len(records) == 1
        assert records[0].fields[0].field_key == key_64

    def test_over_max_length_header_key_rejected(self):
        """AH: 65-char header key fails closed."""
        parser = SentinaxCanonicalCsvParserV1()
        key_65 = "a" * 65
        content = f"{key_65}\nval\n".encode("utf-8")
        with pytest.raises(SentinaxCanonicalCsvError, match="Invalid header column key"):
            parser.extract_records(content)

    def test_over_max_columns_rejected(self):
        """AI: Header exceeding MAX_COLUMNS (128) fails closed."""
        parser = SentinaxCanonicalCsvParserV1()
        headers_129 = ",".join(f"col_{i}" for i in range(MAX_COLUMNS + 1))
        content = f"{headers_129}\n".encode("utf-8")
        with pytest.raises(SentinaxCanonicalCsvError, match="exceeds maximum column limit"):
            parser.extract_records(content)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Row Structure & Cardinality Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestRowStructureAndCardinality:
    """Verifies column count matching, all-empty rows, and field preservation."""

    def test_exact_column_count_accepted(self):
        """AJ: Data row with exact matching column count succeeds."""
        parser = SentinaxCanonicalCsvParserV1()
        records = parser.extract_records(b"a,b,c\n1,2,3\n")
        assert len(records) == 1
        assert len(records[0].fields) == 3

    def test_missing_column_rejected(self):
        """AK: Data row with fewer columns than header fails closed with row context."""
        parser = SentinaxCanonicalCsvParserV1()
        content = b"a,b,c\n1,2\n"
        with pytest.raises(SentinaxCanonicalCsvError, match="Malformed CSV at physical row 2: expected 3 columns, got 2"):
            parser.extract_records(content)

    def test_extra_column_rejected(self):
        """AL: Data row with more columns than header fails closed with row context."""
        parser = SentinaxCanonicalCsvParserV1()
        content = b"a,b,c\n1,2,3,4\n"
        with pytest.raises(SentinaxCanonicalCsvError, match="Malformed CSV at physical row 2: expected 3 columns, got 4"):
            parser.extract_records(content)

    def test_all_empty_data_cells_accepted(self):
        """AM, AN: Multi-column all-empty row (e.g. ',,') is structurally valid and retains empty string fields."""
        parser = SentinaxCanonicalCsvParserV1()
        content = b"col_a,col_b,col_c\n,,\n"
        records = parser.extract_records(content)
        assert len(records) == 1
        assert records[0].raw_record == b",,"
        assert records[0].fields == (
            ImportParsedField("col_a", ""),
            ImportParsedField("col_b", ""),
            ImportParsedField("col_c", ""),
        )

    def test_every_header_creates_one_field_per_row(self):
        """AO: For N headers, every data row returns exactly N fields."""
        parser = SentinaxCanonicalCsvParserV1()
        content = b"a,b,c\n1,,3\n"
        records = parser.extract_records(content)
        assert len(records[0].fields) == 3
        fields_map = {f.field_key: f.field_value for f in records[0].fields}
        assert fields_map == {"a": "1", "b": "", "c": "3"}


# ─────────────────────────────────────────────────────────────────────────────
# 6. CSV Quoting & Syntax Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCsvQuotingAndSyntax:
    """Verifies standard CSV quote decoding, escaped quotes, and syntactic error detection."""

    def test_quoted_comma_accepted(self):
        """AP: Quoted comma in cell value decodes as single cell value."""
        parser = SentinaxCanonicalCsvParserV1()
        content = b'name,code\n"Apple, Inc.",AAPL\n'
        records = parser.extract_records(content)
        fields_map = {f.field_key: f.field_value for f in records[0].fields}
        assert fields_map["name"] == "Apple, Inc."

    def test_escaped_double_quote_accepted(self):
        """AQ: Escaped double quotes ("") decode to single double quote in field value."""
        parser = SentinaxCanonicalCsvParserV1()
        content = b'notes,symbol\n"He said ""buy""",AAPL\n'
        records = parser.extract_records(content)
        fields_map = {f.field_key: f.field_value for f in records[0].fields}
        assert fields_map["notes"] == 'He said "buy"'

    def test_unclosed_quote_rejected(self):
        """AR: Unclosed quote fails closed with physical row context."""
        parser = SentinaxCanonicalCsvParserV1()
        content = b'symbol,notes\nAAPL,"unclosed_quote\n'
        with pytest.raises(SentinaxCanonicalCsvError, match="Malformed CSV quoting at physical row 2"):
            parser.extract_records(content)

    def test_malformed_quote_syntax_rejected(self):
        """AS: Malformed quote placement (e.g. a,"b"c,d) fails closed."""
        parser = SentinaxCanonicalCsvParserV1()
        content = b'a,b,c\n1,"2"3,4\n'
        with pytest.raises(SentinaxCanonicalCsvError, match="Malformed CSV quoting at physical row 2"):
            parser.extract_records(content)

    def test_unquoted_field_with_embedded_quote_rejected(self):
        """6A-6D: Quotes anywhere inside unquoted fields are strictly rejected."""
        parser = SentinaxCanonicalCsvParserV1()

        cases = [
            b"a,b,c\n1,ab\"cd,3\n",     # 6A: ab"cd
            b"a,b,c\n1,ab\"cd\",3\n",   # 6B: ab"cd"
            b"a,b\nabc\",2\n",          # 6C: abc"
            b"a,b\nab\"c\"d,2\n",       # 6D: ab"c"d
        ]
        for content in cases:
            with pytest.raises(SentinaxCanonicalCsvError, match="Malformed CSV quoting at physical row 2"):
                parser.extract_records(content)

    def test_junk_or_whitespace_after_closing_quote_rejected(self):
        """7E, 7F: Non-delimiter characters or whitespace after closing quote are strictly rejected."""
        parser = SentinaxCanonicalCsvParserV1()

        cases = [
            b'a,b,c\n1,"abc"x,3\n',     # 7E: "abc"x
            b'a,b,c\n1,"abc" ,3\n',     # 7F: "abc" (space after closing quote)
            b'a,b,c\n1,"abc"\t,3\n',    # tab after closing quote
        ]
        for content in cases:
            with pytest.raises(SentinaxCanonicalCsvError, match="Malformed CSV quoting at physical row 2"):
                parser.extract_records(content)

    def test_leading_whitespace_before_quote_rejected(self):
        """9: Leading whitespace before quote treats field as unquoted and fails closed."""
        parser = SentinaxCanonicalCsvParserV1()
        invalid_leading = b'a,b\n  "ABC",1\n'
        with pytest.raises(SentinaxCanonicalCsvError, match="Malformed CSV quoting at physical row 2"):
            parser.extract_records(invalid_leading)

        # But whitespace INSIDE quoted field is valid
        valid_inside = b'a,b\n"  ABC",1\n'
        records = parser.extract_records(valid_inside)
        assert len(records) == 1
        assert records[0].fields[0].field_value == "  ABC"

    def test_unclosed_or_unescaped_internal_quotes_rejected(self):
        """10L, 10M: Unescaped single quotes inside quoted fields fail closed."""
        parser = SentinaxCanonicalCsvParserV1()

        cases = [
            b'a,b\n"ab"cd",1\n',   # 10L: "ab"cd"
            b'a,b\n"a"b",1\n',     # 10M: "a"b"
        ]
        for content in cases:
            with pytest.raises(SentinaxCanonicalCsvError, match="Malformed CSV quoting at physical row 2"):
                parser.extract_records(content)

        # Valid escaped doubled quote
        valid_doubled = b'a,b\n"a""b",1\n'
        records = parser.extract_records(valid_doubled)
        assert records[0].fields[0].field_value == 'a"b'

    def test_malformed_header_quote_syntax_rejected(self):
        """17N-17P: Malformed quotes in header fail closed; valid quoted header accepted."""
        parser = SentinaxCanonicalCsvParserV1()

        # 17N: Unquoted quote in header
        with pytest.raises(SentinaxCanonicalCsvError, match="Malformed CSV quoting at physical row 1"):
            parser.extract_records(b'sym"bol,quantity\nAAPL,10\n')

        # 17O: Junk after closing quote in header
        with pytest.raises(SentinaxCanonicalCsvError, match="Malformed CSV quoting at physical row 1"):
            parser.extract_records(b'"symbol"x,quantity\nAAPL,10\n')

        # 17P: Valid quoted canonical header
        records = parser.extract_records(b'"symbol","quantity"\nAAPL,10\n')
        assert len(records) == 1
        fields_map = {f.field_key: f.field_value for f in records[0].fields}
        assert fields_map == {"symbol": "AAPL", "quantity": "10"}

    def test_quoted_and_unquoted_same_logical_value_have_different_raw_bytes(self):
        """AT: Quoted ABC and unquoted ABC decode to same field value but distinct raw_record bytes."""
        parser = SentinaxCanonicalCsvParserV1()
        r1 = parser.extract_records(b"symbol\nABC\n")
        r2 = parser.extract_records(b'symbol\n"ABC"\n')

        assert r1[0].fields[0].field_value == r2[0].fields[0].field_value == "ABC"
        assert r1[0].raw_record == b"ABC"
        assert r2[0].raw_record == b'"ABC"'
        assert r1[0].raw_record != r2[0].raw_record


# ─────────────────────────────────────────────────────────────────────────────
# 7. Whitespace Preservation Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestWhitespacePreservation:
    """Verifies that cell whitespace is preserved exactly without stripping."""

    def test_leading_whitespace_preserved(self):
        """AU: Leading whitespace in unquoted/quoted cells is preserved."""
        parser = SentinaxCanonicalCsvParserV1()
        records = parser.extract_records(b"symbol\n  AAPL\n")
        assert records[0].fields[0].field_value == "  AAPL"

    def test_trailing_whitespace_preserved(self):
        """AV: Trailing whitespace in unquoted/quoted cells is preserved."""
        parser = SentinaxCanonicalCsvParserV1()
        records = parser.extract_records(b"symbol\nAAPL  \n")
        assert records[0].fields[0].field_value == "AAPL  "

    def test_whitespace_only_cell_preserved(self):
        """AW: Whitespace-only cell is preserved as whitespace (not empty string)."""
        parser = SentinaxCanonicalCsvParserV1()
        records = parser.extract_records(b"symbol\n   \n")
        assert records[0].fields[0].field_value == "   "

    def test_quoted_surrounding_whitespace_preserved(self):
        """AX: Quoted whitespace is preserved exactly."""
        parser = SentinaxCanonicalCsvParserV1()
        records = parser.extract_records(b'symbol\n"  AAPL  "\n')
        assert records[0].fields[0].field_value == "  AAPL  "


# ─────────────────────────────────────────────────────────────────────────────
# 8. Raw Byte Slice Authority Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestRawByteSliceAuthority:
    """Verifies that raw_record comes directly from original content bytes without re-encoding."""

    def test_raw_record_is_exact_original_slice_minus_lf(self):
        """AY: raw_record equals exact slice of original content bytes excluding LF."""
        parser = SentinaxCanonicalCsvParserV1()
        content = b"symbol,quantity\n 123 , 456 \n"
        records = parser.extract_records(content)

        expected_slice = b" 123 , 456 "
        assert records[0].raw_record == expected_slice

    def test_raw_record_is_exact_original_slice_minus_crlf(self):
        """AZ: raw_record equals exact slice of original content bytes excluding CRLF."""
        parser = SentinaxCanonicalCsvParserV1()
        content = b"symbol,quantity\r\n 123 , 456 \r\n"
        records = parser.extract_records(content)

        expected_slice = b" 123 , 456 "
        assert records[0].raw_record == expected_slice

    def test_turkish_utf8_raw_bytes_preserved(self):
        """BB: Non-ASCII UTF-8 Turkish text is preserved byte-for-byte in raw_record."""
        parser = SentinaxCanonicalCsvParserV1()
        content = "hisse,aciklama\nGARAN,Garanti Bankası Alım\n".encode("utf-8")
        records = parser.extract_records(content)

        expected_raw = "GARAN,Garanti Bankası Alım".encode("utf-8")
        assert records[0].raw_record == expected_raw
        fields_map = {f.field_key: f.field_value for f in records[0].fields}
        assert fields_map["aciklama"] == "Garanti Bankası Alım"

    def test_escaped_quote_raw_syntax_preserved_in_raw_record(self):
        """BC: Escaped quotes remain in raw_record while decoded in field_value."""
        parser = SentinaxCanonicalCsvParserV1()
        content = b'notes,sym\n"He said ""buy""",AAPL\n'
        records = parser.extract_records(content)

        assert records[0].raw_record == b'"He said ""buy""",AAPL'
        fields_map = {f.field_key: f.field_value for f in records[0].fields}
        assert fields_map["notes"] == 'He said "buy"'


# ─────────────────────────────────────────────────────────────────────────────
# 9. Order & Duplicate Preservation Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestOrderAndDuplicatePreservation:
    """Verifies that source physical row order is preserved and duplicate rows are not deduplicated."""

    def test_source_row_order_preserved(self):
        """BD: Source row order defines record index order."""
        parser = SentinaxCanonicalCsvParserV1()
        content = b"id\n3\n1\n2\n"
        records = parser.extract_records(content)

        assert [r.fields[0].field_value for r in records] == ["3", "1", "2"]

    def test_identical_data_row_repeated_returns_two_records(self):
        """BE, BF: Identical data rows return two separate ExtractedImportRecord objects."""
        parser = SentinaxCanonicalCsvParserV1()
        content = b"sym,qty\nAAPL,10\nAAPL,10\n"
        records = parser.extract_records(content)

        assert len(records) == 2
        assert records[0].raw_record == records[1].raw_record
        assert records[0].fields == records[1].fields


# ─────────────────────────────────────────────────────────────────────────────
# 10. Defensive Limits Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDefensiveLimits:
    """Verifies MAX_COLUMNS, MAX_RECORD_BYTES, and MAX_DATA_RECORDS boundaries."""

    def test_max_columns_boundary(self):
        """BK, BL: Exactly 128 columns succeeds; 129 columns fails closed."""
        parser = SentinaxCanonicalCsvParserV1()

        header_128 = ",".join(f"c_{i}" for i in range(MAX_COLUMNS))
        row_128 = ",".join(str(i) for i in range(MAX_COLUMNS))
        valid_content = f"{header_128}\n{row_128}\n".encode("utf-8")
        records = parser.extract_records(valid_content)
        assert len(records) == 1
        assert len(records[0].fields) == 128

        header_129 = ",".join(f"c_{i}" for i in range(MAX_COLUMNS + 1))
        invalid_content = f"{header_129}\n".encode("utf-8")
        with pytest.raises(SentinaxCanonicalCsvError, match="exceeds maximum column limit"):
            parser.extract_records(invalid_content)

    def test_max_record_bytes_boundary(self):
        """BI, BJ: Record exceeding MAX_RECORD_BYTES (1 MiB) fails closed."""
        parser = SentinaxCanonicalCsvParserV1()
        # Row larger than 1 MiB
        oversized_row = b"sym\n" + b"A" * (MAX_RECORD_BYTES + 1) + b"\n"
        with pytest.raises(SentinaxCanonicalCsvError, match="exceeds maximum size limit"):
            parser.extract_records(oversized_row)

    def test_exact_max_record_bytes_scanner_boundary(self):
        """P: Exactly MAX_RECORD_BYTES in physical scanner passes without error."""
        exact_record = b"x" * MAX_RECORD_BYTES + b"\n"
        lines = _scan_physical_lines(exact_record)
        assert len(lines) == 1
        assert len(lines[0]) == MAX_RECORD_BYTES

        # Exceeding by 1 byte fails closed
        over_record = b"x" * (MAX_RECORD_BYTES + 1) + b"\n"
        with pytest.raises(SentinaxCanonicalCsvError, match="exceeds maximum size limit"):
            _scan_physical_lines(over_record)

    def test_max_data_records_scanner_overflow_rejected(self):
        """R, S: Physical scanner immediately rejects row count beyond MAX_DATA_RECORDS."""
        # 1 header + (MAX_DATA_RECORDS + 1) rows exceeds MAX_DATA_RECORDS (250,000)
        # We test with a small content snippet where data rows exceed MAX_DATA_RECORDS
        # Construct header + 250,001 data rows
        # To avoid giant memory allocation in pytest, test the scanner limit check logic
        # 250,001 lines: header + 250,000 data lines is allowed; 250,001 data lines is rejected.
        header_plus_over = b"h\n" + b"1\n" * (MAX_DATA_RECORDS + 1)
        with pytest.raises(SentinaxCanonicalCsvError, match="Data row count exceeds maximum limit"):
            _scan_physical_lines(header_plus_over)


# ─────────────────────────────────────────────────────────────────────────────
# 10b. Linear Scan Complexity & Large Dataset Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestLinearScanComplexity:
    """Verifies that physical newline scanner is strictly O(len(content)) without tail rescans."""

    def test_no_repeated_suffix_find_in_scanner_source(self):
        """30O: Assert scanner implementation does not contain repeated .find( or .index( calls."""
        import inspect
        source = inspect.getsource(_scan_physical_lines)
        assert ".find(" not in source, "Physical line scanner must not use .find() tail searches"
        assert ".index(" not in source, "Physical line scanner must not use .index() tail searches"

    def test_large_lf_dataset_20k_rows(self):
        """21, 30M: Large LF-only dataset with 20,000 rows executes in linear time with exact ordering."""
        parser = SentinaxCanonicalCsvParserV1()

        # Build 20,000 rows LF-only CSV
        header = b"symbol,quantity,price\n"
        rows = [f"AAPL_{i},10,150.50\n".encode("utf-8") for i in range(20_000)]
        content = header + b"".join(rows)

        records = parser.extract_records(content)
        assert len(records) == 20_000
        assert records[0].raw_record == b"AAPL_0,10,150.50"
        assert records[-1].raw_record == b"AAPL_19999,10,150.50"

    def test_large_crlf_dataset_5k_rows(self):
        """22, 30N: Large CRLF-only dataset with 5,000 rows executes in linear time with exact ordering."""
        parser = SentinaxCanonicalCsvParserV1()

        # Build 5,000 rows CRLF-only CSV
        header = b"symbol,quantity\r\n"
        rows = [f"MSFT_{i},20\r\n".encode("utf-8") for i in range(5_000)]
        content = header + b"".join(rows)

        records = parser.extract_records(content)
        assert len(records) == 5_000
        assert records[0].raw_record == b"MSFT_0,20"
        assert records[-1].raw_record == b"MSFT_4999,20"


# ─────────────────────────────────────────────────────────────────────────────
# 11. Phase 13E Pipeline Integration Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestPhase13EPipelineIntegration:
    """Verifies full end-to-end integration of SentinaxCanonicalCsvParserV1 with build_import_staging_result."""

    def test_pipeline_header_only_file(self):
        """BM: Header-only CSV through pipeline produces staging result with 0 records."""
        parser = SentinaxCanonicalCsvParserV1()
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        content = b"symbol,quantity,price\n"

        result = build_import_staging_result(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="empty_statement.csv",
            content=content,
            imported_at=t,
            parser=parser,
        )

        assert isinstance(result, ImportStagingResult)
        assert result.raw_manifest.record_count == 0
        assert result.parsed_manifest.record_count == 0
        assert result.file_provenance.source_key == "sentinax_csv"
        assert result.parsed_manifest.parser_revision == 1

    def test_pipeline_two_row_file(self):
        """BN-BU: Multi-row CSV through pipeline produces verified, complete manifests."""
        parser = SentinaxCanonicalCsvParserV1()
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        content = b"symbol,quantity,price\nAAPL,10,150.00\nMSFT,20,300.00\n"

        result = build_import_staging_result(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=content,
            imported_at=t,
            parser=parser,
        )

        assert result.raw_manifest.record_count == 2
        assert result.parsed_manifest.record_count == 2
        assert result.file_provenance.source_key == "sentinax_csv"
        assert result.parsed_manifest.parser_revision == 1

        # Ordinal checks (1-indexed 1..N)
        assert result.raw_manifest.records[0].record_ordinal == 1
        assert result.raw_manifest.records[1].record_ordinal == 2

        # Raw SHA check matches SHA-256 of exact data row bytes
        expected_sha_1 = hashlib.sha256(b"AAPL,10,150.00").hexdigest()
        expected_sha_2 = hashlib.sha256(b"MSFT,20,300.00").hexdigest()
        assert result.raw_manifest.records[0].record_sha256 == expected_sha_1
        assert result.raw_manifest.records[1].record_sha256 == expected_sha_2

        # Parsed fields checks
        p1_fields = {f.field_key: f.field_value for f in result.parsed_manifest.parsed_records[0].fields}
        assert p1_fields == {"symbol": "AAPL", "quantity": "10", "price": "150.00"}

        # Exact cross-layer object binding
        assert result.raw_manifest.file_provenance is result.file_provenance
        assert result.parsed_manifest.raw_manifest is result.raw_manifest

    def test_pipeline_malformed_quote_propagates_unchanged_without_partial_result(self):
        """19Q, 19S: Malformed quote through pipeline propagates SentinaxCanonicalCsvError unchanged without partial result."""
        parser = SentinaxCanonicalCsvParserV1()
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        bad_content = b'symbol,qty\n1,ab"cd\n'

        result = None
        with pytest.raises(SentinaxCanonicalCsvError, match="Malformed CSV quoting at physical row 2"):
            result = build_import_staging_result(
                portfolio_id=port_id,
                account_id=acc_id,
                filename="trades.csv",
                content=bad_content,
                imported_at=t,
                parser=parser,
            )

        assert result is None

    def test_pipeline_valid_escaped_quote_succeeds(self):
        """19R: Valid escaped quote through pipeline produces verified parsed fields."""
        parser = SentinaxCanonicalCsvParserV1()
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        content = b'symbol,notes\nAAPL,"He said ""buy"""\n'

        result = build_import_staging_result(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=content,
            imported_at=t,
            parser=parser,
        )

        assert result.parsed_manifest.record_count == 1
        p_fields = {f.field_key: f.field_value for f in result.parsed_manifest.parsed_records[0].fields}
        assert p_fields == {"symbol": "AAPL", "notes": 'He said "buy"'}


# ─────────────────────────────────────────────────────────────────────────────
# 12. LF vs. CRLF Pipeline Sensitivity Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestLfVsCrlfPipelineSensitivity:
    """Verifies that LF vs CRLF produces distinct file identity and raw/parsed manifest digests."""

    def test_lf_and_crlf_produce_distinct_pipeline_identities(self):
        """BV-BY: Same table with LF vs CRLF produces distinct file and manifest identities."""
        parser = SentinaxCanonicalCsvParserV1()
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

        lf_content = b"symbol,qty\nAAPL,10\n"
        crlf_content = b"symbol,qty\r\nAAPL,10\r\n"

        res_lf = build_import_staging_result(port_id, acc_id, "f.csv", lf_content, t, parser)
        res_crlf = build_import_staging_result(port_id, acc_id, "f.csv", crlf_content, t, parser)

        # File content SHA differs
        assert res_lf.file_provenance.content_sha256 != res_crlf.file_provenance.content_sha256
        assert res_lf.file_provenance.file_identity != res_crlf.file_provenance.file_identity

        # Manifest SHAs differ
        assert res_lf.raw_manifest.manifest_sha256 != res_crlf.raw_manifest.manifest_sha256
        assert res_lf.parsed_manifest.parsed_manifest_sha256 != res_crlf.parsed_manifest.parsed_manifest_sha256

        # But individual row raw_record SHA is identical
        assert res_lf.raw_manifest.records[0].record_sha256 == res_crlf.raw_manifest.records[0].record_sha256


# ─────────────────────────────────────────────────────────────────────────────
# 13. Financial Semantic Separation Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestFinancialSemanticSeparation:
    """Verifies that SentinaxCanonicalCsvParserV1 does not contain financial or ledger fields."""

    def test_no_financial_fields_in_parser(self):
        """Parser has no portfolio/financial attributes or methods."""
        parser = SentinaxCanonicalCsvParserV1()
        forbidden_attrs = {
            "portfolio_id",
            "account_id",
            "transaction_type",
            "instrument_id",
            "currency",
            "amount",
            "quantity",
            "price",
            "execute_trade",
            "append_ledger",
        }

        parser_dir = set(dir(parser))
        overlap = parser_dir & forbidden_attrs
        assert not overlap, f"Forbidden financial attributes found in parser: {overlap}"


# ─────────────────────────────────────────────────────────────────────────────
# 14. Final Red-Team Quote Hardening Comprehensive Matrix
# ─────────────────────────────────────────────────────────────────────────────

class TestFinalRedTeamQuoteHardening:
    """Explicitly tests the exact Red-Team challenge cases from Section 23."""

    def test_final_red_team_quote_matrix(self):
        """23: Verifies exact Red-Team quote patterns."""
        parser = SentinaxCanonicalCsvParserV1()

        # Reject cases
        reject_cases = [
            b"sym\nab\"cd\n",     # ab"cd
            b"sym\nab\"cd\"\n",   # ab"cd"
            b"sym\nabc\"\n",      # abc"
            b"sym\n\"a\"b\n",     # "a"b
            b"sym\n\"a\"x\n",     # "a"x
            b"sym\n\"a\" \n",     # "a" (trailing space after closing quote)
            b"sym\n \"a\"\n",     #  "a" (leading space before quote)
        ]
        for content in reject_cases:
            with pytest.raises(SentinaxCanonicalCsvError, match="Malformed CSV quoting at physical row 2"):
                parser.extract_records(content)

        # Accept cases
        # "a""b" -> a"b
        r1 = parser.extract_records(b'sym\n"a""b"\n')
        assert len(r1) == 1
        assert r1[0].fields[0].field_value == 'a"b'

        # "a,b" -> a,b
        r2 = parser.extract_records(b'sym\n"a,b"\n')
        assert len(r2) == 1
        assert r2[0].fields[0].field_value == "a,b"

        # "" -> ""
        r3 = parser.extract_records(b'sym\n""\n')
        assert len(r3) == 1
        assert r3[0].fields[0].field_value == ""

