"""
Sentinax Canonical CSV v1 end-to-end pre-ledger import orchestration.
Composes raw bytes -> staging/parser -> semantic interpreter -> PIT instrument resolver.
"""

from datetime import datetime
from uuid import UUID

from .import_pipeline import build_import_staging_result
from .parsers.sentinax_csv import SentinaxCanonicalCsvParserV1
from .parsers.sentinax_csv_semantics import SentinaxCanonicalCsvSemanticInterpreterV1
from .import_instrument_resolver import (
    PortfolioImportInstrumentResolver,
    resolve_import_draft_batch_instruments,
)
from .import_instrument_resolution import ImportInstrumentResolutionBatch


def run_sentinax_canonical_csv_import_v1(
    *,
    portfolio_id: UUID,
    account_id: UUID,
    filename: str,
    content: bytes,
    imported_at: datetime,
    resolver: PortfolioImportInstrumentResolver,
) -> ImportInstrumentResolutionBatch:
    """
    Orchestrates end-to-end pre-ledger import for Sentinax Canonical CSV v1.

    Args:
        portfolio_id: Target portfolio UUID.
        account_id: Target account UUID within portfolio.
        filename: Original file name string.
        content: Raw immutable CSV bytes.
        imported_at: Explicit timezone-aware import timestamp.
        resolver: Source-neutral PIT instrument resolver.

    Returns:
        Authoritative immutable ImportInstrumentResolutionBatch.
    """
    parser = SentinaxCanonicalCsvParserV1()

    staging = build_import_staging_result(
        portfolio_id=portfolio_id,
        account_id=account_id,
        filename=filename,
        content=content,
        imported_at=imported_at,
        parser=parser,
    )

    semantic_interpreter = SentinaxCanonicalCsvSemanticInterpreterV1()

    draft_manifest = semantic_interpreter.interpret(
        staging.parsed_manifest
    )

    resolution_batch = resolve_import_draft_batch_instruments(
        draft_manifest=draft_manifest,
        resolver=resolver,
    )

    return resolution_batch
