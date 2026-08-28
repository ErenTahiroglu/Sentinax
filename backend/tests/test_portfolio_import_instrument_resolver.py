"""
backend/tests/test_portfolio_import_instrument_resolver.py
==========================================================
Tests for Phase 13K: PIT-Safe Instrument Resolver Execution Port & Complete Batch Harness.

Zero network calls (pytest-socket enforced).
Pure in-memory domain evaluation using real Phase 13A-13J builders and models.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable, List, Optional, Sequence, Tuple
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import Currency, TransactionType
from backend.engine.private.portfolio.import_assessment import (
    ImportAssessmentBatch,
    ImportAssessmentDiagnostic,
    ImportAssessmentStatus,
    build_import_assessment_batch,
    build_import_record_assessment,
)
from backend.engine.private.portfolio.import_batch import build_import_batch_manifest
from backend.engine.private.portfolio.import_draft import (
    ImportTransactionDraft,
    build_import_transaction_draft,
)
from backend.engine.private.portfolio.import_draft_batch import (
    ImportDraftBatchManifest,
    build_import_draft_batch_manifest,
)
from backend.engine.private.portfolio.import_instrument_resolution import (
    ImportInstrumentResolution,
    ImportInstrumentResolutionBatch,
    ImportInstrumentResolutionStatus,
)
from backend.engine.private.portfolio.import_instrument_resolver import (
    PortfolioImportInstrumentResolver,
    PortfolioImportInstrumentResolverError,
    resolve_import_draft_batch_instruments,
)
from backend.engine.private.portfolio.import_parsing import (
    ImportParsedField,
    build_parsed_import_record,
)
from backend.engine.private.portfolio.import_parsed_batch import (
    build_parsed_import_batch_manifest,
)
from backend.engine.private.portfolio.import_provenance import (
    build_import_file_provenance,
    build_import_record_provenance,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test Fixtures & Mock Resolver Adapters
# ─────────────────────────────────────────────────────────────────────────────

def _make_test_assessment_batch(
    statuses: List[ImportAssessmentStatus],
    portfolio_id=None,
    account_id=None,
    source_key: str = "sentinax_csv",
    parser_revision: int = 1,
) -> ImportAssessmentBatch:
    """Builds a real, verified ImportAssessmentBatch with specified statuses."""
    port_id = portfolio_id or uuid4()
    acc_id = account_id or uuid4()
    t = datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)

    file_prov = build_import_file_provenance(
        portfolio_id=port_id,
        account_id=acc_id,
        source_key=source_key,
        filename="test.csv",
        content=b"dummy_content",
        imported_at=t,
    )

    raw_rows = [f"row_{i}".encode("utf-8") for i in range(len(statuses))]

    rec_provs = [
        build_import_record_provenance(
            file_provenance=file_prov,
            record_ordinal=i + 1,
            raw_record=raw_rows[i],
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
            raw_record=raw_rows[i],
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
            diag = ImportAssessmentDiagnostic(
                code="diag_code", message="Diag message", field_key="symbol"
            )
            ass = build_import_record_assessment(parsed_records[i], status, [diag])
        assessments.append(ass)

    return build_import_assessment_batch(parsed_manifest, assessments)


def _make_empty_draft_batch() -> ImportDraftBatchManifest:
    """Builds a verified ImportDraftBatchManifest with zero drafts."""
    port_id = uuid4()
    acc_id = uuid4()
    t = datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)

    file_prov = build_import_file_provenance(
        portfolio_id=port_id,
        account_id=acc_id,
        source_key="sentinax_csv",
        filename="empty.csv",
        content=b"empty",
        imported_at=t,
    )
    raw_manifest = build_import_batch_manifest(file_provenance=file_prov, records=[])
    parsed_manifest = build_parsed_import_batch_manifest(raw_manifest=raw_manifest, parser_revision=1, parsed_records=[])
    ass_batch = build_import_assessment_batch(parsed_manifest, [])
    return build_import_draft_batch_manifest(ass_batch, [])


def _make_buy_draft(
    assessment_batch: ImportAssessmentBatch,
    record_ordinal: int,
    instrument: str = "AAPL",
    qty: str = "10",
    price: str = "150.00",
    eff_date: date = date(2026, 8, 28),
    executed_at: Optional[datetime] = None,
) -> ImportTransactionDraft:
    return build_import_transaction_draft(
        assessment_batch=assessment_batch,
        record_ordinal=record_ordinal,
        transaction_type=TransactionType.BUY,
        effective_date=eff_date,
        executed_at=executed_at,
        instrument_reference=instrument,
        quantity=Decimal(qty),
        unit_price=Decimal(price),
        trade_currency=Currency.USD,
    )


def _make_sell_draft(
    assessment_batch: ImportAssessmentBatch,
    record_ordinal: int,
    instrument: str = "MSFT",
    qty: str = "5",
    price: str = "300.00",
    eff_date: date = date(2026, 8, 28),
) -> ImportTransactionDraft:
    return build_import_transaction_draft(
        assessment_batch=assessment_batch,
        record_ordinal=record_ordinal,
        transaction_type=TransactionType.SELL,
        effective_date=eff_date,
        instrument_reference=instrument,
        quantity=Decimal(qty),
        unit_price=Decimal(price),
        trade_currency=Currency.USD,
    )


def _make_cash_deposit_draft(
    assessment_batch: ImportAssessmentBatch,
    record_ordinal: int,
    amount: str = "500.00",
    eff_date: date = date(2026, 8, 28),
) -> ImportTransactionDraft:
    return build_import_transaction_draft(
        assessment_batch=assessment_batch,
        record_ordinal=record_ordinal,
        transaction_type=TransactionType.CASH_DEPOSIT,
        effective_date=eff_date,
        cash_amount=Decimal(amount),
        cash_currency=Currency.USD,
    )


def _make_cash_withdrawal_draft(
    assessment_batch: ImportAssessmentBatch,
    record_ordinal: int,
    amount: str = "200.00",
    eff_date: date = date(2026, 8, 28),
) -> ImportTransactionDraft:
    return build_import_transaction_draft(
        assessment_batch=assessment_batch,
        record_ordinal=record_ordinal,
        transaction_type=TransactionType.CASH_WITHDRAWAL,
        effective_date=eff_date,
        cash_amount=Decimal(amount),
        cash_currency=Currency.USD,
    )


def _make_dividend_draft(
    assessment_batch: ImportAssessmentBatch,
    record_ordinal: int,
    amount: str = "50.00",
    instrument: Optional[str] = "AAPL",
    eff_date: date = date(2026, 8, 28),
) -> ImportTransactionDraft:
    return build_import_transaction_draft(
        assessment_batch=assessment_batch,
        record_ordinal=record_ordinal,
        transaction_type=TransactionType.DIVIDEND,
        effective_date=eff_date,
        instrument_reference=instrument,
        cash_amount=Decimal(amount),
        cash_currency=Currency.USD,
    )


def _make_fx_draft(
    assessment_batch: ImportAssessmentBatch,
    record_ordinal: int,
    from_amt: str = "100.00",
    to_amt: str = "3200.00",
    eff_date: date = date(2026, 8, 28),
) -> ImportTransactionDraft:
    return build_import_transaction_draft(
        assessment_batch=assessment_batch,
        record_ordinal=record_ordinal,
        transaction_type=TransactionType.FX_CONVERSION,
        effective_date=eff_date,
        from_currency=Currency.USD,
        from_amount=Decimal(from_amt),
        to_currency=Currency.TRY,
        to_amount=Decimal(to_amt),
    )


class MockResolver:
    """Mock implementation of PortfolioImportInstrumentResolver for test suites."""

    def __init__(
        self,
        mapping: Optional[dict[str, Sequence[UUID]]] = None,
        key: str = "test_resolver",
        revision: int = 1,
        hook: Optional[Callable[[str, date], Sequence[UUID]]] = None,
    ) -> None:
        self._key = key
        self._revision = revision
        self._mapping = mapping or {}
        self._hook = hook
        self.call_log: List[Tuple[str, date]] = []
        self.key_access_count = 0
        self.revision_access_count = 0
        self.resolve_candidates_access_count = 0

    @property
    def resolver_key(self) -> str:
        self.key_access_count += 1
        return self._key

    @property
    def resolver_revision(self) -> int:
        self.revision_access_count += 1
        return self._revision

    def resolve_candidates(self, instrument_reference: str, as_of_date: date) -> Sequence[UUID]:
        self.call_log.append((instrument_reference, as_of_date))
        if self._hook:
            return self._hook(instrument_reference, as_of_date)
        return self._mapping.get(instrument_reference, [])


# ─────────────────────────────────────────────────────────────────────────────
# 1. Basic Execution Matrix (A-F)
# ─────────────────────────────────────────────────────────────────────────────

class TestBasicExecutionMatrix:
    """A-F: Basic execution mechanics."""

    def test_A_empty_draft_batch_returns_valid_empty_batch(self):
        """A: Empty draft batch produces valid empty resolution batch."""
        draft_manifest = _make_empty_draft_batch()
        resolver = MockResolver()
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert isinstance(batch, ImportInstrumentResolutionBatch)
        assert batch.resolution_count == 0
        assert len(resolver.call_log) == 0

    def test_B_cash_deposit_not_required_zero_calls(self):
        """B: CASH_DEPOSIT maps to NOT_REQUIRED with zero resolver calls."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])

        resolver = MockResolver()
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolution_count == 1
        assert batch.resolutions[0].status == ImportInstrumentResolutionStatus.NOT_REQUIRED
        assert len(resolver.call_log) == 0

    def test_C_buy_zero_candidates_unresolved(self):
        """C: BUY with zero candidates maps to UNRESOLVED."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1, instrument="UNKNOWN")
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])

        resolver = MockResolver(mapping={"UNKNOWN": []})
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolution_count == 1
        res = batch.resolutions[0]
        assert res.status == ImportInstrumentResolutionStatus.UNRESOLVED
        assert res.instrument_id is None
        assert len(res.diagnostics) == 1
        assert res.diagnostics[0].code == "instrument_not_found"

    def test_D_buy_one_candidate_resolved(self):
        """D: BUY with one candidate maps to RESOLVED."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1, instrument="AAPL")
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])

        inst_uuid = uuid4()
        resolver = MockResolver(mapping={"AAPL": [inst_uuid]})
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolution_count == 1
        res = batch.resolutions[0]
        assert res.status == ImportInstrumentResolutionStatus.RESOLVED
        assert res.instrument_id == inst_uuid
        assert res.candidate_instrument_ids == ()
        assert res.diagnostics == ()

    def test_E_buy_two_candidates_ambiguous(self):
        """E: BUY with two candidates maps to AMBIGUOUS."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1, instrument="AMBIG")
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])

        u1, u2 = uuid4(), uuid4()
        resolver = MockResolver(mapping={"AMBIG": [u1, u2]})
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolution_count == 1
        res = batch.resolutions[0]
        assert res.status == ImportInstrumentResolutionStatus.AMBIGUOUS
        assert res.instrument_id is None
        assert len(res.candidate_instrument_ids) == 2
        assert res.diagnostics[0].code == "ambiguous_reference"

    def test_F_output_is_real_resolution_batch(self):
        """F: Output is authentic ImportInstrumentResolutionBatch."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])

        resolver = MockResolver(mapping={"AAPL": [uuid4()]})
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert isinstance(batch, ImportInstrumentResolutionBatch)
        assert batch.draft_manifest is draft_manifest


