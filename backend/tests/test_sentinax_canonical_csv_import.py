"""
Unit and integration test suite for Sentinax Canonical CSV v1 end-to-end import orchestration (Phase 13M).
Verifies composition of staging/parser (13E/F) -> semantics (13L) -> instrument resolver (13K) -> resolution batch (13J).
"""

from datetime import date, datetime, timezone
from decimal import Decimal
import inspect
from typing import Dict, List, Sequence, Tuple
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import Currency, TransactionType
from backend.engine.private.portfolio.import_assessment import (
    ImportAssessmentStatus,
)
from backend.engine.private.portfolio.import_instrument_resolution import (
    ImportInstrumentResolutionStatus,
)
from backend.engine.private.portfolio.import_instrument_resolver import (
    PortfolioImportInstrumentResolver,
    PortfolioImportInstrumentResolverError,
)
from backend.engine.private.portfolio.parsers import (
    SentinaxCanonicalCsvError,
    SentinaxCanonicalCsvSemanticError,
)
from backend.engine.private.portfolio.sentinax_csv_import import (
    run_sentinax_canonical_csv_import_v1,
)


CANONICAL_HEADERS = (
    "transaction_type,effective_date,executed_at,instrument_reference,"
    "quantity,unit_price,trade_currency,cash_amount,cash_currency,"
    "from_currency,from_amount,to_currency,to_amount"
)


class MockInstrumentResolver(PortfolioImportInstrumentResolver):
    """
    Test double for PortfolioImportInstrumentResolver.
    """
    def __init__(
        self,
        resolver_key: str = "test_resolver",
        resolver_revision: int = 1,
        mapping: Dict[Tuple[str, date], Sequence[UUID]] | None = None,
        exception_on_ref: str | None = None,
        malformed_return: bool = False,
    ):
        self._resolver_key = resolver_key
        self._resolver_revision = resolver_revision
        self._mapping = mapping or {}
        self._exception_on_ref = exception_on_ref
        self._malformed_return = malformed_return
        self.invocations: List[Tuple[str, date]] = []

    @property
    def resolver_key(self) -> str:
        return self._resolver_key

    @property
    def resolver_revision(self) -> int:
        return self._resolver_revision

    def resolve_candidates(
        self,
        instrument_reference: str,
        as_of_date: date,
    ) -> Sequence[UUID]:
        self.invocations.append((instrument_reference, as_of_date))
        if self._exception_on_ref and instrument_reference == self._exception_on_ref:
            raise RuntimeError(f"Resolver failed for reference: {instrument_reference}")
        if self._malformed_return:
            return "not-a-list"  # type: ignore
        return self._mapping.get((instrument_reference, as_of_date), ())


