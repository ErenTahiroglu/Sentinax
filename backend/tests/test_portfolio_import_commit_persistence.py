"""
Unit test suite for Phase 13P:
Strict Persistence Codec & Serialization/Hydration Boundary for Import Claim-to-Ledger Bindings.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import inspect
from typing import Any, Dict, Optional, Tuple
from uuid import UUID, uuid4

import pytest

from backend.engine.private.domain import Currency, TransactionType
from backend.engine.private.portfolio.import_assessment import (
    ImportAssessmentStatus,
    build_import_assessment_batch,
    build_import_record_assessment,
)
from backend.engine.private.portfolio.import_batch import build_import_batch_manifest
from backend.engine.private.portfolio.import_commit import (
    ImportLedgerBindingIntent,
    build_import_ledger_binding_intent,
)
from backend.engine.private.portfolio.import_commit_persistence import (
    PersistedImportLedgerBinding,
    PortfolioImportCommitPersistenceError,
    hydrate_import_ledger_binding,
    serialize_import_ledger_binding,
)
from backend.engine.private.portfolio.import_draft import build_import_transaction_draft
from backend.engine.private.portfolio.import_instrument_resolution import (
    ImportInstrumentResolutionStatus,
    build_import_instrument_resolution,
)
from backend.engine.private.portfolio.import_materialization import (
    ImportLedgerTransactionPlan,
    build_import_ledger_transaction_plan,
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


def _make_test_binding_intent(
    portfolio_id: Optional[UUID] = None,
    account_id: Optional[UUID] = None,
    ordinal: int = 1,
    transaction_type: TransactionType = TransactionType.BUY,
    effective_date: date = date(2026, 8, 28),
    instrument_id: Optional[UUID] = None,
    source_key: str = "sentinax_csv",
    filename: str = "test.csv",
    imported_at: Optional[datetime] = None,
    raw_content: bytes = b"dummy_content",
    resolver_key: str = "mock",
    resolver_revision: int = 1,
) -> ImportLedgerBindingIntent:
    port_id = portfolio_id or uuid4()
    acc_id = account_id or uuid4()
    inst_id = instrument_id or uuid4()
    t = imported_at or datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc)

    file_prov = build_import_file_provenance(
        portfolio_id=port_id,
        account_id=acc_id,
        source_key=source_key,
        filename=filename,
        content=raw_content,
        imported_at=t,
    )

    records = []
    parsed_records = []
    assessments = []
    for ord_idx in range(1, ordinal + 1):
        raw_r = f"raw_row_{ord_idx}".encode("utf-8")
        rp = build_import_record_provenance(
            file_provenance=file_prov,
            record_ordinal=ord_idx,
            raw_record=raw_r,
        )
        records.append(rp)
        pr = build_parsed_import_record(
            record_provenance=rp,
            raw_record=raw_r,
            parser_revision=1,
            fields=[
                ImportParsedField("symbol", "AAPL"),
                ImportParsedField("quantity", "10"),
                ImportParsedField("price", "150.00"),
            ],
        )
        parsed_records.append(pr)
        ass = build_import_record_assessment(
            parsed_record=pr,
            status=ImportAssessmentStatus.READY,
        )
        assessments.append(ass)

    raw_manifest = build_import_batch_manifest(
        file_provenance=file_prov,
        records=records,
    )

    parsed_manifest = build_parsed_import_batch_manifest(
        raw_manifest=raw_manifest,
        parser_revision=1,
        parsed_records=parsed_records,
    )

    ass_batch = build_import_assessment_batch(
        parsed_manifest=parsed_manifest,
        assessments=assessments,
    )

    draft_kwargs: Dict[str, Any] = {
        "assessment_batch": ass_batch,
        "record_ordinal": ordinal,
        "transaction_type": transaction_type,
        "effective_date": effective_date,
    }

    if transaction_type == TransactionType.BUY:
        draft_kwargs.update({
            "instrument_reference": "AAPL",
            "quantity": Decimal("10"),
            "unit_price": Decimal("150.00"),
            "trade_currency": Currency.USD,
        })
    elif transaction_type == TransactionType.CASH_DEPOSIT:
        draft_kwargs.update({
            "cash_amount": Decimal("500.00"),
            "cash_currency": Currency.TRY,
        })

    draft = build_import_transaction_draft(**draft_kwargs)

    res_status = (
        ImportInstrumentResolutionStatus.RESOLVED
        if transaction_type == TransactionType.BUY
        else ImportInstrumentResolutionStatus.NOT_REQUIRED
    )

    res = build_import_instrument_resolution(
        draft=draft,
        status=res_status,
        resolution_as_of_date=effective_date,
        resolver_key=resolver_key if res_status == ImportInstrumentResolutionStatus.RESOLVED else None,
        resolver_revision=resolver_revision if res_status == ImportInstrumentResolutionStatus.RESOLVED else None,
        instrument_id=inst_id if res_status == ImportInstrumentResolutionStatus.RESOLVED else None,
    )

    plan = build_import_ledger_transaction_plan(res)
    return build_import_ledger_binding_intent(plan)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Serialization Tests (Matrix U-AA, AB-AF)
# ─────────────────────────────────────────────────────────────────────────────

class TestBindingSerialization:
    """Matrix U-AA: Serialization tests and payload checks."""

    def test_valid_buy_intent_serializes(self):
        """Matrix U, W, X, Y, Z, AA: BUY intent serializes with exact fields."""
        intent = _make_test_binding_intent(transaction_type=TransactionType.BUY)
        owner_id = uuid4()
        tx_id = uuid4()

        payload = serialize_import_ledger_binding(
            intent,
            transaction_id=tx_id,
            expected_owner_id=owner_id,
        )

        assert payload["owner_id"] == str(owner_id)
        assert payload["portfolio_id"] == str(intent.portfolio_id)
        assert payload["account_id"] == str(intent.account_id)
        assert payload["source_key"] == intent.source_key
        assert payload["file_content_sha256"] == intent.file_content_sha256
        assert payload["record_ordinal"] == intent.record_ordinal
        assert payload["record_sha256"] == intent.record_sha256
        assert payload["expected_plan_sha256"] == intent.expected_plan_sha256
        assert payload["transaction_id"] == str(tx_id)

        # bound_at is excluded (DB-generated default)
        assert "bound_at" not in payload

    def test_valid_cash_intent_serializes(self):
        """Matrix V: CASH_DEPOSIT intent serializes with exact fields."""
        intent = _make_test_binding_intent(transaction_type=TransactionType.CASH_DEPOSIT)
        owner_id = uuid4()
        tx_id = uuid4()

        payload = serialize_import_ledger_binding(
            intent,
            transaction_id=tx_id,
            expected_owner_id=owner_id,
        )

        assert payload["source_key"] == "sentinax_csv"
        assert payload["transaction_id"] == str(tx_id)
        assert payload["expected_plan_sha256"] == intent.expected_plan_sha256

    def test_serializer_rejects_non_intent(self):
        """Matrix AB: Non-intent input rejected."""
        with pytest.raises(PortfolioImportCommitPersistenceError, match="ImportLedgerBindingIntent"):
            serialize_import_ledger_binding(
                "not_an_intent",  # type: ignore[arg-type]
                transaction_id=uuid4(),
                expected_owner_id=uuid4(),
            )

    def test_serializer_rejects_invalid_owner_and_tx_id(self):
        """Matrix AC-AF: Invalid owner UUID or transaction UUID rejected."""
        intent = _make_test_binding_intent()

        with pytest.raises(PortfolioImportCommitPersistenceError, match="expected_owner_id"):
            serialize_import_ledger_binding(
                intent,
                transaction_id=uuid4(),
                expected_owner_id="invalid-uuid",
            )

        with pytest.raises(PortfolioImportCommitPersistenceError, match="transaction_id"):
            serialize_import_ledger_binding(
                intent,
                transaction_id="invalid-uuid",
                expected_owner_id=uuid4(),
            )

        with pytest.raises(PortfolioImportCommitPersistenceError, match="expected_owner_id"):
            serialize_import_ledger_binding(
                intent,
                transaction_id=uuid4(),
                expected_owner_id=True,  # bool rejection
            )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Hydration Tests (Matrix AG-AY)
# ─────────────────────────────────────────────────────────────────────────────

class TestBindingHydration:
    """Matrix AG-AY: Hydration tests and fail-closed validation."""

    def test_valid_row_hydrates(self):
        """Matrix AG-AK: Valid database row hydrates into PersistedImportLedgerBinding."""
        owner_id = uuid4()
        port_id = uuid4()
        acc_id = uuid4()
        tx_id = uuid4()
        bound_at = datetime(2026, 8, 28, 15, 30, tzinfo=timezone.utc)

        row = {
            "owner_id": str(owner_id),
            "portfolio_id": str(port_id),
            "account_id": str(acc_id),
            "source_key": "sentinax_csv",
            "file_content_sha256": "a" * 64,
            "record_ordinal": 1,
            "record_sha256": "b" * 64,
            "expected_plan_sha256": "c" * 64,
            "transaction_id": str(tx_id),
            "bound_at": bound_at.isoformat(),
        }

        binding = hydrate_import_ledger_binding(row, expected_owner_id=owner_id)

        assert binding.owner_id == owner_id
        assert binding.portfolio_id == port_id
        assert binding.account_id == acc_id
        assert binding.source_key == "sentinax_csv"
        assert binding.file_content_sha256 == "a" * 64
        assert binding.record_ordinal == 1
        assert binding.record_sha256 == "b" * 64
        assert binding.expected_plan_sha256 == "c" * 64
        assert binding.transaction_id == tx_id
        assert binding.bound_at == bound_at
        assert binding.claim_identity == (port_id, acc_id, "sentinax_csv", "a" * 64, 1, "b" * 64)
        assert binding.interpreted_claim_identity == (port_id, acc_id, "sentinax_csv", "a" * 64, 1, "b" * 64, "c" * 64)

    def test_missing_column_rejected(self):
        """Matrix AL: Missing required column in row rejected."""
        owner_id = uuid4()
        row = {
            "owner_id": str(owner_id),
            "portfolio_id": str(uuid4()),
            # missing account_id
            "source_key": "sentinax_csv",
            "file_content_sha256": "a" * 64,
            "record_ordinal": 1,
            "record_sha256": "b" * 64,
            "expected_plan_sha256": "c" * 64,
            "transaction_id": str(uuid4()),
            "bound_at": "2026-08-28T15:30:00+00:00",
        }
        with pytest.raises(PortfolioImportCommitPersistenceError, match="account_id"):
            hydrate_import_ledger_binding(row, expected_owner_id=owner_id)

    def test_owner_mismatch_rejected(self):
        """Matrix AM: Row owner differing from expected_owner_id rejected."""
        owner_a = uuid4()
        owner_b = uuid4()
        row = {
            "owner_id": str(owner_a),
            "portfolio_id": str(uuid4()),
            "account_id": str(uuid4()),
            "source_key": "sentinax_csv",
            "file_content_sha256": "a" * 64,
            "record_ordinal": 1,
            "record_sha256": "b" * 64,
            "expected_plan_sha256": "c" * 64,
            "transaction_id": str(uuid4()),
            "bound_at": "2026-08-28T15:30:00+00:00",
        }
        with pytest.raises(PortfolioImportCommitPersistenceError, match="Owner isolation violation"):
            hydrate_import_ledger_binding(row, expected_owner_id=owner_b)

    def test_malformed_fields_rejected(self):
        """Matrix AN-AY: Malformed UUIDs, source keys, SHAs, ordinals, and naive datetimes rejected."""
        owner_id = uuid4()
        base_row = {
            "owner_id": str(owner_id),
            "portfolio_id": str(uuid4()),
            "account_id": str(uuid4()),
            "source_key": "sentinax_csv",
            "file_content_sha256": "a" * 64,
            "record_ordinal": 1,
            "record_sha256": "b" * 64,
            "expected_plan_sha256": "c" * 64,
            "transaction_id": str(uuid4()),
            "bound_at": "2026-08-28T15:30:00+00:00",
        }

        # Malformed portfolio UUID
        r = base_row.copy()
        r["portfolio_id"] = "bad-uuid"
        with pytest.raises(PortfolioImportCommitPersistenceError, match="portfolio_id"):
            hydrate_import_ledger_binding(r, expected_owner_id=owner_id)

        # Malformed source key (uppercase)
        r = base_row.copy()
        r["source_key"] = "SENTINAX_CSV"
        with pytest.raises(PortfolioImportCommitPersistenceError, match="source_key"):
            hydrate_import_ledger_binding(r, expected_owner_id=owner_id)

        # Malformed source key with whitespace
        r = base_row.copy()
        r["source_key"] = " sentinax_csv"
        with pytest.raises(PortfolioImportCommitPersistenceError, match="source_key"):
            hydrate_import_ledger_binding(r, expected_owner_id=owner_id)

        # Malformed file SHA (short)
        r = base_row.copy()
        r["file_content_sha256"] = "abc"
        with pytest.raises(PortfolioImportCommitPersistenceError, match="file_content_sha256"):
            hydrate_import_ledger_binding(r, expected_owner_id=owner_id)

        # Zero record_ordinal
        r = base_row.copy()
        r["record_ordinal"] = 0
        with pytest.raises(PortfolioImportCommitPersistenceError, match="record_ordinal"):
            hydrate_import_ledger_binding(r, expected_owner_id=owner_id)

        # Bool record_ordinal
        r = base_row.copy()
        r["record_ordinal"] = True
        with pytest.raises(PortfolioImportCommitPersistenceError, match="record_ordinal"):
            hydrate_import_ledger_binding(r, expected_owner_id=owner_id)

        # Naive datetime
        r = base_row.copy()
        r["bound_at"] = "2026-08-28T15:30:00"  # no tz
        with pytest.raises(PortfolioImportCommitPersistenceError, match="bound_at"):
            hydrate_import_ledger_binding(r, expected_owner_id=owner_id)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Invariance, Sensitivity & Conflict Principles (Sections 69-73)
# ─────────────────────────────────────────────────────────────────────────────

class TestPersistenceInvarianceAndSensitivity:
    """Sections 69-73: Persistence properties for conflict detection and invariance."""

    def test_same_claim_changed_plan_payload(self):
        """
        Section 69: Same claim identity with changed plan produces identical
        claim key columns and different expected_plan_sha256.
        """
        port_id = uuid4()
        acc_id = uuid4()
        owner_id = uuid4()
        tx_id = uuid4()

        intent1 = _make_test_binding_intent(
            portfolio_id=port_id,
            account_id=acc_id,
            resolver_revision=1,
        )
        intent2 = _make_test_binding_intent(
            portfolio_id=port_id,
            account_id=acc_id,
            resolver_revision=2,
        )

        p1 = serialize_import_ledger_binding(intent1, transaction_id=tx_id, expected_owner_id=owner_id)
        p2 = serialize_import_ledger_binding(intent2, transaction_id=tx_id, expected_owner_id=owner_id)

        # Claim key columns match
        assert p1["portfolio_id"] == p2["portfolio_id"]
        assert p1["account_id"] == p2["account_id"]
        assert p1["source_key"] == p2["source_key"]
        assert p1["file_content_sha256"] == p2["file_content_sha256"]
        assert p1["record_ordinal"] == p2["record_ordinal"]
        assert p1["record_sha256"] == p2["record_sha256"]

        # Plan SHA differs
        assert p1["expected_plan_sha256"] != p2["expected_plan_sha256"]

    def test_filename_and_imported_at_invariance(self):
        """Section 70: Filename and imported_at differences yield identical serialized claim keys."""
        port_id = uuid4()
        acc_id = uuid4()
        owner_id = uuid4()
        tx_id = uuid4()
        content = b"same_exact_content"

        intent1 = _make_test_binding_intent(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="statement1.csv",
            imported_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
            raw_content=content,
        )
        intent2 = _make_test_binding_intent(
            portfolio_id=port_id,
            account_id=acc_id,
            filename="statement2_renamed.csv",
            imported_at=datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc),
            raw_content=content,
        )

        p1 = serialize_import_ledger_binding(intent1, transaction_id=tx_id, expected_owner_id=owner_id)
        p2 = serialize_import_ledger_binding(intent2, transaction_id=tx_id, expected_owner_id=owner_id)

        assert p1["file_content_sha256"] == p2["file_content_sha256"]
        assert p1["record_sha256"] == p2["record_sha256"]

    def test_transaction_id_does_not_define_claim(self):
        """Section 72: Same intent serialized with different transaction IDs preserves identical claim keys."""
        intent = _make_test_binding_intent()
        owner_id = uuid4()
        tx1 = uuid4()
        tx2 = uuid4()

        p1 = serialize_import_ledger_binding(intent, transaction_id=tx1, expected_owner_id=owner_id)
        p2 = serialize_import_ledger_binding(intent, transaction_id=tx2, expected_owner_id=owner_id)

        assert p1["portfolio_id"] == p2["portfolio_id"]
        assert p1["account_id"] == p2["account_id"]
        assert p1["source_key"] == p2["source_key"]
        assert p1["file_content_sha256"] == p2["file_content_sha256"]
        assert p1["record_ordinal"] == p2["record_ordinal"]
        assert p1["record_sha256"] == p2["record_sha256"]
        assert p1["transaction_id"] != p2["transaction_id"]


# ─────────────────────────────────────────────────────────────────────────────
# 4. Direct Model Type Integrity Tests (Phase 13P.1)
# ─────────────────────────────────────────────────────────────────────────────

class TestDirectModelTypeIntegrity:
    """Matrix J-Q: Direct PersistedImportLedgerBinding constructor type enforcement."""

    def test_direct_model_accepts_actual_types(self):
        """Matrix J, O: Direct construction with actual UUIDs and aware datetime succeeds."""
        owner_id = uuid4()
        port_id = uuid4()
        acc_id = uuid4()
        tx_id = uuid4()
        bound_at = datetime(2026, 8, 28, 15, 30, tzinfo=timezone.utc)

        binding = PersistedImportLedgerBinding(
            owner_id=owner_id,
            portfolio_id=port_id,
            account_id=acc_id,
            source_key="sentinax_csv",
            file_content_sha256="a" * 64,
            record_ordinal=1,
            record_sha256="b" * 64,
            expected_plan_sha256="c" * 64,
            transaction_id=tx_id,
            bound_at=bound_at,
        )

        assert type(binding.owner_id) is UUID
        assert type(binding.portfolio_id) is UUID
        assert type(binding.account_id) is UUID
        assert type(binding.transaction_id) is UUID
        assert type(binding.bound_at) is datetime

    def test_direct_model_rejects_string_uuids(self):
        """Matrix K, L, M, N: Direct model rejects canonical UUID strings."""
        owner_id = uuid4()
        port_id = uuid4()
        acc_id = uuid4()
        tx_id = uuid4()
        bound_at = datetime(2026, 8, 28, 15, 30, tzinfo=timezone.utc)

        # owner_id as str
        with pytest.raises(PortfolioImportCommitPersistenceError, match="owner_id"):
            PersistedImportLedgerBinding(
                owner_id=str(owner_id),  # type: ignore[arg-type]
                portfolio_id=port_id,
                account_id=acc_id,
                source_key="sentinax_csv",
                file_content_sha256="a" * 64,
                record_ordinal=1,
                record_sha256="b" * 64,
                expected_plan_sha256="c" * 64,
                transaction_id=tx_id,
                bound_at=bound_at,
            )

        # portfolio_id as str
        with pytest.raises(PortfolioImportCommitPersistenceError, match="portfolio_id"):
            PersistedImportLedgerBinding(
                owner_id=owner_id,
                portfolio_id=str(port_id),  # type: ignore[arg-type]
                account_id=acc_id,
                source_key="sentinax_csv",
                file_content_sha256="a" * 64,
                record_ordinal=1,
                record_sha256="b" * 64,
                expected_plan_sha256="c" * 64,
                transaction_id=tx_id,
                bound_at=bound_at,
            )

        # account_id as str
        with pytest.raises(PortfolioImportCommitPersistenceError, match="account_id"):
            PersistedImportLedgerBinding(
                owner_id=owner_id,
                portfolio_id=port_id,
                account_id=str(acc_id),  # type: ignore[arg-type]
                source_key="sentinax_csv",
                file_content_sha256="a" * 64,
                record_ordinal=1,
                record_sha256="b" * 64,
                expected_plan_sha256="c" * 64,
                transaction_id=tx_id,
                bound_at=bound_at,
            )

        # transaction_id as str
        with pytest.raises(PortfolioImportCommitPersistenceError, match="transaction_id"):
            PersistedImportLedgerBinding(
                owner_id=owner_id,
                portfolio_id=port_id,
                account_id=acc_id,
                source_key="sentinax_csv",
                file_content_sha256="a" * 64,
                record_ordinal=1,
                record_sha256="b" * 64,
                expected_plan_sha256="c" * 64,
                transaction_id=str(tx_id),  # type: ignore[arg-type]
                bound_at=bound_at,
            )

    def test_direct_model_rejects_string_and_naive_datetime(self):
        """Matrix P, Q: Direct model rejects datetime strings and naive datetimes."""
        owner_id = uuid4()
        port_id = uuid4()
        acc_id = uuid4()
        tx_id = uuid4()

        # string bound_at
        with pytest.raises(PortfolioImportCommitPersistenceError, match="bound_at"):
            PersistedImportLedgerBinding(
                owner_id=owner_id,
                portfolio_id=port_id,
                account_id=acc_id,
                source_key="sentinax_csv",
                file_content_sha256="a" * 64,
                record_ordinal=1,
                record_sha256="b" * 64,
                expected_plan_sha256="c" * 64,
                transaction_id=tx_id,
                bound_at="2026-08-28T15:30:00+00:00",  # type: ignore[arg-type]
            )

        # naive bound_at
        with pytest.raises(PortfolioImportCommitPersistenceError, match="bound_at"):
            PersistedImportLedgerBinding(
                owner_id=owner_id,
                portfolio_id=port_id,
                account_id=acc_id,
                source_key="sentinax_csv",
                file_content_sha256="a" * 64,
                record_ordinal=1,
                record_sha256="b" * 64,
                expected_plan_sha256="c" * 64,
                transaction_id=tx_id,
                bound_at=datetime(2026, 8, 28, 15, 30),
            )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Static / Source Inspection Tests (Sections 74-77)
# ─────────────────────────────────────────────────────────────────────────────

class TestPersistenceSourceInspection:
    """Sections 74-77: Verifies pure codec properties and absence of forbidden operations."""

    def test_no_hashlib_in_persistence_module(self):
        """Section 54: import_commit_persistence does not import hashlib."""
        import backend.engine.private.portfolio.import_commit_persistence as mod
        src = inspect.getsource(mod)
        assert "import hashlib" not in src
        assert "from hashlib" not in src

    def test_no_uuid_generation(self):
        """Section 55: import_commit_persistence contains no uuid4 or uuid5."""
        import backend.engine.private.portfolio.import_commit_persistence as mod
        src = inspect.getsource(mod)
        assert "uuid4" not in src
        assert "uuid5" not in src

    def test_no_current_time_calls(self):
        """Section 56: import_commit_persistence contains no clock calls."""
        import backend.engine.private.portfolio.import_commit_persistence as mod
        src = inspect.getsource(mod)
        assert "datetime.now" not in src
        assert "datetime.utcnow" not in src
        assert "date.today" not in src
        assert "utcnow" not in src

    def test_no_financial_fields_or_external_identity_in_model(self):
        """Sections 74-75: PersistedImportLedgerBinding has no financial or external identity fields."""
        fields = [f.name for f in PersistedImportLedgerBinding.__dataclass_fields__.values()]
        forbidden = [
            "quantity",
            "unit_price",
            "trade_currency",
            "cash_amount",
            "cash_currency",
            "from_amount",
            "to_amount",
            "instrument_id",
            "external_source",
            "external_reference",
            "idempotency_key",
        ]
        for f in forbidden:
            assert f not in fields, f"Forbidden field '{f}' found in PersistedImportLedgerBinding."