# ─────────────────────────────────────────────────────────────────────────────
# 2. NOT_REQUIRED Matrix (G-K)
# ─────────────────────────────────────────────────────────────────────────────

class TestNotRequiredMatrix:
    """G-K: NOT_REQUIRED execution bypass and dividend branching."""

    def test_G_cash_deposit_no_call(self):
        """G: CASH_DEPOSIT does not call resolver."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver()
        resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert len(resolver.call_log) == 0

    def test_H_cash_withdrawal_no_call(self):
        """H: CASH_WITHDRAWAL does not call resolver."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_withdrawal_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver()
        resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert len(resolver.call_log) == 0

    def test_I_fx_conversion_no_call(self):
        """I: FX_CONVERSION does not call resolver."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_fx_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver()
        resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert len(resolver.call_log) == 0

    def test_J_unreferenced_dividend_no_call(self):
        """J: DIVIDEND without instrument reference does not call resolver."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_dividend_draft(ass_batch, 1, instrument=None)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver()
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolutions[0].status == ImportInstrumentResolutionStatus.NOT_REQUIRED
        assert len(resolver.call_log) == 0

    def test_K_referenced_dividend_does_call_resolver(self):
        """K: DIVIDEND with instrument reference calls resolver."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_dividend_draft(ass_batch, 1, instrument="AAPL")
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver(mapping={"AAPL": [uuid4()]})
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolutions[0].status == ImportInstrumentResolutionStatus.RESOLVED
        assert len(resolver.call_log) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 3. Reference / PIT Matrix (L-Q)
# ─────────────────────────────────────────────────────────────────────────────

class TestReferencePITMatrix:
    """L-Q: Verbatim reference and PIT date forwarding."""

    def test_L_exact_aapl_reference_passed_unchanged(self):
        """L: Exact AAPL reference passed unchanged."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1, instrument="AAPL")
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver()
        resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert resolver.call_log[0][0] == "AAPL"

    def test_M_whitespace_reference_passed_unchanged(self):
        """M: Reference with leading/trailing spaces passed unchanged."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        ref = "  AAPL.IS_VERBATIM  "
        draft = _make_buy_draft(ass_batch, 1, instrument=ref)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver()
        resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert resolver.call_log[0][0] == ref

    def test_N_unicode_reference_passed_unchanged(self):
        """N: Unicode reference characters passed unchanged."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        ref = "GARAN.İŞ"
        draft = _make_buy_draft(ass_batch, 1, instrument=ref)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver()
        resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert resolver.call_log[0][0] == ref

    def test_O_resolver_date_is_exactly_effective_date(self):
        """O: Resolver receives draft.effective_date exactly."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        eff_d = date(2025, 4, 12)
        draft = _make_buy_draft(ass_batch, 1, eff_date=eff_d)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver()
        resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert resolver.call_log[0][1] == eff_d

    def test_P_executed_at_date_is_not_used(self):
        """P: executed_at date differing from effective_date is NOT forwarded."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        eff_d = date(2025, 4, 12)
        exec_at = datetime(2025, 4, 13, 15, 30, tzinfo=timezone.utc)
        draft = _make_buy_draft(ass_batch, 1, eff_date=eff_d, executed_at=exec_at)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver()
        resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert resolver.call_log[0][1] == eff_d
        assert resolver.call_log[0][1] != exec_at.date()

    def test_Q_current_date_is_not_used(self):
        """Q: date.today() is not passed to resolver."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        past_date = date(2020, 1, 1)
        draft = _make_buy_draft(ass_batch, 1, eff_date=past_date)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver()
        resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert resolver.call_log[0][1] == past_date


# ─────────────────────────────────────────────────────────────────────────────
# 4. Return-Shape Matrix (R-Z)
# ─────────────────────────────────────────────────────────────────────────────

class TestReturnShapeMatrix:
    """R-Z: Return collection validation."""

    def test_R_list_accepted(self):
        """R: List return accepted."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        u = uuid4()
        resolver = MockResolver(hook=lambda ref, dt: [u])
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolutions[0].status == ImportInstrumentResolutionStatus.RESOLVED

    def test_S_tuple_accepted(self):
        """S: Tuple return accepted."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        u = uuid4()
        resolver = MockResolver(hook=lambda ref, dt: (u,))
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolutions[0].status == ImportInstrumentResolutionStatus.RESOLVED

    def test_T_generator_rejected(self):
        """T: Generator return rejected."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        u = uuid4()
        resolver = MockResolver(hook=lambda ref, dt: (x for x in [u]))  # generator
        with pytest.raises(PortfolioImportInstrumentResolverError, match="must return a list or tuple"):
            resolve_import_draft_batch_instruments(draft_manifest, resolver)

    def test_U_set_rejected(self):
        """U: Set return rejected."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        u = uuid4()
        resolver = MockResolver(hook=lambda ref, dt: {u})  # set
        with pytest.raises(PortfolioImportInstrumentResolverError, match="must return a list or tuple"):
            resolve_import_draft_batch_instruments(draft_manifest, resolver)

    def test_V_dict_rejected(self):
        """V: Dict return rejected."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        u = uuid4()
        resolver = MockResolver(hook=lambda ref, dt: {u: 1})  # dict
        with pytest.raises(PortfolioImportInstrumentResolverError, match="must return a list or tuple"):
            resolve_import_draft_batch_instruments(draft_manifest, resolver)

    def test_W_str_rejected(self):
        """W: String return rejected."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver(hook=lambda ref, dt: "not_a_list")  # str
        with pytest.raises(PortfolioImportInstrumentResolverError, match="must return a list or tuple"):
            resolve_import_draft_batch_instruments(draft_manifest, resolver)

    def test_X_bytes_rejected(self):
        """X: Bytes return rejected."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver(hook=lambda ref, dt: b"bytes")
        with pytest.raises(PortfolioImportInstrumentResolverError, match="must return a list or tuple"):
            resolve_import_draft_batch_instruments(draft_manifest, resolver)

    def test_Y_none_rejected(self):
        """Y: None return rejected."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver(hook=lambda ref, dt: None)
        with pytest.raises(PortfolioImportInstrumentResolverError, match="must return a list or tuple"):
            resolve_import_draft_batch_instruments(draft_manifest, resolver)

    def test_Z_arbitrary_iterator_rejected(self):
        """Z: Custom iterator return rejected."""
        class CustomIter:
            def __iter__(self):
                return iter([uuid4()])

        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver(hook=lambda ref, dt: CustomIter())
        with pytest.raises(PortfolioImportInstrumentResolverError, match="must return a list or tuple"):
            resolve_import_draft_batch_instruments(draft_manifest, resolver)


# ─────────────────────────────────────────────────────────────────────────────
# 5. UUID Matrix (AA-AE)
# ─────────────────────────────────────────────────────────────────────────────

class TestUUIDMatrix:
    """AA-AE: Candidate item types and uniqueness."""

    def test_AA_actual_uuid_accepted(self):
        """AA: Actual UUID instance accepted."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        u = uuid4()
        resolver = MockResolver(mapping={"AAPL": [u]})
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolutions[0].instrument_id == u

    def test_AB_uuid_shaped_string_rejected(self):
        """AB: UUID-shaped string in candidate list fails closed."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver(mapping={"AAPL": ["00000000-0000-0000-0000-000000000001"]})
        with pytest.raises(PortfolioImportInstrumentResolverError, match="must be a UUID instance"):
            resolve_import_draft_batch_instruments(draft_manifest, resolver)

    def test_AC_int_rejected(self):
        """AC: Integer in candidate list fails closed."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver(mapping={"AAPL": [12345]})
        with pytest.raises(PortfolioImportInstrumentResolverError, match="must be a UUID instance"):
            resolve_import_draft_batch_instruments(draft_manifest, resolver)

    def test_AD_duplicate_uuids_rejected(self):
        """AD: Duplicate UUIDs in candidate list fail closed (no silent deduplication)."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        u = uuid4()
        resolver = MockResolver(mapping={"AAPL": [u, u]})
        with pytest.raises(PortfolioImportInstrumentResolverError, match="duplicate candidate UUID"):
            resolve_import_draft_batch_instruments(draft_manifest, resolver)

    def test_AE_shuffled_unique_uuids_canonicalized(self):
        """AE: Shuffled candidate UUIDs are canonicalized by str(uuid) ascending."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])

        u1 = UUID("00000000-0000-0000-0000-000000000001")
        u2 = UUID("00000000-0000-0000-0000-000000000002")

        res_ordered = MockResolver(mapping={"AAPL": [u1, u2]})
        res_shuffled = MockResolver(mapping={"AAPL": [u2, u1]})

        b1 = resolve_import_draft_batch_instruments(draft_manifest, res_ordered)
        b2 = resolve_import_draft_batch_instruments(draft_manifest, res_shuffled)
        assert b1.resolutions[0].candidate_instrument_ids == (u1, u2)
        assert b2.resolutions[0].candidate_instrument_ids == (u1, u2)
        assert b1.resolution_manifest_sha256 == b2.resolution_manifest_sha256