def _make_csv(rows: Sequence[str]) -> bytes:
    content = f"{CANONICAL_HEADERS}\n" + "\n".join(rows) + "\n"
    return content.encode("utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# 1. End-to-End Golden Workflows
# ─────────────────────────────────────────────────────────────────────────────

class TestCanonicalCsvImportEndToEnd:
    """Sections 42-48: End-to-end pipeline execution from CSV bytes to resolution batch."""

    def test_valid_buy_end_to_end(self):
        """Section 42: Valid BUY CSV parses, converts to draft, resolves 1 candidate to RESOLVED."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"
        ])

        resolver = MockInstrumentResolver(
            mapping={("AAPL", eff_date): [inst_id]}
        )

        batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        # 1. Resolution batch verification
        assert batch.resolution_count == 1
        assert batch.resolved_count == 1
        assert batch.unresolved_count == 0
        assert batch.ambiguous_count == 0
        assert batch.not_required_count == 0
        assert batch.is_fully_resolved is True

        res = batch.resolutions[0]
        assert res.status == ImportInstrumentResolutionStatus.RESOLVED
        assert res.instrument_id == inst_id
        assert res.candidate_instrument_ids == ()
        assert res.resolution_as_of_date == eff_date

        # 2. Complete nested provenance chain inspectability
        draft_manifest = batch.draft_manifest
        assert draft_manifest.draft_count == 1
        draft = draft_manifest.drafts[0]
        assert draft.transaction_type == TransactionType.BUY
        assert draft.quantity == Decimal("10")
        assert draft.unit_price == Decimal("150.00")
        assert draft.trade_currency == Currency.USD
        assert draft.instrument_reference == "AAPL"

        assessment_batch = draft_manifest.assessment_batch
        assert assessment_batch.record_count == 1
        assert assessment_batch.ready_count == 1
        assert assessment_batch.rejected_count == 0
        assert assessment_batch.assessments[0].status == ImportAssessmentStatus.READY

        parsed_manifest = assessment_batch.parsed_manifest
        assert parsed_manifest.record_count == 1
        assert parsed_manifest.parser_revision == 1

        raw_manifest = parsed_manifest.raw_manifest
        assert raw_manifest.record_count == 1

        file_prov = raw_manifest.file_provenance
        assert file_prov.portfolio_id == port_id
        assert file_prov.account_id == acc_id
        assert file_prov.source_key == "sentinax_csv"
        assert file_prov.filename == "trades.csv"
        assert file_prov.imported_at == imported_at

        # 3. Resolver invoked exactly once with exact args
        assert resolver.invocations == [("AAPL", eff_date)]

    def test_valid_cash_deposit_end_to_end(self):
        """Section 43: CASH_DEPOSIT produces NOT_REQUIRED resolution with zero resolver calls."""
        port_id = uuid4()
        acc_id = uuid4()
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = _make_csv([
            "cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,500.00,USD,,,,"
        ])

        resolver = MockInstrumentResolver()

        batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="cash.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        assert batch.resolution_count == 1
        assert batch.not_required_count == 1
        assert batch.resolved_count == 0
        assert batch.resolutions[0].status == ImportInstrumentResolutionStatus.NOT_REQUIRED
        assert batch.resolutions[0].instrument_id is None
        assert batch.is_fully_resolved is True
        assert len(resolver.invocations) == 0

    def test_zero_candidate_unresolved(self):
        """Section 44: Resolver returning empty list produces UNRESOLVED with exact diagnostic."""
        port_id = uuid4()
        acc_id = uuid4()
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,UNKNOWN_TICKER,10,10.00,USD,,,,,,"
        ])

        resolver = MockInstrumentResolver(mapping={})  # 0 candidates

        batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        assert batch.resolution_count == 1
        assert batch.unresolved_count == 1
        assert batch.is_fully_resolved is False

        res = batch.resolutions[0]
        assert res.status == ImportInstrumentResolutionStatus.UNRESOLVED
        assert res.instrument_id is None
        assert len(res.diagnostics) == 1
        assert res.diagnostics[0].code == "instrument_not_found"

    def test_multi_candidate_ambiguous(self):
        """Section 45: Resolver returning multiple candidates produces AMBIGUOUS with sorted UUIDs."""
        port_id = uuid4()
        acc_id = uuid4()
        id1 = uuid4()
        id2 = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,DUAL_LISTED,10,10.00,USD,,,,,,"
        ])

        resolver = MockInstrumentResolver(
            mapping={("DUAL_LISTED", eff_date): [id2, id1]}
        )

        batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        assert batch.resolution_count == 1
        assert batch.ambiguous_count == 1
        assert batch.is_fully_resolved is False

        res = batch.resolutions[0]
        assert res.status == ImportInstrumentResolutionStatus.AMBIGUOUS
        assert res.instrument_id is None
        assert res.candidate_instrument_ids == tuple(sorted([id1, id2], key=str))
        assert len(res.diagnostics) == 1
        assert res.diagnostics[0].code == "ambiguous_reference"

    def test_mixed_semantic_batch(self):
        """Section 46: 5-row mixed batch yields 3 READY drafts and 2 resolver invocations."""
        port_id = uuid4()
        acc_id = uuid4()
        aapl_id = uuid4()
        msft_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,BAD_DECIMAL,bad_qty,300.00,USD,,,,,,",
            "cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,500.00,USD,,,,",
            "fx_conversion,2026-08-28,2026-08-28T10:15:30+00:00,,,,,,,USD,100.00,USD,100.00",
            "dividend,2026-08-28,2026-08-28T10:15:30+00:00,MSFT,,,,50.00,USD,,,,",
        ])

        resolver = MockInstrumentResolver(
            mapping={
                ("AAPL", eff_date): [aapl_id],
                ("MSFT", eff_date): [msft_id],
            }
        )

        batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="mixed.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        # Assessment check
        assessments = batch.draft_manifest.assessment_batch
        assert assessments.record_count == 5
        assert assessments.ready_count == 3
        assert assessments.rejected_count == 2

        # Draft check
        assert batch.draft_manifest.draft_count == 3
        assert [d.record_ordinal for d in batch.draft_manifest.drafts] == [1, 3, 5]

        # Resolution check: 3 resolutions (BUY, CASH_DEPOSIT, DIVIDEND)
        assert batch.resolution_count == 3
        assert batch.resolutions[0].status == ImportInstrumentResolutionStatus.RESOLVED
        assert batch.resolutions[0].instrument_id == aapl_id

        assert batch.resolutions[1].status == ImportInstrumentResolutionStatus.NOT_REQUIRED

        assert batch.resolutions[2].status == ImportInstrumentResolutionStatus.RESOLVED
        assert batch.resolutions[2].instrument_id == msft_id

        # Only row 1 and row 5 invoked resolver
        assert resolver.invocations == [("AAPL", eff_date), ("MSFT", eff_date)]

    def test_all_rejected_batch(self):
        """Section 47: Batch with all semantically rejected rows produces 0 drafts and 0 resolutions."""
        port_id = uuid4()
        acc_id = uuid4()
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,bad_qty,150.00,USD,,,,,,",
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,MSFT,10,bad_price,USD,,,,,,",
        ])

        resolver = MockInstrumentResolver()

        batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="rejected.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        assert batch.draft_manifest.assessment_batch.record_count == 2
        assert batch.draft_manifest.assessment_batch.ready_count == 0
        assert batch.draft_manifest.assessment_batch.rejected_count == 2
        assert batch.draft_manifest.draft_count == 0
        assert batch.resolution_count == 0
        assert len(resolver.invocations) == 0

    def test_header_only_batch(self):
        """Section 48: Header-only CSV produces 0 records, drafts, and resolutions."""
        port_id = uuid4()
        acc_id = uuid4()
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = f"{CANONICAL_HEADERS}\n".encode("utf-8")
        resolver = MockInstrumentResolver()

        batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="empty.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        assert batch.draft_manifest.assessment_batch.record_count == 0
        assert batch.draft_manifest.draft_count == 0
        assert batch.resolution_count == 0
        assert len(resolver.invocations) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 2. Error Propagation & Fault Isolation
# ─────────────────────────────────────────────────────────────────────────────

class TestErrorPropagation:
    """Sections 49-53: Lower-layer exceptions propagate unchanged without partial batches."""

    def test_malformed_csv_raises_syntax_error(self):
        """Section 49: Malformed CSV quotes/syntax raises SentinaxCanonicalCsvError."""
        port_id = uuid4()
        acc_id = uuid4()
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        # Unbalanced quotes
        csv_bytes = b'transaction_type,effective_date\n"buy,2026-08-28\n'
        resolver = MockInstrumentResolver()

        with pytest.raises(SentinaxCanonicalCsvError):
            run_sentinax_canonical_csv_import_v1(
                portfolio_id=port_id,
                account_id=acc_id,
                filename="bad.csv",
                content=csv_bytes,
                imported_at=imported_at,
                resolver=resolver,
            )

        assert len(resolver.invocations) == 0

    def test_wrong_semantic_schema_raises_semantic_error(self):
        """Section 50: Non-13-column valid CSV raises SentinaxCanonicalCsvSemanticError."""
        port_id = uuid4()
        acc_id = uuid4()
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        # 2 columns header (valid CSV syntax, invalid semantic schema)
        csv_bytes = b"transaction_type,effective_date\nbuy,2026-08-28\n"
        resolver = MockInstrumentResolver()

        with pytest.raises(SentinaxCanonicalCsvSemanticError, match="13 canonical field keys"):
            run_sentinax_canonical_csv_import_v1(
                portfolio_id=port_id,
                account_id=acc_id,
                filename="bad_schema.csv",
                content=csv_bytes,
                imported_at=imported_at,
                resolver=resolver,
            )

        assert len(resolver.invocations) == 0

    def test_resolver_execution_error_propagates_verbatim(self):
        """Section 52: Exception raised by resolver callable propagates unchanged."""
        port_id = uuid4()
        acc_id = uuid4()
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,EXPLODING_TICKER,10,150.00,USD,,,,,,"
        ])

        resolver = MockInstrumentResolver(
            exception_on_ref="EXPLODING_TICKER"
        )

        with pytest.raises(RuntimeError, match="Resolver failed for reference: EXPLODING_TICKER"):
            run_sentinax_canonical_csv_import_v1(
                portfolio_id=port_id,
                account_id=acc_id,
                filename="trades.csv",
                content=csv_bytes,
                imported_at=imported_at,
                resolver=resolver,
            )

    def test_resolver_contract_error_propagates(self):
        """Section 53: Malformed resolver return collection raises PortfolioImportInstrumentResolverError."""
        port_id = uuid4()
        acc_id = uuid4()
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"
        ])

        resolver = MockInstrumentResolver(
            malformed_return=True
        )

        with pytest.raises(PortfolioImportInstrumentResolverError):
            run_sentinax_canonical_csv_import_v1(
                portfolio_id=port_id,
                account_id=acc_id,
                filename="trades.csv",
                content=csv_bytes,
                imported_at=imported_at,
                resolver=resolver,
            )

    def test_partial_failure_raises_no_partial_batch(self):
        """Section 69: If second row resolver fails, exception propagates and no partial batch is returned."""
        port_id = uuid4()
        acc_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,BOOM,10,150.00,USD,,,,,,",
        ])

        resolver = MockInstrumentResolver(
            mapping={("AAPL", eff_date): [uuid4()]},
            exception_on_ref="BOOM",
        )

        with pytest.raises(RuntimeError, match="BOOM"):
            run_sentinax_canonical_csv_import_v1(
                portfolio_id=port_id,
                account_id=acc_id,
                filename="trades.csv",
                content=csv_bytes,
                imported_at=imported_at,
                resolver=resolver,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Provenance, Date, Reference & Hash Invariants
# ─────────────────────────────────────────────────────────────────────────────

class TestInvariantsAndRegressions:
    """Sections 54-63: Precision, determinism, provenance, and sensitivity regressions."""

    def test_exact_bytes_preserved_in_file_provenance(self):
        """Section 54: Exact input bytes preserved in file provenance."""
        import hashlib
        port_id = uuid4()
        acc_id = uuid4()
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"
        ])

        resolver = MockInstrumentResolver(
            mapping={("AAPL", date(2026, 8, 28)): [uuid4()]}
        )

        batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        file_prov = batch.draft_manifest.assessment_batch.parsed_manifest.raw_manifest.file_provenance
        assert file_prov.content_sha256 == hashlib.sha256(csv_bytes).hexdigest()
        assert file_prov.byte_length == len(csv_bytes)

    def test_exact_reference_preserved_to_resolver(self):
        """Section 55: Non-normalized instrument reference passed unchanged to resolver."""
        port_id = uuid4()
        acc_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)
        special_ref = " ALTIN.S1  "

        csv_bytes = _make_csv([
            f'buy,2026-08-28,2026-08-28T10:15:30+00:00,"{special_ref}",10,150.00,USD,,,,,,'
        ])

        resolver = MockInstrumentResolver(
            mapping={(special_ref, eff_date): [uuid4()]}
        )

        batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        assert resolver.invocations == [(special_ref, eff_date)]
        assert batch.resolved_count == 1

    def test_pit_date_passed_to_resolver(self):
        """Section 56: Resolver receives effective_date, not imported_at or executed_at."""
        port_id = uuid4()
        acc_id = uuid4()
        eff_date = date(2025, 1, 15)  # effective_date is in 2025
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)  # imported_at is in 2026

        csv_bytes = _make_csv([
            "buy,2025-01-15,2025-01-15T09:30:00-05:00,AAPL,10,150.00,USD,,,,,,"
        ])

        resolver = MockInstrumentResolver(
            mapping={("AAPL", eff_date): [uuid4()]}
        )

        run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        # Resolver received 2025-01-15
        assert resolver.invocations == [("AAPL", eff_date)]

    def test_repeated_run_determinism(self):
        """Section 57: Running identical import twice yields identical manifest SHAs at all layers."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
            "cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,500.00,USD,,,,",
        ])

        resolver = MockInstrumentResolver(
            mapping={("AAPL", eff_date): [inst_id]}
        )

        batch1 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        batch2 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        assert batch1.resolution_manifest_sha256 == batch2.resolution_manifest_sha256
        assert batch1.draft_manifest.draft_manifest_sha256 == batch2.draft_manifest.draft_manifest_sha256
        assert batch1.draft_manifest.assessment_batch.assessment_manifest_sha256 == batch2.draft_manifest.assessment_batch.assessment_manifest_sha256
        assert batch1.draft_manifest.assessment_batch.parsed_manifest.parsed_manifest_sha256 == batch2.draft_manifest.assessment_batch.parsed_manifest.parsed_manifest_sha256
        assert batch1.draft_manifest.assessment_batch.parsed_manifest.raw_manifest.manifest_sha256 == batch2.draft_manifest.assessment_batch.parsed_manifest.raw_manifest.manifest_sha256

    def test_filename_hash_invariance(self):
        """Section 58: Changing filename preserves canonical staging/economic/resolution digests."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"
        ])

        resolver = MockInstrumentResolver(
            mapping={("AAPL", eff_date): [inst_id]}
        )

        batch1 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="file_alpha.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        batch2 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="file_beta.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        # File provenance stores filename, but canonical digests are filename-invariant
        assert batch1.resolution_manifest_sha256 == batch2.resolution_manifest_sha256
        assert batch1.draft_manifest.draft_manifest_sha256 == batch2.draft_manifest.draft_manifest_sha256

    def test_imported_at_hash_invariance(self):
        """Section 59: Changing imported_at preserves canonical digests."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"
        ])

        resolver = MockInstrumentResolver(
            mapping={("AAPL", eff_date): [inst_id]}
        )

        batch1 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
            resolver=resolver,
        )

        batch2 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=csv_bytes,
            imported_at=datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc),
            resolver=resolver,
        )

        assert batch1.resolution_manifest_sha256 == batch2.resolution_manifest_sha256
        assert batch1.draft_manifest.draft_manifest_sha256 == batch2.draft_manifest.draft_manifest_sha256

    def test_portfolio_sensitivity(self):
        """Section 60: Different portfolio_id changes digests."""
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"
        ])

        resolver = MockInstrumentResolver(
            mapping={("AAPL", eff_date): [inst_id]}
        )

        batch1 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=uuid4(),
            account_id=acc_id,
            filename="trades.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        batch2 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=uuid4(),
            account_id=acc_id,
            filename="trades.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        assert batch1.resolution_manifest_sha256 != batch2.resolution_manifest_sha256

    def test_account_sensitivity(self):
        """Section 61: Different account_id changes digests."""
        port_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"
        ])

        resolver = MockInstrumentResolver(
            mapping={("AAPL", eff_date): [inst_id]}
        )

        batch1 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=uuid4(),
            filename="trades.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        batch2 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=uuid4(),
            filename="trades.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        assert batch1.resolution_manifest_sha256 != batch2.resolution_manifest_sha256

    def test_resolver_revision_sensitivity(self):
        """Section 62: Changing resolver_revision changes resolution hashes but leaves staging/draft hashes unchanged."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"
        ])

        resolver1 = MockInstrumentResolver(
            resolver_revision=1,
            mapping={("AAPL", eff_date): [inst_id]},
        )
        resolver2 = MockInstrumentResolver(
            resolver_revision=2,
            mapping={("AAPL", eff_date): [inst_id]},
        )

        batch1 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver1,
        )

        batch2 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver2,
        )

        # Staging and drafts identical
        assert batch1.draft_manifest.draft_manifest_sha256 == batch2.draft_manifest.draft_manifest_sha256
        # Resolution manifest SHA differs due to revision change
        assert batch1.resolution_manifest_sha256 != batch2.resolution_manifest_sha256

    def test_resolver_key_sensitivity(self):
        """Section 63: Changing resolver_key changes resolution hashes."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,"
        ])

        resolver1 = MockInstrumentResolver(
            resolver_key="resolver_a",
            mapping={("AAPL", eff_date): [inst_id]},
        )
        resolver2 = MockInstrumentResolver(
            resolver_key="resolver_b",
            mapping={("AAPL", eff_date): [inst_id]},
        )

        batch1 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver1,
        )

        batch2 = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver2,
        )

        assert batch1.draft_manifest.draft_manifest_sha256 == batch2.draft_manifest.draft_manifest_sha256
        assert batch1.resolution_manifest_sha256 != batch2.resolution_manifest_sha256


