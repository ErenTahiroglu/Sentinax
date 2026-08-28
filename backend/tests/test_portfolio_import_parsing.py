"""
backend/tests/test_portfolio_import_parsing.py
==============================================
Tests for Phase 13C: Parser-Neutral Record Extraction Contract & Raw-Byte Binding.

Zero network calls (pytest-socket enforced).
Pure in-memory domain evaluation.

Test Matrix:
    1. Field Model (valid fields, empty/whitespace/Unicode values, syntax rejections, length bounds, frozen immutability)
    2. Basic Parsed Record (zero-field, single/multi fields, tuple output, frozen, derived source_key)
    3. Raw-Byte Provenance Binding (exact match, single-byte mismatch, type rejections, empty bytes, non-retention)
    4. Collection & Field Ordering Invariance (list/tuple accepted, key sorting, permutation invariance)
    5. Duplicate Field Keys Rejection (same value duplicate, different value duplicate, no silent overwrite)
    6. Hash Determinism & Digest Sensitivity (independent compact JSON, revision sensitivity, whitespace, empty vs absent)
    7. Direct Constructor Hardening (invalid provenance, bool/zero revision, unsorted fields, incorrect SHA)
    8. Immutability & Surface Red-Team (no raw bytes, no ledger fields, no file metadata in models)
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
import hashlib
import json
import random
from typing import Any, List, Tuple
from uuid import UUID, uuid4

import pytest

from backend.engine.private.portfolio.import_parsing import (
    ImportParsedField,
    ParsedImportRecord,
    PortfolioImportParsingError,
    build_parsed_import_record,
)
from backend.engine.private.portfolio.import_provenance import (
    ImportFileProvenance,
    ImportRecordProvenance,
    build_import_file_provenance,
    build_import_record_provenance,
)


def _make_provenance_and_record(
    raw_record: bytes = b"2026-08-28,AAPL,BUY,10,150.00",
    ordinal: int = 1,
    source_key: str = "midas_csv",
) -> Tuple[ImportFileProvenance, ImportRecordProvenance]:
    file_prov = build_import_file_provenance(
        portfolio_id=uuid4(),
        account_id=uuid4(),
        source_key=source_key,
        filename="statement.csv",
        content=b"header\n" + raw_record + b"\n",
        imported_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
    )
    rec_prov = build_import_record_provenance(
        file_provenance=file_prov,
        record_ordinal=ordinal,
        raw_record=raw_record,
    )
    return file_prov, rec_prov


# ─────────────────────────────────────────────────────────────────────────────
# 1. Field Model Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestImportParsedFieldModel:
    """Verifies ImportParsedField constraints, key syntax, value bounds, and immutability."""

    def test_valid_field(self):
        """A: Valid field key and value."""
        field = ImportParsedField("trade_date", "2026-08-28")
        assert field.field_key == "trade_date"
        assert field.field_value == "2026-08-28"

    def test_empty_field_value_accepted(self):
        """B: Empty field value is accepted and preserved."""
        field = ImportParsedField("notes", "")
        assert field.field_value == ""

    def test_whitespace_only_value_accepted_and_preserved(self):
        """C: Whitespace-only value is preserved without stripping."""
        field = ImportParsedField("custom_field", "   \t\n  ")
        assert field.field_value == "   \t\n  "

    def test_unicode_value_accepted_and_preserved(self):
        """D: Unicode value is preserved without normalization."""
        field = ImportParsedField("description", "İstanbul / Fon Alış (₺)")
        assert field.field_value == "İstanbul / Fon Alış (₺)"

    @pytest.mark.parametrize("bad_key", [
        "TradeDate",       # E: Uppercase
        "trade-date",      # F: Hyphen
        " trade_date",     # G: Leading whitespace
        "trade_date ",     # G: Trailing whitespace
        "miktar_₺",        # H: Unicode key
        "1symbol",         # I: Starting with digit
        "",                # J: Empty key
        "a" * 65,          # K: Exceeds 64 chars
        "foo.bar",         # Dot
        "foo@bar",         # Punctuation
    ])
    def test_invalid_field_keys_rejected(self, bad_key: str):
        """E-K: Malformed field keys fail closed."""
        with pytest.raises(PortfolioImportParsingError, match="field_key"):
            ImportParsedField(bad_key, "value")

    def test_non_string_key_or_value_rejected(self):
        """L: Non-string (int, bool, None) keys and values are rejected."""
        for bad in (123, True, False, None, b"bytes"):
            with pytest.raises(PortfolioImportParsingError):
                ImportParsedField(bad, "value")  # type: ignore
            with pytest.raises(PortfolioImportParsingError):
                ImportParsedField("key", bad)  # type: ignore

    def test_excessive_value_length_rejected(self):
        """M: Field values exceeding 16384 characters are rejected."""
        too_long = "a" * 16385
        with pytest.raises(PortfolioImportParsingError, match="exceeds maximum length"):
            ImportParsedField("key", too_long)

        # Exact boundary 16384 is accepted
        boundary = "a" * 16384
        field = ImportParsedField("key", boundary)
        assert len(field.field_value) == 16384

    def test_frozen_field_mutation_rejected(self):
        """N: Mutating field properties raises FrozenInstanceError."""
        field = ImportParsedField("symbol", "AAPL")
        with pytest.raises(FrozenInstanceError):
            field.field_key = "other"  # type: ignore
        with pytest.raises(FrozenInstanceError):
            field.field_value = "MSFT"  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 2. Basic Parsed Record Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBasicParsedRecord:
    """Verifies baseline ParsedImportRecord creation, properties, and immutability."""

    def test_valid_zero_field_record(self):
        """O, R: Zero-field parsed record produces valid model with empty tuple fields."""
        raw = b"raw_line_data"
        _, rec_prov = _make_provenance_and_record(raw)
        record = build_parsed_import_record(rec_prov, raw, 1, [])

        assert isinstance(record, ParsedImportRecord)
        assert record.fields == ()
        assert type(record.fields) is tuple
        assert record.parser_revision == 1
        assert len(record.parsed_sha256) == 64

    def test_valid_one_field_record(self):
        """P: Single-field parsed record succeeds."""
        raw = b"raw_line_data"
        _, rec_prov = _make_provenance_and_record(raw)
        f1 = ImportParsedField("symbol", "AAPL")
        record = build_parsed_import_record(rec_prov, raw, 1, [f1])

        assert record.fields == (f1,)
        assert record.parser_revision == 1

    def test_valid_multi_field_record(self):
        """Q: Multi-field parsed record succeeds."""
        raw = b"raw_line_data"
        _, rec_prov = _make_provenance_and_record(raw)
        f1 = ImportParsedField("symbol", "AAPL")
        f2 = ImportParsedField("quantity", "10")
        f3 = ImportParsedField("trade_date", "2026-08-28")

        record = build_parsed_import_record(rec_prov, raw, 1, [f1, f2, f3])
        assert len(record.fields) == 3

    def test_parsed_record_frozen_immutability(self):
        """S: Mutation of ParsedImportRecord raises FrozenInstanceError."""
        raw = b"raw_line_data"
        _, rec_prov = _make_provenance_and_record(raw)
        record = build_parsed_import_record(rec_prov, raw, 1, [])

        with pytest.raises(FrozenInstanceError):
            record.parser_revision = 2  # type: ignore
        with pytest.raises(FrozenInstanceError):
            record.parsed_sha256 = "0" * 64  # type: ignore

    def test_source_key_derived_from_provenance(self):
        """T: source_key property matches parent provenance source_key."""
        raw = b"raw_data"
        file_prov, rec_prov = _make_provenance_and_record(raw, source_key="custom.bank_v2")
        record = build_parsed_import_record(rec_prov, raw, 1, [])

        assert record.source_key == "custom.bank_v2"
        assert record.source_key == file_prov.source_key


# ─────────────────────────────────────────────────────────────────────────────
# 3. Raw-Byte Provenance Binding Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestRawByteProvenanceBinding:
    """Verifies that builder requires exact raw record bytes matching record_provenance.record_sha256."""

    def test_exact_raw_bytes_accepted(self):
        """U: Exact raw bytes matching record_provenance succeed."""
        raw = b"exact_row_bytes_content"
        _, rec_prov = _make_provenance_and_record(raw)
        record = build_parsed_import_record(rec_prov, raw, 1, [])
        assert record.record_provenance == rec_prov

    def test_single_byte_mismatch_rejected(self):
        """V: Even one byte difference between raw_record and record_provenance fails closed."""
        raw = b"exact_row_bytes_content"
        tampered_raw = b"exact_row_bytes_contant"  # 1 byte difference
        _, rec_prov = _make_provenance_and_record(raw)

        with pytest.raises(PortfolioImportParsingError, match="does not match record_provenance.record_sha256"):
            build_parsed_import_record(rec_prov, tampered_raw, 1, [])

    def test_entirely_different_raw_bytes_rejected(self):
        """W: Unrelated raw bytes fail closed."""
        raw = b"row_one"
        other_raw = b"row_two"
        _, rec_prov = _make_provenance_and_record(raw)

        with pytest.raises(PortfolioImportParsingError, match="does not match record_provenance.record_sha256"):
            build_parsed_import_record(rec_prov, other_raw, 1, [])

    def test_non_bytes_raw_record_rejected(self):
        """X, Y, Z: Strings, bytearrays, and memoryviews are rejected as raw_record."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)

        for bad in ("row_data", bytearray(raw), memoryview(raw)):
            with pytest.raises(PortfolioImportParsingError, match="raw_record must be exact immutable bytes"):
                build_parsed_import_record(rec_prov, bad, 1, [])  # type: ignore

    def test_empty_raw_record_rejected(self):
        """AA: Empty raw bytes fail closed."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)

        with pytest.raises(PortfolioImportParsingError, match="raw_record bytes cannot be empty"):
            build_parsed_import_record(rec_prov, b"", 1, [])

    def test_raw_bytes_not_retained_in_model(self):
        """AB: ParsedImportRecord model does not store raw_record bytes."""
        raw = b"row_data_to_verify"
        _, rec_prov = _make_provenance_and_record(raw)
        record = build_parsed_import_record(rec_prov, raw, 1, [])

        assert not hasattr(record, "raw_record")
        assert not hasattr(record, "raw_bytes")
        assert not hasattr(record, "content")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Collection & Field Ordering Invariance Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCollectionAndOrderingInvariance:
    """Verifies that builder canonicalizes field order and accepts lists/tuples."""

    def test_list_and_tuple_accepted(self):
        """AC, AD: Both lists and tuples of ImportParsedField are accepted."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)
        f1 = ImportParsedField("symbol", "AAPL")

        rec_from_list = build_parsed_import_record(rec_prov, raw, 1, [f1])
        rec_from_tuple = build_parsed_import_record(rec_prov, raw, 1, (f1,))

        assert rec_from_list.fields == rec_from_tuple.fields
        assert rec_from_list.parsed_sha256 == rec_from_tuple.parsed_sha256

    def test_shuffled_fields_become_canonical_sorted_tuple(self):
        """AE: Shuffled fields become sorted ascending by field_key in the manifest."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)
        f_sym = ImportParsedField("symbol", "AAPL")
        f_qty = ImportParsedField("quantity", "10")
        f_date = ImportParsedField("trade_date", "2026-08-28")

        # Input: trade_date, symbol, quantity -> Expected: quantity, symbol, trade_date
        record = build_parsed_import_record(rec_prov, raw, 1, [f_date, f_sym, f_qty])
        assert record.fields == (f_qty, f_sym, f_date)

    def test_shuffled_and_ordered_inputs_produce_identical_parsed_identity(self):
        """AF: Permuting input field sequence produces identical parsed_sha256 and parsed_identity."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)
        field_list = [
            ImportParsedField(f"field_{k:02d}", f"val_{k}")
            for k in range(10)
        ]

        shuffled = list(field_list)
        random.seed(42)
        random.shuffle(shuffled)

        rec_ordered = build_parsed_import_record(rec_prov, raw, 1, field_list)
        rec_shuffled = build_parsed_import_record(rec_prov, raw, 1, shuffled)

        assert rec_ordered.fields == rec_shuffled.fields
        assert rec_ordered.parsed_sha256 == rec_shuffled.parsed_sha256
        assert rec_ordered.parsed_identity == rec_shuffled.parsed_identity

    def test_invalid_collection_types_rejected(self):
        """AG-AJ: Dicts, sets, generators, strings, bytes are rejected for fields."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)
        f1 = ImportParsedField("symbol", "AAPL")

        for bad_coll in (
            {"symbol": "AAPL"},
            {f1},
            (x for x in [f1]),
            "string",
            b"bytes",
        ):
            with pytest.raises(PortfolioImportParsingError, match="fields must be a materialized list or tuple"):
                build_parsed_import_record(rec_prov, raw, 1, bad_coll)  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 5. Duplicate Field Keys Rejection Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDuplicateFieldKeysRejection:
    """Verifies that duplicate field keys fail closed without silent overwriting."""

    def test_duplicate_key_same_value_rejected(self):
        """AK: Duplicate field key with identical value fails closed."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)
        f1_a = ImportParsedField("symbol", "AAPL")
        f1_b = ImportParsedField("symbol", "AAPL")

        with pytest.raises(PortfolioImportParsingError, match="duplicate field_key detected: symbol"):
            build_parsed_import_record(rec_prov, raw, 1, [f1_a, f1_b])

    def test_duplicate_key_different_value_rejected(self):
        """AL: Duplicate field key with conflicting value fails closed."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)
        f1_a = ImportParsedField("symbol", "AAPL")
        f1_b = ImportParsedField("symbol", "MSFT")

        with pytest.raises(PortfolioImportParsingError, match="duplicate field_key detected: symbol"):
            build_parsed_import_record(rec_prov, raw, 1, [f1_a, f1_b])


# ─────────────────────────────────────────────────────────────────────────────
# 6. Hash Determinism & Digest Sensitivity Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestHashDeterminismAndDigestSensitivity:
    """Verifies canonical JSON preimage encoding, sensitivity to values, revisions, whitespace, and empty fields."""

    def test_independent_json_preimage_hash_matches(self):
        """AM: Independent compact JSON hashing matches parsed_sha256 exactly."""
        raw = b"row_data"
        file_prov, rec_prov = _make_provenance_and_record(raw)
        f_sym = ImportParsedField("symbol", "AAPL")
        f_qty = ImportParsedField("quantity", "10")

        record = build_parsed_import_record(rec_prov, raw, 2, [f_sym, f_qty])

        # Manual preimage reconstruction
        preimage = [
            str(file_prov.portfolio_id),
            str(file_prov.account_id),
            file_prov.source_key,
            file_prov.content_sha256,
            rec_prov.record_ordinal,
            rec_prov.record_sha256,
            2,
            [
                ["quantity", "10"],  # Sorted by field_key
                ["symbol", "AAPL"],
            ],
        ]
        raw_json = json.dumps(preimage, ensure_ascii=True, separators=(",", ":"))
        expected_sha = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()

        assert record.parsed_sha256 == expected_sha

    def test_repeated_calls_identical(self):
        """AN: Repeated builder calls yield exact identical parsed_sha256."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)
        f = [ImportParsedField("symbol", "AAPL")]

        r1 = build_parsed_import_record(rec_prov, raw, 1, f)
        r2 = build_parsed_import_record(rec_prov, raw, 1, f)

        assert r1.parsed_sha256 == r2.parsed_sha256

    def test_parser_revision_change_alters_sha(self):
        """AO: Varying parser_revision alters parsed_sha256."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)
        f = [ImportParsedField("symbol", "AAPL")]

        r_rev1 = build_parsed_import_record(rec_prov, raw, 1, f)
        r_rev2 = build_parsed_import_record(rec_prov, raw, 2, f)

        assert r_rev1.parsed_sha256 != r_rev2.parsed_sha256
        assert r_rev1.parsed_identity != r_rev2.parsed_identity

    def test_field_value_one_character_change_alters_sha(self):
        """AP: Modifying field value (e.g. '100' vs '100.0') alters parsed_sha256."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)

        r_100 = build_parsed_import_record(rec_prov, raw, 1, [ImportParsedField("quantity", "100")])
        r_100_0 = build_parsed_import_record(rec_prov, raw, 1, [ImportParsedField("quantity", "100.0")])

        assert r_100.parsed_sha256 != r_100_0.parsed_sha256

    def test_whitespace_difference_alters_sha(self):
        """AQ: Variations in whitespace ('ABC' vs ' ABC' vs 'ABC ') produce distinct digests."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)

        r_exact = build_parsed_import_record(rec_prov, raw, 1, [ImportParsedField("symbol", "ABC")])
        r_leading = build_parsed_import_record(rec_prov, raw, 1, [ImportParsedField("symbol", " ABC")])
        r_trailing = build_parsed_import_record(rec_prov, raw, 1, [ImportParsedField("symbol", "ABC ")])

        assert r_exact.parsed_sha256 != r_leading.parsed_sha256
        assert r_exact.parsed_sha256 != r_trailing.parsed_sha256
        assert r_leading.parsed_sha256 != r_trailing.parsed_sha256

    def test_empty_field_vs_absent_field_alters_sha(self):
        """AR: Empty field value is distinct from absent field."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)

        r_absent = build_parsed_import_record(rec_prov, raw, 1, [])
        r_empty = build_parsed_import_record(rec_prov, raw, 1, [ImportParsedField("description", "")])

        assert r_absent.parsed_sha256 != r_empty.parsed_sha256

    def test_provenance_record_sha_change_alters_identity(self):
        """AT: Different record_sha256 produces different parsed_sha256."""
        raw_1 = b"row_data_1"
        raw_2 = b"row_data_2"
        _, rec_prov_1 = _make_provenance_and_record(raw_1)
        _, rec_prov_2 = _make_provenance_and_record(raw_2)

        r1 = build_parsed_import_record(rec_prov_1, raw_1, 1, [ImportParsedField("symbol", "AAPL")])
        r2 = build_parsed_import_record(rec_prov_2, raw_2, 1, [ImportParsedField("symbol", "AAPL")])

        assert r1.parsed_sha256 != r2.parsed_sha256

    def test_file_identity_change_alters_identity(self):
        """AU: Same record content under different file identity produces different parsed_sha256."""
        raw = b"row_data"
        file_prov_a = build_import_file_provenance(uuid4(), uuid4(), "midas_csv", "f.csv", raw, datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))
        file_prov_b = build_import_file_provenance(uuid4(), uuid4(), "midas_csv", "f.csv", raw, datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc))

        rec_prov_a = build_import_record_provenance(file_prov_a, 1, raw)
        rec_prov_b = build_import_record_provenance(file_prov_b, 1, raw)

        r_a = build_parsed_import_record(rec_prov_a, raw, 1, [ImportParsedField("symbol", "AAPL")])
        r_b = build_parsed_import_record(rec_prov_b, raw, 1, [ImportParsedField("symbol", "AAPL")])

        assert r_a.parsed_sha256 != r_b.parsed_sha256