# ─────────────────────────────────────────────────────────────────────────────
# 6. Cardinality Matrix (AF-AM)
# ─────────────────────────────────────────────────────────────────────────────

class TestCardinalityMatrix:
    """AF-AM: Cardinality mapping to UNRESOLVED/RESOLVED/AMBIGUOUS."""

    def test_AF_zero_candidates_unresolved(self):
        """AF: 0 candidates -> UNRESOLVED."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver(mapping={"AAPL": []})
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolutions[0].status == ImportInstrumentResolutionStatus.UNRESOLVED

    def test_AG_diagnostic_code_exactly_instrument_not_found(self):
        """AG: 0 candidates diagnostic code is exactly 'instrument_not_found'."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver(mapping={"AAPL": []})
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        diag = batch.resolutions[0].diagnostics[0]
        assert diag.code == "instrument_not_found"
        assert "No canonical instrument candidate was resolved" in diag.message

    def test_AH_one_candidate_resolved(self):
        """AH: 1 candidate -> RESOLVED."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        u = uuid4()
        resolver = MockResolver(mapping={"AAPL": [u]})
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolutions[0].status == ImportInstrumentResolutionStatus.RESOLVED

    def test_AI_selected_instrument_uuid_exact(self):
        """AI: Selected instrument UUID matches the single returned candidate."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        u = uuid4()
        resolver = MockResolver(mapping={"AAPL": [u]})
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolutions[0].instrument_id == u

    def test_AJ_two_candidates_ambiguous(self):
        """AJ: 2 candidates -> AMBIGUOUS."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        u1, u2 = uuid4(), uuid4()
        resolver = MockResolver(mapping={"AAPL": [u1, u2]})
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolutions[0].status == ImportInstrumentResolutionStatus.AMBIGUOUS
        assert len(batch.resolutions[0].candidate_instrument_ids) == 2

    def test_AK_three_candidates_ambiguous(self):
        """AK: 3 candidates -> AMBIGUOUS."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        u1, u2, u3 = uuid4(), uuid4(), uuid4()
        resolver = MockResolver(mapping={"AAPL": [u1, u2, u3]})
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolutions[0].status == ImportInstrumentResolutionStatus.AMBIGUOUS
        assert len(batch.resolutions[0].candidate_instrument_ids) == 3

    def test_AL_ambiguous_candidates_canonical_sorted(self):
        """AL: AMBIGUOUS candidates are sorted ascending by str(uuid)."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        u_high = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
        u_low = UUID("00000000-0000-0000-0000-000000000001")
        resolver = MockResolver(mapping={"AAPL": [u_high, u_low]})
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolutions[0].candidate_instrument_ids == (u_low, u_high)

    def test_AM_no_candidate_silently_selected(self):
        """AM: Ambiguity does NOT select a primary or first candidate into instrument_id."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        u1, u2 = uuid4(), uuid4()
        resolver = MockResolver(mapping={"AAPL": [u1, u2]})
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolutions[0].instrument_id is None