# ─────────────────────────────────────────────────────────────────────────────
# 4. Source Inspection Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSourceInspection:
    """Sections 64-68: Structural assertions on the new orchestration module."""

    def test_no_hashlib_import(self):
        """Section 64: sentinax_csv_import does not import hashlib."""
        import backend.engine.private.portfolio.sentinax_csv_import as mod
        source = inspect.getsource(mod)
        assert "import hashlib" not in source
        assert "from hashlib" not in source
        assert "sha256" not in source

    def test_no_financial_field_logic(self):
        """Section 65: sentinax_csv_import contains no financial field logic."""
        import backend.engine.private.portfolio.sentinax_csv_import as mod
        source = inspect.getsource(mod)
        for term in ["quantity", "unit_price", "cash_amount", "from_amount", "to_amount", "trade_currency", "cash_currency", "transaction_type"]:
            assert f"draft.{term}" not in source
            assert f"record.{term}" not in source

    def test_no_current_time_calls(self):
        """Section 66: sentinax_csv_import contains no now(), utcnow(), today()."""
        import backend.engine.private.portfolio.sentinax_csv_import as mod
        source = inspect.getsource(mod)
        assert "datetime.now" not in source
        assert "datetime.utcnow" not in source
        assert "date.today" not in source

    def test_no_ledger_surface(self):
        """Section 67: sentinax_csv_import contains no ledger or persistence concepts."""
        import backend.engine.private.portfolio.sentinax_csv_import as mod
        source = inspect.getsource(mod)
        for term in ["PortfolioTransaction", "PortfolioRepository", "append_transaction", "recorded_at", "external_source", "external_reference", "cash_bucket_id"]:
            assert term not in source

    def test_no_legacy_resolver(self):
        """Section 68: sentinax_csv_import contains no InstrumentResolverService or legacy identity import."""
        import backend.engine.private.portfolio.sentinax_csv_import as mod
        source = inspect.getsource(mod)
        assert "InstrumentResolverService" not in source
        assert "backend.engine.private.identity" not in source
