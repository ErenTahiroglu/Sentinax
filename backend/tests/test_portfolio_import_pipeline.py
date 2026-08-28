"""
backend/tests/test_portfolio_import_pipeline.py
===============================================
Tests for Phase 13E: Verified Source-Parser Execution Harness & Canonical Staging Pipeline.

Zero network calls (pytest-socket enforced).
Pure in-memory domain evaluation using real Phase 13A/B/C/D builders.

Test Matrix:
    1. Basic Pipeline Execution (Empty, single, multi, complete result, exact object-binding, immutability)
    2. Parser Dependency Validation (None, missing props, missing/non-callable extract, invalid source_key/revision)
    3. Single-Access & Single-Invocation Defenses (Hostile dynamic source_key/revision, single call, exact content identity)
    4. Parser Output Collection Contract (List, tuple, None/generator/set/dict/str/bytes, non-ExtractedImportRecord)
    5. ExtractedImportRecord Contract Hardening (Valid bytes, empty fields, bytearray/memoryview/str/empty rejections, field sorting, duplicates, immutability)
    6. Ordinal Assignment & Duplicate Preservation (1..N sequence, duplicate raw records preserved, no deduplication, post-return list mutation defense)
    7. Identity Sensitivity & Non-Interference (Source key change, revision change, field change, raw-byte change, raw-record order change)
    8. Failure Propagation & Zero Partial Results (Parser runtime errors, closed 13A/B/C/D error propagation, no partial returns)
    9. Result Constructor Hardening (Type verification, foreign raw/parsed manifest rejection)
    10. Raw-Byte Non-Retention & Ledger Separation Surface (Field inspection, no raw bytes/parser retained, no financial/ledger fields)
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
from typing import Any, List, Sequence, Tuple
from uuid import UUID, uuid4

import pytest

from backend.engine.private.portfolio.import_provenance import (
    ImportFileProvenance,
    ImportRecordProvenance,
    PortfolioImportProvenanceError,
    build_import_file_provenance,
    build_import_record_provenance,
)
from backend.engine.private.portfolio.import_batch import (
    ImportBatchManifest,
    PortfolioImportBatchError,
    build_import_batch_manifest,
)
from backend.engine.private.portfolio.import_parsing import (
    ImportParsedField,
    ParsedImportRecord,
    PortfolioImportParsingError,
    build_parsed_import_record,
)
from backend.engine.private.portfolio.import_parsed_batch import (
    ParsedImportBatchManifest,
    PortfolioParsedImportBatchError,
    build_parsed_import_batch_manifest,
)
from backend.engine.private.portfolio.import_pipeline import (
    ExtractedImportRecord,
    ImportStagingResult,
    PortfolioImportPipelineError,
    PortfolioImportSourceParser,
    build_import_staging_result,
)


class MockSourceParser:
    """Test fake for PortfolioImportSourceParser with call and access counters."""

    def __init__(
        self,
        source_key: str = "midas_csv",
        parser_revision: int = 1,
        records_to_return: Sequence[ExtractedImportRecord] | None = None,
        extract_side_effect: Exception | None = None,
    ) -> None:
        self._source_key = source_key
        self._parser_revision = parser_revision
        self._records_to_return = list(records_to_return) if records_to_return is not None else []
        self._extract_side_effect = extract_side_effect

        self.source_key_access_count = 0
        self.parser_revision_access_count = 0
        self.extract_records_call_count = 0
        self.received_content: bytes | None = None

    @property
    def source_key(self) -> str:
        self.source_key_access_count += 1
        return self._source_key

    @property
    def parser_revision(self) -> int:
        self.parser_revision_access_count += 1
        return self._parser_revision

    def extract_records(self, content: bytes) -> Sequence[ExtractedImportRecord]:
        self.extract_records_call_count += 1
        self.received_content = content
        if self._extract_side_effect:
            raise self._extract_side_effect
        return self._records_to_return


def _make_extracted_record(
    raw_str: str,
    fields_dict: dict[str, str] | None = None,
) -> ExtractedImportRecord:
    raw_bytes = raw_str.encode("utf-8")
    parsed_fields = ()
    if fields_dict:
        # Construct sorted fields tuple
        sorted_keys = sorted(fields_dict.keys())
        parsed_fields = tuple(
            ImportParsedField(k, fields_dict[k])
            for k in sorted_keys
        )
    return ExtractedImportRecord(raw_record=raw_bytes, fields=parsed_fields)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Basic Pipeline Execution Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBasicPipelineExecution:
    """Verifies standard pipeline flow, return types, count correctness, and exact object-binding."""

    def test_empty_parser_output_valid(self):
        """A: Empty parser output returns valid result with record_count=0."""
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        file_bytes = b"header_only_file\n"

        parser = MockSourceParser(source_key="midas_csv", parser_revision=1, records_to_return=[])
        result = build_import_staging_result(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="statement.csv",
            content=file_bytes,
            imported_at=t,
            parser=parser,
        )

        assert isinstance(result, ImportStagingResult)
        assert result.raw_manifest.record_count == 0
        assert result.parsed_manifest.record_count == 0
        assert result.parsed_manifest.parser_revision == 1
        assert result.file_provenance.portfolio_id == port_id
        assert result.file_provenance.account_id == acc_id
        assert result.file_provenance.source_key == "midas_csv"

    def test_one_record_valid(self):
        """B: Single-record parser output produces complete staging result with record_count=1."""
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        file_bytes = b"2026-08-28,AAPL,BUY,10,150.00\n"

        rec = _make_extracted_record("2026-08-28,AAPL,BUY,10,150.00", {"symbol": "AAPL", "qty": "10"})
        parser = MockSourceParser(source_key="midas_csv", parser_revision=2, records_to_return=[rec])

        result = build_import_staging_result(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="statement.csv",
            content=file_bytes,
            imported_at=t,
            parser=parser,
        )

        assert result.raw_manifest.record_count == 1
        assert result.parsed_manifest.record_count == 1
        assert result.parsed_manifest.parser_revision == 2
        assert len(result.parsed_manifest.parsed_records[0].fields) == 2

    def test_multi_record_valid(self):
        """C: Multi-record parser output produces complete staging result with record_count=N."""
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        file_bytes = b"row1\nrow2\nrow3\n"

        recs = [
            _make_extracted_record(f"row{i+1}", {"idx": str(i+1)})
            for i in range(3)
        ]
        parser = MockSourceParser(source_key="ibkr.flex", parser_revision=1, records_to_return=recs)

        result = build_import_staging_result(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="flex.csv",
            content=file_bytes,
            imported_at=t,
            parser=parser,
        )

        assert result.raw_manifest.record_count == 3
        assert result.parsed_manifest.record_count == 3

    def test_exact_object_binding_between_layers(self):
        """D, E: Result preserves exact object identity between file_provenance, raw_manifest, and parsed_manifest."""
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        file_bytes = b"data\n"

        rec = _make_extracted_record("data")
        parser = MockSourceParser(source_key="midas_csv", parser_revision=1, records_to_return=[rec])

        result = build_import_staging_result(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="statement.csv",
            content=file_bytes,
            imported_at=t,
            parser=parser,
        )

        # Exact object binding verification
        assert result.raw_manifest.file_provenance is result.file_provenance
        assert result.parsed_manifest.raw_manifest is result.raw_manifest

    def test_result_is_frozen(self):
        """F: Mutation of ImportStagingResult fields raises FrozenInstanceError."""
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        file_bytes = b"data\n"

        parser = MockSourceParser(source_key="midas_csv", parser_revision=1, records_to_return=[])
        result = build_import_staging_result(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="statement.csv",
            content=file_bytes,
            imported_at=t,
            parser=parser,
        )

        with pytest.raises(FrozenInstanceError):
            result.file_provenance = None  # type: ignore

        with pytest.raises(FrozenInstanceError):
            result.raw_manifest = None  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 2. Parser Dependency Validation Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestParserDependencyValidation:
    """Verifies fail-closed rejection of missing or malformed parser objects, attributes, and revisions."""

    def test_parser_none_rejected(self):
        """G: parser=None raises PortfolioImportPipelineError."""
        with pytest.raises(PortfolioImportPipelineError, match="parser must not be None"):
            build_import_staging_result(
                portfolio_id=uuid4(),
                account_id=uuid4(),
                filename="file.csv",
                content=b"content",
                imported_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
                parser=None,  # type: ignore
            )

    def test_arbitrary_object_rejected(self):
        """H: Arbitrary object without parser interface raises PortfolioImportPipelineError."""
        with pytest.raises(PortfolioImportPipelineError, match="parser must provide"):
            build_import_staging_result(
                portfolio_id=uuid4(),
                account_id=uuid4(),
                filename="file.csv",
                content=b"content",
                imported_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
                parser=object(),  # type: ignore
            )

    def test_missing_source_key_rejected(self):
        """I: Object missing source_key raises PortfolioImportPipelineError."""
        class IncompleteParser:
            parser_revision = 1
            def extract_records(self, c: bytes): return []

        with pytest.raises(PortfolioImportPipelineError, match="source_key"):
            build_import_staging_result(
                portfolio_id=uuid4(),
                account_id=uuid4(),
                filename="file.csv",
                content=b"content",
                imported_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
                parser=IncompleteParser(),  # type: ignore
            )

    def test_missing_parser_revision_rejected(self):
        """J: Object missing parser_revision raises PortfolioImportPipelineError."""
        class IncompleteParser:
            source_key = "midas_csv"
            def extract_records(self, c: bytes): return []

        with pytest.raises(PortfolioImportPipelineError, match="parser_revision"):
            build_import_staging_result(
                portfolio_id=uuid4(),
                account_id=uuid4(),
                filename="file.csv",
                content=b"content",
                imported_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
                parser=IncompleteParser(),  # type: ignore
            )

    def test_missing_extract_records_rejected(self):
        """K: Object missing extract_records raises PortfolioImportPipelineError."""
        class IncompleteParser:
            source_key = "midas_csv"
            parser_revision = 1

        with pytest.raises(PortfolioImportPipelineError, match="extract_records"):
            build_import_staging_result(
                portfolio_id=uuid4(),
                account_id=uuid4(),
                filename="file.csv",
                content=b"content",
                imported_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
                parser=IncompleteParser(),  # type: ignore
            )

    def test_non_callable_extract_records_rejected(self):
        """L: Object with non-callable extract_records raises PortfolioImportPipelineError."""
        class BadParser:
            source_key = "midas_csv"
            parser_revision = 1
            extract_records = "not a callable"

        with pytest.raises(PortfolioImportPipelineError, match="callable 'extract_records'"):
            build_import_staging_result(
                portfolio_id=uuid4(),
                account_id=uuid4(),
                filename="file.csv",
                content=b"content",
                imported_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
                parser=BadParser(),  # type: ignore
            )

    def test_invalid_source_key_rejected_before_execution(self):
        """M: Invalid source_key fails before parser execution."""
        for bad_key in ("", "UPPERCASE", "with spaces", "a" * 65, True, False, 123, None):
            parser = MockSourceParser(source_key=bad_key, parser_revision=1)  # type: ignore
            with pytest.raises(PortfolioImportPipelineError, match="source_key"):
                build_import_staging_result(
                    portfolio_id=uuid4(),
                    account_id=uuid4(),
                    filename="file.csv",
                    content=b"content",
                    imported_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
                    parser=parser,
                )
            assert parser.extract_records_call_count == 0

    def test_revision_bool_rejected(self):
        """N: Boolean parser_revision fails before parser execution."""
        for bad_rev in (True, False):
            parser = MockSourceParser(source_key="midas_csv", parser_revision=bad_rev)  # type: ignore
            with pytest.raises(PortfolioImportPipelineError, match="parser_revision"):
                build_import_staging_result(
                    portfolio_id=uuid4(),
                    account_id=uuid4(),
                    filename="file.csv",
                    content=b"content",
                    imported_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
                    parser=parser,
                )
            assert parser.extract_records_call_count == 0

    def test_revision_zero_and_negative_rejected(self):
        """O: parser_revision <= 0 or non-int fails before parser execution."""
        for bad_rev in (0, -1, -99, "1", 1.5, None):
            parser = MockSourceParser(source_key="midas_csv", parser_revision=bad_rev)  # type: ignore
            with pytest.raises(PortfolioImportPipelineError, match="parser_revision"):
                build_import_staging_result(
                    portfolio_id=uuid4(),
                    account_id=uuid4(),
                    filename="file.csv",
                    content=b"content",
                    imported_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
                    parser=parser,
                )
            assert parser.extract_records_call_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. Single-Access & Single-Invocation Defense Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSingleAccessAndSingleInvocationDefenses:
    """Verifies single-snapshot metadata capture and single parser execution."""

    def test_hostile_dynamic_source_key_read_once(self):
        """P: Dynamic/hostile source_key property is accessed exactly ONCE."""
        class HostileSourceParser:
            def __init__(self):
                self.calls = 0

            @property
            def source_key(self) -> str:
                self.calls += 1
                if self.calls == 1:
                    return "valid_source_1"
                return "hostile_tampered_source_2"

            @property
            def parser_revision(self) -> int:
                return 1

            def extract_records(self, content: bytes) -> Sequence[ExtractedImportRecord]:
                return []

        parser = HostileSourceParser()
        result = build_import_staging_result(
            portfolio_id=uuid4(),
            account_id=uuid4(),
            filename="file.csv",
            content=b"content",
            imported_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
            parser=parser,
        )

        assert parser.calls == 1
        assert result.file_provenance.source_key == "valid_source_1"

    def test_hostile_dynamic_parser_revision_read_once(self):
        """Q: Dynamic/hostile parser_revision property is accessed exactly ONCE."""
        class HostileRevisionParser:
            def __init__(self):
                self.calls = 0

            @property
            def source_key(self) -> str:
                return "valid_source"

            @property
            def parser_revision(self) -> int:
                self.calls += 1
                if self.calls == 1:
                    return 1
                return 999  # hostile second value

            def extract_records(self, content: bytes) -> Sequence[ExtractedImportRecord]:
                return []

        parser = HostileRevisionParser()
        result = build_import_staging_result(
            portfolio_id=uuid4(),
            account_id=uuid4(),
            filename="file.csv",
            content=b"content",
            imported_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
            parser=parser,
        )

        assert parser.calls == 1
        assert result.parsed_manifest.parser_revision == 1

    def test_extract_records_called_exactly_once(self):
        """R: extract_records is called exactly ONCE per pipeline execution."""
        parser = MockSourceParser(source_key="midas_csv", parser_revision=1, records_to_return=[])
        build_import_staging_result(
            portfolio_id=uuid4(),
            account_id=uuid4(),
            filename="file.csv",
            content=b"content",
            imported_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
            parser=parser,
        )
        assert parser.extract_records_call_count == 1

    def test_parser_receives_exact_original_content_object(self):
        """S: Parser receives the exact original content bytes object without copying or mutation."""
        content = b"exact_unmodified_bytes_payload\r\n\t"
        parser = MockSourceParser(source_key="midas_csv", parser_revision=1, records_to_return=[])
        build_import_staging_result(
            portfolio_id=uuid4(),
            account_id=uuid4(),
            filename="file.csv",
            content=content,
            imported_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
            parser=parser,
        )
        assert parser.received_content is content


# ─────────────────────────────────────────────────────────────────────────────
# 4. Parser Output Collection Contract Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestParserOutputCollectionContract:
    """Verifies that extract_records returns only materialized list/tuple of ExtractedImportRecord."""

    def test_list_and_tuple_output_accepted(self):
        """T, U: Materialized list and tuple outputs succeed identically."""
        rec = _make_extracted_record("row1")
        p_list = MockSourceParser(records_to_return=[rec])
        p_tuple = MockSourceParser()
        p_tuple.extract_records = lambda c: (rec,)  # type: ignore

        res_list = build_import_staging_result(
            uuid4(), uuid4(), "f.csv", b"c", datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc), p_list
        )
        res_tuple = build_import_staging_result(
            uuid4(), uuid4(), "f.csv", b"c", datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc), p_tuple
        )

        assert res_list.raw_manifest.record_count == 1
        assert res_tuple.raw_manifest.record_count == 1

    def test_invalid_collection_types_rejected(self):
        """V-AA: None, generator, set, dict, str, bytes rejected as parser output."""
        rec = _make_extracted_record("row1")

        for bad_output in (
            None,
            (x for x in [rec]),      # generator
            {rec},                   # set
            {"record": rec},         # dict
            "string",
            b"bytes",
            bytearray(b"bytes"),
        ):
            p = MockSourceParser()
            p.extract_records = lambda c, bo=bad_output: bo  # type: ignore

            with pytest.raises(PortfolioImportPipelineError, match="materialized list or tuple"):
                build_import_staging_result(
                    uuid4(), uuid4(), "f.csv", b"c", datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc), p
                )

    def test_non_extracted_import_record_element_rejected(self):
        """AB: Loose tuples, dicts, strings, raw bytes inside collection are rejected."""
        for bad_elem in (
            (b"raw", ()),
            {"raw": b"raw"},
            b"raw_bytes",
            "raw_string",
            object(),
        ):
            p = MockSourceParser()
            p.extract_records = lambda c, be=bad_elem: [be]  # type: ignore

            with pytest.raises(PortfolioImportPipelineError, match="must be an ExtractedImportRecord"):
                build_import_staging_result(
                    uuid4(), uuid4(), "f.csv", b"c", datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc), p
                )


# ─────────────────────────────────────────────────────────────────────────────
# 5. ExtractedImportRecord Contract Hardening Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractedImportRecordContract:
    """Verifies direct constructor hardening on ExtractedImportRecord."""

    def test_valid_record_with_empty_fields_accepted(self):
        """AC: Valid raw bytes and empty tuple fields succeed."""
        rec = ExtractedImportRecord(raw_record=b"valid_raw", fields=())
        assert rec.raw_record == b"valid_raw"
        assert rec.fields == ()

    def test_bytearray_raw_record_rejected(self):
        """AD: Mutable bytearray raw_record is rejected."""
        with pytest.raises(PortfolioImportPipelineError, match="immutable bytes"):
            ExtractedImportRecord(raw_record=bytearray(b"data"), fields=())  # type: ignore

    def test_memoryview_raw_record_rejected(self):
        """AE: memoryview raw_record is rejected."""
        with pytest.raises(PortfolioImportPipelineError, match="immutable bytes"):
            ExtractedImportRecord(raw_record=memoryview(b"data"), fields=())  # type: ignore

    def test_str_raw_record_rejected(self):
        """AF: str raw_record is rejected."""
        with pytest.raises(PortfolioImportPipelineError, match="immutable bytes"):
            ExtractedImportRecord(raw_record="data", fields=())  # type: ignore

    def test_empty_raw_record_rejected(self):
        """AG: Empty raw_record is rejected."""
        with pytest.raises(PortfolioImportPipelineError, match="must not be empty"):
            ExtractedImportRecord(raw_record=b"", fields=())

    def test_list_fields_rejected(self):
        """AH: list for fields direct constructor is rejected."""
        f = ImportParsedField("sym", "AAPL")
        with pytest.raises(PortfolioImportPipelineError, match="immutable tuple"):
            ExtractedImportRecord(raw_record=b"data", fields=[f])  # type: ignore

    def test_unsorted_fields_rejected(self):
        """AI: Unsorted fields tuple is rejected."""
        f1 = ImportParsedField("b_key", "val")
        f2 = ImportParsedField("a_key", "val")
        with pytest.raises(PortfolioImportPipelineError, match="sorted ascending"):
            ExtractedImportRecord(raw_record=b"data", fields=(f1, f2))

    def test_duplicate_field_keys_rejected(self):
        """AJ: Duplicate field keys in tuple are rejected."""
        f1 = ImportParsedField("key", "val1")
        f2 = ImportParsedField("key", "val2")
        with pytest.raises(PortfolioImportPipelineError, match="duplicate field_key detected"):
            ExtractedImportRecord(raw_record=b"data", fields=(f1, f2))

    def test_frozen_mutation_rejected(self):
        """AK: Mutation of ExtractedImportRecord raises FrozenInstanceError."""
        rec = ExtractedImportRecord(raw_record=b"data", fields=())
        with pytest.raises(FrozenInstanceError):
            rec.raw_record = b"mutated"  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 6. Ordinal Assignment & Duplicate Preservation Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestOrdinalAssignmentAndDuplicates:
    """Verifies that parser sequence defines 1..N ordinals without reordering or deduplication."""

    def test_output_sequence_produces_ordinals_1_to_n(self):
        """AL: Parser output order [rA, rB, rC] assigns ordinals 1, 2, 3."""
        rA = _make_extracted_record("rowA")
        rB = _make_extracted_record("rowB")
        rC = _make_extracted_record("rowC")

        parser = MockSourceParser(records_to_return=[rA, rB, rC])
        res = build_import_staging_result(
            uuid4(), uuid4(), "f.csv", b"content", datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc), parser
        )

        assert res.raw_manifest.records[0].record_ordinal == 1
        assert res.raw_manifest.records[1].record_ordinal == 2
        assert res.raw_manifest.records[2].record_ordinal == 3

    def test_identical_raw_record_twice_preserved_as_two_records(self):
        """AM, AN: Identical raw records are preserved as separate records with distinct ordinals (no deduplication)."""
        r1 = _make_extracted_record("IDENTICAL_ROW")
        r2 = _make_extracted_record("IDENTICAL_ROW")

        parser = MockSourceParser(records_to_return=[r1, r2])
        res = build_import_staging_result(
            uuid4(), uuid4(), "f.csv", b"content", datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc), parser
        )

        assert res.raw_manifest.record_count == 2
        assert res.raw_manifest.records[0].record_ordinal == 1
        assert res.raw_manifest.records[1].record_ordinal == 2
        assert res.raw_manifest.records[0].record_sha256 == res.raw_manifest.records[1].record_sha256

    def test_parser_output_list_mutation_does_not_affect_result(self):
        """AO: Mutating the list returned by extract_records after execution does not alter the result."""
        r1 = _make_extracted_record("row1")
        output_list = [r1]

        parser = MockSourceParser()
        parser.extract_records = lambda c: output_list  # type: ignore

        res = build_import_staging_result(
            uuid4(), uuid4(), "f.csv", b"content", datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc), parser
        )

        assert res.raw_manifest.record_count == 1

        # Mutate the original list
        output_list.append(_make_extracted_record("row2"))
        output_list.clear()

        # Result manifests remain unaffected
        assert res.raw_manifest.record_count == 1
        assert res.parsed_manifest.record_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# 7. Identity Sensitivity & Non-Interference Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestIdentitySensitivityAndNonInterference:
    """Verifies that identity changes appropriately with source_key, revision, fields, raw bytes, and order."""

    def test_source_key_change_changes_identities(self):
        """AP: Changing source_key changes file identity, raw manifest identity, and parsed batch identity."""
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        file_bytes = b"row1\n"
        rec = _make_extracted_record("row1")

        p1 = MockSourceParser(source_key="midas_csv", parser_revision=1, records_to_return=[rec])
        p2 = MockSourceParser(source_key="ibkr_flex", parser_revision=1, records_to_return=[rec])

        res1 = build_import_staging_result(port_id, acc_id, "f.csv", file_bytes, t, p1)
        res2 = build_import_staging_result(port_id, acc_id, "f.csv", file_bytes, t, p2)

        assert res1.file_provenance.file_identity != res2.file_provenance.file_identity
        assert res1.raw_manifest.manifest_identity != res2.raw_manifest.manifest_identity
        assert res1.parsed_manifest.parsed_manifest_identity != res2.parsed_manifest.parsed_manifest_identity

    def test_revision_change_leaves_raw_manifest_identical_but_changes_parsed_manifest(self):
        """AQ: Changing parser_revision leaves file identity & raw manifest identical, but changes parsed batch identity."""
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        file_bytes = b"row1\n"
        rec = _make_extracted_record("row1", {"k": "v"})

        p1 = MockSourceParser(source_key="midas_csv", parser_revision=1, records_to_return=[rec])
        p2 = MockSourceParser(source_key="midas_csv", parser_revision=2, records_to_return=[rec])

        res1 = build_import_staging_result(port_id, acc_id, "f.csv", file_bytes, t, p1)
        res2 = build_import_staging_result(port_id, acc_id, "f.csv", file_bytes, t, p2)

        # File and raw manifest identities are IDENTICAL
        assert res1.file_provenance.file_identity == res2.file_provenance.file_identity
        assert res1.raw_manifest.manifest_identity == res2.raw_manifest.manifest_identity
        assert res1.raw_manifest.manifest_sha256 == res2.raw_manifest.manifest_sha256

        # Parsed batch identity is DIFFERENT
        assert res1.parsed_manifest.parsed_manifest_identity != res2.parsed_manifest.parsed_manifest_identity
        assert res1.parsed_manifest.parsed_manifest_sha256 != res2.parsed_manifest.parsed_manifest_sha256

    def test_field_change_leaves_raw_manifest_identical_but_changes_parsed_manifest(self):
        """AR: Changing extracted textual fields leaves raw manifest identical, but changes parsed manifest."""
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        file_bytes = b"row1\n"

        rec1 = _make_extracted_record("row1", {"k": "v1"})
        rec2 = _make_extracted_record("row1", {"k": "v2"})

        p1 = MockSourceParser(source_key="midas_csv", parser_revision=1, records_to_return=[rec1])
        p2 = MockSourceParser(source_key="midas_csv", parser_revision=1, records_to_return=[rec2])

        res1 = build_import_staging_result(port_id, acc_id, "f.csv", file_bytes, t, p1)
        res2 = build_import_staging_result(port_id, acc_id, "f.csv", file_bytes, t, p2)

        assert res1.raw_manifest.manifest_sha256 == res2.raw_manifest.manifest_sha256
        assert res1.parsed_manifest.parsed_manifest_sha256 != res2.parsed_manifest.parsed_manifest_sha256

    def test_raw_byte_change_changes_all_manifests(self):
        """AS: Changing one byte of raw record changes raw record SHA, raw manifest SHA, and parsed manifest SHA."""
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

        rec1 = _make_extracted_record("row1_A")
        rec2 = _make_extracted_record("row1_B")

        p1 = MockSourceParser(source_key="midas_csv", parser_revision=1, records_to_return=[rec1])
        p2 = MockSourceParser(source_key="midas_csv", parser_revision=1, records_to_return=[rec2])

        res1 = build_import_staging_result(port_id, acc_id, "f.csv", b"file1", t, p1)
        res2 = build_import_staging_result(port_id, acc_id, "f.csv", b"file2", t, p2)

        assert res1.raw_manifest.records[0].record_sha256 != res2.raw_manifest.records[0].record_sha256
        assert res1.raw_manifest.manifest_sha256 != res2.raw_manifest.manifest_sha256
        assert res1.parsed_manifest.parsed_manifest_sha256 != res2.parsed_manifest.parsed_manifest_sha256

    def test_raw_record_order_change_changes_manifest_identities(self):
        """AT: Reversing parser-return record order changes raw and parsed manifest identities."""
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        file_bytes = b"rowA\nrowB\n"

        rA = _make_extracted_record("rowA")
        rB = _make_extracted_record("rowB")

        p1 = MockSourceParser(records_to_return=[rA, rB])
        p2 = MockSourceParser(records_to_return=[rB, rA])

        res1 = build_import_staging_result(port_id, acc_id, "f.csv", file_bytes, t, p1)
        res2 = build_import_staging_result(port_id, acc_id, "f.csv", file_bytes, t, p2)

        assert res1.raw_manifest.manifest_sha256 != res2.raw_manifest.manifest_sha256
        assert res1.parsed_manifest.parsed_manifest_sha256 != res2.parsed_manifest.parsed_manifest_sha256


# ─────────────────────────────────────────────────────────────────────────────
# 8. Failure Propagation & Zero Partial Results Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestFailurePropagationAndZeroPartialResults:
    """Verifies that lower-layer errors and parser exceptions propagate unchanged without partial returns."""

    def test_parser_runtime_error_propagates_unchanged(self):
        """AU: Exception raised by parser.extract_records propagates directly without wrapping."""
        class CustomParserError(RuntimeError):
            pass

        parser = MockSourceParser(extract_side_effect=CustomParserError("CSV parse corrupt"))

        with pytest.raises(CustomParserError, match="CSV parse corrupt"):
            build_import_staging_result(
                uuid4(), uuid4(), "f.csv", b"content", datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc), parser
            )

    def test_provenance_error_propagates_unchanged(self):
        """AV: Invalid input to file provenance raises PortfolioImportProvenanceError directly."""
        parser = MockSourceParser()
        with pytest.raises(PortfolioImportProvenanceError, match="portfolio_id must be a UUID"):
            build_import_staging_result(
                portfolio_id="not-a-uuid",  # type: ignore
                account_id=uuid4(),
                filename="f.csv",
                content=b"content",
                imported_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
                parser=parser,
            )

    def test_no_partial_result_returned_on_failure(self):
        """AX: Any failure produces an exception and returns nothing (no partial manifests)."""
        parser = MockSourceParser(extract_side_effect=ValueError("bad format"))

        result = None
        try:
            result = build_import_staging_result(
                uuid4(), uuid4(), "f.csv", b"content", datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc), parser
            )
        except ValueError:
            pass

        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# 9. Result Constructor Hardening Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestResultConstructorHardening:
    """Verifies direct constructor validation of ImportStagingResult."""

    def test_canonical_direct_constructor_accepted(self):
        """AY: Direct constructor with matching components succeeds."""
        port_id = uuid4()
        acc_id = uuid4()
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

        file_prov = build_import_file_provenance(port_id, acc_id, "midas_csv", "f.csv", b"data", t)
        raw_manifest = build_import_batch_manifest(file_prov, [])
        parsed_manifest = build_parsed_import_batch_manifest(raw_manifest, 1, [])

        staging = ImportStagingResult(
            file_provenance=file_prov,
            raw_manifest=raw_manifest,
            parsed_manifest=parsed_manifest,
        )

        assert staging.file_provenance == file_prov
        assert staging.raw_manifest == raw_manifest
        assert staging.parsed_manifest == parsed_manifest

    def test_foreign_raw_manifest_rejected(self):
        """AZ: Raw manifest from different file provenance fails closed."""
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        fp1 = build_import_file_provenance(uuid4(), uuid4(), "midas_csv", "f1.csv", b"data1", t)
        fp2 = build_import_file_provenance(uuid4(), uuid4(), "midas_csv", "f2.csv", b"data2", t)

        raw_manifest_2 = build_import_batch_manifest(fp2, [])
        parsed_manifest_2 = build_parsed_import_batch_manifest(raw_manifest_2, 1, [])

        with pytest.raises(PortfolioImportPipelineError, match="raw_manifest.file_provenance does not match"):
            ImportStagingResult(
                file_provenance=fp1,  # fp1 vs raw_manifest_2 (fp2)
                raw_manifest=raw_manifest_2,
                parsed_manifest=parsed_manifest_2,
            )

    def test_foreign_parsed_manifest_rejected(self):
        """BA: Parsed manifest from different raw manifest fails closed."""
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        fp = build_import_file_provenance(uuid4(), uuid4(), "midas_csv", "f.csv", b"data", t)
        raw_manifest_1 = build_import_batch_manifest(fp, [])

        fp_other = build_import_file_provenance(uuid4(), uuid4(), "midas_csv", "other.csv", b"other", t)
        raw_manifest_2 = build_import_batch_manifest(fp_other, [])
        parsed_manifest_2 = build_parsed_import_batch_manifest(raw_manifest_2, 1, [])

        with pytest.raises(PortfolioImportPipelineError, match="parsed_manifest.raw_manifest does not match"):
            ImportStagingResult(
                file_provenance=fp,
                raw_manifest=raw_manifest_1,
                parsed_manifest=parsed_manifest_2,  # parsed_manifest_2 wraps raw_manifest_2
            )

    def test_wrong_component_types_rejected(self):
        """BB: Non-dataclass or wrong type components fail closed."""
        t = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
        fp = build_import_file_provenance(uuid4(), uuid4(), "midas_csv", "f.csv", b"data", t)
        raw_m = build_import_batch_manifest(fp, [])
        parsed_m = build_parsed_import_batch_manifest(raw_m, 1, [])

        with pytest.raises(PortfolioImportPipelineError, match="file_provenance"):
            ImportStagingResult(file_provenance=object(), raw_manifest=raw_m, parsed_manifest=parsed_m)  # type: ignore

        with pytest.raises(PortfolioImportPipelineError, match="raw_manifest"):
            ImportStagingResult(file_provenance=fp, raw_manifest=object(), parsed_manifest=parsed_m)  # type: ignore

        with pytest.raises(PortfolioImportPipelineError, match="parsed_manifest"):
            ImportStagingResult(file_provenance=fp, raw_manifest=raw_m, parsed_manifest=object())  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# 10. Raw-Byte Non-Retention & Ledger Separation Surface Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestRawByteNonRetentionAndLedgerSeparation:
    """Verifies that ImportStagingResult and ExtractedImportRecord strictly segregate from ledger and raw bytes."""

    def test_staging_result_field_surface(self):
        """ImportStagingResult contains exactly file_provenance, raw_manifest, parsed_manifest."""
        staging_fields = {f.name for f in fields(ImportStagingResult)}
        assert staging_fields == {"file_provenance", "raw_manifest", "parsed_manifest"}

    def test_extracted_record_field_surface(self):
        """ExtractedImportRecord contains exactly raw_record, fields."""
        extracted_fields = {f.name for f in fields(ExtractedImportRecord)}
        assert extracted_fields == {"raw_record", "fields"}

    def test_no_ledger_fields_in_staging_result(self):
        """No financial transaction, instrument, or persistence attributes in pipeline result."""
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
            "owner_id",
            "content",
            "raw_file",
            "raw_record",
            "parser",
        }

        staging_fields = {f.name for f in fields(ImportStagingResult)}
        overlap = staging_fields & forbidden_fields
        assert not overlap, f"Forbidden fields found in ImportStagingResult: {overlap}"
