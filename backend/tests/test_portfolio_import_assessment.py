"""
backend/tests/test_portfolio_import_assessment.py
=================================================
Tests for Phase 13G: Immutable Import Interpretation Assessment & Batch Review Foundation.

Zero network calls (pytest-socket enforced).
Pure in-memory domain evaluation using real Phase 13A-13F builders and models.

Test Matrix:
    1. Diagnostic Model Contract (Code grammar, message limits, field-key grammar, immutability)
    2. Record Assessment Basic Contract (READY/UNRESOLVED/REJECTED, type enforcement, immutability)
    3. Diagnostic Ordering & Duplicate Contract (Canonical sort, duplicate detection, field-context validation)
    4. Record Assessment Identity (Determinism, status sensitivity, diagnostic sensitivity)
    5. Assessed Batch Coverage & 1:1 Correspondence (Full coverage, omission, extra, duplicate, foreign records)
    6. Assessed Batch Ordering & Input Invariance (Shuffled builder input, direct constructor order check)
    7. Assessment Manifest Preimage & Cryptographic Hash (Independent calculation, sensitivity, format check)
    8. Batch Status & Record Counts (ready_count, unresolved_count, rejected_count, total record_count)
    9. Surface Red-Team Invariants (No transaction economics, no raw bytes, no repository)
    10. Canonical CSV v1 Real Pipeline Integration (2-row CSV -> staging pipeline -> assessment batch)
"""

from __future__ import annotations

from dataclasses import fields, FrozenInstanceError
from datetime import datetime, timezone
import hashlib
import json
from typing import Sequence, Tuple
from uuid import uuid4

import pytest

