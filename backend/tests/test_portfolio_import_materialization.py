"""
Unit and integration test suite for Phase 13N:
Immutable Ledger-Materialization Plan Contract & Full Resolution-Batch Eligibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import inspect
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
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
from backend.engine.private.portfolio.import_batch import (
    ImportBatchManifest,
    build_import_batch_manifest,
)
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
    ImportInstrumentResolutionDiagnostic,
    ImportInstrumentResolutionStatus,
    build_import_instrument_resolution,
    build_import_instrument_resolution_batch,
)
from backend.engine.private.portfolio.import_instrument_resolver import (
    PortfolioImportInstrumentResolver,
    resolve_import_draft_batch_instruments,
)
from backend.engine.private.portfolio.import_materialization import (
    ImportLedgerMaterializationBatch,
    ImportLedgerTransactionPlan,
    PortfolioImportMaterializationError,
    _canonical_datetime_str,
    _canonical_decimal_str,
    _compute_materialization_manifest_sha256,
    _compute_plan_sha256,
    build_import_ledger_materialization_batch,
    build_import_ledger_transaction_plan,
)
from backend.engine.private.portfolio.import_parsing import (
    ImportParsedField,
    ParsedImportRecord,
    build_parsed_import_record,
)
from backend.engine.private.portfolio.import_parsed_batch import (
    ParsedImportBatchManifest,
    build_parsed_import_batch_manifest,
)
from backend.engine.private.portfolio.import_pipeline import (
    build_import_staging_result,
)
from backend.engine.private.portfolio.import_provenance import (
    ImportFileProvenance,
    ImportRecordProvenance,
    build_import_file_provenance,
    build_import_record_provenance,
)
from backend.engine.private.portfolio.parsers.sentinax_csv import (
    SentinaxCanonicalCsvParserV1,
)
from backend.engine.private.portfolio.parsers.sentinax_csv_semantics import (
    SentinaxCanonicalCsvSemanticInterpreterV1,
)
from backend.engine.private.portfolio.sentinax_csv_import import (
    run_sentinax_canonical_csv_import_v1,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test Fixtures & Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_test_assessment_batch(
    count: int = 1,
    portfolio_id: Optional[UUID] = None,
    account_id: Optional[UUID] = None,
    source_key: str = "sentinax_csv",
    parser_revision: int = 1,
) -> ImportAssessmentBatch:
    """Builds a real, verified ImportAssessmentBatch with READY assessments."""
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

    raw_rows = [f"row_{i}".encode("utf-8") for i in range(count)]

    rec_provs = [
        build_import_record_provenance(
            file_provenance=file_prov,
            record_ordinal=i + 1,
            raw_record=raw_rows[i],
        )
        for i in range(count)
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
        for i in range(count)
    ]

    parsed_manifest = build_parsed_import_batch_manifest(
        raw_manifest=raw_manifest,
        parser_revision=parser_revision,
        parsed_records=parsed_records,
    )

    assessments = [
        build_import_record_assessment(parsed_records[i], ImportAssessmentStatus.READY)
        for i in range(count)
    ]

    return build_import_assessment_batch(
        parsed_manifest=parsed_manifest,
        assessments=assessments,
    )


def _make_assessment_and_draft(
    portfolio_id: UUID,
    account_id: UUID,
    ordinal: int,
    imported_at: datetime,
    transaction_type: TransactionType,
    effective_date: date,
    executed_at: Optional[datetime] = None,
    instrument_reference: Optional[str] = None,
    quantity: Optional[Decimal] = None,
    unit_price: Optional[Decimal] = None,
    trade_currency: Optional[Currency] = None,
    cash_amount: Optional[Decimal] = None,
    cash_currency: Optional[Currency] = None,
    from_currency: Optional[Currency] = None,
    from_amount: Optional[Decimal] = None,
    to_currency: Optional[Currency] = None,
    to_amount: Optional[Decimal] = None,
) -> Tuple[ImportAssessmentBatch, ImportTransactionDraft]:
    assessment_batch = _make_test_assessment_batch(
        count=1,
        portfolio_id=portfolio_id,
        account_id=account_id,
    )
    draft = build_import_transaction_draft(
        assessment_batch=assessment_batch,
        record_ordinal=ordinal,
        transaction_type=transaction_type,
        effective_date=effective_date,
        executed_at=executed_at,
        instrument_reference=instrument_reference,
        quantity=quantity,
        unit_price=unit_price,
        trade_currency=trade_currency,
        cash_amount=cash_amount,
        cash_currency=cash_currency,
        from_currency=from_currency,
        from_amount=from_amount,
        to_currency=to_currency,
        to_amount=to_amount,
    )
    return assessment_batch, draft


class MockInstrumentResolver:
    """Configurable mock instrument resolver."""

    def __init__(
        self,
        mapping: Optional[Dict[Tuple[str, date], Sequence[UUID]]] = None,
    ) -> None:
        self.mapping = mapping or {}
        self.invocations: List[Tuple[str, date]] = []
        self.resolver_key: str = "mock_resolver"
        self.resolver_revision: int = 1

    def resolve_candidates(self, instrument_reference: str, as_of_date: date) -> Sequence[UUID]:
        self.invocations.append((instrument_reference, as_of_date))
        return self.mapping.get((instrument_reference, as_of_date), [])


def _make_csv(rows: List[str]) -> bytes:
    header = (
        "transaction_type,effective_date,executed_at,instrument_reference,"
        "quantity,unit_price,trade_currency,cash_amount,cash_currency,"
        "from_currency,from_amount,to_currency,to_amount\n"
    )
    return (header + "\n".join(rows) + "\n").encode("utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Basic Record Plan Matrix (Sections 52-53)
# ─────────────────────────────────────────────────────────────────────────────

class TestBasicRecordPlanMatrix:
    """Section 52 & 53: Record builder matrix across resolution statuses."""

    def test_resolved_buy_produces_valid_plan(self):
        """Matrix A: RESOLVED BUY -> valid plan."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        _, draft = _make_assessment_and_draft(
            portfolio_id=port_id,
            account_id=acc_id,
            ordinal=1,
            imported_at=imported_at,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )

        resolution = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=eff_date,
            resolver_key="mock",
            resolver_revision=1,
            instrument_id=inst_id,
        )

        plan = build_import_ledger_transaction_plan(resolution)

        assert plan.resolution == resolution
        assert plan.portfolio_id == port_id
        assert plan.account_id == acc_id
        assert plan.transaction_type == TransactionType.BUY
        assert plan.effective_date == eff_date
        assert plan.instrument_id == inst_id
        assert plan.quantity == Decimal("10")
        assert plan.unit_price == Decimal("150.00")
        assert plan.trade_currency == Currency.USD
        assert plan.record_ordinal == 1

    def test_resolved_sell_produces_valid_plan(self):
        """Matrix B: RESOLVED SELL -> valid plan."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        _, draft = _make_assessment_and_draft(
            portfolio_id=port_id,
            account_id=acc_id,
            ordinal=1,
            imported_at=imported_at,
            transaction_type=TransactionType.SELL,
            effective_date=eff_date,
            instrument_reference="MSFT",
            quantity=Decimal("5"),
            unit_price=Decimal("300.00"),
            trade_currency=Currency.USD,
        )

        resolution = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=eff_date,
            resolver_key="mock",
            resolver_revision=1,
            instrument_id=inst_id,
        )

        plan = build_import_ledger_transaction_plan(resolution)

        assert plan.instrument_id == inst_id
        assert plan.quantity == Decimal("5")
        assert plan.unit_price == Decimal("300.00")

    def test_not_required_cash_deposit_produces_valid_plan(self):
        """Matrix C: NOT_REQUIRED CASH_DEPOSIT -> valid plan with instrument_id=None."""
        port_id = uuid4()
        acc_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        _, draft = _make_assessment_and_draft(
            portfolio_id=port_id,
            account_id=acc_id,
            ordinal=1,
            imported_at=imported_at,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=eff_date,
            cash_amount=Decimal("1000.00"),
            cash_currency=Currency.TRY,
        )

        resolution = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
            resolution_as_of_date=eff_date,
        )

        plan = build_import_ledger_transaction_plan(resolution)

        assert plan.instrument_id is None
        assert plan.cash_amount == Decimal("1000.00")
        assert plan.cash_currency == Currency.TRY

    def test_not_required_fx_conversion_produces_valid_plan(self):
        """Matrix D: NOT_REQUIRED FX_CONVERSION -> valid plan."""
        port_id = uuid4()
        acc_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        _, draft = _make_assessment_and_draft(
            portfolio_id=port_id,
            account_id=acc_id,
            ordinal=1,
            imported_at=imported_at,
            transaction_type=TransactionType.FX_CONVERSION,
            effective_date=eff_date,
            from_currency=Currency.USD,
            from_amount=Decimal("100.00"),
            to_currency=Currency.TRY,
            to_amount=Decimal("3400.00"),
        )

        resolution = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
            resolution_as_of_date=eff_date,
        )

        plan = build_import_ledger_transaction_plan(resolution)

        assert plan.instrument_id is None
        assert plan.from_currency == Currency.USD
        assert plan.from_amount == Decimal("100.00")
        assert plan.to_currency == Currency.TRY
        assert plan.to_amount == Decimal("3400.00")

    def test_referenced_dividend_resolved_produces_valid_plan(self):
        """Matrix E: Referenced DIVIDEND RESOLVED -> instrument UUID copied."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        _, draft = _make_assessment_and_draft(
            portfolio_id=port_id,
            account_id=acc_id,
            ordinal=1,
            imported_at=imported_at,
            transaction_type=TransactionType.DIVIDEND,
            effective_date=eff_date,
            instrument_reference="AAPL",
            cash_amount=Decimal("50.00"),
            cash_currency=Currency.USD,
        )

        resolution = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=eff_date,
            resolver_key="mock",
            resolver_revision=1,
            instrument_id=inst_id,
        )

        plan = build_import_ledger_transaction_plan(resolution)

        assert plan.instrument_id == inst_id
        assert plan.cash_amount == Decimal("50.00")
        assert plan.cash_currency == Currency.USD

    def test_unreferenced_dividend_not_required_produces_valid_plan(self):
        """Matrix F: Unreferenced DIVIDEND NOT_REQUIRED -> instrument_id=None."""
        port_id = uuid4()
        acc_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        _, draft = _make_assessment_and_draft(
            portfolio_id=port_id,
            account_id=acc_id,
            ordinal=1,
            imported_at=imported_at,
            transaction_type=TransactionType.DIVIDEND,
            effective_date=eff_date,
            cash_amount=Decimal("50.00"),
            cash_currency=Currency.USD,
        )

        resolution = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
            resolution_as_of_date=eff_date,
        )

        plan = build_import_ledger_transaction_plan(resolution)

        assert plan.instrument_id is None
        assert plan.cash_amount == Decimal("50.00")

    def test_unresolved_record_rejected(self):
        """Matrix G: UNRESOLVED record builder rejected."""
        port_id = uuid4()
        acc_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        _, draft = _make_assessment_and_draft(
            portfolio_id=port_id,
            account_id=acc_id,
            ordinal=1,
            imported_at=imported_at,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            instrument_reference="UNKNOWN",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )

        resolution = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.UNRESOLVED,
            resolution_as_of_date=eff_date,
            resolver_key="mock",
            resolver_revision=1,
            diagnostics=[
                ImportInstrumentResolutionDiagnostic(
                    code="instrument_not_found",
                    message="No match",
                )
            ],
        )

        with pytest.raises(PortfolioImportMaterializationError, match="not eligible"):
            build_import_ledger_transaction_plan(resolution)

    def test_ambiguous_record_rejected(self):
        """Matrix H & I: AMBIGUOUS record builder rejected (no candidate auto-selection)."""
        port_id = uuid4()
        acc_id = uuid4()
        cand1 = uuid4()
        cand2 = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        _, draft = _make_assessment_and_draft(
            portfolio_id=port_id,
            account_id=acc_id,
            ordinal=1,
            imported_at=imported_at,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            instrument_reference="AMBIG",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )

        resolution = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.AMBIGUOUS,
            resolution_as_of_date=eff_date,
            resolver_key="mock",
            resolver_revision=1,
            candidate_instrument_ids=[cand1, cand2],
            diagnostics=[
                ImportInstrumentResolutionDiagnostic(
                    code="ambiguous_reference",
                    message="Multiple matches",
                )
            ],
        )

        with pytest.raises(PortfolioImportMaterializationError, match="not eligible"):
            build_import_ledger_transaction_plan(resolution)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Direct Tamper & Validation Matrix (Sections 54-55)
