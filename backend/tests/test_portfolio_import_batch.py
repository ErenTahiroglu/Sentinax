"""
backend/tests/test_portfolio_import_batch.py
============================================
Tests for Phase 13B: Immutable Import Batch Manifest & Record-Set Integrity.

Zero network calls (pytest-socket enforced).
Pure in-memory domain evaluation.

Test Matrix:
    1. Basic Manifest Construction (Empty/Single/Multi records, record_count, tuple output, frozen mutation)
    2. Ordering & Invariance (Unordered input sorting, permutation invariance of manifest_sha256 and manifest_identity)
    3. Ordinal Integrity & Contiguity (Duplicate ordinals, Gaps, Non-1 start, Same hash multiple ordinals)
    4. Cross-File Record Binding & Isolation (Different portfolio, account, source_key, content_sha256)
    5. Hash Determinism & Digest Sensitivity (Independent compact JSON comparison, Filename/imported_at invariance)
    6. Direct Constructor Hardening (Non-tuple, unsorted, duplicate ordinals, invalid SHA format/matching)
    7. Builder Collection Input Contract (Lists, Tuples, Generator/String/Dict rejections)
    8. Ledger Identity Separation (No external_source, external_reference, transaction_id fields in manifest)
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

from backend.engine.private.portfolio.import_batch import (
    ImportBatchManifest,
    PortfolioImportBatchError,
    build_import_batch_manifest,
)
from backend.engine.private.portfolio.import_provenance import (
    ImportFileProvenance,
    ImportRecordProvenance,
    build_import_file_provenance,
    build_import_record_provenance,
)


def _make_file_provenance(
    portfolio_id: Optional[UUID] = None,
    account_id: Optional[UUID] = None,
    source_key: str = "midas_csv",
    filename: str = "statement.csv",
    content: bytes = b"Date,Symbol,Type,Quantity,Price\n2026-08-01,AAPL,BUY,10,150.00\n",
    imported_at: Optional[datetime] = None,
) -> ImportFileProvenance:
    return build_import_file_provenance(
        portfolio_id=portfolio_id or uuid4(),
        account_id=account_id or uuid4(),
        source_key=source_key,
        filename=filename,
        content=content,
        imported_at=imported_at or datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
    )


def _make_record_provenance(
    file_prov: ImportFileProvenance,
    ordinal: int,
    raw_record: bytes,
) -> ImportRecordProvenance:
    return build_import_record_provenance(
        file_provenance=file_prov,
        record_ordinal=ordinal,
        raw_record=raw_record,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Basic Manifest Construction
# ─────────────────────────────────────────────────────────────────────────────

class TestBasicManifestConstruction:
    """Verifies baseline manifest creation, properties, and immutability."""

    def test_empty_manifest_valid(self):
        """A, D, E: Empty record set produces valid manifest with record_count=0 and tuple records."""
        file_prov = _make_file_provenance()
        manifest = build_import_batch_manifest(file_prov, [])

        assert isinstance(manifest, ImportBatchManifest)
        assert manifest.file_provenance == file_prov
        assert manifest.records == ()
        assert manifest.record_count == 0
        assert type(manifest.records) is tuple
        assert len(manifest.manifest_sha256) == 64

    def test_single_record_manifest_valid(self):
        """B: Single-record manifest produces valid manifest with record_count=1."""
        file_prov = _make_file_provenance()
        rec = _make_record_provenance(file_prov, 1, b"row_1_data")
        manifest = build_import_batch_manifest(file_prov, [rec])

        assert manifest.record_count == 1
        assert manifest.records == (rec,)
        assert manifest.manifest_identity[4] == manifest.manifest_sha256

    def test_multiple_record_manifest_valid(self):
        """C: Multiple-record manifest produces valid manifest with correct record_count."""
        file_prov = _make_file_provenance()
        recs = [
            _make_record_provenance(file_prov, 1, b"row_1"),
            _make_record_provenance(file_prov, 2, b"row_2"),
            _make_record_provenance(file_prov, 3, b"row_3"),
        ]
        manifest = build_import_batch_manifest(file_prov, recs)

        assert manifest.record_count == 3
        assert len(manifest.records) == 3
        assert manifest.records[0] == recs[0]
        assert manifest.records[1] == recs[1]
        assert manifest.records[2] == recs[2]

    def test_frozen_dataclass_mutation_rejected(self):
        """F: Mutation of ImportBatchManifest fields raises FrozenInstanceError."""
        file_prov = _make_file_provenance()
        manifest = build_import_batch_manifest(file_prov, [])

        with pytest.raises(FrozenInstanceError):
            manifest.manifest_sha256 = "0" * 64  # type: ignore

        with pytest.raises(FrozenInstanceError):
            manifest.records = ()  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 2. Ordering & Invariance
# ─────────────────────────────────────────────────────────────────────────────

class TestOrderingAndInvariance:
    """Verifies that builder sorts records by ordinal and produces identical digests regardless of input order."""

    def test_unordered_builder_input_becomes_sorted(self):
        """G, H: Unordered builder input [3, 1, 2] becomes canonically sorted (1, 2, 3)."""
        file_prov = _make_file_provenance()
        r1 = _make_record_provenance(file_prov, 1, b"r1")
        r2 = _make_record_provenance(file_prov, 2, b"r2")
        r3 = _make_record_provenance(file_prov, 3, b"r3")

        manifest = build_import_batch_manifest(file_prov, [r3, r1, r2])
        assert manifest.records == (r1, r2, r3)

    def test_ordered_and_shuffled_inputs_produce_identical_manifest(self):
        """I, J: Shuffled input yields exact same records tuple, manifest_sha256, and manifest_identity."""
        file_prov = _make_file_provenance()
        recs = [_make_record_provenance(file_prov, i, f"row_{i}".encode("utf-8")) for i in range(1, 6)]

        shuffled = list(recs)
        random.seed(42)
        random.shuffle(shuffled)

        manifest_ordered = build_import_batch_manifest(file_prov, recs)
        manifest_shuffled = build_import_batch_manifest(file_prov, shuffled)

        assert manifest_ordered.records == manifest_shuffled.records
        assert manifest_ordered.manifest_sha256 == manifest_shuffled.manifest_sha256
        assert manifest_ordered.manifest_identity == manifest_shuffled.manifest_identity


# ─────────────────────────────────────────────────────────────────────────────
# 3. Ordinal Integrity & Contiguity
# ─────────────────────────────────────────────────────────────────────────────

class TestOrdinalIntegrityAndContiguity:
    """Verifies that ordinals must be unique and contiguous 1..N."""

    def test_duplicate_ordinal_same_hash_rejected(self):
        """K: Duplicate ordinal with identical record hash fails closed."""
        file_prov = _make_file_provenance()
        r1 = _make_record_provenance(file_prov, 1, b"row_data")
        r1_dup = _make_record_provenance(file_prov, 1, b"row_data")

        with pytest.raises(PortfolioImportBatchError, match="duplicate record_ordinal detected"):
            build_import_batch_manifest(file_prov, [r1, r1_dup])

    def test_duplicate_ordinal_different_hash_rejected(self):
        """L: Duplicate ordinal with different record hash fails closed."""
        file_prov = _make_file_provenance()
        r1_a = _make_record_provenance(file_prov, 1, b"row_data_a")
        r1_b = _make_record_provenance(file_prov, 1, b"row_data_b")

        with pytest.raises(PortfolioImportBatchError, match="duplicate record_ordinal detected"):
            build_import_batch_manifest(file_prov, [r1_a, r1_b])

    def test_gap_in_ordinals_rejected(self):
        """M: Gap in ordinals (e.g. 1, 3) fails closed."""
        file_prov = _make_file_provenance()
        r1 = _make_record_provenance(file_prov, 1, b"row_1")
        r3 = _make_record_provenance(file_prov, 3, b"row_3")

        with pytest.raises(PortfolioImportBatchError, match="records must be contiguous 1..N"):
            build_import_batch_manifest(file_prov, [r1, r3])

    def test_starts_at_non_one_rejected(self):
        """N: Records starting at ordinal > 1 (e.g. 2, 3) fails closed."""
        file_prov = _make_file_provenance()
        r2 = _make_record_provenance(file_prov, 2, b"row_2")
        r3 = _make_record_provenance(file_prov, 3, b"row_3")

        with pytest.raises(PortfolioImportBatchError, match="records must be contiguous 1..N"):
            build_import_batch_manifest(file_prov, [r2, r3])

    def test_contiguous_ordinals_accepted(self):
        """O: Contiguous sequence 1, 2, 3 succeeds."""
        file_prov = _make_file_provenance()
        r1 = _make_record_provenance(file_prov, 1, b"row_1")
        r2 = _make_record_provenance(file_prov, 2, b"row_2")
        r3 = _make_record_provenance(file_prov, 3, b"row_3")

        manifest = build_import_batch_manifest(file_prov, [r1, r2, r3])
        assert manifest.record_count == 3

    def test_identical_record_sha_at_different_ordinals_accepted(self):
        """P: Identical raw record content at ordinal 1 and ordinal 2 is valid."""
        file_prov = _make_file_provenance()
        r1 = _make_record_provenance(file_prov, 1, b"repeated_row")
        r2 = _make_record_provenance(file_prov, 2, b"repeated_row")

        manifest = build_import_batch_manifest(file_prov, [r1, r2])
        assert manifest.record_count == 2
        assert manifest.records[0].record_sha256 == manifest.records[1].record_sha256


# ─────────────────────────────────────────────────────────────────────────────
# 4. Cross-File Record Binding & Isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestCrossFileRecordBinding:
    """Verifies that all records must match the parent file_provenance.file_identity exactly."""

    def test_record_from_different_portfolio_rejected(self):
        """Q: Record with different portfolio_id in file_identity is rejected."""
        file_prov_a = _make_file_provenance(portfolio_id=uuid4())
        file_prov_b = _make_file_provenance(portfolio_id=uuid4())

        foreign_rec = _make_record_provenance(file_prov_b, 1, b"row")
        with pytest.raises(PortfolioImportBatchError, match="does not match file_provenance"):
            build_import_batch_manifest(file_prov_a, [foreign_rec])

    def test_record_from_different_account_rejected(self):
        """R: Record with different account_id in file_identity is rejected."""
        port_id = uuid4()
        file_prov_a = _make_file_provenance(portfolio_id=port_id, account_id=uuid4())
        file_prov_b = _make_file_provenance(portfolio_id=port_id, account_id=uuid4())

        foreign_rec = _make_record_provenance(file_prov_b, 1, b"row")
        with pytest.raises(PortfolioImportBatchError, match="does not match file_provenance"):
            build_import_batch_manifest(file_prov_a, [foreign_rec])

    def test_record_from_different_source_key_rejected(self):
        """S: Record with different source_key in file_identity is rejected."""
        port_id = uuid4()
        acc_id = uuid4()
        file_prov_midas = _make_file_provenance(port_id, acc_id, source_key="midas_csv")
        file_prov_ibkr = _make_file_provenance(port_id, acc_id, source_key="ibkr.flex")

        foreign_rec = _make_record_provenance(file_prov_ibkr, 1, b"row")
        with pytest.raises(PortfolioImportBatchError, match="does not match file_provenance"):
            build_import_batch_manifest(file_prov_midas, [foreign_rec])

    def test_record_from_different_file_sha_rejected(self):
        """T: Record with different file content SHA in file_identity is rejected."""
        port_id = uuid4()
        acc_id = uuid4()
        file_prov_1 = _make_file_provenance(port_id, acc_id, content=b"content_1")
        file_prov_2 = _make_file_provenance(port_id, acc_id, content=b"content_2")

        foreign_rec = _make_record_provenance(file_prov_2, 1, b"row")
        with pytest.raises(PortfolioImportBatchError, match="does not match file_provenance"):
            build_import_batch_manifest(file_prov_1, [foreign_rec])

    def test_mixed_valid_and_foreign_record_rejects_entire_batch(self):
        """U: Batch containing one valid and one foreign record fails closed without partial creation."""
        file_prov_a = _make_file_provenance()
        file_prov_b = _make_file_provenance()

        r1 = _make_record_provenance(file_prov_a, 1, b"valid_row")
        r2_foreign = _make_record_provenance(file_prov_b, 2, b"foreign_row")

        with pytest.raises(PortfolioImportBatchError, match="does not match file_provenance"):
            build_import_batch_manifest(file_prov_a, [r1, r2_foreign])


# ─────────────────────────────────────────────────────────────────────────────
# 5. Hash Determinism & Digest Sensitivity
# ─────────────────────────────────────────────────────────────────────────────

class TestHashDeterminismAndDigestSensitivity:
    """Verifies canonical JSON preimage hashing and digest sensitivity to changes."""

    def test_independent_json_preimage_hash_matches(self):
        """V: Direct independent canonical JSON encoding matches manifest_sha256."""
        file_prov = _make_file_provenance()
        r1 = _make_record_provenance(file_prov, 1, b"row_alpha")
        r2 = _make_record_provenance(file_prov, 2, b"row_beta")

        manifest = build_import_batch_manifest(file_prov, [r1, r2])

        # Manual direct preimage reconstruction
        preimage = [
            str(file_prov.portfolio_id),
            str(file_prov.account_id),
            file_prov.source_key,
            file_prov.content_sha256,
            [
                [1, hashlib.sha256(b"row_alpha").hexdigest()],
                [2, hashlib.sha256(b"row_beta").hexdigest()],
            ],
        ]
        raw_json = json.dumps(preimage, ensure_ascii=True, separators=(",", ":"))
        expected_sha = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()

        assert manifest.manifest_sha256 == expected_sha

    def test_repeated_builder_calls_produce_identical_sha(self):
        """W: Repeated builder invocations yield exact same manifest_sha256."""
        file_prov = _make_file_provenance()
        recs = [_make_record_provenance(file_prov, 1, b"row_1")]

        m1 = build_import_batch_manifest(file_prov, recs)
        m2 = build_import_batch_manifest(file_prov, recs)

        assert m1.manifest_sha256 == m2.manifest_sha256

    def test_filename_rename_does_not_alter_sha(self):
        """X: Changing filename does NOT alter manifest_sha256."""
        port_id = uuid4()
        acc_id = uuid4()
        raw_file = b"same file content"
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

        file_prov_1 = build_import_file_provenance(port_id, acc_id, "midas_csv", "august.csv", raw_file, t)
        file_prov_2 = build_import_file_provenance(port_id, acc_id, "midas_csv", "renamed_august.csv", raw_file, t)

        r1_a = build_import_record_provenance(file_prov_1, 1, b"row")
        r1_b = build_import_record_provenance(file_prov_2, 1, b"row")

        m1 = build_import_batch_manifest(file_prov_1, [r1_a])
        m2 = build_import_batch_manifest(file_prov_2, [r1_b])

        assert m1.manifest_sha256 == m2.manifest_sha256
        assert m1.manifest_identity == m2.manifest_identity

    def test_imported_at_change_does_not_alter_sha(self):
        """Y: Changing imported_at timestamp does NOT alter manifest_sha256."""
        port_id = uuid4()
        acc_id = uuid4()
        raw_file = b"same file content"

        file_prov_1 = build_import_file_provenance(port_id, acc_id, "midas_csv", "f.csv", raw_file, datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        file_prov_2 = build_import_file_provenance(port_id, acc_id, "midas_csv", "f.csv", raw_file, datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc))

        r1_a = build_import_record_provenance(file_prov_1, 1, b"row")
        r1_b = build_import_record_provenance(file_prov_2, 1, b"row")

        m1 = build_import_batch_manifest(file_prov_1, [r1_a])
        m2 = build_import_batch_manifest(file_prov_2, [r1_b])

        assert m1.manifest_sha256 == m2.manifest_sha256

    def test_record_hash_change_alters_sha(self):
        """Z: Changing a single record hash alters manifest_sha256."""
        file_prov = _make_file_provenance()
        r1_a = _make_record_provenance(file_prov, 1, b"row_v1")
        r1_b = _make_record_provenance(file_prov, 1, b"row_v2")

        m1 = build_import_batch_manifest(file_prov, [r1_a])
        m2 = build_import_batch_manifest(file_prov, [r1_b])

        assert m1.manifest_sha256 != m2.manifest_sha256

    def test_record_count_change_alters_sha(self):
        """AA: Adding or removing records alters manifest_sha256."""
        file_prov = _make_file_provenance()
        r1 = _make_record_provenance(file_prov, 1, b"row_1")
        r2 = _make_record_provenance(file_prov, 2, b"row_2")

        m_empty = build_import_batch_manifest(file_prov, [])
        m_single = build_import_batch_manifest(file_prov, [r1])
        m_double = build_import_batch_manifest(file_prov, [r1, r2])

        assert m_empty.manifest_sha256 != m_single.manifest_sha256
        assert m_single.manifest_sha256 != m_double.manifest_sha256

    def test_file_identity_change_alters_sha(self):
        """AB: Changing target account or portfolio alters manifest_sha256 even with identical records."""
        file_prov_a = _make_file_provenance(account_id=uuid4())
        file_prov_b = _make_file_provenance(account_id=uuid4())

        r_a = _make_record_provenance(file_prov_a, 1, b"row")
        r_b = _make_record_provenance(file_prov_b, 1, b"row")

        m_a = build_import_batch_manifest(file_prov_a, [r_a])
        m_b = build_import_batch_manifest(file_prov_b, [r_b])

        assert m_a.manifest_sha256 != m_b.manifest_sha256


# ─────────────────────────────────────────────────────────────────────────────
# 6. Direct Constructor Hardening
# ─────────────────────────────────────────────────────────────────────────────

class TestDirectConstructorHardening:
    """Verifies that direct instantiation of ImportBatchManifest enforces all validation rules."""

    def test_valid_direct_constructor(self):
        """AC: Direct constructor with valid components succeeds."""
        file_prov = _make_file_provenance()
        r1 = _make_record_provenance(file_prov, 1, b"row")
        manifest_sha = hashlib.sha256(
            json.dumps([
                str(file_prov.portfolio_id),
                str(file_prov.account_id),
                file_prov.source_key,
                file_prov.content_sha256,
                [[1, r1.record_sha256]],
            ], ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        manifest = ImportBatchManifest(
            file_provenance=file_prov,
            records=(r1,),
            manifest_sha256=manifest_sha,
        )
        assert manifest.record_count == 1

    def test_non_file_provenance_rejected(self):
        """AD: Non-ImportFileProvenance in constructor fails closed."""
        with pytest.raises(PortfolioImportBatchError, match="file_provenance must be an ImportFileProvenance"):
            ImportBatchManifest(
                file_provenance="not_file_prov",  # type: ignore
                records=(),
                manifest_sha256="0" * 64,
            )

    def test_list_records_in_direct_constructor_rejected(self):
        """AE: List records passed to direct constructor is rejected."""
        file_prov = _make_file_provenance()
        with pytest.raises(PortfolioImportBatchError, match="records must be an immutable tuple"):
            ImportBatchManifest(
                file_provenance=file_prov,
                records=[],  # type: ignore
                manifest_sha256="0" * 64,
            )

    def test_generator_records_rejected(self):
        """AF: Generator records in direct constructor is rejected."""
        file_prov = _make_file_provenance()
        with pytest.raises(PortfolioImportBatchError, match="records must be an immutable tuple"):
            ImportBatchManifest(
                file_provenance=file_prov,
                records=(x for x in []),  # type: ignore
                manifest_sha256="0" * 64,
            )

    def test_non_record_tuple_element_rejected(self):
        """AG: Non-ImportRecordProvenance element in records tuple is rejected."""
        file_prov = _make_file_provenance()
        with pytest.raises(PortfolioImportBatchError, match="must be an ImportRecordProvenance"):
            ImportBatchManifest(
                file_provenance=file_prov,
                records=("fake_record",),  # type: ignore
                manifest_sha256="0" * 64,
            )

    def test_unsorted_records_in_direct_constructor_rejected(self):
        """AH: Unsorted records in direct constructor fails closed (must not silently reorder)."""
        file_prov = _make_file_provenance()
        r1 = _make_record_provenance(file_prov, 1, b"row_1")
        r2 = _make_record_provenance(file_prov, 2, b"row_2")

        with pytest.raises(PortfolioImportBatchError, match="strictly sorted and contiguous"):
            ImportBatchManifest(
                file_provenance=file_prov,
                records=(r2, r1),  # Unsorted
                manifest_sha256="0" * 64,
            )

    def test_duplicate_ordinal_in_direct_constructor_rejected(self):
        """AI: Duplicate ordinals in direct constructor fail closed."""
        file_prov = _make_file_provenance()
        r1_a = _make_record_provenance(file_prov, 1, b"row_1")
        r1_b = _make_record_provenance(file_prov, 1, b"row_2")

        with pytest.raises(PortfolioImportBatchError, match="strictly sorted and contiguous"):
            ImportBatchManifest(
                file_provenance=file_prov,
                records=(r1_a, r1_b),
                manifest_sha256="0" * 64,
            )

    def test_non_contiguous_ordinals_in_direct_constructor_rejected(self):
        """AJ: Non-contiguous ordinals in direct constructor fail closed."""
        file_prov = _make_file_provenance()
        r1 = _make_record_provenance(file_prov, 1, b"row_1")
        r3 = _make_record_provenance(file_prov, 3, b"row_3")

        with pytest.raises(PortfolioImportBatchError, match="strictly sorted and contiguous"):
            ImportBatchManifest(
                file_prov,
                (r1, r3),
                "0" * 64,
            )

    def test_malformed_manifest_sha_rejected(self):
        """AK, AL: Malformed or uppercase SHA string is rejected."""
        file_prov = _make_file_provenance()

        for bad_sha in ("short_sha", "Z" * 64, "0" * 63, "0" * 65, True, 123, None):
            with pytest.raises(PortfolioImportBatchError):
                ImportBatchManifest(
                    file_provenance=file_prov,
                    records=(),
                    manifest_sha256=bad_sha,  # type: ignore
                )

    def test_incorrect_sha_rejected(self):
        """AM: Valid-format 64-char SHA that does not match computed digest fails closed."""
        file_prov = _make_file_provenance()
        fake_sha = "0" * 64

        with pytest.raises(PortfolioImportBatchError, match="does not match canonical preimage digest"):
            ImportBatchManifest(
                file_provenance=file_prov,
                records=(),
                manifest_sha256=fake_sha,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 7. Builder Collection Input Contract
# ─────────────────────────────────────────────────────────────────────────────

class TestBuilderCollectionInputContract:
    """Verifies that build_import_batch_manifest accepts finite sequences and rejects unmaterialized/invalid types."""

    def test_list_and_tuple_accepted(self):
        """AO, AP: Both lists and tuples are accepted by builder."""
        file_prov = _make_file_provenance()
        r1 = _make_record_provenance(file_prov, 1, b"row")

        m_from_list = build_import_batch_manifest(file_prov, [r1])
        m_from_tuple = build_import_batch_manifest(file_prov, (r1,))

        assert m_from_list.records == m_from_tuple.records
        assert m_from_list.manifest_sha256 == m_from_tuple.manifest_sha256

    def test_invalid_collection_types_rejected(self):
        """AQ, AR, AS, AT, AU: Strings, bytes, bytearrays, dicts, and generators are rejected."""
        file_prov = _make_file_provenance()

        for bad_coll in ("string", b"bytes", bytearray(b"data"), {"k": 1}, (x for x in [])):
            with pytest.raises(PortfolioImportBatchError, match="records must be a materialized list or tuple"):
                build_import_batch_manifest(file_prov, bad_coll)  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 8. Ledger Identity Separation
# ─────────────────────────────────────────────────────────────────────────────

class TestLedgerIdentitySeparation:
    """Verifies that ImportBatchManifest does not contain ledger economic or external identity fields."""

    def test_no_ledger_fields_in_manifest(self):
        """Public manifest model has no external_source, external_reference, transaction_id, or financial fields."""
        forbidden_field_names = {
            "external_source",
            "external_reference",
            "transaction_id",
            "transaction_type",
            "instrument_id",
            "idempotency_key",
            "quantity",
            "unit_price",
            "cash_amount",
        }

        manifest_field_names = {f.name for f in fields(ImportBatchManifest)}
        overlap = manifest_field_names & forbidden_field_names
        assert not overlap, f"Forbidden ledger fields found in ImportBatchManifest: {overlap}"
