"""
backend/engine/private/providers/sec_edgar.py
==============================================
DataProviderContract Adapter for SEC EDGAR Filing & CompanyFacts Ingestion.

Core Invariants:
    - Base URL: https://data.sec.gov
    - source_role: SECURITIES_REGULATOR
    - delivery_provider: SEC EDGAR
    - SourceTier: TIER_1_REGULATORY
    - AccessStatus: GREEN
    - Historical SYSTEM_AS_OF & SOURCE_AS_OF external reconstruction rejected (fail closed).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from backend.engine.private.domain import (
    AsOfMode,
    DataStatus,
    ProviderAccessStatus,
    SourceTier,
)
from backend.engine.private.exceptions import (
    ProviderConfigurationError,
    ProviderInvalidSymbolError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderServerError,
    ProviderTimeoutError,
)
from backend.engine.private.provider_contract import (
    DataProviderContract,
    FetchContext,
    ProviderProvenance,
    ProviderResponse,
)
from backend.engine.private.sec.cik import normalize_cik
from backend.engine.private.sec.client import SECEdgarClient
from backend.engine.private.sec.company_facts import SECCompanyFactsProvider
from backend.engine.private.sec.submissions import SECSubmissionsProvider

logger = logging.getLogger(__name__)


class SECEdgarProvider(DataProviderContract):
    """
    Unified DataProviderContract adapter for SEC EDGAR Submissions and CompanyFacts.
    """
    provider_name: str = "SEC_EDGAR"
    provider_version: str = "1.0.0"
    source_quality: SourceTier = SourceTier.TIER_1_REGULATORY
    access_status: ProviderAccessStatus = ProviderAccessStatus.GREEN
    base_url: str = "https://data.sec.gov"

    def __init__(self, client: Optional[SECEdgarClient] = None) -> None:
        self.client = client or SECEdgarClient()
        self.submissions_provider = SECSubmissionsProvider(client=self.client)
        self.company_facts_provider = SECCompanyFactsProvider(client=self.client)

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        """
        Fetches SEC EDGAR data for a canonical instrument or CIK.
        """
        raw_symbol = context.provider_symbol
        if not raw_symbol:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=datetime.now(timezone.utc),
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=["Missing provider_symbol or CIK for SEC request."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        # 1. Historical AS_OF Guards (Fail Closed)
        if context.is_historical:
            if context.as_of_mode == AsOfMode.SYSTEM_AS_OF:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=datetime.now(timezone.utc),
                    published_at=None,
                    effective_date=None,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=["Historical SYSTEM_AS_OF requires local PIT storage; cannot be reconstructed via external API."],
                    canonical_instrument_id=context.canonical_instrument_id,
                    provider_symbol=context.provider_symbol,
                )
            elif context.as_of_mode == AsOfMode.SOURCE_AS_OF:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=datetime.now(timezone.utc),
                    published_at=None,
                    effective_date=None,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=["SEC EDGAR current aggregate data does not support external historical SOURCE_AS_OF reconstruction; local PIT storage required."],
                    canonical_instrument_id=context.canonical_instrument_id,
                    provider_symbol=context.provider_symbol,
                )

        try:
            canonical_cik = normalize_cik(raw_symbol)
        except ValueError as e:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=datetime.now(timezone.utc),
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=[f"Invalid CIK format: {e}"],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        t_retrieved = datetime.now(timezone.utc)
        try:
            meta, filings, snapshot = await self.submissions_provider.fetch_submissions(
                cik=canonical_cik,
                instrument_id=context.canonical_instrument_id,
            )
        except ProviderConfigurationError as e:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=t_retrieved,
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=[str(e)],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        source_meta: Dict[str, Any] = {
            "delivery_provider": "SEC EDGAR",
            "source_role": "SECURITIES_REGULATOR",
            "origin_source": "U.S. Securities and Exchange Commission",
            "cik": canonical_cik,
            "entity_name": meta.entity_name,
            "filings_count": len(filings),
            "snapshot_id": str(snapshot.id),
        }

        return ProviderResponse(
            provider_name=self.provider_name,
            source_quality=self.source_quality,
            retrieved_at=t_retrieved,
            published_at=None,
            effective_date=filings[0].filing_date if filings else None,
            observed_at=t_retrieved,
            status=DataStatus.COMPLETE if filings else DataStatus.UNAVAILABLE,
            raw={"cik": canonical_cik, "entity_name": meta.entity_name, "filings": [f.to_dict() for f in filings]},
            warnings=[],
            canonical_instrument_id=context.canonical_instrument_id,
            provider_symbol=context.provider_symbol,
            source_metadata=source_meta,
        )

    def normalize(self, raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        return {
            "cik": raw.get("cik"),
            "entity_name": raw.get("entity_name"),
            "filings_count": len(raw.get("filings", [])),
        }

    def validate(self, normalized: Dict[str, Any]) -> List[str]:
        return []

    def provenance(self, response: ProviderResponse) -> ProviderProvenance:
        meta = dict(response.source_metadata)
        meta["delivery_provider"] = "SEC EDGAR"
        meta["source_role"] = "SECURITIES_REGULATOR"
        meta["origin_source"] = "U.S. Securities and Exchange Commission"

        return ProviderProvenance(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            endpoint=self.base_url,
            retrieved_at=response.retrieved_at,
            source_quality=self.source_quality,
            canonical_instrument_id=response.canonical_instrument_id,
            provider_symbol=response.provider_symbol,
            effective_date=response.effective_date,
            metadata=meta,
        )