# ─────────────────────────────────────────────────────────────────────────────

class TestDirectTamperMatrix:
    """Sections 54-55: Direct construction verifies exact draft/resolution economics."""

    @pytest.fixture
    def valid_resolved_resolution(self):
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)
        exec_at = datetime(2026, 8, 28, 10, 15, 30, tzinfo=timezone.utc)

        _, draft = _make_assessment_and_draft(
            portfolio_id=port_id,
            account_id=acc_id,
            ordinal=1,
            imported_at=imported_at,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            executed_at=exec_at,
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )

        resolution = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=eff_date,
            resolver_key="mock",
            resolver_revision=1,
            instrument_id=inst_id,
        )
        return port_id, acc_id, inst_id, eff_date, exec_at, resolution

    def test_tampered_portfolio_id_rejected(self, valid_resolved_resolution):
        """Matrix T: tampered portfolio_id rejected."""
        port_id, acc_id, inst_id, eff_date, exec_at, res = valid_resolved_resolution
        other_port = uuid4()
        sha = _compute_plan_sha256(
            resolution=res,
            portfolio_id=other_port,
            account_id=acc_id,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            executed_at=exec_at,
            instrument_id=inst_id,
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
            cash_amount=None,
            cash_currency=None,
            from_currency=None,
            from_amount=None,
            to_currency=None,
            to_amount=None,
        )
        with pytest.raises(PortfolioImportMaterializationError, match="portfolio_id"):
            ImportLedgerTransactionPlan(
                resolution=res,
                portfolio_id=other_port,
                account_id=acc_id,
                transaction_type=TransactionType.BUY,
                effective_date=eff_date,
                executed_at=exec_at,
                instrument_id=inst_id,
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
                cash_amount=None,
                cash_currency=None,
                from_currency=None,
                from_amount=None,
                to_currency=None,
                to_amount=None,
                plan_sha256=sha,
            )

    def test_tampered_account_id_rejected(self, valid_resolved_resolution):
        """Matrix U: tampered account_id rejected."""
        port_id, acc_id, inst_id, eff_date, exec_at, res = valid_resolved_resolution
        other_acc = uuid4()
        sha = _compute_plan_sha256(
            resolution=res,
            portfolio_id=port_id,
            account_id=other_acc,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            executed_at=exec_at,
            instrument_id=inst_id,
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
            cash_amount=None,
            cash_currency=None,
            from_currency=None,
            from_amount=None,
            to_currency=None,
            to_amount=None,
        )
        with pytest.raises(PortfolioImportMaterializationError, match="account_id"):
            ImportLedgerTransactionPlan(
                resolution=res,
                portfolio_id=port_id,
                account_id=other_acc,
                transaction_type=TransactionType.BUY,
                effective_date=eff_date,
                executed_at=exec_at,
                instrument_id=inst_id,
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
                cash_amount=None,
                cash_currency=None,
                from_currency=None,
                from_amount=None,
                to_currency=None,
                to_amount=None,
                plan_sha256=sha,
            )

    def test_tampered_quantity_rejected(self, valid_resolved_resolution):
        """Matrix Y: tampered quantity rejected."""
        port_id, acc_id, inst_id, eff_date, exec_at, res = valid_resolved_resolution
        tampered_qty = Decimal("999")
        sha = _compute_plan_sha256(
            resolution=res,
            portfolio_id=port_id,
            account_id=acc_id,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            executed_at=exec_at,
            instrument_id=inst_id,
            quantity=tampered_qty,
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
            cash_amount=None,
            cash_currency=None,
            from_currency=None,
            from_amount=None,
            to_currency=None,
            to_amount=None,
        )
        with pytest.raises(PortfolioImportMaterializationError, match="quantity"):
            ImportLedgerTransactionPlan(
                resolution=res,
                portfolio_id=port_id,
                account_id=acc_id,
                transaction_type=TransactionType.BUY,
                effective_date=eff_date,
                executed_at=exec_at,
                instrument_id=inst_id,
                quantity=tampered_qty,
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
                cash_amount=None,
                cash_currency=None,
                from_currency=None,
                from_amount=None,
                to_currency=None,
                to_amount=None,
                plan_sha256=sha,
            )

    def test_tampered_instrument_id_rejected(self, valid_resolved_resolution):
        """Matrix X: tampered instrument_id rejected."""
        port_id, acc_id, inst_id, eff_date, exec_at, res = valid_resolved_resolution
        other_inst = uuid4()
        sha = _compute_plan_sha256(
            resolution=res,
            portfolio_id=port_id,
            account_id=acc_id,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            executed_at=exec_at,
            instrument_id=other_inst,
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
            cash_amount=None,
            cash_currency=None,
            from_currency=None,
            from_amount=None,
            to_currency=None,
            to_amount=None,
        )
        with pytest.raises(PortfolioImportMaterializationError, match="instrument_id"):
            ImportLedgerTransactionPlan(
                resolution=res,
                portfolio_id=port_id,
                account_id=acc_id,
                transaction_type=TransactionType.BUY,
                effective_date=eff_date,
                executed_at=exec_at,
                instrument_id=other_inst,
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
                cash_amount=None,
                cash_currency=None,
                from_currency=None,
                from_amount=None,
                to_currency=None,
                to_amount=None,
                plan_sha256=sha,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Hash & Identity Matrix (Sections 56-57)
# ─────────────────────────────────────────────────────────────────────────────

class TestHashAndIdentityMatrix:
    """Sections 56-57: Plan hash determinism, format strictness, and staging identity."""

    def test_canonical_plan_hash_matches_manual_computation(self):
        """Matrix AD: Independent manual computation matches plan_sha256."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)
        exec_at = datetime(2026, 8, 28, 10, 15, 30, tzinfo=timezone.utc)

        _, draft = _make_assessment_and_draft(
            portfolio_id=port_id,
            account_id=acc_id,
            ordinal=1,
            imported_at=imported_at,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            executed_at=exec_at,
            instrument_reference="AAPL",
            quantity=Decimal("10.00"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )

        resolution = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=eff_date,
            resolver_key="mock",
            resolver_revision=1,
            instrument_id=inst_id,
        )

        plan = build_import_ledger_transaction_plan(resolution)

        expected_preimage = [
            resolution.resolution_sha256,
            str(port_id),
            str(acc_id),
            "buy",
            "2026-08-28",
            "2026-08-28T10:15:30+00:00",
            str(inst_id),
            "10",
            "150",
            "USD",
            None,
            None,
            None,
            None,
            None,
            None,
        ]
        import hashlib
        encoded = json.dumps(expected_preimage, ensure_ascii=True, separators=(",", ":"))
        manual_sha = hashlib.sha256(encoded.encode("utf-8")).hexdigest()

        assert plan.plan_sha256 == manual_sha

    def test_decimal_lexical_variation_hashes_identically(self):
        """Matrix AF: Decimal('10.00') and Decimal('10') hash identically in plan."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        _, draft1 = _make_assessment_and_draft(
            portfolio_id=port_id,
            account_id=acc_id,
            ordinal=1,
            imported_at=imported_at,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            instrument_reference="AAPL",
            quantity=Decimal("10.00"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )

        _, draft2 = _make_assessment_and_draft(
            portfolio_id=port_id,
            account_id=acc_id,
            ordinal=1,
            imported_at=imported_at,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150"),
            trade_currency=Currency.USD,
        )

        res1 = build_import_instrument_resolution(
            draft=draft1,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=eff_date,
            resolver_key="mock",
            resolver_revision=1,
            instrument_id=inst_id,
        )

        res2 = build_import_instrument_resolution(
            draft=draft2,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=eff_date,
            resolver_key="mock",
            resolver_revision=1,
            instrument_id=inst_id,
        )

        plan1 = build_import_ledger_transaction_plan(res1)
        plan2 = build_import_ledger_transaction_plan(res2)

        assert plan1.plan_sha256 == plan2.plan_sha256

    def test_timezone_equivalent_instants_hash_identically(self):
        """Matrix AG: Timezone-equivalent executed_at instants hash identically."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        from datetime import timedelta, timezone as dt_tz
        tz_plus_3 = dt_tz(timedelta(hours=3))

        exec1 = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
        exec2 = datetime(2026, 8, 28, 13, 0, tzinfo=tz_plus_3)

        _, draft1 = _make_assessment_and_draft(
            portfolio_id=port_id,
            account_id=acc_id,
            ordinal=1,
            imported_at=imported_at,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            executed_at=exec1,
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150"),
            trade_currency=Currency.USD,
        )

        _, draft2 = _make_assessment_and_draft(
            portfolio_id=port_id,
            account_id=acc_id,
            ordinal=1,
            imported_at=imported_at,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            executed_at=exec2,
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150"),
            trade_currency=Currency.USD,
        )

        res1 = build_import_instrument_resolution(
            draft=draft1,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=eff_date,
            resolver_key="mock",
            resolver_revision=1,
            instrument_id=inst_id,
        )

        res2 = build_import_instrument_resolution(
            draft=draft2,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=eff_date,
            resolver_key="mock",
            resolver_revision=1,
            instrument_id=inst_id,
        )

        plan1 = build_import_ledger_transaction_plan(res1)
        plan2 = build_import_ledger_transaction_plan(res2)

        assert plan1.plan_sha256 == plan2.plan_sha256

    def test_plan_sha_strictness_checks(self):
        """Matrix AJ-AL: SHA format strictness (fake, uppercase, newline)."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        _, draft = _make_assessment_and_draft(
            portfolio_id=port_id,
            account_id=acc_id,
            ordinal=1,
            imported_at=imported_at,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )

        res = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=eff_date,
            resolver_key="mock",
            resolver_revision=1,
            instrument_id=inst_id,
        )

        # Uppercase rejected
        valid_sha = _compute_plan_sha256(
            resolution=res,
            portfolio_id=port_id,
            account_id=acc_id,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            executed_at=None,
            instrument_id=inst_id,
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
            cash_amount=None,
            cash_currency=None,
            from_currency=None,
            from_amount=None,
            to_currency=None,
            to_amount=None,
        )

        with pytest.raises(PortfolioImportMaterializationError):
            ImportLedgerTransactionPlan(
                resolution=res,
                portfolio_id=port_id,
                account_id=acc_id,
                transaction_type=TransactionType.BUY,
                effective_date=eff_date,
                executed_at=None,
                instrument_id=inst_id,
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
                cash_amount=None,
                cash_currency=None,
                from_currency=None,
                from_amount=None,
                to_currency=None,
                to_amount=None,
                plan_sha256=valid_sha.upper(),
            )

        # Newline rejected
        with pytest.raises(PortfolioImportMaterializationError):
            ImportLedgerTransactionPlan(
                resolution=res,
                portfolio_id=port_id,
                account_id=acc_id,
                transaction_type=TransactionType.BUY,
                effective_date=eff_date,
                executed_at=None,
                instrument_id=inst_id,
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
                cash_amount=None,
                cash_currency=None,
                from_currency=None,
                from_amount=None,
                to_currency=None,
                to_amount=None,
                plan_sha256=valid_sha + "\n",
            )

        # Mismatch rejected
        with pytest.raises(PortfolioImportMaterializationError, match="digest mismatch"):
            ImportLedgerTransactionPlan(
                resolution=res,
                portfolio_id=port_id,
                account_id=acc_id,
                transaction_type=TransactionType.BUY,
                effective_date=eff_date,
                executed_at=None,
                instrument_id=inst_id,
                quantity=Decimal("10"),
                unit_price=Decimal("150.00"),
                trade_currency=Currency.USD,
                cash_amount=None,
                cash_currency=None,
                from_currency=None,
                from_amount=None,
                to_currency=None,
                to_amount=None,
                plan_sha256="0" * 64,
            )

    def test_plan_identity_composition(self):
        """Matrix AM-AP: plan_identity extends resolution_identity deterministically."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        _, draft = _make_assessment_and_draft(
            portfolio_id=port_id,
            account_id=acc_id,
            ordinal=1,
            imported_at=imported_at,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )

        res = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=eff_date,
            resolver_key="mock",
            resolver_revision=1,
            instrument_id=inst_id,
        )

        plan = build_import_ledger_transaction_plan(res)

        assert plan.plan_identity == (*res.resolution_identity, plan.plan_sha256)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Batch Layer & Fail-Closed Matrix (Sections 58-60)
# ─────────────────────────────────────────────────────────────────────────────

class TestBatchLayerAndFailClosedMatrix:
    """Sections 58-60: Batch manifest construction, fail-closed gate, and completeness."""

    def test_empty_resolution_batch_is_valid(self):
        """Matrix AQ: Empty resolution batch produces valid empty materialization batch."""
        port_id = uuid4()
        acc_id = uuid4()
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        file_prov = build_import_file_provenance(
            portfolio_id=port_id,
            account_id=acc_id,
            source_key="sentinax_csv",
            filename="empty.csv",
            content=b"header\n",
            imported_at=imported_at,
        )
        raw_manifest = build_import_batch_manifest(
            file_provenance=file_prov,
            records=[],
        )
        parsed_manifest = build_parsed_import_batch_manifest(
            raw_manifest=raw_manifest,
            parser_revision=1,
            parsed_records=[],
        )
        assessment_batch = build_import_assessment_batch(
            parsed_manifest=parsed_manifest,
            assessments=[],
        )
        draft_manifest = build_import_draft_batch_manifest(
            assessment_batch=assessment_batch,
            drafts=[],
        )
        res_batch = build_import_instrument_resolution_batch(
            draft_manifest=draft_manifest,
            resolutions=[],
        )

        mat_batch = build_import_ledger_materialization_batch(res_batch)

        assert mat_batch.plan_count == 0
        assert mat_batch.plans == ()
        assert len(mat_batch.materialization_manifest_sha256) == 64
        assert mat_batch.materialization_manifest_identity == (
            *res_batch.resolution_manifest_identity,
            mat_batch.materialization_manifest_sha256,
        )

    def test_resolved_and_not_required_batch_valid(self):
        """Matrix AS & AT & AU: Multi-row fully resolved batch produces complete plans."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        ass_batch = _make_test_assessment_batch(count=2, portfolio_id=port_id, account_id=acc_id)

        d1 = build_import_transaction_draft(
            assessment_batch=ass_batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        d2 = build_import_transaction_draft(
            assessment_batch=ass_batch,
            record_ordinal=2,
            transaction_type=TransactionType.CASH_DEPOSIT,
            effective_date=eff_date,
            cash_amount=Decimal("500.00"),
            cash_currency=Currency.USD,
        )
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        r1 = build_import_instrument_resolution(
            draft=d1,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=eff_date,
            resolver_key="mock",
            resolver_revision=1,
            instrument_id=inst_id,
        )
        r2 = build_import_instrument_resolution(
            draft=d2,
            status=ImportInstrumentResolutionStatus.NOT_REQUIRED,
            resolution_as_of_date=eff_date,
        )
        res_batch = build_import_instrument_resolution_batch(draft_manifest, [r1, r2])

        mat_batch = build_import_ledger_materialization_batch(res_batch)

        assert mat_batch.plan_count == 2
        assert mat_batch.plans[0].instrument_id == inst_id
        assert mat_batch.plans[1].instrument_id is None
        assert mat_batch.plans[0].record_ordinal == 1
        assert mat_batch.plans[1].record_ordinal == 2

    def test_one_unresolved_blocks_entire_materialization_batch(self):
        """Matrix AV & AX: 1 UNRESOLVED outcome blocks entire batch (no partial return)."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        ass_batch = _make_test_assessment_batch(count=2, portfolio_id=port_id, account_id=acc_id)

        d1 = build_import_transaction_draft(
            assessment_batch=ass_batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        d2 = build_import_transaction_draft(
            assessment_batch=ass_batch,
            record_ordinal=2,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            instrument_reference="UNKNOWN",
            quantity=Decimal("5"),
            unit_price=Decimal("100.00"),
            trade_currency=Currency.USD,
        )
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        r1 = build_import_instrument_resolution(
            draft=d1,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=eff_date,
            resolver_key="mock",
            resolver_revision=1,
            instrument_id=inst_id,
        )
        r2 = build_import_instrument_resolution(
            draft=d2,
            status=ImportInstrumentResolutionStatus.UNRESOLVED,
            resolution_as_of_date=eff_date,
            resolver_key="mock",
            resolver_revision=1,
            diagnostics=[
                ImportInstrumentResolutionDiagnostic(
                    code="instrument_not_found",
                    message="No match",
                )
            ],
        )
        res_batch = build_import_instrument_resolution_batch(draft_manifest, [r1, r2])

        assert res_batch.is_fully_resolved is False

        with pytest.raises(PortfolioImportMaterializationError, match="Cannot materialize resolution batch"):
            build_import_ledger_materialization_batch(res_batch)

    def test_one_ambiguous_blocks_entire_materialization_batch(self):
        """Matrix AW: 1 AMBIGUOUS outcome blocks entire batch."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        cand1 = uuid4()
        cand2 = uuid4()
        eff_date = date(2026, 8, 28)
        ass_batch = _make_test_assessment_batch(count=2, portfolio_id=port_id, account_id=acc_id)

        d1 = build_import_transaction_draft(
            assessment_batch=ass_batch,
            record_ordinal=1,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )
        d2 = build_import_transaction_draft(
            assessment_batch=ass_batch,
            record_ordinal=2,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            instrument_reference="AMBIG",
            quantity=Decimal("5"),
            unit_price=Decimal("100.00"),
            trade_currency=Currency.USD,
        )
        draft_manifest = build_import_draft_batch_manifest(ass_batch, [d1, d2])

        r1 = build_import_instrument_resolution(
            draft=d1,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=eff_date,
            resolver_key="mock",
            resolver_revision=1,
            instrument_id=inst_id,
        )
        r2 = build_import_instrument_resolution(
            draft=d2,
            status=ImportInstrumentResolutionStatus.AMBIGUOUS,
            resolution_as_of_date=eff_date,
            resolver_key="mock",
            resolver_revision=1,
            candidate_instrument_ids=[cand1, cand2],
            diagnostics=[
                ImportInstrumentResolutionDiagnostic(
                    code="ambiguous_reference",
                    message="Multiple matches",
                )
            ],
        )
        res_batch = build_import_instrument_resolution_batch(draft_manifest, [r1, r2])

        assert res_batch.is_fully_resolved is False

        with pytest.raises(PortfolioImportMaterializationError, match="Cannot materialize resolution batch"):
            build_import_ledger_materialization_batch(res_batch)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Semantic Equality & Object Identity (Section 61)
# ─────────────────────────────────────────────────────────────────────────────

class TestSemanticEquality:
    """Section 61: Proves semantic equality without object identity dependency."""

    def test_semantically_equal_reconstructed_resolution_valid(self):
        """Proves A == B and A is not B works without Python identity coupling."""
        port_id = uuid4()
        acc_id = uuid4()
        inst_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        ass_batch, draft = _make_assessment_and_draft(
            portfolio_id=port_id,
            account_id=acc_id,
            ordinal=1,
            imported_at=imported_at,
            transaction_type=TransactionType.BUY,
            effective_date=eff_date,
            instrument_reference="AAPL",
            quantity=Decimal("10"),
            unit_price=Decimal("150.00"),
            trade_currency=Currency.USD,
        )

        res_a = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=eff_date,
            resolver_key="mock",
            resolver_revision=1,
            instrument_id=inst_id,
        )

        res_b = build_import_instrument_resolution(
            draft=draft,
            status=ImportInstrumentResolutionStatus.RESOLVED,
            resolution_as_of_date=eff_date,
            resolver_key="mock",
            resolver_revision=1,
            instrument_id=inst_id,
        )

        assert res_a == res_b
        assert res_a is not res_b

        plan_a = build_import_ledger_transaction_plan(res_a)
        plan_b = build_import_ledger_transaction_plan(res_b)

        assert plan_a == plan_b
        assert plan_a.plan_sha256 == plan_b.plan_sha256