# ─────────────────────────────────────────────────────────────────────────────
# 7. Snapshot Matrix (AN-AR)
# ─────────────────────────────────────────────────────────────────────────────

class TestSnapshotMatrix:
    """AN-AR: Property access and callable snapshot counts."""

    def test_AN_resolver_key_accessed_once(self):
        """AN: resolver_key accessed exactly once per batch execution."""
        ass_batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(ass_batch, 1, instrument="AAPL")
        d2 = _make_buy_draft(ass_batch, 2, instrument="MSFT")
        d3 = _make_cash_deposit_draft(ass_batch, 3)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2, d3])

        resolver = MockResolver()
        resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert resolver.key_access_count == 1

    def test_AO_resolver_revision_accessed_once(self):
        """AO: resolver_revision accessed exactly once per batch execution."""
        ass_batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(ass_batch, 1)
        d2 = _make_buy_draft(ass_batch, 2)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        resolver = MockResolver()
        resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert resolver.revision_access_count == 1

    def test_AP_resolve_candidates_attribute_resolved_once(self):
        """AP: resolve_candidates resolved once from resolver instance."""
        ass_batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(ass_batch, 1)
        d2 = _make_buy_draft(ass_batch, 2)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        resolver = MockResolver()
        resolve_import_draft_batch_instruments(draft_manifest, resolver)
        # 2 invocations of the SAME captured callable
        assert len(resolver.call_log) == 2

    def test_AQ_callable_invoked_once_per_instrument_draft(self):
        """AQ: Callable invoked once per instrument-bearing draft."""
        ass_batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(ass_batch, 1)
        d2 = _make_buy_draft(ass_batch, 2)
        d3 = _make_sell_draft(ass_batch, 3)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2, d3])

        resolver = MockResolver()
        resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert len(resolver.call_log) == 3

    def test_AR_no_callable_invocation_for_not_required_draft(self):
        """AR: Zero callable invocations for NOT_REQUIRED drafts."""
        ass_batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_cash_deposit_draft(ass_batch, 1)
        d2 = _make_fx_draft(ass_batch, 2)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        resolver = MockResolver()
        resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert len(resolver.call_log) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 8. Hostile Callable Matrix (AS-AV)