from backend.engine.private.portfolio.import_assessment import (
    ImportAssessmentBatch,
    ImportAssessmentDiagnostic,
    ImportAssessmentStatus,
    ImportRecordAssessment,
    PortfolioImportAssessmentError,
    build_import_assessment_batch,
    build_import_record_assessment,
    _compute_assessment_manifest_sha256,
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

def _make_test_parsed_manifest(record_count: int = 2):
    """Builds a real, verified ParsedImportBatchManifest with N records."""
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
        for i in range(record_count)
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
        for i in range(record_count)
    ]

    return build_parsed_import_batch_manifest(
        raw_manifest=raw_manifest,
        parser_revision=parser_revision,
        parsed_records=parsed_records,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Diagnostic Model Contract Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDiagnosticModelContract:
    """Verifies grammar, bounds, immutability, and field-key constraints on diagnostics."""

    def test_valid_record_level_diagnostic(self):
        """A: Valid record-level diagnostic with field_key=None."""
        diag = ImportAssessmentDiagnostic(
            code="missing_instrument",
            message="No instrument matching ticker was found.",
            field_key=None,
        )
        assert diag.code == "missing_instrument"
        assert diag.message == "No instrument matching ticker was found."
        assert diag.field_key is None

    def test_valid_field_level_diagnostic(self):
        """B: Valid field-level diagnostic with field_key."""
        diag = ImportAssessmentDiagnostic(
            code="invalid_quantity",
            message="Quantity text cannot be parsed as decimal.",
            field_key="quantity",
        )
        assert diag.code == "invalid_quantity"
        assert diag.field_key == "quantity"

    def test_invalid_diagnostic_codes_rejected(self):
        """C-F: Uppercase, hyphenated, newline, digit-first, and Unicode codes fail closed."""
        invalid_codes = [
            "MissingInstrument",    # C: Uppercase
            "missing-instrument",   # D: Hyphen
            "missing_instrument\n", # E: Newline
            "1missing",             # Digit-first
            "türkçe",               # F: Unicode
            "",                     # Empty
            "a" * 65,               # >64 chars
            123,                    # Non-string
            True,                   # Bool
        ]
        for bad_code in invalid_codes:
            with pytest.raises(PortfolioImportAssessmentError):
                ImportAssessmentDiagnostic(code=bad_code, message="Valid message")  # type: ignore

    def test_invalid_diagnostic_messages_rejected(self):
        """G-I: Empty, whitespace-only, and oversized (>2048) messages fail closed."""
        invalid_messages = [
            "",                  # G: Empty
            "   \t\n",           # H: Whitespace only
            "a" * 2049,          # I: >2048 chars
            123,                 # Non-string
            True,                # Bool
            None,                # None
        ]
        for bad_msg in invalid_messages:
            with pytest.raises(PortfolioImportAssessmentError):
                ImportAssessmentDiagnostic(code="valid_code", message=bad_msg)  # type: ignore

    def test_field_key_grammar_enforced(self):
        """J, K: Valid field_key accepted; malformed field_key fails closed."""
        diag = ImportAssessmentDiagnostic(code="err", message="Msg", field_key="trade_date")
        assert diag.field_key == "trade_date"

        invalid_field_keys = [
            "TradeDate",
            "1trade",
            "trade-date",
            "trade_date\n",
            "a" * 65,
            123,
            True,
        ]
        for bad_fk in invalid_field_keys:
            with pytest.raises(PortfolioImportAssessmentError):
                ImportAssessmentDiagnostic(code="err", message="Msg", field_key=bad_fk)  # type: ignore

    def test_diagnostic_frozen_immutability(self):
        """L: Frozen mutation fails closed."""
        diag = ImportAssessmentDiagnostic(code="err", message="Msg")
        with pytest.raises(FrozenInstanceError):
            diag.code = "other"  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 2. Record Assessment Basic Contract Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestRecordAssessmentBasicContract:
    """Verifies status rules, diagnostic requirements, and type integrity for ImportRecordAssessment."""

    def test_ready_status_with_empty_diagnostics_accepted(self):
        """M: READY status with empty diagnostics succeeds."""
        manifest = _make_test_parsed_manifest(1)
        rec = manifest.parsed_records[0]

        ass = ImportRecordAssessment(
            parsed_record=rec,
            status=ImportAssessmentStatus.READY,
            diagnostics=(),
        )
        assert ass.status == ImportAssessmentStatus.READY
        assert ass.diagnostics == ()

    def test_ready_status_with_diagnostics_rejected(self):
        """N: READY status carrying diagnostics fails closed."""
        manifest = _make_test_parsed_manifest(1)
        rec = manifest.parsed_records[0]
        diag = ImportAssessmentDiagnostic(code="note", message="Some note")

        with pytest.raises(PortfolioImportAssessmentError, match="READY assessment must not contain diagnostics"):
            ImportRecordAssessment(
                parsed_record=rec,
                status=ImportAssessmentStatus.READY,
                diagnostics=(diag,),
            )

    def test_unresolved_status_with_diagnostic_accepted(self):
        """O: UNRESOLVED status with at least one diagnostic succeeds."""
        manifest = _make_test_parsed_manifest(1)
        rec = manifest.parsed_records[0]
        diag = ImportAssessmentDiagnostic(code="missing_instrument", message="Unresolved symbol")

        ass = ImportRecordAssessment(
            parsed_record=rec,
            status=ImportAssessmentStatus.UNRESOLVED,
            diagnostics=(diag,),
        )
        assert ass.status == ImportAssessmentStatus.UNRESOLVED
        assert len(ass.diagnostics) == 1

    def test_unresolved_status_with_empty_diagnostics_rejected(self):
        """P: UNRESOLVED status with empty diagnostics fails closed."""
        manifest = _make_test_parsed_manifest(1)
        rec = manifest.parsed_records[0]

        with pytest.raises(PortfolioImportAssessmentError, match="UNRESOLVED assessment must contain at least one diagnostic"):
            ImportRecordAssessment(
                parsed_record=rec,
                status=ImportAssessmentStatus.UNRESOLVED,
                diagnostics=(),
            )

    def test_rejected_status_with_diagnostic_accepted(self):
        """Q: REJECTED status with at least one diagnostic succeeds."""
        manifest = _make_test_parsed_manifest(1)
        rec = manifest.parsed_records[0]
        diag = ImportAssessmentDiagnostic(code="unsupported_event", message="Non-trade row")

        ass = ImportRecordAssessment(
            parsed_record=rec,
            status=ImportAssessmentStatus.REJECTED,
            diagnostics=(diag,),
        )
        assert ass.status == ImportAssessmentStatus.REJECTED
        assert len(ass.diagnostics) == 1

    def test_rejected_status_with_empty_diagnostics_rejected(self):
        """R: REJECTED status with empty diagnostics fails closed."""
        manifest = _make_test_parsed_manifest(1)
        rec = manifest.parsed_records[0]

        with pytest.raises(PortfolioImportAssessmentError, match="REJECTED assessment must contain at least one diagnostic"):
            ImportRecordAssessment(
                parsed_record=rec,
                status=ImportAssessmentStatus.REJECTED,
                diagnostics=(),
            )

    def test_invalid_parsed_record_type_rejected(self):
        """S: Non-ParsedImportRecord object fails closed."""
        diag = ImportAssessmentDiagnostic(code="err", message="Msg")
        with pytest.raises(PortfolioImportAssessmentError, match="parsed_record must be a ParsedImportRecord"):
            ImportRecordAssessment(
                parsed_record={"fake": "record"},  # type: ignore
                status=ImportAssessmentStatus.UNRESOLVED,
                diagnostics=(diag,),
            )

    def test_string_status_rejected(self):
        """T: String status (e.g. 'ready') fails closed (requires Enum member)."""
        manifest = _make_test_parsed_manifest(1)
        rec = manifest.parsed_records[0]
        with pytest.raises(PortfolioImportAssessmentError, match="status must be an ImportAssessmentStatus enum member"):
            ImportRecordAssessment(
                parsed_record=rec,
                status="ready",  # type: ignore
                diagnostics=(),
            )

    def test_non_tuple_direct_diagnostics_rejected(self):
        """U: Direct constructor with list diagnostics fails closed."""
        manifest = _make_test_parsed_manifest(1)
        rec = manifest.parsed_records[0]
        diag = ImportAssessmentDiagnostic(code="err", message="Msg")
        with pytest.raises(PortfolioImportAssessmentError, match="diagnostics must be an immutable tuple"):
            ImportRecordAssessment(
                parsed_record=rec,
                status=ImportAssessmentStatus.UNRESOLVED,
                diagnostics=[diag],  # type: ignore
            )

    def test_non_diagnostic_item_rejected(self):
        """V: Non-ImportAssessmentDiagnostic item in diagnostics fails closed."""
        manifest = _make_test_parsed_manifest(1)
        rec = manifest.parsed_records[0]
        with pytest.raises(PortfolioImportAssessmentError, match="must be an ImportAssessmentDiagnostic instance"):
            ImportRecordAssessment(
                parsed_record=rec,
                status=ImportAssessmentStatus.UNRESOLVED,
                diagnostics=("not_a_diagnostic",),  # type: ignore
            )

        with pytest.raises(PortfolioImportAssessmentError, match="must be an ImportAssessmentDiagnostic instance"):
            build_import_record_assessment(
                parsed_record=rec,
                status=ImportAssessmentStatus.UNRESOLVED,
                diagnostics=["not_a_diagnostic"],  # type: ignore
            )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Diagnostic Ordering & Duplicate Contract Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDiagnosticOrderingAndDuplicateContract:
    """Verifies canonical sorting, duplicate (code, field_key) rejection, and field context binding."""

    def test_builder_canonicalizes_unordered_diagnostics(self):
        """W: Builder accepts unordered list and sorts canonically."""
        manifest = _make_test_parsed_manifest(1)
        rec = manifest.parsed_records[0]

        d1 = ImportAssessmentDiagnostic(code="b_code", message="Msg 1")
        d2 = ImportAssessmentDiagnostic(code="a_code", message="Msg 2")

        ass = build_import_record_assessment(
            parsed_record=rec,
            status=ImportAssessmentStatus.UNRESOLVED,
            diagnostics=[d1, d2],
        )
        assert ass.diagnostics == (d2, d1)

    def test_direct_constructor_unsorted_tuple_rejected(self):
        """X: Direct constructor with unsorted diagnostics tuple fails closed."""
        manifest = _make_test_parsed_manifest(1)
        rec = manifest.parsed_records[0]

        d1 = ImportAssessmentDiagnostic(code="b_code", message="Msg 1")
        d2 = ImportAssessmentDiagnostic(code="a_code", message="Msg 2")

        with pytest.raises(PortfolioImportAssessmentError, match="Diagnostics must be canonically sorted"):
            ImportRecordAssessment(
                parsed_record=rec,
                status=ImportAssessmentStatus.UNRESOLVED,
                diagnostics=(d1, d2),
            )

    def test_duplicate_diagnostic_same_message_rejected(self):
        """Y: Duplicate (code, field_key) with same message fails closed."""
        manifest = _make_test_parsed_manifest(1)
        rec = manifest.parsed_records[0]

        d1 = ImportAssessmentDiagnostic(code="missing_value", message="Missing", field_key="quantity")
        d2 = ImportAssessmentDiagnostic(code="missing_value", message="Missing", field_key="quantity")

        with pytest.raises(PortfolioImportAssessmentError, match="Duplicate diagnostic identity detected"):
            build_import_record_assessment(rec, ImportAssessmentStatus.UNRESOLVED, [d1, d2])

    def test_duplicate_diagnostic_different_message_rejected(self):
        """Z: Duplicate (code, field_key) with different messages fails closed."""
        manifest = _make_test_parsed_manifest(1)
        rec = manifest.parsed_records[0]

        d1 = ImportAssessmentDiagnostic(code="missing_value", message="Missing val", field_key="quantity")
        d2 = ImportAssessmentDiagnostic(code="missing_value", message="Other explanation", field_key="quantity")

        with pytest.raises(PortfolioImportAssessmentError, match="Duplicate diagnostic identity detected"):
            build_import_record_assessment(rec, ImportAssessmentStatus.UNRESOLVED, [d1, d2])

    def test_same_code_different_fields_accepted(self):
        """AA: Same code with distinct field_key values succeeds."""
        manifest = _make_test_parsed_manifest(1)
        rec = manifest.parsed_records[0]

        d1 = ImportAssessmentDiagnostic(code="missing_value", message="Missing qty", field_key="quantity")
        d2 = ImportAssessmentDiagnostic(code="missing_value", message="Missing price", field_key="price")

        ass = build_import_record_assessment(rec, ImportAssessmentStatus.UNRESOLVED, [d1, d2])
        assert len(ass.diagnostics) == 2

    def test_field_context_validation(self):
        """AB, AC: Diagnostic field_key must exist in parsed_record.fields."""
        manifest = _make_test_parsed_manifest(1)
        rec = manifest.parsed_records[0]  # has symbol, quantity, price

        # AB: Existing field
        valid_diag = ImportAssessmentDiagnostic(code="err", message="Msg", field_key="symbol")
        ass = build_import_record_assessment(rec, ImportAssessmentStatus.UNRESOLVED, [valid_diag])
        assert ass.diagnostics[0].field_key == "symbol"

        # AC: Missing field
        invalid_diag = ImportAssessmentDiagnostic(code="err", message="Msg", field_key="nonexistent_field")
        with pytest.raises(PortfolioImportAssessmentError, match="Diagnostic references non-existent field_key 'nonexistent_field'"):
            build_import_record_assessment(rec, ImportAssessmentStatus.UNRESOLVED, [invalid_diag])

    def test_record_level_field_key_none_accepted(self):
        """AD: Record-level diagnostic (field_key=None) is accepted regardless of fields."""
        manifest = _make_test_parsed_manifest(1)
        rec = manifest.parsed_records[0]

        diag = ImportAssessmentDiagnostic(code="file_corruption", message="Corrupt row syntax", field_key=None)
        ass = build_import_record_assessment(rec, ImportAssessmentStatus.REJECTED, [diag])
        assert ass.diagnostics[0].field_key is None


# ─────────────────────────────────────────────────────────────────────────────
# 4. Record Assessment Identity Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestRecordAssessmentIdentity:
    """Verifies deterministic composite tuple assessment_identity."""

    def test_assessment_identity_determinism_and_sensitivity(self):
        """AE-AH: Identity is deterministic, sensitive to status/message, and invariant to builder input order."""
        manifest = _make_test_parsed_manifest(1)
        rec = manifest.parsed_records[0]

        d1 = ImportAssessmentDiagnostic(code="b_code", message="Msg B", field_key="quantity")
        d2 = ImportAssessmentDiagnostic(code="a_code", message="Msg A", field_key="price")

        # AE: Deterministic
        ass1 = build_import_record_assessment(rec, ImportAssessmentStatus.UNRESOLVED, [d1, d2])
        ass2 = build_import_record_assessment(rec, ImportAssessmentStatus.UNRESOLVED, [d2, d1])
        assert ass1.assessment_identity == ass2.assessment_identity

        # AF: Status sensitivity
        ass_rejected = build_import_record_assessment(rec, ImportAssessmentStatus.REJECTED, [d1, d2])
        assert ass1.assessment_identity != ass_rejected.assessment_identity

        # AG: Message sensitivity
        d1_mod = ImportAssessmentDiagnostic(code="b_code", message="Modified Msg", field_key="quantity")
        ass_mod = build_import_record_assessment(rec, ImportAssessmentStatus.UNRESOLVED, [d1_mod, d2])
        assert ass1.assessment_identity != ass_mod.assessment_identity


# ─────────────────────────────────────────────────────────────────────────────
# 5. Assessed Batch Coverage & 1:1 Correspondence Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestAssessedBatchCoverageAndCorrespondence:
    """Verifies complete 1:1 coverage against ParsedImportBatchManifest."""

    def test_empty_parsed_batch_accepted(self):
        """AI: Empty parsed batch with empty assessments succeeds."""
        manifest = _make_test_parsed_manifest(0)
        batch = build_import_assessment_batch(manifest, [])
        assert batch.record_count == 0
        assert batch.assessments == ()
        assert len(batch.assessment_manifest_sha256) == 64

    def test_multi_record_full_coverage_accepted(self):
        """AJ, AK: Full coverage of all records in parsed manifest succeeds."""
        manifest = _make_test_parsed_manifest(2)
        r0 = manifest.parsed_records[0]
        r1 = manifest.parsed_records[1]

        a0 = build_import_record_assessment(r0, ImportAssessmentStatus.READY)
        a1 = build_import_record_assessment(
            r1,
            ImportAssessmentStatus.UNRESOLVED,
            [ImportAssessmentDiagnostic(code="err", message="Msg", field_key="symbol")],
        )

        batch = build_import_assessment_batch(manifest, [a0, a1])
        assert batch.record_count == 2
        assert batch.ready_count == 1
        assert batch.unresolved_count == 1
        assert batch.rejected_count == 0

    def test_omitted_assessment_rejected(self):
        """AL: Assessment missing for one parsed record fails closed."""
        manifest = _make_test_parsed_manifest(2)
        r0 = manifest.parsed_records[0]
        a0 = build_import_record_assessment(r0, ImportAssessmentStatus.READY)

        with pytest.raises(PortfolioImportAssessmentError, match="Assessment count mismatch: expected 2 from parsed manifest, got 1"):
            build_import_assessment_batch(manifest, [a0])

    def test_extra_assessment_rejected(self):
        """AM: Extra assessments fail closed."""
        manifest = _make_test_parsed_manifest(1)
        r0 = manifest.parsed_records[0]
        a0 = build_import_record_assessment(r0, ImportAssessmentStatus.READY)
        a1 = build_import_record_assessment(
            r0,
            ImportAssessmentStatus.UNRESOLVED,
            [ImportAssessmentDiagnostic(code="err", message="Msg", field_key="symbol")],
        )

        with pytest.raises(PortfolioImportAssessmentError, match="Duplicate record ordinal 1"):
            build_import_assessment_batch(manifest, [a0, a1])

    def test_foreign_parsed_record_rejected(self):
        """AO, AP: Assessment for a parsed record from another batch fails closed."""
        manifest1 = _make_test_parsed_manifest(2)
        manifest2 = _make_test_parsed_manifest(2)

        a0 = build_import_record_assessment(manifest1.parsed_records[0], ImportAssessmentStatus.READY)
        # Foreign record from manifest2
        a1_foreign = build_import_record_assessment(manifest2.parsed_records[1], ImportAssessmentStatus.READY)

        with pytest.raises(PortfolioImportAssessmentError, match="does not match parsed record in manifest"):
            build_import_assessment_batch(manifest1, [a0, a1_foreign])


# ─────────────────────────────────────────────────────────────────────────────
# 6. Assessed Batch Ordering & Input Invariance Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestAssessedBatchOrderingAndInvariance:
    """Verifies that builder input order does not affect resulting batch manifest."""

    def test_shuffled_builder_input_invariance(self):
        """AQ-AT: Shuffled assessment inputs produce identical batch, identical SHA, and identical identity."""
        manifest = _make_test_parsed_manifest(3)
        r0, r1, r2 = manifest.parsed_records

        a0 = build_import_record_assessment(r0, ImportAssessmentStatus.READY)
        a1 = build_import_record_assessment(
            r1,
            ImportAssessmentStatus.UNRESOLVED,
            [ImportAssessmentDiagnostic(code="err", message="Msg", field_key="symbol")],
        )
        a2 = build_import_record_assessment(
            r2,
            ImportAssessmentStatus.REJECTED,
            [ImportAssessmentDiagnostic(code="bad_row", message="Corrupt row")],
        )

        batch_ordered = build_import_assessment_batch(manifest, [a0, a1, a2])
        batch_shuffled = build_import_assessment_batch(manifest, [a2, a0, a1])

        assert batch_ordered.assessments == batch_shuffled.assessments
        assert batch_ordered.assessment_manifest_sha256 == batch_shuffled.assessment_manifest_sha256
        assert batch_ordered.assessment_manifest_identity == batch_shuffled.assessment_manifest_identity

    def test_unsorted_direct_constructor_rejected(self):
        """AU: Direct constructor with unsorted assessments tuple fails closed."""
        manifest = _make_test_parsed_manifest(2)
        r0, r1 = manifest.parsed_records

        a0 = build_import_record_assessment(r0, ImportAssessmentStatus.READY)
        a1 = build_import_record_assessment(
            r1,
            ImportAssessmentStatus.UNRESOLVED,
            [ImportAssessmentDiagnostic(code="err", message="Msg", field_key="symbol")],
        )

        with pytest.raises(PortfolioImportAssessmentError, match="does not match parsed manifest record at index 0"):
            ImportAssessmentBatch(
                parsed_manifest=manifest,
                assessments=(a1, a0),
                assessment_manifest_sha256="a" * 64,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 7. Assessment Manifest Preimage & Cryptographic Hash Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestAssessmentManifestHash:
    """Verifies preimage calculation, determinism, sensitivity, and strict hash syntax."""

    def test_independent_preimage_hash_matches(self):
        """AV: Independently calculated compact JSON SHA matches batch digest."""
        manifest = _make_test_parsed_manifest(2)
        r0, r1 = manifest.parsed_records

        a0 = build_import_record_assessment(r0, ImportAssessmentStatus.READY)
        diag = ImportAssessmentDiagnostic(code="err", message="Msg", field_key="symbol")
        a1 = build_import_record_assessment(r1, ImportAssessmentStatus.UNRESOLVED, [diag])

        batch = build_import_assessment_batch(manifest, [a0, a1])

        file_prov = manifest.raw_manifest.file_provenance
        raw_man = manifest.raw_manifest
        expected_preimage = [
            str(file_prov.portfolio_id),
            str(file_prov.account_id),
            file_prov.source_key,
            file_prov.content_sha256,
            raw_man.manifest_sha256,
            manifest.parser_revision,
            manifest.parsed_manifest_sha256,
            [
                [
                    1,
                    r0.parsed_sha256,
                    "ready",
                    [],
                ],
                [
                    2,
                    r1.parsed_sha256,
                    "unresolved",
                    [["err", "symbol", "Msg"]],
                ],
            ],
        ]
        json_bytes = json.dumps(expected_preimage, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        expected_sha = hashlib.sha256(json_bytes).hexdigest()

        assert batch.assessment_manifest_sha256 == expected_sha

    def test_hash_sensitivity_to_status_and_diagnostics(self):
        """AX-BA: Changing status or any diagnostic attribute alters manifest SHA."""
        manifest = _make_test_parsed_manifest(1)
        rec = manifest.parsed_records[0]

        d_base = ImportAssessmentDiagnostic(code="err_a", message="Msg A", field_key="symbol")
        batch_base = build_import_assessment_batch(
            manifest,
            [build_import_record_assessment(rec, ImportAssessmentStatus.UNRESOLVED, [d_base])],
        )

        # AX: Status change
        batch_rej = build_import_assessment_batch(
            manifest,
            [build_import_record_assessment(rec, ImportAssessmentStatus.REJECTED, [d_base])],
        )
        assert batch_base.assessment_manifest_sha256 != batch_rej.assessment_manifest_sha256

        # AY: Code change
        d_code = ImportAssessmentDiagnostic(code="err_b", message="Msg A", field_key="symbol")
        batch_code = build_import_assessment_batch(
            manifest,
            [build_import_record_assessment(rec, ImportAssessmentStatus.UNRESOLVED, [d_code])],
        )
        assert batch_base.assessment_manifest_sha256 != batch_code.assessment_manifest_sha256

        # AZ: Field key change
        d_fk = ImportAssessmentDiagnostic(code="err_a", message="Msg A", field_key="price")
        batch_fk = build_import_assessment_batch(
            manifest,
            [build_import_record_assessment(rec, ImportAssessmentStatus.UNRESOLVED, [d_fk])],
        )
        assert batch_base.assessment_manifest_sha256 != batch_fk.assessment_manifest_sha256

        # BA: Message change
        d_msg = ImportAssessmentDiagnostic(code="err_a", message="Msg Modified", field_key="symbol")
        batch_msg = build_import_assessment_batch(
            manifest,
            [build_import_record_assessment(rec, ImportAssessmentStatus.UNRESOLVED, [d_msg])],
        )
        assert batch_base.assessment_manifest_sha256 != batch_msg.assessment_manifest_sha256

    def test_malformed_hashes_rejected(self):
        """BC-BE: Incorrect hash, uppercase hex, and whitespace fail closed."""
        manifest = _make_test_parsed_manifest(1)
        rec = manifest.parsed_records[0]
        a0 = build_import_record_assessment(rec, ImportAssessmentStatus.READY)

        # BC: Incorrect hash
        with pytest.raises(PortfolioImportAssessmentError, match="digest mismatch"):
            ImportAssessmentBatch(
                parsed_manifest=manifest,
                assessments=(a0,),
                assessment_manifest_sha256="0" * 64,
            )

        # BD: Uppercase hex
        with pytest.raises(PortfolioImportAssessmentError, match="64-character lowercase hex"):
            ImportAssessmentBatch(
                parsed_manifest=manifest,
                assessments=(a0,),
                assessment_manifest_sha256=("A" * 64),
            )

        # BE: Hash with newline
        with pytest.raises(PortfolioImportAssessmentError, match="64-character lowercase hex"):
            ImportAssessmentBatch(
                parsed_manifest=manifest,
                assessments=(a0,),
                assessment_manifest_sha256=(a0.parsed_record.parsed_sha256 + "\n"),
            )

    def test_repeated_build_deterministic(self):
        """AW: Repeated builds produce identical manifest hash."""
        manifest = _make_test_parsed_manifest(1)
        rec = manifest.parsed_records[0]
        a0 = build_import_record_assessment(rec, ImportAssessmentStatus.READY)

        b1 = build_import_assessment_batch(manifest, [a0])
        b2 = build_import_assessment_batch(manifest, [a0])
        assert b1.assessment_manifest_sha256 == b2.assessment_manifest_sha256

    def test_parsed_manifest_change_changes_hash(self):
        """BB: Changing underlying parsed manifest changes assessment manifest hash."""
        m1 = _make_test_parsed_manifest(1)
        m2 = _make_test_parsed_manifest(1)  # Different portfolio UUID

        a1 = build_import_record_assessment(m1.parsed_records[0], ImportAssessmentStatus.READY)
        a2 = build_import_record_assessment(m2.parsed_records[0], ImportAssessmentStatus.READY)

        b1 = build_import_assessment_batch(m1, [a1])
        b2 = build_import_assessment_batch(m2, [a2])
        assert b1.assessment_manifest_sha256 != b2.assessment_manifest_sha256


# ─────────────────────────────────────────────────────────────────────────────
# 8. Batch Status & Record Counts Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBatchStatusAndRecordCounts:
    """Verifies that status count sums and record_count are exact."""

    def test_status_counts_sum_to_record_count(self):
        """BF-BJ: ready_count + unresolved_count + rejected_count == record_count == parsed_manifest.record_count."""
        manifest = _make_test_parsed_manifest(3)
        r0, r1, r2 = manifest.parsed_records

        a0 = build_import_record_assessment(r0, ImportAssessmentStatus.READY)
        a1 = build_import_record_assessment(
            r1,
            ImportAssessmentStatus.UNRESOLVED,
            [ImportAssessmentDiagnostic(code="err", message="Msg", field_key="symbol")],
        )
        a2 = build_import_record_assessment(
            r2,
            ImportAssessmentStatus.REJECTED,
            [ImportAssessmentDiagnostic(code="bad", message="Msg", field_key="price")],
        )

        batch = build_import_assessment_batch(manifest, [a0, a1, a2])
        assert batch.ready_count == 1
        assert batch.unresolved_count == 1
        assert batch.rejected_count == 1
        assert batch.record_count == 3
        assert batch.ready_count + batch.unresolved_count + batch.rejected_count == batch.record_count
        assert batch.record_count == manifest.record_count


# ─────────────────────────────────────────────────────────────────────────────
# 9. Surface Red-Team Invariants Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSurfaceRedTeamInvariants:
    """Verifies that assessment models contain zero financial fields or raw byte arrays."""

    def test_no_forbidden_fields_in_assessment_models(self):
        """54: Asserts that models have only declared domain fields."""
        record_ass_fields = {f.name for f in fields(ImportRecordAssessment)}
        assert record_ass_fields == {"parsed_record", "status", "diagnostics"}

        batch_fields = {f.name for f in fields(ImportAssessmentBatch)}
        assert batch_fields == {"parsed_manifest", "assessments", "assessment_manifest_sha256"}


# ─────────────────────────────────────────────────────────────────────────────
# 10. Canonical CSV v1 Real Pipeline Integration Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCanonicalCsvRealPipelineIntegration:
    """Verifies end-to-end flow from real CSV staging to complete Assessment Batch."""

    def test_canonical_csv_to_assessment_batch(self):
        """55: Real Canonical CSV parser -> staging pipeline -> assessment batch."""
        parser = SentinaxCanonicalCsvParserV1()
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime(2026, 8, 28, 13, 30, tzinfo=timezone.utc)

        csv_content = b"symbol,quantity,price\nAAPL,10,150.00\nUNKNOWN_TICKER,20,50.00\n"

        staging_result = build_import_staging_result(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=csv_content,
            imported_at=t,
            parser=parser,
        )

        assert staging_result.parsed_manifest.record_count == 2
        p0, p1 = staging_result.parsed_manifest.parsed_records

        # Record 1: READY
        ass0 = build_import_record_assessment(p0, ImportAssessmentStatus.READY)

        # Record 2: UNRESOLVED due to unmapped ticker
        diag = ImportAssessmentDiagnostic(
            code="unmapped_symbol",
            message="Ticker UNKNOWN_TICKER is not recognized in instrument registry.",
            field_key="symbol",
        )
        ass1 = build_import_record_assessment(p1, ImportAssessmentStatus.UNRESOLVED, [diag])

        # Complete assessment batch
        batch = build_import_assessment_batch(
            parsed_manifest=staging_result.parsed_manifest,
            assessments=[ass0, ass1],
        )

        assert batch.record_count == 2
        assert batch.ready_count == 1
        assert batch.unresolved_count == 1
        assert batch.rejected_count == 0
        assert len(batch.assessment_manifest_sha256) == 64
        assert batch.parsed_manifest is staging_result.parsed_manifest