# ─────────────────────────────────────────────────────────────────────────────
# 6. End-to-End Integration (Sections 62-64)
# ─────────────────────────────────────────────────────────────────────────────

class TestEndToEndIntegration:
    """Sections 62-64: Full pipeline integration through Sentinax Canonical CSV."""

    def test_mixed_end_to_end_with_rejected_row(self):
        """
        Section 62: Real Canonical CSV with BUY, CASH_DEPOSIT, DIVIDEND, FX_CONVERSION,
        and 1 semantic REJECTED row -> plan_count = 4 (zero plans for rejected row).
        """
        port_id = uuid4()
        acc_id = uuid4()
        aapl_id = uuid4()
        msft_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AAPL,10,150.00,USD,,,,,,",
            "cash_deposit,2026-08-28,2026-08-28T10:15:30+00:00,,,,,500.00,USD,,,,",
            "dividend,2026-08-28,2026-08-28T10:15:30+00:00,MSFT,,,,25.00,USD,,,,",
            "fx_conversion,2026-08-28,2026-08-28T10:15:30+00:00,,,,,,,USD,100.00,TRY,3400.00",
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,BAD_ROW,not_a_num,150.00,USD,,,,,,",  # REJECTED row
        ])

        resolver = MockInstrumentResolver(
            mapping={
                ("AAPL", eff_date): [aapl_id],
                ("MSFT", eff_date): [msft_id],
            }
        )

        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="mixed.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        # Assessment check
        assert res_batch.draft_manifest.assessment_batch.record_count == 5
        assert res_batch.draft_manifest.assessment_batch.ready_count == 4
        assert res_batch.draft_manifest.assessment_batch.rejected_count == 1
        assert res_batch.resolution_count == 4
        assert res_batch.is_fully_resolved is True

        # Materialization check
        mat_batch = build_import_ledger_materialization_batch(res_batch)

        assert mat_batch.plan_count == 4
        assert len(mat_batch.plans) == 4

        p1, p2, p3, p4 = mat_batch.plans
        assert p1.transaction_type == TransactionType.BUY
        assert p1.instrument_id == aapl_id
        assert p1.quantity == Decimal("10")
        assert p1.record_ordinal == 1

        assert p2.transaction_type == TransactionType.CASH_DEPOSIT
        assert p2.instrument_id is None
        assert p2.cash_amount == Decimal("500.00")
        assert p2.record_ordinal == 2

        assert p3.transaction_type == TransactionType.DIVIDEND
        assert p3.instrument_id == msft_id
        assert p3.cash_amount == Decimal("25.00")
        assert p3.record_ordinal == 3

        assert p4.transaction_type == TransactionType.FX_CONVERSION
        assert p4.instrument_id is None
        assert p4.from_currency == Currency.USD
        assert p4.from_amount == Decimal("100.00")
        assert p4.to_currency == Currency.TRY
        assert p4.to_amount == Decimal("3400.00")
        assert p4.record_ordinal == 4

    def test_unresolved_end_to_end_rejects_materialization(self):
        """Section 63: Valid BUY with 0 resolver candidates produces UNRESOLVED, blocking batch materialization."""
        port_id = uuid4()
        acc_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,UNKNOWN_TICKER,10,150.00,USD,,,,,,",
        ])

        resolver = MockInstrumentResolver(mapping={})

        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        assert res_batch.is_fully_resolved is False
        assert res_batch.unresolved_count == 1

        with pytest.raises(PortfolioImportMaterializationError, match="Cannot materialize resolution batch"):
            build_import_ledger_materialization_batch(res_batch)

    def test_ambiguous_end_to_end_rejects_materialization(self):
        """Section 64: Valid BUY with 2 resolver candidates produces AMBIGUOUS, blocking batch materialization."""
        port_id = uuid4()
        acc_id = uuid4()
        eff_date = date(2026, 8, 28)
        imported_at = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)

        csv_bytes = _make_csv([
            "buy,2026-08-28,2026-08-28T10:15:30+00:00,AMBIG_TICKER,10,150.00,USD,,,,,,",
        ])

        resolver = MockInstrumentResolver(
            mapping={("AMBIG_TICKER", eff_date): [uuid4(), uuid4()]}
        )

        res_batch = run_sentinax_canonical_csv_import_v1(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="trades.csv",
            content=csv_bytes,
            imported_at=imported_at,
            resolver=resolver,
        )

        assert res_batch.is_fully_resolved is False
        assert res_batch.ambiguous_count == 1

        with pytest.raises(PortfolioImportMaterializationError, match="Cannot materialize resolution batch"):
            build_import_ledger_materialization_batch(res_batch)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Static / Source Inspection Tests (Sections 65-69)
