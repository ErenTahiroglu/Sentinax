"""
backend/engine/private/providers/sec_edgar.py
==============================================
DataProviderContract Adapters for SEC EDGAR Submissions & CompanyFacts Ingestion.

Core Invariants:
    - Base URL: https://data.sec.gov
    - source_role: SECURITIES_REGULATOR
    - delivery_provider: SEC EDGAR
    - SourceTier: TIER_1_REGULATORY
    - AccessStatus: GREEN
    - SEC data is stored strictly at the entity level (CIK); canonical_instrument_id is retained in ProviderResponse.
    - Explicit resource routing (SECResource.SUBMISSIONS vs SECResource.COMPANY_FACTS).
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
from backend.engine.private.sec.models import SECResource
from backend.engine.private.sec.submissions import SECSubmissionsProvider

logger = logging.getLogger(__name__)


class SECSubmissionsDataProvider(DataProviderContract):
    """
    Adapter for SEC EDGAR Submissions endpoint.
    """
    provider_name: str = "SEC_SUBMISSIONS"
    provider_version: str = "1.2.0"
    source_quality: SourceTier = SourceTier.TIER_1_REGULATORY
    access_status: ProviderAccessStatus = ProviderAccessStatus.GREEN
    base_url: str = "https://data.sec.gov"

    def __init__(self, client: Optional[SECEdgarClient] = None) -> None:
        self.client = client or SECEdgarClient()
        self.submissions_service = SECSubmissionsProvider(client=self.client)

    async def fetch(self, context: FetchContext) -> ProviderResponse:
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
                warnings=["Missing provider_symbol or CIK for SEC Submissions request."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        if context.is_historical:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=datetime.now(timezone.utc),
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=["Historical AS_OF requires local PIT storage; cannot be reconstructed via external API."],
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
            res = await self.submissions_service.fetch_submissions(
                cik=canonical_cik,
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
            "entity_name": res.metadata.entity_name,
            "filings_count": len(res.filings),
            "snapshot_id": str(res.main_snapshot.id),
        }

        return ProviderResponse(
            provider_name=self.provider_name,
            source_quality=self.source_quality,
            retrieved_at=t_retrieved,
            published_at=None,
            effective_date=res.filings[0].filing_date if res.filings else None,
            observed_at=t_retrieved,
            status=DataStatus.COMPLETE if res.filings else DataStatus.UNAVAILABLE,
            raw={"cik": canonical_cik, "entity_name": res.metadata.entity_name, "filings": [f.to_dict() for f in res.filings]},
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
            endpoint=f"{self.base_url}/submissions",
            retrieved_at=response.retrieved_at,
            source_quality=self.source_quality,
            canonical_instrument_id=response.canonical_instrument_id,
            provider_symbol=response.provider_symbol,
            effective_date=response.effective_date,
            metadata=meta,
        )


class SECCompanyFactsDataProvider(DataProviderContract):
    """
    Adapter for SEC EDGAR CompanyFacts endpoint.
    """
    provider_name: str = "SEC_COMPANY_FACTS"
    provider_version: str = "1.2.0"
    source_quality: SourceTier = SourceTier.TIER_1_REGULATORY
    access_status: ProviderAccessStatus = ProviderAccessStatus.GREEN
    base_url: str = "https://data.sec.gov"

    def __init__(self, client: Optional[SECEdgarClient] = None) -> None:
        self.client = client or SECEdgarClient()
        self.facts_service = SECCompanyFactsProvider(client=self.client)

    async def fetch(self, context: FetchContext) -> ProviderResponse:
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
                warnings=["Missing provider_symbol or CIK for SEC CompanyFacts request."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        if context.is_historical:
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
            facts, snapshot = await self.facts_service.fetch_company_facts(
                cik=canonical_cik,
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
            "facts_count": len(facts),
            "snapshot_id": str(snapshot.id),
        }

        return ProviderResponse(
            provider_name=self.provider_name,
            source_quality=self.source_quality,
            retrieved_at=t_retrieved,
            published_at=None,
            effective_date=None,
            observed_at=t_retrieved,
            status=DataStatus.COMPLETE if facts else DataStatus.UNAVAILABLE,
            raw={"cik": canonical_cik, "facts_count": len(facts), "facts": [f.to_dict() for f in facts]},
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
            "facts_count": raw.get("facts_count", 0),
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
            endpoint=f"{self.base_url}/api/xbrl/companyfacts",
            retrieved_at=response.retrieved_at,
            source_quality=self.source_quality,
            canonical_instrument_id=response.canonical_instrument_id,
            provider_symbol=response.provider_symbol,
            effective_date=response.effective_date,
            metadata=meta,
        )


class SECEdgarProvider(DataProviderContract):
    """
    Unified router DataProviderContract adapter dispatching to Submissions or CompanyFacts.
    """
    provider_name: str = "SEC_EDGAR"
    provider_version: str = "1.2.0"
    source_quality: SourceTier = SourceTier.TIER_1_REGULATORY
    access_status: ProviderAccessStatus = ProviderAccessStatus.GREEN
    base_url: str = "https://data.sec.gov"

    def __init__(self, client: Optional[SECEdgarClient] = None) -> None:
        self.client = client or SECEdgarClient()
        self.submissions_adapter = SECSubmissionsDataProvider(client=self.client)
        self.company_facts_adapter = SECCompanyFactsDataProvider(client=self.client)

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        resource = context.observation_type
        if resource in ("SEC_COMPANY_FACTS", "COMPANY_FACTS"):
            return await self.company_facts_adapter.fetch(context)
        elif resource in ("SEC_SUBMISSIONS", "SUBMISSIONS"):
            return await self.submissions_adapter.fetch(context)
        else:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=datetime.now(timezone.utc),
                published_at=None,
                effective_date=None,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=[f"Unknown SEC EDGAR resource observation_type: '{resource}'. Specify SEC_SUBMISSIONS or SEC_COMPANY_FACTS."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

    def normalize(self, raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        return raw

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
