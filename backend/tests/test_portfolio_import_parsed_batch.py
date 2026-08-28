"""
backend/tests/test_portfolio_import_parsed_batch.py
==================================================
Tests for Phase 13D: Immutable Parsed-Batch Manifest & Full Record-Coverage Integrity.

Zero network calls (pytest-socket enforced).
Pure in-memory domain evaluation.

Test Matrix:
    1. Basic Parsed-Batch Construction (Empty, single, multi, record_count, tuple output, frozen immutability)
    2. Coverage & Cardinality Integrity (Full 1..N coverage, omissions, extras, non-empty raw + zero parsed, duplicate ordinals)
    3. Provenance Correspondence & Foreign Record Rejection (Same ordinal wrong SHA, foreign portfolio/account/source/file)
    4. Parser Revision Enforcement (Matching revision, mismatch revision, mixed revisions, bool/zero/negative rejections)
    5. Ordering & Invariance (Builder canonical sorting, permutation invariance of parsed digest and identity, unsorted constructor rejection)
    6. Hash Determinism & Digest Sensitivity (Independent compact JSON, revision sensitivity, field value sensitivity, filename invariance)
    7. Collection Input Contract (Lists, tuples, generator/set/dict/string rejections)
    8. Zero-Field Record Full Coverage (ParsedImportRecord with empty fields counts as valid coverage)
    9. Ledger Identity Separation (No transaction/financial/ledger fields in model)
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
import hashlib
import json
import random
from typing import Any, List, Sequence, Tuple
from uuid import UUID, uuid4

import pytest

from backend.engine.private.portfolio.import_batch import (
    ImportBatchManifest,
    build_import_batch_manifest,
)
from backend.engine.private.portfolio.import_parsed_batch import (
    ParsedImportBatchManifest,
    PortfolioParsedImportBatchError,
    build_parsed_import_batch_manifest,
)
from backend.engine.private.portfolio.import_parsing import (
    ImportParsedField,
    ParsedImportRecord,
    build_parsed_import_record,
)
from backend.engine.private.portfolio.import_provenance import (
    ImportFileProvenance,
    ImportRecordProvenance,
    build_import_file_provenance,
    build_import_record_provenance,
)


def _make_fixture(
    num_records: int = 3,
    portfolio_id: UUID | None = None,
    account_id: UUID | None = None,
    source_key: str = "midas_csv",
    filename: str = "statement.csv",
    imported_at: datetime | None = None,
    parser_revision: int = 1,
) -> Tuple[ImportBatchManifest, List[bytes], List[ParsedImportRecord]]:
    port_id = portfolio_id or uuid4()
    acc_id = account_id or uuid4()
    t = imported_at or datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

    raw_records = [f"2026-08-{i+1:02d},SYM{i+1},BUY,10,100.00".encode("utf-8") for i in range(num_records)]
    file_bytes = b"header\n" + b"\n".join(raw_records) + b"\n"

    file_prov = build_import_file_provenance(
        portfolio_id=port_id,
        account_id=acc_id,
        source_key=source_key,
        filename=filename,
        content=file_bytes,
        imported_at=t,
    )

    rec_provs = [
        build_import_record_provenance(file_prov, i + 1, raw_records[i])
        for i in range(num_records)
    ]

    raw_manifest = build_import_batch_manifest(file_prov, rec_provs)

    parsed_records = [
        build_parsed_import_record(
            record_provenance=rec_provs[i],
            raw_record=raw_records[i],
            parser_revision=parser_revision,
            fields=[
                ImportParsedField("symbol", f"SYM{i+1}"),
                ImportParsedField("trade_date", f"2026-08-{i+1:02d}"),
            ],
        )
        for i in range(num_records)
    ]

    return raw_manifest, raw_records, parsed_records


# ─────────────────────────────────────────────────────────────────────────────
# 1. Basic Parsed-Batch Construction Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBasicParsedBatchConstruction:
    """Verifies baseline ParsedImportBatchManifest creation, properties, and immutability."""

    def test_empty_raw_and_empty_parsed_valid(self):
        """A, D, E: Empty raw manifest + empty parsed records produces valid manifest with record_count=0."""
        file_prov = build_import_file_provenance(
            uuid4(), uuid4(), "midas_csv", "empty.csv", b"empty_file", datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        )
        raw_manifest = build_import_batch_manifest(file_prov, [])
        parsed_manifest = build_parsed_import_batch_manifest(raw_manifest, 1, [])

        assert isinstance(parsed_manifest, ParsedImportBatchManifest)
        assert parsed_manifest.raw_manifest == raw_manifest
        assert parsed_manifest.parsed_records == ()
        assert parsed_manifest.record_count == 0
        assert type(parsed_manifest.parsed_records) is tuple
        assert len(parsed_manifest.parsed_manifest_sha256) == 64

    def test_one_record_parsed_batch_valid(self):
        """B: Single-record parsed batch succeeds with record_count=1."""
        raw_manifest, _, parsed_records = _make_fixture(num_records=1)
        parsed_manifest = build_parsed_import_batch_manifest(raw_manifest, 1, parsed_records)

        assert parsed_manifest.record_count == 1
        assert parsed_manifest.parsed_records == (parsed_records[0],)
        assert parsed_manifest.parser_revision == 1

    def test_multi_record_parsed_batch_valid(self):
        """C: Multi-record parsed batch succeeds with exact record_count."""
        raw_manifest, _, parsed_records = _make_fixture(num_records=3)
        parsed_manifest = build_parsed_import_batch_manifest(raw_manifest, 1, parsed_records)

        assert parsed_manifest.record_count == 3
        assert len(parsed_manifest.parsed_records) == 3

    def test_frozen_mutation_rejected(self):
        """F: Mutation of ParsedImportBatchManifest fields raises FrozenInstanceError."""
        raw_manifest, _, parsed_records = _make_fixture(num_records=1)
        parsed_manifest = build_parsed_import_batch_manifest(raw_manifest, 1, parsed_records)

        with pytest.raises(FrozenInstanceError):
            parsed_manifest.parser_revision = 2  # type: ignore

        with pytest.raises(FrozenInstanceError):
            parsed_manifest.parsed_manifest_sha256 = "0" * 64  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 2. Coverage & Cardinality Integrity Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCoverageAndCardinalityIntegrity:
    """Verifies strict 1:1 coverage, rejection of omissions, extras, and duplicates."""

    def test_raw_and_parsed_matching_coverage_accepted(self):
        """G: Raw 1,2,3 + parsed 1,2,3 succeeds."""
        raw_manifest, _, parsed_records = _make_fixture(num_records=3)
        manifest = build_parsed_import_batch_manifest(raw_manifest, 1, parsed_records)
        assert manifest.record_count == 3

    def test_omission_rejected(self):
        """H: Raw 1,2,3 + parsed 1,2 (missing ordinal 3) fails closed."""
        raw_manifest, _, parsed_records = _make_fixture(num_records=3)
        with pytest.raises(PortfolioParsedImportBatchError, match="count 2 does not match"):
            build_parsed_import_batch_manifest(raw_manifest, 1, parsed_records[:2])

    def test_extra_record_rejected(self):
        """I: Raw 1,2 + parsed 1,2,3 (extra record) fails closed."""
        raw_manifest_2, _, _ = _make_fixture(num_records=2)
        _, _, parsed_records_3 = _make_fixture(num_records=3)

        with pytest.raises(PortfolioParsedImportBatchError, match="count 3 does not match"):
            build_parsed_import_batch_manifest(raw_manifest_2, 1, parsed_records_3)

    def test_non_empty_raw_with_zero_parsed_rejected(self):
        """J: Non-empty raw manifest + zero parsed records fails closed."""
        raw_manifest, _, _ = _make_fixture(num_records=2)
        with pytest.raises(PortfolioParsedImportBatchError, match="count 0 does not match"):
            build_parsed_import_batch_manifest(raw_manifest, 1, [])

    def test_duplicate_parsed_ordinal_rejected(self):
        """K: Duplicate parsed ordinals fail closed without silent deduplication."""
        raw_manifest, _, parsed_records = _make_fixture(num_records=2)
        # Duplicate record 1 twice
        with pytest.raises(PortfolioParsedImportBatchError, match="duplicate parsed record_ordinal detected: 1"):
            build_parsed_import_batch_manifest(raw_manifest, 1, [parsed_records[0], parsed_records[0]])


# ─────────────────────────────────────────────────────────────────────────────
# 3. Provenance Binding & Foreign Record Rejection Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestProvenanceBindingAndForeignRecords:
    """Verifies that each parsed record matches raw manifest provenance semantic equality."""

    def test_same_ordinal_different_record_sha_rejected(self):
        """L: Parsed record at ordinal 1 derived from different raw bytes fails closed."""
        raw_manifest, _, parsed_records = _make_fixture(num_records=2)

        # Create another record with same ordinal 1 but different raw bytes
        file_prov = raw_manifest.file_provenance
        other_raw = b"completely_different_bytes"
        rec_prov_tampered = build_import_record_provenance(file_prov, 1, other_raw)
        parsed_tampered = build_parsed_import_record(rec_prov_tampered, other_raw, 1, [])

        with pytest.raises(PortfolioParsedImportBatchError, match="record_provenance does not match"):
            build_parsed_import_batch_manifest(raw_manifest, 1, [parsed_tampered, parsed_records[1]])

    def test_different_portfolio_rejected(self):
        """M: Parsed record from different portfolio fails closed."""
        raw_manifest, _, _ = _make_fixture(num_records=1, portfolio_id=uuid4())
        _, _, foreign_parsed = _make_fixture(num_records=1, portfolio_id=uuid4())

        with pytest.raises(PortfolioParsedImportBatchError, match="record_provenance does not match"):
            build_parsed_import_batch_manifest(raw_manifest, 1, foreign_parsed)

    def test_different_account_rejected(self):
        """N: Parsed record from different account fails closed."""
        port_id = uuid4()
        raw_manifest, _, _ = _make_fixture(num_records=1, portfolio_id=port_id, account_id=uuid4())
        _, _, foreign_parsed = _make_fixture(num_records=1, portfolio_id=port_id, account_id=uuid4())

        with pytest.raises(PortfolioParsedImportBatchError, match="record_provenance does not match"):
            build_parsed_import_batch_manifest(raw_manifest, 1, foreign_parsed)

    def test_different_source_key_rejected(self):
        """O: Parsed record from different source_key fails closed."""
        port_id = uuid4()
        acc_id = uuid4()
        raw_manifest, _, _ = _make_fixture(num_records=1, portfolio_id=port_id, account_id=acc_id, source_key="midas_csv")
        _, _, foreign_parsed = _make_fixture(num_records=1, portfolio_id=port_id, account_id=acc_id, source_key="ibkr.flex")

        with pytest.raises(PortfolioParsedImportBatchError, match="record_provenance does not match"):
            build_parsed_import_batch_manifest(raw_manifest, 1, foreign_parsed)

    def test_different_file_sha_rejected(self):
        """P: Parsed record from different file content SHA fails closed."""
        port_id = uuid4()
        acc_id = uuid4()
        raw_manifest, raw_bytes_1, _ = _make_fixture(num_records=1, portfolio_id=port_id, account_id=acc_id)

        # Create foreign file with different content
        foreign_file_prov = build_import_file_provenance(
            portfolio_id=port_id,
            account_id=acc_id,
            source_key="midas_csv",
            filename="other.csv",
            content=b"different_file_content_altogether\n",
            imported_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
        )
        foreign_rec_prov = build_import_record_provenance(foreign_file_prov, 1, raw_bytes_1[0])
        foreign_parsed_rec = build_parsed_import_record(foreign_rec_prov, raw_bytes_1[0], 1, [])

        with pytest.raises(PortfolioParsedImportBatchError, match="record_provenance does not match"):
            build_parsed_import_batch_manifest(raw_manifest, 1, [foreign_parsed_rec])

    def test_mixed_valid_and_foreign_record_rejects_entire_batch(self):
        """Q: One foreign record in an otherwise valid batch rejects entire batch."""
        raw_manifest, _, valid_parsed = _make_fixture(num_records=2)
        _, _, foreign_parsed = _make_fixture(num_records=2)

        with pytest.raises(PortfolioParsedImportBatchError, match="record_provenance does not match"):
            build_parsed_import_batch_manifest(raw_manifest, 1, [valid_parsed[0], foreign_parsed[1]])


# ─────────────────────────────────────────────────────────────────────────────
# 4. Parser Revision Enforcement Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestParserRevisionEnforcement:
    """Verifies that all records and the parsed batch share the exact same parser_revision."""

    def test_matching_revision_accepted(self):
        """R: Matching revision across batch and all records succeeds."""
        raw_manifest, _, parsed_records = _make_fixture(num_records=2, parser_revision=3)
        manifest = build_parsed_import_batch_manifest(raw_manifest, 3, parsed_records)
        assert manifest.parser_revision == 3

    def test_record_wrong_revision_rejected(self):
        """S: Parsed record revision differs from batch revision."""
        raw_manifest, _, parsed_records = _make_fixture(num_records=1, parser_revision=1)
        with pytest.raises(PortfolioParsedImportBatchError, match="parser_revision 1 does not match batch parser_revision 2"):
            build_parsed_import_batch_manifest(raw_manifest, 2, parsed_records)

    def test_mixed_revisions_rejected(self):
        """T: Mixed record revisions fail closed."""
        raw_manifest, raw_bytes, _ = _make_fixture(num_records=2)
        r1 = build_parsed_import_record(raw_manifest.records[0], raw_bytes[0], 1, [])
        r2 = build_parsed_import_record(raw_manifest.records[1], raw_bytes[1], 2, [])

        with pytest.raises(PortfolioParsedImportBatchError, match="parser_revision"):
            build_parsed_import_batch_manifest(raw_manifest, 1, [r1, r2])

    def test_invalid_parser_revision_types_rejected(self):
        """U, V: Boolean, non-integer, zero, or negative revisions fail closed."""
        raw_manifest, _, _ = _make_fixture(num_records=0)

        for bad_rev in (True, False, 0, -1, "1", None, 1.5):
            with pytest.raises(PortfolioParsedImportBatchError, match="parser_revision"):
                build_parsed_import_batch_manifest(raw_manifest, bad_rev, [])  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 5. Ordering & Invariance Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestOrderingAndInvariance:
    """Verifies that builder sorts records by ordinal and produces identical digests regardless of input order."""

    def test_shuffled_builder_input_becomes_canonical(self):
        """W: Unordered builder input [3, 1, 2] becomes canonically sorted (1, 2, 3)."""
        raw_manifest, _, parsed_records = _make_fixture(num_records=3)
        r1, r2, r3 = parsed_records

        manifest = build_parsed_import_batch_manifest(raw_manifest, 1, [r3, r1, r2])
        assert manifest.parsed_records == (r1, r2, r3)

    def test_shuffled_and_ordered_inputs_produce_identical_manifest(self):
        """X, Y, Z: Shuffled input yields exact same parsed_records, parsed_manifest_sha256, and parsed_manifest_identity."""
        raw_manifest, _, parsed_records = _make_fixture(num_records=5)

        shuffled = list(parsed_records)
        random.seed(42)
        random.shuffle(shuffled)

        manifest_ordered = build_parsed_import_batch_manifest(raw_manifest, 1, parsed_records)
        manifest_shuffled = build_parsed_import_batch_manifest(raw_manifest, 1, shuffled)

        assert manifest_ordered.parsed_records == manifest_shuffled.parsed_records
        assert manifest_ordered.parsed_manifest_sha256 == manifest_shuffled.parsed_manifest_sha256
        assert manifest_ordered.parsed_manifest_identity == manifest_shuffled.parsed_manifest_identity

    def test_unsorted_direct_constructor_rejected(self):
        """AA: Direct constructor rejects unsorted parsed_records tuple (no silent sorting)."""
        raw_manifest, _, parsed_records = _make_fixture(num_records=2)
        r1, r2 = parsed_records

        with pytest.raises(PortfolioParsedImportBatchError, match="record_provenance does not match"):
            ParsedImportBatchManifest(
                raw_manifest=raw_manifest,
                parser_revision=1,
                parsed_records=(r2, r1),  # Unsorted
                parsed_manifest_sha256="0" * 64,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 6. Hash Determinism & Digest Sensitivity Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestHashDeterminismAndDigestSensitivity:
    """Verifies canonical JSON preimage encoding, sensitivity to values, revisions, manifests, and metadata invariance."""

    def test_independent_json_preimage_hash_matches(self):
        """AB: Independent canonical JSON encoding matches parsed_manifest_sha256."""
        raw_manifest, _, parsed_records = _make_fixture(num_records=2, parser_revision=2)
        manifest = build_parsed_import_batch_manifest(raw_manifest, 2, parsed_records)

        file_prov = raw_manifest.file_provenance
        preimage = [
            str(file_prov.portfolio_id),
            str(file_prov.account_id),
            file_prov.source_key,
            file_prov.content_sha256,
            raw_manifest.manifest_sha256,
            2,
            [
                [1, parsed_records[0].record_provenance.record_sha256, parsed_records[0].parsed_sha256],
                [2, parsed_records[1].record_provenance.record_sha256, parsed_records[1].parsed_sha256],
            ],
        ]
        raw_json = json.dumps(preimage, ensure_ascii=True, separators=(",", ":"))
        expected_sha = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()

        assert manifest.parsed_manifest_sha256 == expected_sha

    def test_repeated_build_deterministic(self):
        """AC: Repeated invocations produce identical parsed_manifest_sha256."""
        raw_manifest, _, parsed_records = _make_fixture(num_records=2)
        m1 = build_parsed_import_batch_manifest(raw_manifest, 1, parsed_records)
        m2 = build_parsed_import_batch_manifest(raw_manifest, 1, parsed_records)

        assert m1.parsed_manifest_sha256 == m2.parsed_manifest_sha256

    def test_parsed_sha_change_changes_batch_sha(self):
        """AD: Changing extracted fields (and thus parsed_sha256) of one record changes batch SHA."""
        raw_manifest, raw_bytes, _ = _make_fixture(num_records=1)

        p1 = build_parsed_import_record(raw_manifest.records[0], raw_bytes[0], 1, [ImportParsedField("symbol", "AAPL")])
        p2 = build_parsed_import_record(raw_manifest.records[0], raw_bytes[0], 1, [ImportParsedField("symbol", "MSFT")])

        m1 = build_parsed_import_batch_manifest(raw_manifest, 1, [p1])
        m2 = build_parsed_import_batch_manifest(raw_manifest, 1, [p2])

        assert m1.parsed_manifest_sha256 != m2.parsed_manifest_sha256

    def test_parser_revision_change_changes_sha(self):
        """AE: Changing parser_revision changes parsed_manifest_sha256 and identity."""
        raw_manifest, raw_bytes, _ = _make_fixture(num_records=1)

        p_rev1 = build_parsed_import_record(raw_manifest.records[0], raw_bytes[0], 1, [])
        p_rev2 = build_parsed_import_record(raw_manifest.records[0], raw_bytes[0], 2, [])

        m_rev1 = build_parsed_import_batch_manifest(raw_manifest, 1, [p_rev1])
        m_rev2 = build_parsed_import_batch_manifest(raw_manifest, 2, [p_rev2])

        assert m_rev1.parsed_manifest_sha256 != m_rev2.parsed_manifest_sha256
        assert m_rev1.parsed_manifest_identity != m_rev2.parsed_manifest_identity

    def test_raw_manifest_change_changes_sha(self):
        """AF: Changing raw manifest alters parsed batch SHA."""
        raw_manifest_1, _, p1 = _make_fixture(num_records=1, portfolio_id=uuid4())
        raw_manifest_2, _, p2 = _make_fixture(num_records=1, portfolio_id=uuid4())

        m1 = build_parsed_import_batch_manifest(raw_manifest_1, 1, p1)
        m2 = build_parsed_import_batch_manifest(raw_manifest_2, 1, p2)

        assert m1.parsed_manifest_sha256 != m2.parsed_manifest_sha256

    def test_filename_rename_does_not_change_sha(self):
        """AG: Changing display filename leaves parsed_manifest_sha256 unchanged."""
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

        raw_manifest_1, _, p1 = _make_fixture(num_records=1, portfolio_id=port_id, account_id=acc_id, filename="orig.csv", imported_at=t)
        raw_manifest_2, _, p2 = _make_fixture(num_records=1, portfolio_id=port_id, account_id=acc_id, filename="renamed.csv", imported_at=t)

        m1 = build_parsed_import_batch_manifest(raw_manifest_1, 1, p1)
        m2 = build_parsed_import_batch_manifest(raw_manifest_2, 1, p2)

        assert m1.parsed_manifest_sha256 == m2.parsed_manifest_sha256
        assert m1.parsed_manifest_identity == m2.parsed_manifest_identity

    def test_imported_at_change_does_not_change_sha(self):
        """AH: Changing imported_at leaves parsed_manifest_sha256 unchanged."""
        port_id = uuid4()
        acc_id = uuid4()

        raw_manifest_1, _, p1 = _make_fixture(num_records=1, portfolio_id=port_id, account_id=acc_id, imported_at=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc))
        raw_manifest_2, _, p2 = _make_fixture(num_records=1, portfolio_id=port_id, account_id=acc_id, imported_at=datetime(2026, 8, 20, 15, 0, tzinfo=timezone.utc))

        m1 = build_parsed_import_batch_manifest(raw_manifest_1, 1, p1)
        m2 = build_parsed_import_batch_manifest(raw_manifest_2, 1, p2)

        assert m1.parsed_manifest_sha256 == m2.parsed_manifest_sha256

    def test_strict_fullmatch_sha_validation(self):
        """AI, AJ, AK, AL: SHA with newline, CRLF, uppercase, or invalid format is rejected."""
        raw_manifest, _, parsed_records = _make_fixture(num_records=1)
        valid_sha = hashlib.sha256(
            json.dumps([
                str(raw_manifest.file_provenance.portfolio_id),
                str(raw_manifest.file_provenance.account_id),
                raw_manifest.file_provenance.source_key,
                raw_manifest.file_provenance.content_sha256,
                raw_manifest.manifest_sha256,
                1,
                [[1, parsed_records[0].record_provenance.record_sha256, parsed_records[0].parsed_sha256]],
            ], ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        for bad_sha in (
            "short_sha",
            "Z" * 64,
            "0" * 63,
            "0" * 65,
            valid_sha.upper(),   # AK: Uppercase
            valid_sha + "\n",    # AI: Final newline
            valid_sha + "\r",
            valid_sha + "\r\n",  # AJ: Final CRLF
            valid_sha + " ",
            " " + valid_sha,
            "\n" + valid_sha,
            "0" * 64,            # AL: Valid format, incorrect SHA
            True,
            123,
            None,
        ):
            with pytest.raises(PortfolioParsedImportBatchError):
                ParsedImportBatchManifest(
                    raw_manifest=raw_manifest,
                    parser_revision=1,
                    parsed_records=(parsed_records[0],),
                    parsed_manifest_sha256=bad_sha,  # type: ignore
                )


# ─────────────────────────────────────────────────────────────────────────────
# 7. Collection Input Contract Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCollectionInputContract:
    """Verifies that builder accepts lists and tuples and rejects invalid collection types."""

    def test_list_and_tuple_accepted(self):
        """AM, AN: Both lists and tuples of ParsedImportRecord are accepted."""
        raw_manifest, _, parsed_records = _make_fixture(num_records=1)

        m_list = build_parsed_import_batch_manifest(raw_manifest, 1, [parsed_records[0]])
        m_tuple = build_parsed_import_batch_manifest(raw_manifest, 1, (parsed_records[0],))

        assert m_list.parsed_records == m_tuple.parsed_records
        assert m_list.parsed_manifest_sha256 == m_tuple.parsed_manifest_sha256

    def test_invalid_collection_types_rejected(self):
        """AO-AT: Generators, sets, dicts, strings, bytes rejected by builder, and lists rejected by direct constructor."""
        raw_manifest, _, parsed_records = _make_fixture(num_records=1)
        r = parsed_records[0]

        for bad_coll in (
            (x for x in [r]),
            {r},
            {"record": r},
            "string",
            b"bytes",
        ):
            with pytest.raises(PortfolioParsedImportBatchError, match="parsed_records must be a materialized list or tuple"):
                build_parsed_import_batch_manifest(raw_manifest, 1, bad_coll)  # type: ignore

        # Direct constructor rejects list
        with pytest.raises(PortfolioParsedImportBatchError, match="parsed_records must be an immutable tuple"):
            ParsedImportBatchManifest(
                raw_manifest=raw_manifest,
                parser_revision=1,
                parsed_records=[r],  # type: ignore
                parsed_manifest_sha256="0" * 64,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 8. Zero-Field Record Full Coverage Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestZeroFieldRecordFullCoverage:
    """Verifies that ParsedImportRecord with empty fields satisfies full coverage."""

    def test_zero_field_record_satisfies_full_coverage(self):
        """AU: Zero-field parsed record (fields=()) counts as valid coverage."""
        raw_manifest, raw_bytes, _ = _make_fixture(num_records=2)

        # Record 1 has fields, Record 2 has empty fields ()
        p1 = build_parsed_import_record(raw_manifest.records[0], raw_bytes[0], 1, [ImportParsedField("symbol", "AAPL")])
        p2 = build_parsed_import_record(raw_manifest.records[1], raw_bytes[1], 1, [])

        assert p2.fields == ()
        manifest = build_parsed_import_batch_manifest(raw_manifest, 1, [p1, p2])

        assert manifest.record_count == 2
        assert manifest.parsed_records[1].fields == ()


# ─────────────────────────────────────────────────────────────────────────────
# 9. Ledger Identity Separation Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestLedgerIdentitySeparation:
    """Verifies that ParsedImportBatchManifest does not contain ledger economic or external identity fields."""

    def test_no_ledger_fields_in_parsed_batch_manifest(self):
        """ParsedImportBatchManifest contains only raw_manifest, parser_revision, parsed_records, parsed_manifest_sha256."""
        forbidden_fields = {
            "transaction_id",
            "external_source",
            "external_reference",
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

        manifest_field_names = {f.name for f in fields(ParsedImportBatchManifest)}
        overlap = manifest_field_names & forbidden_fields
        assert not overlap, f"Forbidden ledger fields found in ParsedImportBatchManifest: {overlap}"