# ─────────────────────────────────────────────────────────────────────────────

class TestHostileCallableMatrix:
    """AS-AV: Hostile descriptor and exception behavior."""

    def test_AS_first_descriptor_used_second_ignored(self):
        """AS: First descriptor access returns callable A, hypothetical second B -> only A used."""
        call_a_count = 0
        call_b_count = 0

        def callable_a(ref: str, dt: date):
            nonlocal call_a_count
            call_a_count += 1
            return [uuid4()]

        def callable_b(ref: str, dt: date):
            nonlocal call_b_count
            call_b_count += 1
            return [uuid4()]

        class HostileCallableResolver:
            def __init__(self):
                self._accesses = 0

            @property
            def resolver_key(self) -> str:
                return "hostile"

            @property
            def resolver_revision(self) -> int:
                return 1

            @property
            def resolve_candidates(self):
                self._accesses += 1
                if self._accesses == 1:
                    return callable_a
                return callable_b

        ass_batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(ass_batch, 1)
        d2 = _make_buy_draft(ass_batch, 2)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        resolver = HostileCallableResolver()
        resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert call_a_count == 2
        assert call_b_count == 0
        assert resolver._accesses == 1

    def test_AT_first_non_callable_fails_immediately(self):
        """AT: Non-callable on first access fails immediately with zero retries."""
        class NonCallableResolver:
            resolver_key = "test"
            resolver_revision = 1
            resolve_candidates = "not_a_callable"

        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])

        with pytest.raises(PortfolioImportInstrumentResolverError, match="must be callable"):
            resolve_import_draft_batch_instruments(draft_manifest, NonCallableResolver())

    def test_AU_descriptor_raising_on_second_access_succeeds(self):
        """AU: Descriptor raising on hypothetical second access succeeds because it is accessed once."""
        class ExplosiveSecondAccessResolver:
            def __init__(self):
                self._accesses = 0

            resolver_key = "test"
            resolver_revision = 1

            @property
            def resolve_candidates(self):
                self._accesses += 1
                if self._accesses > 1:
                    raise RuntimeError("Should not be accessed a second time!")
                return lambda ref, dt: [uuid4()]

        ass_batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(ass_batch, 1)
        d2 = _make_buy_draft(ass_batch, 2)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        resolver = ExplosiveSecondAccessResolver()
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolution_count == 2
        assert resolver._accesses == 1

    def test_AV_callable_runtime_error_propagates_unchanged(self):
        """AV: Resolver callable execution RuntimeError propagates unchanged (not wrapped)."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])

        def exploding_resolver(ref: str, dt: date):
            raise RuntimeError("Database connection down")

        resolver = MockResolver(hook=exploding_resolver)
        with pytest.raises(RuntimeError, match="Database connection down"):
            resolve_import_draft_batch_instruments(draft_manifest, resolver)


# ─────────────────────────────────────────────────────────────────────────────
# 9. Hostile Metadata Matrix (AW-AY)
# ─────────────────────────────────────────────────────────────────────────────

class TestHostileMetadataMatrix:
    """AW-AY: Hostile metadata properties."""

    def test_AW_first_resolver_key_snapshotted(self):
        """AW: resolver_key snapshotted once; subsequent property changes ignored."""
        class DynamicKeyResolver:
            def __init__(self):
                self._accesses = 0

            @property
            def resolver_key(self) -> str:
                self._accesses += 1
                if self._accesses == 1:
                    return "key_one"
                return "key_two"

            resolver_revision = 1

            def resolve_candidates(self, ref: str, dt: date):
                return [uuid4()]

        ass_batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(ass_batch, 1)
        d2 = _make_buy_draft(ass_batch, 2)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        resolver = DynamicKeyResolver()
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolutions[0].resolver_key == "key_one"
        assert batch.resolutions[1].resolver_key == "key_one"
        assert resolver._accesses == 1

    def test_AX_first_resolver_revision_snapshotted(self):
        """AX: resolver_revision snapshotted once; subsequent property changes ignored."""
        class DynamicRevResolver:
            def __init__(self):
                self._accesses = 0

            resolver_key = "test"

            @property
            def resolver_revision(self) -> int:
                self._accesses += 1
                if self._accesses == 1:
                    return 1
                return 999

            def resolve_candidates(self, ref: str, dt: date):
                return [uuid4()]

        ass_batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(ass_batch, 1)
        d2 = _make_buy_draft(ass_batch, 2)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        resolver = DynamicRevResolver()
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolutions[0].resolver_revision == 1
        assert batch.resolutions[1].resolver_revision == 1
        assert resolver._accesses == 1

    def test_AY_getter_exception_wrapped_with_cause(self):
        """AY: Property getter exception wrapped in PortfolioImportInstrumentResolverError with cause."""
        class ExplodingPropertyResolver:
            @property
            def resolver_key(self) -> str:
                raise ValueError("Getter failed")

            resolver_revision = 1

            def resolve_candidates(self, ref: str, dt: date):
                return []

        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])

        with pytest.raises(PortfolioImportInstrumentResolverError) as exc_info:
            resolve_import_draft_batch_instruments(draft_manifest, ExplodingPropertyResolver())
        assert exc_info.value.__cause__ is not None


# ─────────────────────────────────────────────────────────────────────────────
# 10. Malformed Metadata Matrix (AZ-BE)
# ─────────────────────────────────────────────────────────────────────────────

class TestMalformedMetadataMatrix:
    """AZ-BE: Resolver contract failures before execution."""

    def test_AZ_missing_resolver_key_rejected(self):
        """AZ: Missing resolver_key attribute fails closed."""
        class MissingKey:
            resolver_revision = 1
            def resolve_candidates(self, ref, dt): return []

        draft_manifest = _make_empty_draft_batch()
        with pytest.raises(PortfolioImportInstrumentResolverError, match="missing required 'resolver_key'"):
            resolve_import_draft_batch_instruments(draft_manifest, MissingKey())

    def test_BA_invalid_resolver_key_rejected(self):
        """BA: Invalid resolver_key grammar fails closed."""
        class InvalidKey:
            resolver_key = "Invalid Key Upper"
            resolver_revision = 1
            def resolve_candidates(self, ref, dt): return []

        draft_manifest = _make_empty_draft_batch()
        with pytest.raises(PortfolioImportInstrumentResolverError, match="resolver_key must match"):
            resolve_import_draft_batch_instruments(draft_manifest, InvalidKey())

    def test_BB_missing_resolver_revision_rejected(self):
        """BB: Missing resolver_revision attribute fails closed."""
        class MissingRev:
            resolver_key = "valid_key"
            def resolve_candidates(self, ref, dt): return []

        draft_manifest = _make_empty_draft_batch()
        with pytest.raises(PortfolioImportInstrumentResolverError, match="missing required 'resolver_revision'"):
            resolve_import_draft_batch_instruments(draft_manifest, MissingRev())

    def test_BC_bool_resolver_revision_rejected(self):
        """BC: Bool as resolver_revision fails closed."""
        class BoolRev:
            resolver_key = "valid_key"
            resolver_revision = True
            def resolve_candidates(self, ref, dt): return []

        draft_manifest = _make_empty_draft_batch()
        with pytest.raises(PortfolioImportInstrumentResolverError, match="resolver_revision must be an int"):
            resolve_import_draft_batch_instruments(draft_manifest, BoolRev())

    def test_BD_zero_resolver_revision_rejected(self):
        """BD: Revision 0 fails closed."""
        class ZeroRev:
            resolver_key = "valid_key"
            resolver_revision = 0
            def resolve_candidates(self, ref, dt): return []

        draft_manifest = _make_empty_draft_batch()
        with pytest.raises(PortfolioImportInstrumentResolverError, match="resolver_revision must be >= 1"):
            resolve_import_draft_batch_instruments(draft_manifest, ZeroRev())

    def test_BE_non_callable_resolve_candidates_rejected(self):
        """BE: Non-callable resolve_candidates fails closed."""
        class NonCallable:
            resolver_key = "valid_key"
            resolver_revision = 1
            resolve_candidates = 12345

        draft_manifest = _make_empty_draft_batch()
        with pytest.raises(PortfolioImportInstrumentResolverError, match="must be callable"):
            resolve_import_draft_batch_instruments(draft_manifest, NonCallable())


# ─────────────────────────────────────────────────────────────────────────────
# 11. Repeated Reference Matrix (BF-BH)
# ─────────────────────────────────────────────────────────────────────────────

class TestRepeatedReferenceMatrix:
    """BF-BH: Zero-cache discipline on repeated references."""

    def test_BF_same_reference_same_date_two_calls(self):
        """BF: Two drafts with same reference and same date result in two separate calls."""
        ass_batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(ass_batch, 1, instrument="AAPL", eff_date=date(2026, 8, 28))
        d2 = _make_buy_draft(ass_batch, 2, instrument="AAPL", eff_date=date(2026, 8, 28))
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        resolver = MockResolver(mapping={"AAPL": [uuid4()]})
        resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert len(resolver.call_log) == 2

    def test_BG_same_reference_different_dates_two_calls(self):
        """BG: Same reference across different dates results in two distinct date calls."""
        ass_batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(ass_batch, 1, instrument="AAPL", eff_date=date(2026, 8, 28))
        d2 = _make_buy_draft(ass_batch, 2, instrument="AAPL", eff_date=date(2026, 8, 29))
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        resolver = MockResolver(mapping={"AAPL": [uuid4()]})
        resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert len(resolver.call_log) == 2
        assert resolver.call_log[0][1] == date(2026, 8, 28)
        assert resolver.call_log[1][1] == date(2026, 8, 29)

    def test_BH_no_hidden_cache(self):
        """BH: Ensure resolver results are not cached across drafts."""
        ass_batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(ass_batch, 1, instrument="AAPL")
        d2 = _make_buy_draft(ass_batch, 2, instrument="AAPL")
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        # Return different UUID on second call
        u1, u2 = uuid4(), uuid4()
        call_count = 0

        def alternating(ref: str, dt: date):
            nonlocal call_count
            call_count += 1
            return [u1] if call_count == 1 else [u2]

        resolver = MockResolver(hook=alternating)
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolutions[0].instrument_id == u1
        assert batch.resolutions[1].instrument_id == u2


# ─────────────────────────────────────────────────────────────────────────────
# 12. Mixed Batch Matrix (Section 63)
# ─────────────────────────────────────────────────────────────────────────────

class TestMixedBatchMatrix:
    """Section 63: Five-draft mixed scenario."""

    def test_mixed_five_draft_scenario(self):
        """
        1 BUY -> 1 candidate (RESOLVED)
        2 CASH_DEPOSIT -> NOT_REQUIRED
        3 referenced DIVIDEND -> 0 candidates (UNRESOLVED)
        4 FX_CONVERSION -> NOT_REQUIRED
        5 SELL -> 3 candidates (AMBIGUOUS)
        """
        ass_batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(ass_batch, 1, instrument="AAPL")
        d2 = _make_cash_deposit_draft(ass_batch, 2)
        d3 = _make_dividend_draft(ass_batch, 3, instrument="UNKNOWN")
        d4 = _make_fx_draft(ass_batch, 4)
        d5 = _make_sell_draft(ass_batch, 5, instrument="TRIPLE")

        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2, d3, d4, d5])

        u1 = uuid4()
        u_a, u_b, u_c = uuid4(), uuid4(), uuid4()

        resolver = MockResolver(mapping={
            "AAPL": [u1],
            "UNKNOWN": [],
            "TRIPLE": [u_a, u_b, u_c],
        })

        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)

        assert batch.resolution_count == 5
        assert len(resolver.call_log) == 3

        assert batch.resolutions[0].status == ImportInstrumentResolutionStatus.RESOLVED
        assert batch.resolutions[1].status == ImportInstrumentResolutionStatus.NOT_REQUIRED
        assert batch.resolutions[2].status == ImportInstrumentResolutionStatus.UNRESOLVED
        assert batch.resolutions[3].status == ImportInstrumentResolutionStatus.NOT_REQUIRED
        assert batch.resolutions[4].status == ImportInstrumentResolutionStatus.AMBIGUOUS

        assert batch.resolved_count == 1
        assert batch.not_required_count == 2
        assert batch.unresolved_count == 1
        assert batch.ambiguous_count == 1
        assert batch.is_fully_resolved is False


# ─────────────────────────────────────────────────────────────────────────────
# 13. Partial Failures (Sections 64 & 65)
# ─────────────────────────────────────────────────────────────────────────────

class TestPartialFailures:
    """Sections 64-65: Failure during multi-draft execution."""

    def test_failure_at_second_draft_propagates_no_partial_batch(self):
        """Failure on draft 2 propagates exception without returning partial batch."""
        ass_batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(ass_batch, 1, instrument="AAPL")
        d2 = _make_buy_draft(ass_batch, 2, instrument="FAIL")
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        def fail_on_second(ref: str, dt: date):
            if ref == "FAIL":
                raise RuntimeError("Second draft failure")
            return [uuid4()]

        resolver = MockResolver(hook=fail_on_second)
        with pytest.raises(RuntimeError, match="Second draft failure"):
            resolve_import_draft_batch_instruments(draft_manifest, resolver)

    def test_malformed_second_return_fails_closed(self):
        """Malformed return on draft 2 raises PortfolioImportInstrumentResolverError."""
        ass_batch = _make_test_assessment_batch([
            ImportAssessmentStatus.READY,
            ImportAssessmentStatus.READY,
        ])
        d1 = _make_buy_draft(ass_batch, 1, instrument="AAPL")
        d2 = _make_buy_draft(ass_batch, 2, instrument="GEN")
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        def generator_on_second(ref: str, dt: date):
            if ref == "GEN":
                return (x for x in [uuid4()])
            return [uuid4()]

        resolver = MockResolver(hook=generator_on_second)
        with pytest.raises(PortfolioImportInstrumentResolverError, match="must return a list or tuple"):
            resolve_import_draft_batch_instruments(draft_manifest, resolver)


# ─────────────────────────────────────────────────────────────────────────────
# 14. Determinism Matrix (BI-BM)
# ─────────────────────────────────────────────────────────────────────────────

class TestDeterminismMatrix:
    """BI-BM: Hash determinism and metadata sensitivity."""

    def test_BI_identical_executions_produce_identical_hashes(self):
        """BI: Identical executions produce identical resolution and batch SHAs."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])

        inst_id = uuid4()
        r1 = MockResolver(mapping={"AAPL": [inst_id]})
        r2 = MockResolver(mapping={"AAPL": [inst_id]})

        b1 = resolve_import_draft_batch_instruments(draft_manifest, r1)
        b2 = resolve_import_draft_batch_instruments(draft_manifest, r2)
        assert b1.resolutions[0].resolution_sha256 == b2.resolutions[0].resolution_sha256
        assert b1.resolution_manifest_sha256 == b2.resolution_manifest_sha256

    def test_BK_candidate_input_order_does_not_affect_sha(self):
        """BK: Candidate input order does not change final resolution SHA."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])

        u1, u2 = uuid4(), uuid4()
        r1 = MockResolver(mapping={"AAPL": [u1, u2]})
        r2 = MockResolver(mapping={"AAPL": [u2, u1]})

        b1 = resolve_import_draft_batch_instruments(draft_manifest, r1)
        b2 = resolve_import_draft_batch_instruments(draft_manifest, r2)
        assert b1.resolutions[0].resolution_sha256 == b2.resolutions[0].resolution_sha256

    def test_BL_resolver_revision_change_changes_sha(self):
        """BL: Changing resolver_revision changes resolution outcome SHA."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])

        inst_id = uuid4()
        r1 = MockResolver(mapping={"AAPL": [inst_id]}, revision=1)
        r2 = MockResolver(mapping={"AAPL": [inst_id]}, revision=2)

        b1 = resolve_import_draft_batch_instruments(draft_manifest, r1)
        b2 = resolve_import_draft_batch_instruments(draft_manifest, r2)
        assert b1.resolutions[0].resolution_sha256 != b2.resolutions[0].resolution_sha256

    def test_BM_resolver_key_change_changes_sha(self):
        """BM: Changing resolver_key changes resolution outcome SHA."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])

        inst_id = uuid4()
        r1 = MockResolver(mapping={"AAPL": [inst_id]}, key="resolver_a")
        r2 = MockResolver(mapping={"AAPL": [inst_id]}, key="resolver_b")

        b1 = resolve_import_draft_batch_instruments(draft_manifest, r1)
        b2 = resolve_import_draft_batch_instruments(draft_manifest, r2)
        assert b1.resolutions[0].resolution_sha256 != b2.resolutions[0].resolution_sha256


# ─────────────────────────────────────────────────────────────────────────────
# 15. Empty Batch Dependency Validation (BN-BQ)
# ─────────────────────────────────────────────────────────────────────────────

class TestEmptyBatchDependencyValidation:
    """BN-BQ: Empty draft batch prevalidation."""

    def test_BN_empty_batch_validates_resolver_key(self):
        """BN: Empty draft batch still validates resolver_key."""
        class BadKey:
            resolver_key = "Bad Key"
            resolver_revision = 1
            def resolve_candidates(self, ref, dt): return []

        draft_manifest = _make_empty_draft_batch()
        with pytest.raises(PortfolioImportInstrumentResolverError, match="resolver_key must match"):
            resolve_import_draft_batch_instruments(draft_manifest, BadKey())

    def test_BO_empty_batch_validates_resolver_revision(self):
        """BO: Empty draft batch still validates resolver_revision."""
        class BadRev:
            resolver_key = "valid_key"
            resolver_revision = 0
            def resolve_candidates(self, ref, dt): return []

        draft_manifest = _make_empty_draft_batch()
        with pytest.raises(PortfolioImportInstrumentResolverError, match="resolver_revision must be >= 1"):
            resolve_import_draft_batch_instruments(draft_manifest, BadRev())

    def test_BP_empty_batch_validates_resolve_candidates(self):
        """BP: Empty draft batch still validates resolve_candidates is callable."""
        class BadCallable:
            resolver_key = "valid_key"
            resolver_revision = 1
            resolve_candidates = "not_callable"

        draft_manifest = _make_empty_draft_batch()
        with pytest.raises(PortfolioImportInstrumentResolverError, match="must be callable"):
            resolve_import_draft_batch_instruments(draft_manifest, BadCallable())

    def test_BQ_valid_empty_batch_zero_calls(self):
        """BQ: Valid resolver on empty batch has zero calls and returns valid empty batch."""
        draft_manifest = _make_empty_draft_batch()
        resolver = MockResolver()
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolution_count == 0
        assert len(resolver.call_log) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 16. Output Retention Matrix (BR-BU)
# ─────────────────────────────────────────────────────────────────────────────

class TestOutputRetentionMatrix:
    """BR-BU: Retention isolation."""

    def test_BR_no_resolver_retained(self):
        """BR: Returned batch does not retain resolver object."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver(mapping={"AAPL": [uuid4()]})
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert not hasattr(batch, "resolver")

    def test_BS_no_callable_retained(self):
        """BS: Returned batch does not retain resolver callable."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver(mapping={"AAPL": [uuid4()]})
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert not hasattr(batch, "resolve_candidates")

    def test_BT_outcomes_retain_metadata_only_for_attempted(self):
        """BT: Attempted resolutions retain resolver key/revision."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_buy_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver(mapping={"AAPL": [uuid4()]}, key="my_key", revision=2)
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolutions[0].resolver_key == "my_key"
        assert batch.resolutions[0].resolver_revision == 2

    def test_BU_not_required_metadata_is_none(self):
        """BU: NOT_REQUIRED outcomes retain resolver_key=None, resolver_revision=None."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        draft = _make_cash_deposit_draft(ass_batch, 1)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver(key="my_key", revision=2)
        batch = resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert batch.resolutions[0].resolver_key is None
        assert batch.resolutions[0].resolver_revision is None


# ─────────────────────────────────────────────────────────────────────────────
# 17. Surface Red-Team (Section 69)
# ─────────────────────────────────────────────────────────────────────────────

class TestSurfaceRedTeam:
    """Section 69: Pure execution isolation verification."""

    def test_no_forbidden_imports_or_attributes(self):
        """Module does not import forbidden services or models."""
        import backend.engine.private.portfolio.import_instrument_resolver as module
        assert not hasattr(module, "InstrumentResolverService")
        assert not hasattr(module, "PortfolioTransaction")
        assert not hasattr(module, "CashBucket")


# ─────────────────────────────────────────────────────────────────────────────
# 18. Final Red-Team (Section 74)
# ─────────────────────────────────────────────────────────────────────────────

class TestFinalRedTeam:
    """Section 74: Red-team attack vectors."""

    def test_malformed_draft_manifest_rejected(self):
        """Non-manifest object passed as draft_manifest raises PortfolioImportInstrumentResolverError."""
        with pytest.raises(PortfolioImportInstrumentResolverError, match="must be an ImportDraftBatchManifest"):
            resolve_import_draft_batch_instruments("not_a_manifest", MockResolver())  # type: ignore

    def test_whitespace_reference_not_stripped_by_harness(self):
        """Harness does not strip reference before calling resolver."""
        ass_batch = _make_test_assessment_batch([ImportAssessmentStatus.READY])
        raw_ref = "   spaces   "
        draft = _make_buy_draft(ass_batch, 1, instrument=raw_ref)
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [draft])
        resolver = MockResolver()
        resolve_import_draft_batch_instruments(draft_manifest, resolver)
        assert resolver.call_log[0][0] == raw_ref
