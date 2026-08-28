"""
Immutable ledger-materialization plan contract and full resolution-batch eligibility bridge (Phase 13N).

This module defines the pure domain bridge from an immutable ImportInstrumentResolutionBatch (Phase 13J/K)
to an immutable ImportLedgerMaterializationBatch.

Key Architectural Guarantees:
1. Pure Pre-Ledger Boundary: Zero ledger transaction construction, zero UUID generation,
   zero system clock calls, zero ledger identity derivation, and zero repository persistence.
2. Full-Batch Fail-Closed Gate: A batch is materializable if and only if all resolutions are in
   RESOLVED or NOT_REQUIRED states. Any UNRESOLVED or AMBIGUOUS resolution fails the entire batch closed.
3. Exact Immutable Copying: The plan freezes the exact economic and target fields derived from
   the authoritative draft and resolution hierarchy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import re
from typing import Any, Optional, Sequence, Tuple
from uuid import UUID

from backend.engine.private.domain import Currency, TransactionType
from backend.engine.private.portfolio.import_instrument_resolution import (
    ImportInstrumentResolution,
    ImportInstrumentResolutionBatch,
    ImportInstrumentResolutionStatus,
)

_SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class PortfolioImportMaterializationError(ValueError):
    """Raised when ledger-materialization plan or batch validation fails closed."""
    pass


def _canonical_decimal_str(d: Optional[Decimal]) -> Optional[str]:
    """
    Renders a finite Decimal in canonical text form for plan fingerprinting.
    Numerically equivalent Decimals produce identical text.
    """
    if d is None:
        return None
    if isinstance(d, bool) or not isinstance(d, Decimal):
        raise PortfolioImportMaterializationError(f"Expected Decimal, got {type(d).__name__}: {d!r}")
    if not d.is_finite():
        raise PortfolioImportMaterializationError(f"Expected finite Decimal, got: {d}")

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
        raise PortfolioImportMaterializationError(f"Expected datetime instance, got {type(dt).__name__}: {dt!r}")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise PortfolioImportMaterializationError(f"Datetime must be timezone-aware with non-None utcoffset, got: {dt}")
    utc_dt = dt.astimezone(timezone.utc)
    return utc_dt.isoformat()


def _decimal_storage_equal(
    actual: Any,
    expected: Optional[Decimal],
) -> bool:
    """
    Returns True iff actual and expected have identical Decimal storage representation.
    Fails closed on non-Decimal types (int, float, str, bool) and None mismatches.
    """
    if actual is None or expected is None:
        return actual is None and expected is None
    if isinstance(actual, bool) or type(actual) is not Decimal:
        return False
    if isinstance(expected, bool) or type(expected) is not Decimal:
        return False
    return actual.as_tuple() == expected.as_tuple()


def _datetime_storage_equal(
    actual: Any,
    expected: Optional[datetime],
) -> bool:
    """
    Returns True iff actual and expected have identical timezone-aware datetime representation.
    Fails closed on non-datetime types (date, str, bool) and None mismatches.
    """
    if actual is None or expected is None:
        return actual is None and expected is None
    if isinstance(actual, bool) or type(actual) is not datetime:
        return False
    if isinstance(expected, bool) or type(expected) is not datetime:
        return False
    if actual.tzinfo is None or actual.tzinfo.utcoffset(actual) is None:
        return False
    if expected.tzinfo is None or expected.tzinfo.utcoffset(expected) is None:
        return False
    return actual.isoformat() == expected.isoformat()


def _compute_plan_sha256(
    resolution: ImportInstrumentResolution,
    portfolio_id: UUID,
    account_id: UUID,
    transaction_type: TransactionType,
    effective_date: date,
    executed_at: Optional[datetime],
    instrument_id: Optional[UUID],
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
    Computes deterministic SHA-256 hex digest for an import ledger transaction plan.
    """
    preimage = [
        resolution.resolution_sha256,
        str(portfolio_id),
        str(account_id),
        transaction_type.value,
        effective_date.isoformat(),
        _canonical_datetime_str(executed_at),
        str(instrument_id) if instrument_id is not None else None,
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
class ImportLedgerTransactionPlan:
    """
    Immutable, verified pre-ledger transaction plan bound to an authoritative ImportInstrumentResolution.
    """
    resolution: ImportInstrumentResolution

    portfolio_id: UUID
    account_id: UUID
    transaction_type: TransactionType
    effective_date: date
    executed_at: Optional[datetime]

    instrument_id: Optional[UUID]

    quantity: Optional[Decimal]
    unit_price: Optional[Decimal]
    trade_currency: Optional[Currency]

    cash_amount: Optional[Decimal]
    cash_currency: Optional[Currency]

    from_currency: Optional[Currency]
    from_amount: Optional[Decimal]
    to_currency: Optional[Currency]
    to_amount: Optional[Decimal]

    plan_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.resolution, ImportInstrumentResolution):
            raise PortfolioImportMaterializationError(
                f"resolution must be an ImportInstrumentResolution instance, got {type(self.resolution).__name__}"
            )

        if self.resolution.status not in (
            ImportInstrumentResolutionStatus.RESOLVED,
            ImportInstrumentResolutionStatus.NOT_REQUIRED,
        ):
            raise PortfolioImportMaterializationError(
                f"Resolution status {self.resolution.status.value} is not eligible for ledger materialization"
            )

        draft = self.resolution.draft
        file_id = draft.assessment.parsed_record.record_provenance.file_identity
        expected_port_id = file_id[0]
        expected_acc_id = file_id[1]

        # Target ID verification
        if self.portfolio_id != expected_port_id:
            raise PortfolioImportMaterializationError(
                f"portfolio_id {self.portfolio_id} does not match resolution file provenance {expected_port_id}"
            )
        if self.account_id != expected_acc_id:
            raise PortfolioImportMaterializationError(
                f"account_id {self.account_id} does not match resolution file provenance {expected_acc_id}"
            )

        # Transaction type, effective date, executed_at verification
        if self.transaction_type != draft.transaction_type:
            raise PortfolioImportMaterializationError(
                f"transaction_type {self.transaction_type} does not match draft {draft.transaction_type}"
            )
        if self.effective_date != draft.effective_date:
            raise PortfolioImportMaterializationError(
                f"effective_date {self.effective_date} does not match draft {draft.effective_date}"
            )
        if not _datetime_storage_equal(self.executed_at, draft.executed_at):
            raise PortfolioImportMaterializationError(
                f"executed_at {self.executed_at!r} does not match draft {draft.executed_at!r}"
            )

        # Instrument ID verification
        if self.resolution.status == ImportInstrumentResolutionStatus.RESOLVED:
            if self.instrument_id != self.resolution.instrument_id:
                raise PortfolioImportMaterializationError(
                    f"instrument_id {self.instrument_id} does not match RESOLVED resolution {self.resolution.instrument_id}"
                )
        else:
            if self.instrument_id is not None:
                raise PortfolioImportMaterializationError(
                    f"instrument_id must be None for NOT_REQUIRED resolution, got {self.instrument_id}"
                )

        # Economics verification
        if not _decimal_storage_equal(self.quantity, draft.quantity):
            raise PortfolioImportMaterializationError(f"quantity {self.quantity!r} does not match draft {draft.quantity!r}")
        if not _decimal_storage_equal(self.unit_price, draft.unit_price):
            raise PortfolioImportMaterializationError(f"unit_price {self.unit_price!r} does not match draft {draft.unit_price!r}")
        if self.trade_currency != draft.trade_currency:
            raise PortfolioImportMaterializationError(f"trade_currency {self.trade_currency} does not match draft {draft.trade_currency}")
        if not _decimal_storage_equal(self.cash_amount, draft.cash_amount):
            raise PortfolioImportMaterializationError(f"cash_amount {self.cash_amount!r} does not match draft {draft.cash_amount!r}")
        if self.cash_currency != draft.cash_currency:
            raise PortfolioImportMaterializationError(f"cash_currency {self.cash_currency} does not match draft {draft.cash_currency}")
        if self.from_currency != draft.from_currency:
            raise PortfolioImportMaterializationError(f"from_currency {self.from_currency} does not match draft {draft.from_currency}")
        if not _decimal_storage_equal(self.from_amount, draft.from_amount):
            raise PortfolioImportMaterializationError(f"from_amount {self.from_amount!r} does not match draft {draft.from_amount!r}")
        if self.to_currency != draft.to_currency:
            raise PortfolioImportMaterializationError(f"to_currency {self.to_currency} does not match draft {draft.to_currency}")
        if not _decimal_storage_equal(self.to_amount, draft.to_amount):
            raise PortfolioImportMaterializationError(f"to_amount {self.to_amount!r} does not match draft {draft.to_amount!r}")

        # Plan SHA verification
        if isinstance(self.plan_sha256, bool) or not isinstance(self.plan_sha256, str):
            raise PortfolioImportMaterializationError(
                f"plan_sha256 must be a str instance, got {type(self.plan_sha256).__name__}"
            )
        if not _SHA256_HEX_PATTERN.fullmatch(self.plan_sha256):
            raise PortfolioImportMaterializationError(
                f"plan_sha256 must be a 64-character lowercase hex string, got {self.plan_sha256!r}"
            )

        expected_sha = _compute_plan_sha256(
            resolution=self.resolution,
            portfolio_id=self.portfolio_id,
            account_id=self.account_id,
            transaction_type=self.transaction_type,
            effective_date=self.effective_date,
            executed_at=self.executed_at,
            instrument_id=self.instrument_id,
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
        if self.plan_sha256 != expected_sha:
            raise PortfolioImportMaterializationError(
                f"plan_sha256 digest mismatch: computed {expected_sha}, declared {self.plan_sha256}"
            )

    @property
    def record_ordinal(self) -> int:
        """Derived record ordinal from underlying resolution."""
        return self.resolution.draft.record_ordinal

    @property
    def plan_identity(self) -> Tuple[Any, ...]:
        """
        Immutable composite staging identity extending resolution_identity:
        (*resolution.resolution_identity, plan_sha256)
        """
        return (
            *self.resolution.resolution_identity,
            self.plan_sha256,
        )


def build_import_ledger_transaction_plan(
    resolution: ImportInstrumentResolution,
) -> ImportLedgerTransactionPlan:
    """
    Constructs an immutable ImportLedgerTransactionPlan from a valid, materializable resolution outcome.

    Args:
        resolution: Authoritative ImportInstrumentResolution (must be in RESOLVED or NOT_REQUIRED state).

    Returns:
        Verified ImportLedgerTransactionPlan.

    Raises:
        PortfolioImportMaterializationError: If resolution is not eligible (UNRESOLVED, AMBIGUOUS) or malformed.
    """
    if not isinstance(resolution, ImportInstrumentResolution):
        raise PortfolioImportMaterializationError(
            f"resolution must be an ImportInstrumentResolution instance, got {type(resolution).__name__}"
        )

    if resolution.status not in (
        ImportInstrumentResolutionStatus.RESOLVED,
        ImportInstrumentResolutionStatus.NOT_REQUIRED,
    ):
        raise PortfolioImportMaterializationError(
            f"Resolution status {resolution.status.value} is not eligible for ledger materialization"
        )

    draft = resolution.draft
    file_id = draft.assessment.parsed_record.record_provenance.file_identity
    portfolio_id = file_id[0]
    account_id = file_id[1]

    instrument_id: Optional[UUID] = (
        resolution.instrument_id
        if resolution.status == ImportInstrumentResolutionStatus.RESOLVED
        else None
    )

    plan_sha = _compute_plan_sha256(
        resolution=resolution,
        portfolio_id=portfolio_id,
        account_id=account_id,
        transaction_type=draft.transaction_type,
        effective_date=draft.effective_date,
        executed_at=draft.executed_at,
        instrument_id=instrument_id,
        quantity=draft.quantity,
        unit_price=draft.unit_price,
        trade_currency=draft.trade_currency,
        cash_amount=draft.cash_amount,
        cash_currency=draft.cash_currency,
        from_currency=draft.from_currency,
        from_amount=draft.from_amount,
        to_currency=draft.to_currency,
        to_amount=draft.to_amount,
    )

    return ImportLedgerTransactionPlan(
        resolution=resolution,
        portfolio_id=portfolio_id,
        account_id=account_id,
        transaction_type=draft.transaction_type,
        effective_date=draft.effective_date,
        executed_at=draft.executed_at,
        instrument_id=instrument_id,
        quantity=draft.quantity,
        unit_price=draft.unit_price,
        trade_currency=draft.trade_currency,
        cash_amount=draft.cash_amount,
        cash_currency=draft.cash_currency,
        from_currency=draft.from_currency,
        from_amount=draft.from_amount,
        to_currency=draft.to_currency,
        to_amount=draft.to_amount,
        plan_sha256=plan_sha,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Batch Manifest Layer
# ─────────────────────────────────────────────────────────────────────────────

def _compute_materialization_manifest_sha256(
    resolution_batch: ImportInstrumentResolutionBatch,
    plans: Tuple[ImportLedgerTransactionPlan, ...],
) -> str:
    """
    Computes deterministic SHA-256 hex digest for the materialization batch manifest preimage:
    [
      resolution_batch.resolution_manifest_sha256,
      [
        [record_ordinal, resolution_sha256, plan_sha256],
        ...
      ]
    ]
    Sorted by record_ordinal ascending.
    """
    preimage = [
        resolution_batch.resolution_manifest_sha256,
        [
            [
                p.record_ordinal,
                p.resolution.resolution_sha256,
                p.plan_sha256,
            ]
            for p in plans
        ],
    ]
    encoded_json = json.dumps(preimage, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ImportLedgerMaterializationBatch:
    """
    Immutable batch manifest proving complete, verified pre-ledger materialization plan coverage
    for all resolution outcomes in an ImportInstrumentResolutionBatch.
    """
    resolution_batch: ImportInstrumentResolutionBatch
    plans: Tuple[ImportLedgerTransactionPlan, ...]
    materialization_manifest_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.resolution_batch, ImportInstrumentResolutionBatch):
            raise PortfolioImportMaterializationError(
                f"resolution_batch must be an ImportInstrumentResolutionBatch instance, got {type(self.resolution_batch).__name__}"
            )

        if not self.resolution_batch.is_fully_resolved:
            raise PortfolioImportMaterializationError(
                f"resolution_batch is not fully resolved (unresolved={self.resolution_batch.unresolved_count}, ambiguous={self.resolution_batch.ambiguous_count})"
            )

        if type(self.plans) is not tuple:
            raise PortfolioImportMaterializationError(
                f"plans must be an immutable tuple, got {type(self.plans).__name__}"
            )

        if len(self.plans) != self.resolution_batch.resolution_count:
            raise PortfolioImportMaterializationError(
                f"plans count {len(self.plans)} does not equal resolution_count {self.resolution_batch.resolution_count}"
            )

        prev_ordinal: int = 0
        for idx, plan in enumerate(self.plans):
            if not isinstance(plan, ImportLedgerTransactionPlan):
                raise PortfolioImportMaterializationError(
                    f"plans[{idx}] must be an ImportLedgerTransactionPlan instance, got {type(plan).__name__}"
                )

            ordinal = plan.record_ordinal
            if ordinal <= prev_ordinal:
                raise PortfolioImportMaterializationError(
                    f"plans tuple is not sorted by record_ordinal ascending at index {idx} (ordinal {ordinal} after {prev_ordinal})"
                )
            prev_ordinal = ordinal

            expected_resolution = self.resolution_batch.resolutions[idx]
            if plan.resolution != expected_resolution:
                raise PortfolioImportMaterializationError(
                    f"Plan at index {idx} (ordinal {ordinal}) is not semantically bound to "
                    f"resolution_batch.resolutions[{idx}] (ordinal {expected_resolution.draft.record_ordinal})"
                )

        if isinstance(self.materialization_manifest_sha256, bool) or not isinstance(self.materialization_manifest_sha256, str):
            raise PortfolioImportMaterializationError(
                f"materialization_manifest_sha256 must be a str instance, got {type(self.materialization_manifest_sha256).__name__}"
            )
        if not _SHA256_HEX_PATTERN.fullmatch(self.materialization_manifest_sha256):
            raise PortfolioImportMaterializationError(
                f"materialization_manifest_sha256 must be a 64-character lowercase hex string, got {self.materialization_manifest_sha256!r}"
            )

        expected_sha = _compute_materialization_manifest_sha256(
            resolution_batch=self.resolution_batch,
            plans=self.plans,
        )
        if self.materialization_manifest_sha256 != expected_sha:
            raise PortfolioImportMaterializationError(
                f"materialization_manifest_sha256 digest mismatch: computed {expected_sha}, declared {self.materialization_manifest_sha256}"
            )

    @property
    def plan_count(self) -> int:
        """Total count of ledger-materialization plans (equals resolution_batch.resolution_count)."""
        return len(self.plans)

    @property
    def materialization_manifest_identity(self) -> Tuple[Any, ...]:
        """
        Immutable composite staging identity extending resolution_manifest_identity:
        (*resolution_batch.resolution_manifest_identity, materialization_manifest_sha256)
        """
        return (
            *self.resolution_batch.resolution_manifest_identity,
            self.materialization_manifest_sha256,
        )


def build_import_ledger_materialization_batch(
    resolution_batch: ImportInstrumentResolutionBatch,
) -> ImportLedgerMaterializationBatch:
    """
    Constructs an immutable ImportLedgerMaterializationBatch from a fully-resolved resolution batch.

    Args:
        resolution_batch: Verified ImportInstrumentResolutionBatch (must satisfy is_fully_resolved is True).

    Returns:
        Verified ImportLedgerMaterializationBatch.

    Raises:
        PortfolioImportMaterializationError: If resolution batch contains UNRESOLVED/AMBIGUOUS outcomes or is malformed.
    """
    if not isinstance(resolution_batch, ImportInstrumentResolutionBatch):
        raise PortfolioImportMaterializationError(
            f"resolution_batch must be an ImportInstrumentResolutionBatch instance, got {type(resolution_batch).__name__}"
        )

    if not resolution_batch.is_fully_resolved:
        raise PortfolioImportMaterializationError(
            f"Cannot materialize resolution batch: unresolved_count={resolution_batch.unresolved_count}, ambiguous_count={resolution_batch.ambiguous_count}"
        )

    plans: list[ImportLedgerTransactionPlan] = []
    for res in resolution_batch.resolutions:
        plan = build_import_ledger_transaction_plan(res)
        plans.append(plan)

    plans_tuple = tuple(sorted(plans, key=lambda p: p.record_ordinal))

    manifest_sha = _compute_materialization_manifest_sha256(
        resolution_batch=resolution_batch,
        plans=plans_tuple,
    )

    return ImportLedgerMaterializationBatch(
        resolution_batch=resolution_batch,
        plans=plans_tuple,
        materialization_manifest_sha256=manifest_sha,
    )
