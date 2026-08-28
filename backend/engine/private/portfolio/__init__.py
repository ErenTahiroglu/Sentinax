"""
backend/engine/private/portfolio/__init__.py
=============================================
Private Portfolio Accounting & Immutable Ledger Package.

Pure domain models, in-memory ledger services, and strict persistence codecs for the Personal Investment Decision Engine.
"""

from backend.engine.private.portfolio.ledger import (
    AppendResult,
    AppendStatus,
    PortfolioLedger,
    PortfolioLedgerValidator,
)
from backend.engine.private.portfolio.models import (
    CashBucket,
    InvestmentGoal,
    PlannedContribution,
    Portfolio,
    PortfolioAccount,
    PortfolioTransaction,
    PositionLot,
)
from backend.engine.private.portfolio.persistence import (
    hydrate_cash_bucket,
    hydrate_investment_goal,
    hydrate_planned_contribution,
    hydrate_portfolio,
    hydrate_portfolio_account,
    hydrate_portfolio_transaction,
    serialize_cash_bucket,
    serialize_investment_goal,
    serialize_planned_contribution,
    serialize_portfolio,
    serialize_portfolio_account,
    serialize_portfolio_transaction,
)
from backend.engine.private.portfolio.postgrest_transport import (
    ALL_SEVEN_FINANCIAL_NUMERIC_COLUMNS,
    CASH_BUCKET_SELECT,
    FINANCIAL_NUMERIC_COLUMNS_BY_TABLE,
    INVESTMENT_GOAL_SELECT,
    PLANNED_CONTRIBUTION_SELECT,
    PORTFOLIO_ACCOUNT_SELECT,
    PORTFOLIO_SELECT,
    PORTFOLIO_TRANSACTION_SELECT,
)
from backend.engine.private.portfolio.normalization import (
    normalize_external_reference,
    normalize_external_source,
)
from backend.engine.private.portfolio.accounting import (
    PortfolioAccountingError,
    PortfolioAccountingSnapshot,
    build_portfolio_accounting_snapshot,
)
from backend.engine.private.portfolio.accounting_service import (
    PortfolioAccountingQueryError,
    PortfolioAccountingQueryService,
)
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
from backend.engine.private.portfolio.parsers import (
    SentinaxCanonicalCsvError,
    SentinaxCanonicalCsvParserV1,
)
from backend.engine.private.portfolio.import_assessment import (
    ImportAssessmentBatch,
    ImportAssessmentDiagnostic,
    ImportAssessmentStatus,
    ImportRecordAssessment,
    PortfolioImportAssessmentError,
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
    PortfolioImportDraftBatchError,
    build_import_draft_batch_manifest,
)
from backend.engine.private.portfolio.import_instrument_resolution import (
    ImportInstrumentResolution,
    ImportInstrumentResolutionBatch,
    ImportInstrumentResolutionDiagnostic,
    ImportInstrumentResolutionStatus,
    PortfolioImportInstrumentResolutionError,
    build_import_instrument_resolution,
    build_import_instrument_resolution_batch,
)
from backend.engine.private.portfolio.import_instrument_resolver import (
    PortfolioImportInstrumentResolver,
    PortfolioImportInstrumentResolverError,
    resolve_import_draft_batch_instruments,
)
from backend.engine.private.portfolio.parsers.sentinax_csv_semantics import (
    SentinaxCanonicalCsvSemanticError,
    SentinaxCanonicalCsvSemanticInterpreterV1,
)
from backend.engine.private.portfolio.sentinax_csv_import import (
    run_sentinax_canonical_csv_import_v1,
)
from backend.engine.private.portfolio.import_materialization import (
    ImportLedgerMaterializationBatch,
    ImportLedgerTransactionPlan,
    PortfolioImportMaterializationError,
    build_import_ledger_materialization_batch,
    build_import_ledger_transaction_plan,
)
from backend.engine.private.portfolio.import_commit import (
    ImportLedgerBindingBatch,
    ImportLedgerBindingIntent,
    PortfolioImportCommitError,
    build_import_ledger_binding_batch,
    build_import_ledger_binding_intent,
)
from backend.engine.private.portfolio.cash import (
    CashBalanceProjection,
    CashBalanceState,
    CashProjectionError,
    build_cash_balance_projection,
)
from backend.engine.private.portfolio.positions import (
    PositionProjectionError,
    PositionQuantityProjection,
    PositionQuantityState,
    build_position_quantity_projection,
)
from backend.engine.private.portfolio.projection import (
    LedgerProjectionView,
    PortfolioProjectionError,
    ProjectedTransactionState,
    build_ledger_projection_view,
)
from backend.engine.private.portfolio.repository import PortfolioRepository

