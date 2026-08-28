"""
backend/tests/test_portfolio_import_provenance.py
=================================================
Tests for Phase 13A: Broker/File Import Provenance & Raw-Record Identity Foundation.

Zero network calls (pytest-socket enforced).
Pure in-memory domain evaluation.

Test Matrix:
    1. File Provenance Construction & Invariants (Valid construction, Hash exactness, No byte retention, Rejections)
    2. Source Key & Filename Syntax Contracts (ASCII lowercase, Length limits, No auto-normalization, Unicode filenames)
    3. File Identity Semantics & Independence (Filename rename invariance, Imported_at invariance, Target/Source isolation)
    4. Record Provenance Construction & Identity (Ordinal validation, Hash exactness, Record identity tuple)
    5. Direct Constructor Hardening & Red-Team (Malformed UUIDs, SHA formats, Naive datetimes, Non-tuples)
    6. Immutability & Immutability Defense (Frozen dataclasses, FrozenInstanceError on mutation)
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone, tzinfo, timedelta
import hashlib
from typing import Optional
from uuid import UUID, uuid4

import pytest

from backend.engine.private.portfolio.import_provenance import (
    ImportFileProvenance,
    ImportRecordProvenance,
    PortfolioImportProvenanceError,
    build_import_file_provenance,
    build_import_record_provenance,
)


class NullOffsetTZ(tzinfo):
    """Custom tzinfo implementation returning None for utcoffset (non-None tzinfo but not aware)."""
    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "NULL"


# ─────────────────────────────────────────────────────────────────────────────
# 1. File Provenance Construction & Invariants
# ─────────────────────────────────────────────────────────────────────────────

class TestFileProvenanceConstruction:
    """Verifies baseline file provenance construction, SHA-256 computation, and strict input validation."""

    def test_valid_file_provenance(self):
        """A, B, C, D: Valid construction computes correct SHA, exact byte_length, and does NOT retain raw bytes."""
        port_id = uuid4()
        acc_id = uuid4()
        source_key = "midas_csv"
        filename = "statement_august_2026.csv"
        raw_bytes = b"Date,Symbol,Type,Quantity,Price\n2026-08-01,AAPL,BUY,10,150.00\n"
        imported_at = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)

        prov = build_import_file_provenance(
            portfolio_id=port_id,
            account_id=acc_id,
            source_key=source_key,
            filename=filename,
            content=raw_bytes,
            imported_at=imported_at,
        )

        assert isinstance(prov, ImportFileProvenance)
        assert prov.portfolio_id == port_id
        assert prov.account_id == acc_id
        assert prov.source_key == source_key
        assert prov.filename == filename
        assert prov.content_sha256 == hashlib.sha256(raw_bytes).hexdigest()
        assert prov.byte_length == len(raw_bytes)
        assert prov.imported_at == imported_at

        # D: Verify raw content is not stored in dataclass fields or attributes
        model_field_names = {f.name for f in fields(prov)}
        assert "content" not in model_field_names
        assert "raw_bytes" not in model_field_names
        assert not hasattr(prov, "content")

    def test_empty_content_rejected(self):
        """E: Empty bytes payload b'' is rejected with PortfolioImportProvenanceError."""
        with pytest.raises(PortfolioImportProvenanceError, match="content must not be empty"):
            build_import_file_provenance(
                portfolio_id=uuid4(),
                account_id=uuid4(),
                source_key="midas_csv",
                filename="empty.csv",
                content=b"",
                imported_at=datetime.now(timezone.utc),
            )

    def test_invalid_content_types_rejected(self):
        """F, G, H: str, bytearray, and memoryview content payloads are strictly rejected."""
        port_id = uuid4()
        acc_id = uuid4()
        imported_at = datetime.now(timezone.utc)

        # str
        with pytest.raises(PortfolioImportProvenanceError, match="content must be an immutable bytes instance"):
            build_import_file_provenance(port_id, acc_id, "midas_csv", "f.csv", "string content", imported_at)  # type: ignore

        # bytearray
        with pytest.raises(PortfolioImportProvenanceError, match="content must be an immutable bytes instance"):
            build_import_file_provenance(port_id, acc_id, "midas_csv", "f.csv", bytearray(b"data"), imported_at)  # type: ignore

        # memoryview
        with pytest.raises(PortfolioImportProvenanceError, match="content must be an immutable bytes instance"):
            build_import_file_provenance(port_id, acc_id, "midas_csv", "f.csv", memoryview(b"data"), imported_at)  # type: ignore

        # bool / int
        with pytest.raises(PortfolioImportProvenanceError, match="content must be an immutable bytes instance"):
            build_import_file_provenance(port_id, acc_id, "midas_csv", "f.csv", True, imported_at)  # type: ignore

    def test_malformed_portfolio_or_account_uuid_rejected(self):
        """I, J: Malformed portfolio_id or account_id types (bool, str, int, None) are rejected."""
        valid_id = uuid4()
        valid_bytes = b"sample content"
        imported_at = datetime.now(timezone.utc)

        for invalid in (True, False, "b5b21356-32ed-4603-9be7-9f9bc97e011f", 12345, None):
            with pytest.raises(PortfolioImportProvenanceError, match="portfolio_id must be a UUID"):
                build_import_file_provenance(invalid, valid_id, "midas_csv", "f.csv", valid_bytes, imported_at)  # type: ignore

            with pytest.raises(PortfolioImportProvenanceError, match="account_id must be a UUID"):
                build_import_file_provenance(valid_id, invalid, "midas_csv", "f.csv", valid_bytes, imported_at)  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 2. Source Key & Filename Syntax Contracts
# ─────────────────────────────────────────────────────────────────────────────

class TestSourceKeyAndFilenameContracts:
    """Verifies strict regex and length rules for source_key and filename metadata."""

    def test_valid_source_keys_accepted(self):
        """K: Valid ASCII lowercase alphanumeric source keys with '._-' are accepted."""
        valid_keys = [
            "midas_csv",
            "ibkr.flex",
            "garanti_csv",
            "manual-import-v1",
            "x1",
            "a",
            "0",
            "a" * 64,
        ]
        port_id = uuid4()
        acc_id = uuid4()
        imported_at = datetime.now(timezone.utc)

        for k in valid_keys:
            prov = build_import_file_provenance(port_id, acc_id, k, "f.csv", b"content", imported_at)
            assert prov.source_key == k

    def test_invalid_source_keys_rejected(self):
        """L, M, N, O: Uppercase, whitespace, Unicode, leading punctuation, newlines, and >64-char keys rejected."""
        invalid_keys = [
            "MIDAS",
            "Midas_csv",
            " midas",
            "midas ",
            "midas csv",
            "midas_csv\n",     # A: Trailing newline (Python $ edge case)
            "midas_csv\r",     # B: Trailing CR
            "midas_csv\r\n",   # C: Trailing CRLF
            "midas_csv\t",     # D: Trailing Tab
            "midas\ncsv",      # E: Internal newline
            "\nmidas_csv",     # Leading newline
            "mıdas",           # Turkish dotless i
            "midas_ümlaut",
            "_leading_underscore",
            ".leading_dot",
            "-leading_hyphen",
            "",
            "a" * 65,
            ("a" * 64) + "\n", # Max length + newline edge
            True,
            123,
            None,
        ]
        port_id = uuid4()
        acc_id = uuid4()
        imported_at = datetime.now(timezone.utc)

        for k in invalid_keys:
            with pytest.raises(PortfolioImportProvenanceError):
                build_import_file_provenance(port_id, acc_id, k, "f.csv", b"content", imported_at)  # type: ignore

    def test_valid_unicode_filename_accepted(self):
        """P: Valid unicode filenames are preserved without modification."""
        port_id = uuid4()
        acc_id = uuid4()
        imported_at = datetime.now(timezone.utc)
        name = "2026 Ağustos İşlem Ekstresi — Midas (USD).xlsx"

        prov = build_import_file_provenance(port_id, acc_id, "midas_csv", name, b"content", imported_at)
        assert prov.filename == name

    def test_invalid_filename_rejected(self):
        """Q, R: Empty, whitespace-only, >255 chars, and non-string filenames are rejected."""
        port_id = uuid4()
        acc_id = uuid4()
        imported_at = datetime.now(timezone.utc)

        # Empty / whitespace
        for name in ("", "   ", "\t\n", "a" * 256, True, 123, None):
            with pytest.raises(PortfolioImportProvenanceError):
                build_import_file_provenance(port_id, acc_id, "midas_csv", name, b"content", imported_at)  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 3. File Identity Semantics & Target Isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestFileIdentitySemantics:
    """Verifies that file_identity encapsulates target + source + content_sha256, independent of filename or timestamp."""

    def test_filename_rename_does_not_change_file_identity(self):
        """Z: Same content + different filename produces identical file_identity."""
        port_id = uuid4()
        acc_id = uuid4()
        raw = b"fixed content bytes"
        t = datetime.now(timezone.utc)

        prov1 = build_import_file_provenance(port_id, acc_id, "midas_csv", "august.csv", raw, t)
        prov2 = build_import_file_provenance(port_id, acc_id, "midas_csv", "renamed_august.csv", raw, t)

        assert prov1.file_identity == prov2.file_identity
        assert prov1.file_identity == (port_id, acc_id, "midas_csv", hashlib.sha256(raw).hexdigest())

    def test_different_imported_at_does_not_change_file_identity(self):
        """AA: Same content + different imported_at timestamp produces identical file_identity."""
        port_id = uuid4()
        acc_id = uuid4()
        raw = b"fixed content bytes"

        t1 = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc)

        prov1 = build_import_file_provenance(port_id, acc_id, "midas_csv", "f.csv", raw, t1)
        prov2 = build_import_file_provenance(port_id, acc_id, "midas_csv", "f.csv", raw, t2)

        assert prov1.file_identity == prov2.file_identity

    def test_different_account_changes_file_identity(self):
        """AB: Same content imported for Account A vs Account B produces different file_identity."""
        port_id = uuid4()
        acc_a = uuid4()
        acc_b = uuid4()
        raw = b"fixed content bytes"
        t = datetime.now(timezone.utc)

        prov_a = build_import_file_provenance(port_id, acc_a, "midas_csv", "f.csv", raw, t)
        prov_b = build_import_file_provenance(port_id, acc_b, "midas_csv", "f.csv", raw, t)

        assert prov_a.file_identity != prov_b.file_identity

    def test_different_portfolio_changes_file_identity(self):
        """AC: Same content imported for Portfolio A vs Portfolio B produces different file_identity."""
        port_a = uuid4()
        port_b = uuid4()
        acc_id = uuid4()
        raw = b"fixed content bytes"
        t = datetime.now(timezone.utc)

        prov_a = build_import_file_provenance(port_a, acc_id, "midas_csv", "f.csv", raw, t)
        prov_b = build_import_file_provenance(port_b, acc_id, "midas_csv", "f.csv", raw, t)

        assert prov_a.file_identity != prov_b.file_identity

    def test_different_source_key_changes_file_identity(self):
        """AD: Same content under 'midas_csv' vs 'manual_csv' produces different file_identity."""
        port_id = uuid4()
        acc_id = uuid4()
        raw = b"fixed content bytes"
        t = datetime.now(timezone.utc)

        prov_midas = build_import_file_provenance(port_id, acc_id, "midas_csv", "f.csv", raw, t)
        prov_manual = build_import_file_provenance(port_id, acc_id, "manual_csv", "f.csv", raw, t)

        assert prov_midas.file_identity != prov_manual.file_identity

    def test_one_byte_content_difference_changes_sha_and_identity(self):
        """AE: One-byte difference in content bytes changes SHA-256 digest and file_identity."""
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime.now(timezone.utc)

        prov1 = build_import_file_provenance(port_id, acc_id, "midas_csv", "f.csv", b"content_a", t)
        prov2 = build_import_file_provenance(port_id, acc_id, "midas_csv", "f.csv", b"content_b", t)

        assert prov1.content_sha256 != prov2.content_sha256
        assert prov1.file_identity != prov2.file_identity


# ─────────────────────────────────────────────────────────────────────────────
# 4. Record Provenance Construction & Identity
# ─────────────────────────────────────────────────────────────────────────────

class TestRecordProvenanceConstruction:
    """Verifies raw record provenance builder, hash calculation, and record identity semantics."""

    def test_valid_record_provenance(self):
        """AF, AG, AH: Valid record provenance computes correct record_sha256 and does NOT store raw bytes."""
        file_prov = build_import_file_provenance(
            portfolio_id=uuid4(),
            account_id=uuid4(),
            source_key="midas_csv",
            filename="statement.csv",
            content=b"row1\nrow2\n",
            imported_at=datetime.now(timezone.utc),
        )
        raw_record = b"2026-08-01,AAPL,BUY,10,150.00"

        rec_prov = build_import_record_provenance(
            file_provenance=file_prov,
            record_ordinal=1,
            raw_record=raw_record,
        )

        assert isinstance(rec_prov, ImportRecordProvenance)
        assert rec_prov.file_identity == file_prov.file_identity
        assert rec_prov.record_ordinal == 1
        assert rec_prov.record_sha256 == hashlib.sha256(raw_record).hexdigest()

        # AH: No raw bytes field in dataclass
        rec_field_names = {f.name for f in fields(rec_prov)}
        assert "raw_record" not in rec_field_names
        assert "content" not in rec_field_names

    def test_record_identity_tuple_structure(self):
        """Record identity exposes canonical 6-tuple."""
        file_prov = build_import_file_provenance(
            portfolio_id=uuid4(),
            account_id=uuid4(),
            source_key="midas_csv",
            filename="f.csv",
            content=b"content",
            imported_at=datetime.now(timezone.utc),
        )
        raw = b"record"
        rec = build_import_record_provenance(file_prov, 3, raw)

        expected_identity = (
            file_prov.portfolio_id,
            file_prov.account_id,
            file_prov.source_key,
            file_prov.content_sha256,
            3,
            hashlib.sha256(raw).hexdigest(),
        )
        assert rec.record_identity == expected_identity

    def test_invalid_raw_record_bytes_rejected(self):
        """AI, AJ, AK: Empty raw record, str, and bytearray are rejected."""
        file_prov = build_import_file_provenance(
            portfolio_id=uuid4(),
            account_id=uuid4(),
            source_key="midas_csv",
            filename="f.csv",
            content=b"content",
            imported_at=datetime.now(timezone.utc),
        )

        # Empty bytes
        with pytest.raises(PortfolioImportProvenanceError, match="raw_record must not be empty"):
            build_import_record_provenance(file_prov, 1, b"")

        # str
        with pytest.raises(PortfolioImportProvenanceError, match="raw_record must be an immutable bytes instance"):
            build_import_record_provenance(file_prov, 1, "str record")  # type: ignore

        # bytearray
        with pytest.raises(PortfolioImportProvenanceError, match="raw_record must be an immutable bytes instance"):
            build_import_record_provenance(file_prov, 1, bytearray(b"record"))  # type: ignore

    def test_invalid_record_ordinal_rejected(self):
        """AL, AM, AN: Ordinal 0, negative ordinals, and booleans are rejected."""
        file_prov = build_import_file_provenance(
            portfolio_id=uuid4(),
            account_id=uuid4(),
            source_key="midas_csv",
            filename="f.csv",
            content=b"content",
            imported_at=datetime.now(timezone.utc),
        )

        for invalid_ord in (0, -1, -99, True, False, "1", 1.5, None):
            with pytest.raises(PortfolioImportProvenanceError):
                build_import_record_provenance(file_prov, invalid_ord, b"record")  # type: ignore

    def test_same_raw_bytes_same_ordinal_produces_identical_identity(self):
        """AO: Same raw bytes at same ordinal produces identical record_identity."""
        file_prov = build_import_file_provenance(uuid4(), uuid4(), "midas_csv", "f.csv", b"content", datetime.now(timezone.utc))
        raw = b"same record bytes"

        r1 = build_import_record_provenance(file_prov, 1, raw)
        r2 = build_import_record_provenance(file_prov, 1, raw)

        assert r1.record_identity == r2.record_identity

    def test_same_raw_bytes_different_ordinal_produces_different_identity(self):
        """AP: Same raw bytes at different ordinals produces different record_identity."""
        file_prov = build_import_file_provenance(uuid4(), uuid4(), "midas_csv", "f.csv", b"content", datetime.now(timezone.utc))
        raw = b"same record bytes"

        r1 = build_import_record_provenance(file_prov, 1, raw)
        r2 = build_import_record_provenance(file_prov, 2, raw)

        assert r1.record_identity != r2.record_identity

    def test_same_ordinal_different_raw_bytes_produces_different_identity(self):
        """AQ: Different raw bytes at same ordinal produces different record_identity."""
        file_prov = build_import_file_provenance(uuid4(), uuid4(), "midas_csv", "f.csv", b"content", datetime.now(timezone.utc))

        r1 = build_import_record_provenance(file_prov, 1, b"record_alpha")
        r2 = build_import_record_provenance(file_prov, 1, b"record_beta")

        assert r1.record_identity != r2.record_identity

    def test_same_record_under_different_file_identity(self):
        """AR: Same record bytes under different file identity produces different record_identity."""
        file_prov_a = build_import_file_provenance(uuid4(), uuid4(), "midas_csv", "f.csv", b"file_a", datetime.now(timezone.utc))
        file_prov_b = build_import_file_provenance(uuid4(), uuid4(), "midas_csv", "f.csv", b"file_b", datetime.now(timezone.utc))
        raw = b"identical row bytes"

        r_a = build_import_record_provenance(file_prov_a, 1, raw)
        r_b = build_import_record_provenance(file_prov_b, 1, raw)

        assert r_a.record_identity != r_b.record_identity


# ─────────────────────────────────────────────────────────────────────────────
# 5. Direct Constructor Hardening & Red-Team
# ─────────────────────────────────────────────────────────────────────────────

class TestDirectConstructorHardening:
    """Verifies fail-closed validations on direct model constructions."""

    def test_file_provenance_direct_constructor_validations(self):
        """S, T, U, V, W, X, Y: Direct ImportFileProvenance constructor validates all field types and constraints."""
        valid_pid = uuid4()
        valid_aid = uuid4()
        valid_sha = hashlib.sha256(b"test").hexdigest()
        valid_now = datetime.now(timezone.utc)

        # S: Invalid SHA format (non-hex, wrong length)
        with pytest.raises(PortfolioImportProvenanceError, match="content_sha256 must be exactly 64"):
            ImportFileProvenance(valid_pid, valid_aid, "midas_csv", "f.csv", "invalid_sha", 10, valid_now)

        # T: Uppercase SHA rejected
        with pytest.raises(PortfolioImportProvenanceError, match="content_sha256 must be exactly 64"):
            ImportFileProvenance(valid_pid, valid_aid, "midas_csv", "f.csv", valid_sha.upper(), 10, valid_now)

        # F, G: Content SHA + newline / CRLF rejected
        for bad_sha in (
            valid_sha + "\n",
            valid_sha + "\r",
            valid_sha + "\r\n",
            valid_sha + " ",
            " " + valid_sha,
            "\n" + valid_sha,
        ):
            with pytest.raises(PortfolioImportProvenanceError, match="content_sha256 must be exactly 64"):
                ImportFileProvenance(valid_pid, valid_aid, "midas_csv", "f.csv", bad_sha, 10, valid_now)

        # U, V: byte_length bool / <= 0 rejected
        for bad_len in (True, False, 0, -5, "10", None):
            with pytest.raises(PortfolioImportProvenanceError):
                ImportFileProvenance(valid_pid, valid_aid, "midas_csv", "f.csv", valid_sha, bad_len, valid_now)  # type: ignore

        # W: Naive imported_at rejected
        with pytest.raises(PortfolioImportProvenanceError, match="imported_at must be timezone-aware"):
            ImportFileProvenance(valid_pid, valid_aid, "midas_csv", "f.csv", valid_sha, 10, datetime(2026, 8, 28, 12, 0))

        # X: NullOffsetTZ imported_at rejected
        with pytest.raises(PortfolioImportProvenanceError, match="imported_at must be timezone-aware"):
            ImportFileProvenance(valid_pid, valid_aid, "midas_csv", "f.csv", valid_sha, 10, datetime(2026, 8, 28, 12, 0, tzinfo=NullOffsetTZ()))

        # Y: Non-UTC (+03:00) aware datetime accepted and preserved
        plus3_tz = timezone(timedelta(hours=3))
        dt_plus3 = datetime(2026, 8, 28, 15, 0, tzinfo=plus3_tz)
        prov = ImportFileProvenance(valid_pid, valid_aid, "midas_csv", "f.csv", valid_sha, 10, dt_plus3)
        assert prov.imported_at == dt_plus3
        assert prov.imported_at.tzinfo == plus3_tz

    def test_record_provenance_direct_constructor_validations(self):
        """Direct ImportRecordProvenance constructor validates file_identity tuple, ordinal, and SHA."""
        valid_pid = uuid4()
        valid_aid = uuid4()
        valid_file_sha = hashlib.sha256(b"file").hexdigest()
        valid_rec_sha = hashlib.sha256(b"rec").hexdigest()
        valid_file_id = (valid_pid, valid_aid, "midas_csv", valid_file_sha)

        # Invalid file_identity (list instead of tuple)
        with pytest.raises(PortfolioImportProvenanceError, match="file_identity must be a 4-tuple"):
            ImportRecordProvenance(list(valid_file_id), 1, valid_rec_sha)  # type: ignore

        # Invalid file_identity length
        with pytest.raises(PortfolioImportProvenanceError, match="file_identity must be a 4-tuple"):
            ImportRecordProvenance((valid_pid, valid_aid, "midas_csv"), 1, valid_rec_sha)  # type: ignore

        # Invalid ordinal
        with pytest.raises(PortfolioImportProvenanceError, match="record_ordinal must be at least 1"):
            ImportRecordProvenance(valid_file_id, 0, valid_rec_sha)

        # Invalid record SHA
        with pytest.raises(PortfolioImportProvenanceError, match="record_sha256 must be exactly 64"):
            ImportRecordProvenance(valid_file_id, 1, "short_sha")

        # H: Record SHA + newline / CRLF rejected
        for bad_rec_sha in (
            valid_rec_sha + "\n",
            valid_rec_sha + "\r",
            valid_rec_sha + "\r\n",
            valid_rec_sha + " ",
            " " + valid_rec_sha,
            "\n" + valid_rec_sha,
        ):
            with pytest.raises(PortfolioImportProvenanceError, match="record_sha256 must be exactly 64"):
                ImportRecordProvenance(valid_file_id, 1, bad_rec_sha)

        # I: file_identity embedded SHA + newline rejected
        for bad_f_sha in (
            valid_file_sha + "\n",
            valid_file_sha + "\r\n",
        ):
            bad_file_id = (valid_pid, valid_aid, "midas_csv", bad_f_sha)
            with pytest.raises(PortfolioImportProvenanceError, match="file_identity\\[3\\] \\(content_sha256\\) must be exactly 64"):
                ImportRecordProvenance(bad_file_id, 1, valid_rec_sha)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Immutability & Mutation Defense
# ─────────────────────────────────────────────────────────────────────────────

class TestImmutabilityAndMutationDefense:
    """Verifies that import provenance models are strictly frozen."""

    def test_file_provenance_frozen(self):
        """Mutation of ImportFileProvenance fields raises FrozenInstanceError."""
        prov = build_import_file_provenance(uuid4(), uuid4(), "midas_csv", "f.csv", b"content", datetime.now(timezone.utc))

        with pytest.raises(FrozenInstanceError):
            prov.filename = "new_name.csv"  # type: ignore

        with pytest.raises(FrozenInstanceError):
            prov.content_sha256 = "0" * 64  # type: ignore

    def test_record_provenance_frozen(self):
        """Mutation of ImportRecordProvenance fields raises FrozenInstanceError."""
        file_prov = build_import_file_provenance(uuid4(), uuid4(), "midas_csv", "f.csv", b"content", datetime.now(timezone.utc))
        rec = build_import_record_provenance(file_prov, 1, b"row")

        with pytest.raises(FrozenInstanceError):
            rec.record_ordinal = 2  # type: ignore

        with pytest.raises(FrozenInstanceError):
            rec.record_sha256 = "0" * 64  # type: ignore
