"""
backend/engine/private/portfolio/import_draft.py
================================================
Immutable Source-Neutral Economic Transaction Draft Contract (Phase 13H).

This module defines the pre-ledger draft boundary converting an explicitly READY
Phase 13G import assessment into typed, source-neutral economics.

Key Architectural Invariants:
1. Pure Economic Draft Contract:
   - Contains typed financial economics but remains strictly PRE-LEDGER.
   - Zero internal transaction UUIDs, zero recorded_at timestamps, zero canonical instrument UUIDs.
   - Zero external_source / external_reference / idempotency_key derivations.
   - Zero cash_bucket_id attribution (bucket policy belongs to ledger materialization).
2. Authoritative READY-Only Gate:
   - Strictly requires an authoritative Phase 13G ImportAssessmentBatch.
   - Only assessments with ImportAssessmentStatus.READY may be drafted.
   - UNRESOLVED and REJECTED records fail closed immediately without bypass or override.
3. Strict Type & Numeric Discipline:
   - transaction_type must be an actual TransactionType enum member (REVERSAL forbidden).
   - effective_date must be an exact datetime.date instance (datetime instances rejected).
   - executed_at, when present, must be timezone-aware with a non-None UTC offset.
   - All financial numeric fields must be strictly positive, finite Decimal instances (> 0).
   - All currency fields must be actual Currency enum members.
4. Mutually Exclusive Field Families:
   - BUY / SELL: Requires instrument_reference, quantity, unit_price, trade_currency.
   - CASH_DEPOSIT / CASH_WITHDRAWAL: Requires cash_amount, cash_currency.
   - DIVIDEND / INTEREST / FEE / TAX_WITHHOLDING: Requires cash_amount, cash_currency; optional instrument_reference.
   - FX_CONVERSION: Requires from_currency, from_amount, to_currency, to_amount (from_currency != to_currency).
   - Contradictory cross-family fields fail closed immediately.
5. Deterministic Preimage & Draft Hash:
   - draft_sha256 is computed from compact JSON:
     [assessment_manifest_sha256, record_ordinal, parsed_sha256, transaction_type,
      effective_date, canonical_executed_at, instrument_reference, canonical_quantity,
      canonical_unit_price, trade_currency, canonical_cash_amount, cash_currency,
      from_currency, canonical_from_amount, to_currency, canonical_to_amount]
   - Binds directly to the assessment manifest hash; any assessment change alters the draft hash.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import re
from typing import Any, Optional, Tuple

from backend.engine.private.domain import Currency, TransactionType
from backend.engine.private.portfolio.import_assessment import (
    ImportAssessmentBatch,
    ImportAssessmentStatus,
    ImportRecordAssessment,
)

_SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_INSTRUMENT_REF_LENGTH = 256


class PortfolioImportDraftError(ValueError):
    """Raised when transaction draft validation fails closed."""
    pass


def _canonical_decimal_str(d: Optional[Decimal]) -> Optional[str]:
    """
    Renders a finite Decimal in canonical text form for economic fingerprinting.
    Numerically equivalent Decimals produce identical text.
    """
    if d is None:
        return None
    if isinstance(d, bool) or not isinstance(d, Decimal):
        raise PortfolioImportDraftError(f"Expected Decimal, got {type(d).__name__}: {d!r}")
    if not d.is_finite():
        raise PortfolioImportDraftError(f"Expected finite Decimal, got: {d}")

    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s == "-0":
        s = "0"
    return s


def _canonical_datetime_str(dt: Optional[datetime]) -> Optional[str]:
    """
    Renders an aware datetime in canonical UTC instant text form.
    Chronologically equivalent instants produce identical text.
    """
    if dt is None:
        return None
    if isinstance(dt, bool) or not isinstance(dt, datetime):
        raise PortfolioImportDraftError(f"Expected datetime instance, got {type(dt).__name__}: {dt!r}")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise PortfolioImportDraftError(f"Datetime must be timezone-aware with non-None utcoffset, got: {dt}")
    utc_dt = dt.astimezone(timezone.utc)
    return utc_dt.isoformat()


def _validate_positive_decimal(val: Any, field_name: str) -> None:
    """Validates that val is strictly a finite Decimal > 0."""
    if isinstance(val, bool) or not isinstance(val, Decimal):
        raise PortfolioImportDraftError(
            f"{field_name} must be a Decimal instance, got {type(val).__name__}: {val!r}"
        )
    if not val.is_finite():
        raise PortfolioImportDraftError(f"{field_name} must be a finite Decimal, got: {val}")
    if val <= Decimal("0"):
        raise PortfolioImportDraftError(f"{field_name} must be strictly positive (> 0), got: {val}")


def _validate_currency(val: Any, field_name: str) -> None:
    """Validates that val is strictly a Currency enum member."""
    if isinstance(val, bool) or not isinstance(val, Currency):
        raise PortfolioImportDraftError(
            f"{field_name} must be a Currency enum member, got {type(val).__name__}: {val!r}"
        )


def _validate_draft_fields(
    assessment_batch: ImportAssessmentBatch,
    record_ordinal: int,
    transaction_type: TransactionType,
    effective_date: date,
    executed_at: Optional[datetime],
    instrument_reference: Optional[str],
    quantity: Optional[Decimal],
    unit_price: Optional[Decimal],
    trade_currency: Optional[Currency],
    cash_amount: Optional[Decimal],
    cash_currency: Optional[Currency],
    from_currency: Optional[Currency],
    from_amount: Optional[Decimal],
    to_currency: Optional[Currency],
    to_amount: Optional[Decimal],
) -> ImportRecordAssessment:
    """Validates all draft fields and returns the authoritative target assessment."""
    # 1. Assessment batch validation
    if not isinstance(assessment_batch, ImportAssessmentBatch):
        raise PortfolioImportDraftError(
            f"assessment_batch must be an ImportAssessmentBatch instance, got {type(assessment_batch).__name__}"
        )

    # 2. Record ordinal validation & binding
    if isinstance(record_ordinal, bool) or not isinstance(record_ordinal, int):
        raise PortfolioImportDraftError(
            f"record_ordinal must be an int instance, got {type(record_ordinal).__name__}"
        )
    if record_ordinal < 1:
        raise PortfolioImportDraftError(
            f"record_ordinal must be >= 1, got {record_ordinal}"
        )
    if record_ordinal > assessment_batch.record_count:
        raise PortfolioImportDraftError(
            f"record_ordinal {record_ordinal} exceeds batch record count {assessment_batch.record_count}"
        )

    target_assessment = assessment_batch.assessments[record_ordinal - 1]
    if target_assessment.record_ordinal != record_ordinal:
        raise PortfolioImportDraftError(
            f"Assessment at index {record_ordinal - 1} ordinal {target_assessment.record_ordinal} "
            f"does not match requested ordinal {record_ordinal}"
        )

    # 3. READY-only gate
    if target_assessment.status != ImportAssessmentStatus.READY:
        raise PortfolioImportDraftError(
            f"Only records with READY assessment status can be drafted. "
            f"Record ordinal {record_ordinal} has status {target_assessment.status.name}"
        )

    # 4. Transaction type contract
    if isinstance(transaction_type, bool) or not isinstance(transaction_type, TransactionType):
        raise PortfolioImportDraftError(
            f"transaction_type must be a TransactionType enum member, got {type(transaction_type).__name__}: {transaction_type!r}"
        )
    if transaction_type == TransactionType.REVERSAL:
        raise PortfolioImportDraftError(
            "TransactionType.REVERSAL cannot be drafted at pre-ledger stage in Phase 13H"
        )

    # 5. Effective date contract (strict date, reject datetime subclasses)
    if type(effective_date) is not date:
        raise PortfolioImportDraftError(
            f"effective_date must be strictly a built-in date instance, got {type(effective_date).__name__}: {effective_date!r}"
        )

    # 6. Executed at contract
    if executed_at is not None:
        if isinstance(executed_at, bool) or not isinstance(executed_at, datetime):
            raise PortfolioImportDraftError(
                f"executed_at must be a datetime instance or None, got {type(executed_at).__name__}: {executed_at!r}"
            )
        if executed_at.tzinfo is None or executed_at.tzinfo.utcoffset(executed_at) is None:
            raise PortfolioImportDraftError(
                f"executed_at must be a timezone-aware datetime with non-None utcoffset, got: {executed_at}"
            )

    # 7. Instrument reference contract
    if instrument_reference is not None:
        if isinstance(instrument_reference, bool) or not isinstance(instrument_reference, str):
            raise PortfolioImportDraftError(
                f"instrument_reference must be a str instance or None, got {type(instrument_reference).__name__}: {instrument_reference!r}"
            )
        if len(instrument_reference) < 1 or len(instrument_reference) > _MAX_INSTRUMENT_REF_LENGTH:
            raise PortfolioImportDraftError(
                f"instrument_reference length must be between 1 and {_MAX_INSTRUMENT_REF_LENGTH}, got {len(instrument_reference)}"
            )
        if not instrument_reference.strip():
            raise PortfolioImportDraftError("instrument_reference must not be empty or whitespace-only")

    # 8. Numeric & Currency validations for present fields
    if quantity is not None:
        _validate_positive_decimal(quantity, "quantity")
    if unit_price is not None:
        _validate_positive_decimal(unit_price, "unit_price")
    if trade_currency is not None:
        _validate_currency(trade_currency, "trade_currency")

    if cash_amount is not None:
        _validate_positive_decimal(cash_amount, "cash_amount")
    if cash_currency is not None:
        _validate_currency(cash_currency, "cash_currency")

    if from_currency is not None:
        _validate_currency(from_currency, "from_currency")
    if from_amount is not None:
        _validate_positive_decimal(from_amount, "from_amount")
    if to_currency is not None:
        _validate_currency(to_currency, "to_currency")
    if to_amount is not None:
        _validate_positive_decimal(to_amount, "to_amount")

    # 9. Mutually exclusive field families based on transaction_type
    if transaction_type in (TransactionType.BUY, TransactionType.SELL):
        if instrument_reference is None:
            raise PortfolioImportDraftError(f"{transaction_type.name} requires instrument_reference")
        if quantity is None:
            raise PortfolioImportDraftError(f"{transaction_type.name} requires quantity")
        if unit_price is None:
            raise PortfolioImportDraftError(f"{transaction_type.name} requires unit_price")
        if trade_currency is None:
            raise PortfolioImportDraftError(f"{transaction_type.name} requires trade_currency")

        if cash_amount is not None or cash_currency is not None:
            raise PortfolioImportDraftError(f"{transaction_type.name} must not specify cash fields")
        if (
            from_currency is not None
            or from_amount is not None
            or to_currency is not None
            or to_amount is not None
        ):
            raise PortfolioImportDraftError(f"{transaction_type.name} must not specify FX fields")

    elif transaction_type in (TransactionType.CASH_DEPOSIT, TransactionType.CASH_WITHDRAWAL):
        if cash_amount is None:
            raise PortfolioImportDraftError(f"{transaction_type.name} requires cash_amount")
        if cash_currency is None:
            raise PortfolioImportDraftError(f"{transaction_type.name} requires cash_currency")

        if instrument_reference is not None:
            raise PortfolioImportDraftError(f"{transaction_type.name} must not specify instrument_reference")
        if quantity is not None or unit_price is not None or trade_currency is not None:
            raise PortfolioImportDraftError(f"{transaction_type.name} must not specify trade fields")
        if (
            from_currency is not None
            or from_amount is not None
            or to_currency is not None
            or to_amount is not None
        ):
            raise PortfolioImportDraftError(f"{transaction_type.name} must not specify FX fields")

    elif transaction_type in (
        TransactionType.DIVIDEND,
        TransactionType.INTEREST,
        TransactionType.FEE,
        TransactionType.TAX_WITHHOLDING,
    ):
        if cash_amount is None:
            raise PortfolioImportDraftError(f"{transaction_type.name} requires cash_amount")
        if cash_currency is None:
            raise PortfolioImportDraftError(f"{transaction_type.name} requires cash_currency")

        if quantity is not None or unit_price is not None or trade_currency is not None:
            raise PortfolioImportDraftError(f"{transaction_type.name} must not specify trade fields")
        if (
            from_currency is not None
            or from_amount is not None
            or to_currency is not None
            or to_amount is not None
        ):
            raise PortfolioImportDraftError(f"{transaction_type.name} must not specify FX fields")

    elif transaction_type == TransactionType.FX_CONVERSION:
        if from_currency is None:
            raise PortfolioImportDraftError("FX_CONVERSION requires from_currency")
        if from_amount is None:
            raise PortfolioImportDraftError("FX_CONVERSION requires from_amount")
        if to_currency is None:
            raise PortfolioImportDraftError("FX_CONVERSION requires to_currency")
        if to_amount is None:
            raise PortfolioImportDraftError("FX_CONVERSION requires to_amount")

        if from_currency == to_currency:
            raise PortfolioImportDraftError(
                f"FX_CONVERSION from_currency and to_currency must differ, got {from_currency.value}"
            )

        if instrument_reference is not None:
            raise PortfolioImportDraftError("FX_CONVERSION must not specify instrument_reference")
        if quantity is not None or unit_price is not None or trade_currency is not None:
            raise PortfolioImportDraftError("FX_CONVERSION must not specify trade fields")
        if cash_amount is not None or cash_currency is not None:
            raise PortfolioImportDraftError("FX_CONVERSION must not specify simple cash fields")

    return target_assessment


def _compute_draft_sha256(
    assessment_batch: ImportAssessmentBatch,
    record_ordinal: int,
    target_assessment: ImportRecordAssessment,
    transaction_type: TransactionType,
    effective_date: date,
    executed_at: Optional[datetime],
    instrument_reference: Optional[str],
    quantity: Optional[Decimal],
    unit_price: Optional[Decimal],
    trade_currency: Optional[Currency],
    cash_amount: Optional[Decimal],
    cash_currency: Optional[Currency],
    from_currency: Optional[Currency],
    from_amount: Optional[Decimal],
    to_currency: Optional[Currency],
    to_amount: Optional[Decimal],
) -> str:
    """
    Computes deterministic SHA-256 hex digest for an economic draft preimage:
    [assessment_manifest_sha256, record_ordinal, parsed_sha256, transaction_type,
     effective_date, canonical_executed_at, instrument_reference, canonical_quantity,
     canonical_unit_price, trade_currency, canonical_cash_amount, cash_currency,
     from_currency, canonical_from_amount, to_currency, canonical_to_amount]
    """
    preimage = [
        assessment_batch.assessment_manifest_sha256,
        record_ordinal,
        target_assessment.parsed_record.parsed_sha256,
        transaction_type.value,
        effective_date.isoformat(),
        _canonical_datetime_str(executed_at),
        instrument_reference,
        _canonical_decimal_str(quantity),
        _canonical_decimal_str(unit_price),
        trade_currency.value if trade_currency is not None else None,
        _canonical_decimal_str(cash_amount),
        cash_currency.value if cash_currency is not None else None,
        from_currency.value if from_currency is not None else None,
        _canonical_decimal_str(from_amount),
        to_currency.value if to_currency is not None else None,
        _canonical_decimal_str(to_amount),
    ]
    encoded_json = json.dumps(preimage, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ImportTransactionDraft:
    """
    Immutable, source-neutral economic transaction draft bound to one READY import assessment.
    """
    assessment_batch: ImportAssessmentBatch
    record_ordinal: int
    transaction_type: TransactionType
    effective_date: date
    draft_sha256: str
    executed_at: Optional[datetime] = None

    instrument_reference: Optional[str] = None

    quantity: Optional[Decimal] = None
    unit_price: Optional[Decimal] = None
    trade_currency: Optional[Currency] = None

    cash_amount: Optional[Decimal] = None
    cash_currency: Optional[Currency] = None

    from_currency: Optional[Currency] = None
    from_amount: Optional[Decimal] = None
    to_currency: Optional[Currency] = None
    to_amount: Optional[Decimal] = None

    def __post_init__(self) -> None:
        target_assessment = _validate_draft_fields(
            assessment_batch=self.assessment_batch,
            record_ordinal=self.record_ordinal,
            transaction_type=self.transaction_type,
            effective_date=self.effective_date,
            executed_at=self.executed_at,
            instrument_reference=self.instrument_reference,
            quantity=self.quantity,
            unit_price=self.unit_price,
            trade_currency=self.trade_currency,
            cash_amount=self.cash_amount,
            cash_currency=self.cash_currency,
            from_currency=self.from_currency,
            from_amount=self.from_amount,
            to_currency=self.to_currency,
            to_amount=self.to_amount,
        )

        # Digest validation
        if isinstance(self.draft_sha256, bool) or not isinstance(self.draft_sha256, str):
            raise PortfolioImportDraftError(
                f"draft_sha256 must be a str instance, got {type(self.draft_sha256).__name__}"
            )
        if not _SHA256_HEX_PATTERN.fullmatch(self.draft_sha256):
            raise PortfolioImportDraftError(
                f"draft_sha256 must be a 64-character lowercase hex string, got {self.draft_sha256!r}"
            )

        expected_sha = _compute_draft_sha256(
            assessment_batch=self.assessment_batch,
            record_ordinal=self.record_ordinal,
            target_assessment=target_assessment,
            transaction_type=self.transaction_type,
            effective_date=self.effective_date,
            executed_at=self.executed_at,
            instrument_reference=self.instrument_reference,
            quantity=self.quantity,
            unit_price=self.unit_price,
            trade_currency=self.trade_currency,
            cash_amount=self.cash_amount,
            cash_currency=self.cash_currency,
            from_currency=self.from_currency,
            from_amount=self.from_amount,
            to_currency=self.to_currency,
            to_amount=self.to_amount,
        )
        if self.draft_sha256 != expected_sha:
            raise PortfolioImportDraftError(
                f"draft_sha256 digest mismatch: computed {expected_sha}, declared {self.draft_sha256}"
            )

    @property
    def assessment(self) -> ImportRecordAssessment:
        """Derived authoritative record assessment from assessment_batch and record_ordinal."""
        return self.assessment_batch.assessments[self.record_ordinal - 1]

    @property
    def draft_identity(self) -> Tuple[str, int, str]:
        """Immutable staging draft identity tuple: (assessment_manifest_sha256, record_ordinal, draft_sha256)."""
        return (
            self.assessment_batch.assessment_manifest_sha256,
            self.record_ordinal,
            self.draft_sha256,
        )


def build_import_transaction_draft(
    assessment_batch: ImportAssessmentBatch,
    record_ordinal: int,
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
) -> ImportTransactionDraft:
    """
    Constructs an immutable ImportTransactionDraft, validating typed economics and computing draft SHA.
    No automatic field inference or string interpretation is performed.
    """
    target_assessment = _validate_draft_fields(
        assessment_batch=assessment_batch,
        record_ordinal=record_ordinal,
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

    # Compute hash
    computed_sha = _compute_draft_sha256(
        assessment_batch=assessment_batch,
        record_ordinal=record_ordinal,
        target_assessment=target_assessment,
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

    return ImportTransactionDraft(
        assessment_batch=assessment_batch,
        record_ordinal=record_ordinal,
        transaction_type=transaction_type,
        effective_date=effective_date,
        draft_sha256=computed_sha,
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