__all__ = [
    "Portfolio",
    "PortfolioAccount",
    "PortfolioTransaction",
    "CashBucket",
    "InvestmentGoal",
    "PlannedContribution",
    "PositionLot",
    "PortfolioLedger",
    "PortfolioLedgerValidator",
    "PortfolioRepository",
    "normalize_external_source",
    "normalize_external_reference",
    "AppendResult",
    "AppendStatus",
    "LedgerProjectionView",
    "ProjectedTransactionState",
    "PortfolioProjectionError",
    "build_ledger_projection_view",
    "PositionProjectionError",
    "PositionQuantityState",
    "PositionQuantityProjection",
    "build_position_quantity_projection",
    "CashProjectionError",
    "CashBalanceState",
    "CashBalanceProjection",
    "build_cash_balance_projection",
    "PortfolioAccountingError",
    "PortfolioAccountingSnapshot",
    "build_portfolio_accounting_snapshot",
    "PortfolioAccountingQueryError",
    "PortfolioAccountingQueryService",
    "PortfolioImportProvenanceError",
    "ImportFileProvenance",
    "ImportRecordProvenance",
    "build_import_file_provenance",
    "build_import_record_provenance",
    "PortfolioImportBatchError",
    "ImportBatchManifest",
    "build_import_batch_manifest",
    "PortfolioImportParsingError",
    "ImportParsedField",
    "ParsedImportRecord",
    "build_parsed_import_record",
    "PortfolioParsedImportBatchError",
    "ParsedImportBatchManifest",
    "build_parsed_import_batch_manifest",
    "PortfolioImportPipelineError",
    "PortfolioImportSourceParser",
    "ExtractedImportRecord",
    "ImportStagingResult",
    "build_import_staging_result",
    "SentinaxCanonicalCsvError",
    "SentinaxCanonicalCsvParserV1",
    "PortfolioImportAssessmentError",
    "ImportAssessmentStatus",
    "ImportAssessmentDiagnostic",
    "ImportRecordAssessment",
    "ImportAssessmentBatch",
    "build_import_record_assessment",
    "build_import_assessment_batch",
    "PortfolioImportDraftError",
    "ImportTransactionDraft",
    "build_import_transaction_draft",
    "PortfolioImportDraftBatchError",
    "ImportDraftBatchManifest",
    "build_import_draft_batch_manifest",
    "PortfolioImportInstrumentResolutionError",
    "ImportInstrumentResolutionStatus",
    "ImportInstrumentResolutionDiagnostic",
    "ImportInstrumentResolution",
    "ImportInstrumentResolutionBatch",
    "build_import_instrument_resolution",
    "build_import_instrument_resolution_batch",
    "PortfolioImportInstrumentResolverError",
    "PortfolioImportInstrumentResolver",
    "resolve_import_draft_batch_instruments",
    "SentinaxCanonicalCsvSemanticError",
    "SentinaxCanonicalCsvSemanticInterpreterV1",
    "run_sentinax_canonical_csv_import_v1",
    "PortfolioImportMaterializationError",
    "ImportLedgerTransactionPlan",
    "ImportLedgerMaterializationBatch",
    "build_import_ledger_transaction_plan",
    "build_import_ledger_materialization_batch",
    "PortfolioImportCommitError",
    "ImportLedgerBindingIntent",
    "ImportLedgerBindingBatch",
    "build_import_ledger_binding_intent",
    "build_import_ledger_binding_batch",
    "serialize_portfolio",
    "hydrate_portfolio",
    "serialize_portfolio_account",
    "hydrate_portfolio_account",
    "serialize_cash_bucket",
    "hydrate_cash_bucket",
    "serialize_investment_goal",
    "hydrate_investment_goal",
    "serialize_planned_contribution",
    "hydrate_planned_contribution",
    "serialize_portfolio_transaction",
    "hydrate_portfolio_transaction",
    "PORTFOLIO_SELECT",
    "PORTFOLIO_ACCOUNT_SELECT",
    "CASH_BUCKET_SELECT",
    "INVESTMENT_GOAL_SELECT",
    "PLANNED_CONTRIBUTION_SELECT",
    "PORTFOLIO_TRANSACTION_SELECT",
    "FINANCIAL_NUMERIC_COLUMNS_BY_TABLE",
    "ALL_SEVEN_FINANCIAL_NUMERIC_COLUMNS",
]
