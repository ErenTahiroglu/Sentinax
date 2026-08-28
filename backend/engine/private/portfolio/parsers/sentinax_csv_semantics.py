"""
backend/engine/private/portfolio/parsers/sentinax_csv_semantics.py
=================================================================
Sentinax Canonical CSV v1 — First Real Semantic Interpreter (Phase 13L).

This module implements the source-specific semantic interpretation layer for Sentinax Canonical CSV v1:
Input:  ParsedImportBatchManifest (from Phase 13F/13C parsing)
Output: ImportDraftBatchManifest (Phase 13I, embedding Phase 13G assessments and Phase 13H economic drafts)

Key Architectural Invariants:
1. Exact 13-Field Canonical Schema:
   - transaction_type, effective_date, executed_at, instrument_reference, quantity, unit_price,
     trade_currency, cash_amount, cash_currency, from_currency, from_amount, to_currency, to_amount.
   - Any missing or extraneous field triggers a format-level SentinaxCanonicalCsvSemanticError.
2. Explicit Empty String to None Mapping:
   - Empty text ("") in optional fields maps to None.
   - Non-empty text is preserved verbatim without stripping.
3. Strict Lexical Parsing:
   - transaction_type: exact lowercase enum name matching supported set (no reversal).
   - effective_date: exact YYYY-MM-DD round-trip valid calendar date.
   - executed_at: timezone-aware ISO-8601 with explicit ±HH:MM offset.
   - Decimal: (?:0|[1-9][0-9]*)(?:\\.[0-9]+)? without scientific notation/thousands separators.
   - Currency: exact TRY, USD, EUR, GBP, XAU, XAG.
4. Two-Pass Assessment & Economic Contract Authority:
   - Pass 1: Lexical field validation -> provisional ImportAssessmentBatch.
   - Pass 2: Economic field-family validation via build_import_transaction_draft.
   - Failures become REJECTED assessments with deterministic diagnostics.
   - Successful rows become READY and are materialized as typed economic drafts bound to the
     FINAL authoritative ImportAssessmentBatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import re
from typing import Dict, List, Optional, Sequence, Set, Tuple

from backend.engine.private.domain import Currency, TransactionType
from backend.engine.private.portfolio.import_assessment import (
    ImportAssessmentBatch,
    ImportAssessmentDiagnostic,
    ImportAssessmentStatus,
    build_import_assessment_batch,
    build_import_record_assessment,
)
from backend.engine.private.portfolio.import_draft import (
    ImportTransactionDraft,
    PortfolioImportDraftError,
    build_import_transaction_draft,
)
from backend.engine.private.portfolio.import_draft_batch import (
    ImportDraftBatchManifest,
    build_import_draft_batch_manifest,
)
from backend.engine.private.portfolio.import_parsed_batch import ParsedImportBatchManifest
from backend.engine.private.portfolio.import_parsing import ParsedImportRecord


_CANONICAL_FIELD_KEYS = frozenset([
    "transaction_type",
    "effective_date",
    "executed_at",
    "instrument_reference",
    "quantity",
    "unit_price",
    "trade_currency",
    "cash_amount",
    "cash_currency",
    "from_currency",
    "from_amount",
    "to_currency",
    "to_amount",
])

_SUPPORTED_TRANSACTION_TYPES: Dict[str, TransactionType] = {
    "buy": TransactionType.BUY,
    "sell": TransactionType.SELL,
    "cash_deposit": TransactionType.CASH_DEPOSIT,
    "cash_withdrawal": TransactionType.CASH_WITHDRAWAL,
    "dividend": TransactionType.DIVIDEND,
    "interest": TransactionType.INTEREST,
    "fee": TransactionType.FEE,
    "tax_withholding": TransactionType.TAX_WITHHOLDING,
    "fx_conversion": TransactionType.FX_CONVERSION,
}

_EFFECTIVE_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EXECUTED_AT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:[+-]\d{2}:\d{2})$")
_DECIMAL_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")

_DIAG_CODE_INVALID_TRANSACTION_TYPE = "invalid_transaction_type"
_DIAG_MSG_INVALID_TRANSACTION_TYPE = (
    "Transaction type is not supported by Sentinax Canonical CSV semantic revision 1."
)

_DIAG_CODE_INVALID_EFFECTIVE_DATE = "invalid_effective_date"
_DIAG_MSG_INVALID_EFFECTIVE_DATE = (
    "Effective date must be a canonical YYYY-MM-DD calendar date."
)

_DIAG_CODE_INVALID_EXECUTED_AT = "invalid_executed_at"
_DIAG_MSG_INVALID_EXECUTED_AT = (
    "Executed-at must be empty or a canonical timezone-aware ISO-8601 datetime with explicit ±HH:MM offset."
)

_DIAG_CODE_INVALID_DECIMAL = "invalid_decimal"
_DIAG_MSG_INVALID_DECIMAL = (
    "Financial numeric text is not valid Canonical CSV decimal syntax."
)

_DIAG_CODE_INVALID_CURRENCY = "invalid_currency"
_DIAG_MSG_INVALID_CURRENCY = (
    "Currency text is not a supported canonical Currency value."
)

_DIAG_CODE_INVALID_ECONOMIC_CONTRACT = "invalid_economic_contract"
_DIAG_MSG_INVALID_ECONOMIC_CONTRACT = (
    "Parsed row violates the canonical economic transaction draft contract."
)


class SentinaxCanonicalCsvSemanticError(ValueError):
    """Raised when format-level or batch-level contract failures prevent semantic interpretation."""
    pass


@dataclass(frozen=True)
class _TypedRecordArgs:
    """Internal private container for successfully parsed row economic arguments."""
    transaction_type: TransactionType
    effective_date: date
    executed_at: Optional[datetime]
    instrument_reference: Optional[str]
    quantity: Optional[Decimal]
    unit_price: Optional[Decimal]
    trade_currency: Optional[Currency]
    cash_amount: Optional[Decimal]
    cash_currency: Optional[Currency]
    from_currency: Optional[Currency]
    from_amount: Optional[Decimal]
    to_currency: Optional[Currency]
    to_amount: Optional[Decimal]


class _FixedSemanticInterpreterMeta(type):
    """
    Metaclass enforcing class-level immutability for fixed semantic interpreter identity metadata.
    """
    def __setattr__(cls, name: str, value: Any) -> None:
        if name in ("source_key", "parser_revision", "semantic_revision"):
            raise AttributeError(f"Cannot modify immutable metadata attribute {name!r} on {cls.__name__}")
        super().__setattr__(name, value)

    def __delattr__(cls, name: str) -> None:
        if name in ("source_key", "parser_revision", "semantic_revision"):
            raise AttributeError(f"Cannot delete immutable metadata attribute {name!r} on {cls.__name__}")
        super().__delattr__(name)


class SentinaxCanonicalCsvSemanticInterpreterV1(metaclass=_FixedSemanticInterpreterMeta):
    """
    Source-specific semantic interpreter for Sentinax Canonical CSV v1.
    Stateless, deterministic converter from ParsedImportBatchManifest to ImportDraftBatchManifest.
    """
    __slots__ = ()

    source_key: str = "sentinax_csv"
    parser_revision: int = 1
    semantic_revision: int = 1

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError(f"Type {cls.__name__} cannot be subclassed")

    def interpret(
        self,
        parsed_manifest: ParsedImportBatchManifest,
    ) -> ImportDraftBatchManifest:
        """
        Interprets a parsed canonical CSV batch manifest into an economic draft batch manifest.
        """
        # 1. Validate manifest type
        if not isinstance(parsed_manifest, ParsedImportBatchManifest):
            raise SentinaxCanonicalCsvSemanticError(
                f"parsed_manifest must be a ParsedImportBatchManifest instance, got {type(parsed_manifest).__name__}"
            )

        # 2. Validate source_key and parser_revision bindings against literal constants
        source_key = parsed_manifest.raw_manifest.file_provenance.source_key
        if source_key != "sentinax_csv":
            raise SentinaxCanonicalCsvSemanticError(
                f"parsed_manifest source_key {source_key!r} does not match expected 'sentinax_csv'"
            )
        if parsed_manifest.parser_revision != 1:
            raise SentinaxCanonicalCsvSemanticError(
                f"parsed_manifest parser_revision {parsed_manifest.parser_revision} does not match expected 1"
            )

        # 3. Handle empty parsed batch
        if parsed_manifest.record_count == 0:
            empty_assessment_batch = build_import_assessment_batch(
                parsed_manifest=parsed_manifest,
                assessments=[],
            )
            return build_import_draft_batch_manifest(
                assessment_batch=empty_assessment_batch,
                drafts=[],
            )

        # 4. Validate exact 13-field schema across all rows
        for record in parsed_manifest.parsed_records:
            field_keys = {f.field_key for f in record.fields}
            if len(record.fields) != 13 or field_keys != _CANONICAL_FIELD_KEYS:
                raise SentinaxCanonicalCsvSemanticError(
                    f"Record ordinal {record.record_provenance.record_ordinal} does not have the exact 13 canonical field keys"
                )

        # 5. Pass 1: Lexical parsing & validation
        provisional_status: Dict[int, ImportAssessmentStatus] = {}
        provisional_diags: Dict[int, List[ImportAssessmentDiagnostic]] = {}
        typed_args_map: Dict[int, Optional[_TypedRecordArgs]] = {}

        for record in parsed_manifest.parsed_records:
            ordinal = record.record_provenance.record_ordinal
            raw_fields = {f.field_key: f.field_value for f in record.fields}
            diags: List[ImportAssessmentDiagnostic] = []

            # 5a. transaction_type
            raw_tx_type = raw_fields["transaction_type"]
            tx_type: Optional[TransactionType] = None
            if raw_tx_type in _SUPPORTED_TRANSACTION_TYPES:
                tx_type = _SUPPORTED_TRANSACTION_TYPES[raw_tx_type]
            else:
                diags.append(
                    ImportAssessmentDiagnostic(
                        code=_DIAG_CODE_INVALID_TRANSACTION_TYPE,
                        message=_DIAG_MSG_INVALID_TRANSACTION_TYPE,
                        field_key="transaction_type",
                    )
                )

            # 5b. effective_date
            raw_eff_date = raw_fields["effective_date"]
            eff_date: Optional[date] = None
            if _EFFECTIVE_DATE_PATTERN.fullmatch(raw_eff_date):
                try:
                    parsed_date = date.fromisoformat(raw_eff_date)
                    if parsed_date.isoformat() == raw_eff_date:
                        eff_date = parsed_date
                    else:
                        diags.append(
                            ImportAssessmentDiagnostic(
                                code=_DIAG_CODE_INVALID_EFFECTIVE_DATE,
                                message=_DIAG_MSG_INVALID_EFFECTIVE_DATE,
                                field_key="effective_date",
                            )
                        )
                except Exception:
                    diags.append(
                        ImportAssessmentDiagnostic(
                            code=_DIAG_CODE_INVALID_EFFECTIVE_DATE,
                            message=_DIAG_MSG_INVALID_EFFECTIVE_DATE,
                            field_key="effective_date",
                        )
                    )
            else:
                diags.append(
                    ImportAssessmentDiagnostic(
                        code=_DIAG_CODE_INVALID_EFFECTIVE_DATE,
                        message=_DIAG_MSG_INVALID_EFFECTIVE_DATE,
                        field_key="effective_date",
                    )
                )

            # 5c. executed_at
            raw_exec_at = raw_fields["executed_at"]
            exec_at: Optional[datetime] = None
            if raw_exec_at == "":
                exec_at = None
            elif _EXECUTED_AT_PATTERN.fullmatch(raw_exec_at):
                try:
                    parsed_dt = datetime.fromisoformat(raw_exec_at)
                    if parsed_dt.tzinfo is None or parsed_dt.utcoffset() is None:
                        diags.append(
                            ImportAssessmentDiagnostic(
                                code=_DIAG_CODE_INVALID_EXECUTED_AT,
                                message=_DIAG_MSG_INVALID_EXECUTED_AT,
                                field_key="executed_at",
                            )
                        )
                    else:
                        exec_at = parsed_dt
                except Exception:
                    diags.append(
                        ImportAssessmentDiagnostic(
                            code=_DIAG_CODE_INVALID_EXECUTED_AT,
                            message=_DIAG_MSG_INVALID_EXECUTED_AT,
                            field_key="executed_at",
                        )
                    )
            else:
                diags.append(
                    ImportAssessmentDiagnostic(
                        code=_DIAG_CODE_INVALID_EXECUTED_AT,
                        message=_DIAG_MSG_INVALID_EXECUTED_AT,
                        field_key="executed_at",
                    )
                )

            # 5d. instrument_reference
            raw_inst_ref = raw_fields["instrument_reference"]
            inst_ref: Optional[str] = raw_inst_ref if raw_inst_ref != "" else None

            # 5e. Decimal fields
            def _parse_decimal(field_name: str) -> Optional[Decimal]:
                val = raw_fields[field_name]
                if val == "":
                    return None
                if _DECIMAL_PATTERN.fullmatch(val):
                    try:
                        return Decimal(val)
                    except Exception:
                        diags.append(
                            ImportAssessmentDiagnostic(
                                code=_DIAG_CODE_INVALID_DECIMAL,
                                message=_DIAG_MSG_INVALID_DECIMAL,
                                field_key=field_name,
                            )
                        )
                        return None
                else:
                    diags.append(
                        ImportAssessmentDiagnostic(
                            code=_DIAG_CODE_INVALID_DECIMAL,
                            message=_DIAG_MSG_INVALID_DECIMAL,
                            field_key=field_name,
                        )
                    )
                    return None

            quantity = _parse_decimal("quantity")
            unit_price = _parse_decimal("unit_price")
            cash_amount = _parse_decimal("cash_amount")
            from_amount = _parse_decimal("from_amount")
            to_amount = _parse_decimal("to_amount")

            # 5f. Currency fields
            def _parse_currency(field_name: str) -> Optional[Currency]:
                val = raw_fields[field_name]
                if val == "":
                    return None
                if val in Currency.__members__:
                    return Currency[val]
                diags.append(
                    ImportAssessmentDiagnostic(
                        code=_DIAG_CODE_INVALID_CURRENCY,
                        message=_DIAG_MSG_INVALID_CURRENCY,
                        field_key=field_name,
                    )
                )
                return None

            trade_currency = _parse_currency("trade_currency")
            cash_currency = _parse_currency("cash_currency")
            from_currency = _parse_currency("from_currency")
            to_currency = _parse_currency("to_currency")

            if len(diags) > 0:
                provisional_status[ordinal] = ImportAssessmentStatus.REJECTED
                provisional_diags[ordinal] = diags
                typed_args_map[ordinal] = None
            else:
                assert tx_type is not None and eff_date is not None
                provisional_status[ordinal] = ImportAssessmentStatus.READY
                provisional_diags[ordinal] = []
                typed_args_map[ordinal] = _TypedRecordArgs(
                    transaction_type=tx_type,
                    effective_date=eff_date,
                    executed_at=exec_at,
                    instrument_reference=inst_ref,
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

        # 6. Build provisional assessment batch
        provisional_assessments = [
            build_import_record_assessment(
                parsed_record=rec,
                status=provisional_status[rec.record_provenance.record_ordinal],
                diagnostics=provisional_diags[rec.record_provenance.record_ordinal],
            )
            for rec in parsed_manifest.parsed_records
        ]
        provisional_batch = build_import_assessment_batch(
            parsed_manifest=parsed_manifest,
            assessments=provisional_assessments,
        )

        # 7. Pass 2: Economic contract validation via build_import_transaction_draft
        final_status: Dict[int, ImportAssessmentStatus] = {}
        final_diags: Dict[int, Sequence[ImportAssessmentDiagnostic]] = {}

        for rec in parsed_manifest.parsed_records:
            ordinal = rec.record_provenance.record_ordinal
            if provisional_status[ordinal] == ImportAssessmentStatus.REJECTED:
                final_status[ordinal] = ImportAssessmentStatus.REJECTED
                final_diags[ordinal] = provisional_diags[ordinal]
            else:
                args = typed_args_map[ordinal]
                assert args is not None
                try:
                    build_import_transaction_draft(
                        assessment_batch=provisional_batch,
                        record_ordinal=ordinal,
                        transaction_type=args.transaction_type,
                        effective_date=args.effective_date,
                        executed_at=args.executed_at,
                        instrument_reference=args.instrument_reference,
                        quantity=args.quantity,
                        unit_price=args.unit_price,
                        trade_currency=args.trade_currency,
                        cash_amount=args.cash_amount,
                        cash_currency=args.cash_currency,
                        from_currency=args.from_currency,
                        from_amount=args.from_amount,
                        to_currency=args.to_currency,
                        to_amount=args.to_amount,
                    )
                    final_status[ordinal] = ImportAssessmentStatus.READY
                    final_diags[ordinal] = ()
                except PortfolioImportDraftError:
                    diag = ImportAssessmentDiagnostic(
                        code=_DIAG_CODE_INVALID_ECONOMIC_CONTRACT,
                        message=_DIAG_MSG_INVALID_ECONOMIC_CONTRACT,
                        field_key=None,
                    )
                    final_status[ordinal] = ImportAssessmentStatus.REJECTED
                    final_diags[ordinal] = (diag,)

        # 8. Build final authoritative assessment batch
        final_assessments = [
            build_import_record_assessment(
                parsed_record=rec,
                status=final_status[rec.record_provenance.record_ordinal],
                diagnostics=final_diags[rec.record_provenance.record_ordinal],
            )
            for rec in parsed_manifest.parsed_records
        ]
        final_assessment_batch = build_import_assessment_batch(
            parsed_manifest=parsed_manifest,
            assessments=final_assessments,
        )

        # 9. Build final economic drafts bound to final assessment batch
        final_drafts: List[ImportTransactionDraft] = []
        for rec in parsed_manifest.parsed_records:
            ordinal = rec.record_provenance.record_ordinal
            if final_status[ordinal] == ImportAssessmentStatus.READY:
                args = typed_args_map[ordinal]
                assert args is not None
                draft = build_import_transaction_draft(
                    assessment_batch=final_assessment_batch,
                    record_ordinal=ordinal,
                    transaction_type=args.transaction_type,
                    effective_date=args.effective_date,
                    executed_at=args.executed_at,
                    instrument_reference=args.instrument_reference,
                    quantity=args.quantity,
                    unit_price=args.unit_price,
                    trade_currency=args.trade_currency,
                    cash_amount=args.cash_amount,
                    cash_currency=args.cash_currency,
                    from_currency=args.from_currency,
                    from_amount=args.from_amount,
                    to_currency=args.to_currency,
                    to_amount=args.to_amount,
                )
                final_drafts.append(draft)

        # 10. Build and return final draft batch manifest
        return build_import_draft_batch_manifest(
            assessment_batch=final_assessment_batch,
            drafts=final_drafts,
        )