# ─────────────────────────────────────────────────────────────────────────────

class TestSourceInspection:
    """Sections 65-69: Verifies zero forbidden symbols, imports, or authority violations."""

    def test_no_external_identity_in_dataclass_fields(self):
        """Section 65: No external_source, external_reference, or idempotency_key in fields."""
        fields = [f.name for f in ImportLedgerTransactionPlan.__dataclass_fields__.values()]
        assert "external_source" not in fields
        assert "external_reference" not in fields
        assert "idempotency_key" not in fields

    def test_no_ledger_only_fields_in_plan(self):
        """Section 67: No id, recorded_at, cash_bucket_id, or reverses_transaction_id in fields."""
        fields = [f.name for f in ImportLedgerTransactionPlan.__dataclass_fields__.values()]
        assert "id" not in fields
        assert "recorded_at" not in fields
        assert "cash_bucket_id" not in fields
        assert "reverses_transaction_id" not in fields
        assert "notes" not in fields

    def test_no_forbidden_imports_or_calls_in_production_module(self):
        """Sections 66, 68, 69: Inspect source code of import_materialization.py."""
        import backend.engine.private.portfolio.import_materialization as mat_mod
        src = inspect.getsource(mat_mod)

        # No PortfolioTransaction or PortfolioRepository
        assert "PortfolioTransaction" not in src
        assert "PortfolioRepository" not in src

        # No random UUID generation
        assert "uuid4" not in src
        assert "uuid5" not in src

        # No system clock calls
        assert "datetime.now" not in src
        assert "datetime.utcnow" not in src
        assert "date.today" not in src
        assert "utcnow" not in src

        # No ledger external identity mapping
        assert "external_source" not in src
        assert "external_reference" not in src
        assert "cash_bucket_id" not in src
        assert "reverses_transaction_id" not in src