# ─────────────────────────────────────────────────────────────────────────────
# 7. Direct Constructor Hardening Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDirectConstructorHardening:
    """Verifies that direct instantiation of ParsedImportRecord fails closed on malformed inputs."""

    def test_canonical_direct_constructor_accepted(self):
        """AV: Direct constructor with valid components succeeds."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)
        f = ImportParsedField("symbol", "AAPL")

        expected_sha = hashlib.sha256(
            json.dumps([
                str(rec_prov.file_identity[0]),
                str(rec_prov.file_identity[1]),
                rec_prov.file_identity[2],
                rec_prov.file_identity[3],
                rec_prov.record_ordinal,
                rec_prov.record_sha256,
                1,
                [["symbol", "AAPL"]],
            ], ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        record = ParsedImportRecord(
            record_provenance=rec_prov,
            parser_revision=1,
            fields=(f,),
            parsed_sha256=expected_sha,
        )
        assert record.parsed_sha256 == expected_sha

    def test_wrong_provenance_object_rejected(self):
        """AW: Non-ImportRecordProvenance object fails closed."""
        with pytest.raises(PortfolioImportParsingError, match="record_provenance must be an ImportRecordProvenance"):
            ParsedImportRecord(
                record_provenance="not_provenance",  # type: ignore
                parser_revision=1,
                fields=(),
                parsed_sha256="0" * 64,
            )

    def test_parser_revision_bool_or_non_int_rejected(self):
        """AX: Boolean or non-int parser_revision fails closed."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)

        for bad_rev in (True, False, 1.5, "1", None):
            with pytest.raises(PortfolioImportParsingError, match="parser_revision"):
                ParsedImportRecord(
                    record_provenance=rec_prov,
                    parser_revision=bad_rev,  # type: ignore
                    fields=(),
                    parsed_sha256="0" * 64,
                )

    def test_revision_zero_or_negative_rejected(self):
        """AY: Parser revision <= 0 fails closed."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)

        for bad_rev in (0, -1, -99):
            with pytest.raises(PortfolioImportParsingError, match="parser_revision"):
                ParsedImportRecord(
                    record_provenance=rec_prov,
                    parser_revision=bad_rev,
                    fields=(),
                    parsed_sha256="0" * 64,
                )

    def test_list_fields_in_direct_constructor_rejected(self):
        """AZ: List passed to direct constructor fields is rejected (must be tuple)."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)
        f = ImportParsedField("symbol", "AAPL")

        with pytest.raises(PortfolioImportParsingError, match="fields must be an immutable tuple"):
            ParsedImportRecord(
                record_provenance=rec_prov,
                parser_revision=1,
                fields=[f],  # type: ignore
                parsed_sha256="0" * 64,
            )

    def test_unsorted_fields_in_direct_constructor_rejected(self):
        """BA: Unsorted tuple in direct constructor fails closed (no silent sorting in post_init)."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)
        f_sym = ImportParsedField("symbol", "AAPL")
        f_qty = ImportParsedField("quantity", "10")

        with pytest.raises(PortfolioImportParsingError, match="fields must be sorted ascending by field_key"):
            ParsedImportRecord(
                record_provenance=rec_prov,
                parser_revision=1,
                fields=(f_sym, f_qty),  # 'symbol' before 'quantity' is unsorted!
                parsed_sha256="0" * 64,
            )

    def test_duplicate_keys_in_direct_constructor_rejected(self):
        """BB: Duplicate keys in direct constructor fail closed."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)
        f1 = ImportParsedField("symbol", "AAPL")
        f2 = ImportParsedField("symbol", "MSFT")

        with pytest.raises(PortfolioImportParsingError, match="duplicate field_key detected: symbol"):
            ParsedImportRecord(
                record_provenance=rec_prov,
                parser_revision=1,
                fields=(f1, f2),
                parsed_sha256="0" * 64,
            )

    def test_non_field_element_in_fields_tuple_rejected(self):
        """BC: Non-ImportParsedField element in fields tuple is rejected."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)

        with pytest.raises(PortfolioImportParsingError, match="must be an ImportParsedField"):
            ParsedImportRecord(
                record_provenance=rec_prov,
                parser_revision=1,
                fields=("not_a_field",),  # type: ignore
                parsed_sha256="0" * 64,
            )

    def test_malformed_parsed_sha_rejected(self):
        """BD, BE: Malformed or uppercase SHA string is rejected."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)

        for bad_sha in ("short_sha", "Z" * 64, "0" * 63, "0" * 65, True, 123, None):
            with pytest.raises(PortfolioImportParsingError):
                ParsedImportRecord(
                    record_provenance=rec_prov,
                    parser_revision=1,
                    fields=(),
                    parsed_sha256=bad_sha,  # type: ignore
                )

    def test_incorrect_sha_rejected(self):
        """BF: Valid-format 64-char SHA that does not match computed digest fails closed."""
        raw = b"row_data"
        _, rec_prov = _make_provenance_and_record(raw)

        with pytest.raises(PortfolioImportParsingError, match="does not match canonical preimage digest"):
            ParsedImportRecord(
                record_provenance=rec_prov,
                parser_revision=1,
                fields=(),
                parsed_sha256="0" * 64,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 8. Immutability & Surface Red-Team Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSurfaceRedTeam:
    """Verifies that no forbidden raw byte, file metadata, or ledger fields exist in Phase 13C models."""

    def test_no_forbidden_fields_in_parsed_record(self):
        """ParsedImportRecord contains only record_provenance, parser_revision, fields, parsed_sha256."""
        forbidden_field_names = {
            "raw_record",
            "raw_bytes",
            "content",
            "source_bytes",
            "filename",
            "imported_at",
            "byte_length",
            "external_source",
            "external_reference",
            "transaction_id",
            "transaction_type",
            "instrument_id",
            "effective_date",
            "executed_at",
            "quantity",
            "unit_price",
            "cash_amount",
            "cash_currency",
            "reverses_transaction_id",
        }

        record_field_names = {f.name for f in fields(ParsedImportRecord)}
        overlap = record_field_names & forbidden_field_names
        assert not overlap, f"Forbidden fields found in ParsedImportRecord: {overlap}"

    def test_no_forbidden_fields_in_parsed_field(self):
        """ImportParsedField contains only field_key and field_value."""
        field_field_names = {f.name for f in fields(ImportParsedField)}
        assert field_field_names == {"field_key", "field_value"}
