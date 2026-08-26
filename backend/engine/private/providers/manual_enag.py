"""
backend/engine/private/providers/manual_enag.py
=================================================
Manual Verified Ingestion Service & Provider for ENAG Inflation Data.

Core Principles:
    - Automated scraping is STRICTLY FORBIDDEN.
    - Direct overwrites are prohibited; revisions must explicitly link `supersedes_record_id`.
    - Verification lifecycle is immutable regarding data values and source references.
    - Requires `verification_status == VERIFIED` before use in any macro decision calculation.
    - Published_at is None unless explicitly known (NEVER falls back to entered_at).
    - STRICT INVARIANT: ENAG is NEVER used for tax indexation (tax indexation strictly requires TÜİK Yİ-ÜFE).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from backend.engine.private.domain import (
    DataStatus,
    ProviderAccessStatus,
    SourceTier,
)
from backend.engine.private.exceptions import ProviderSchemaError
from backend.engine.private.macro.models import (
    ManualENAGRecord,
    VerificationStatus,
)
from backend.engine.private.provider_contract import (
    DataProviderContract,
    FetchContext,
    ProviderProvenance,
    ProviderResponse,
)

logger = logging.getLogger(__name__)


class ManualENAGProvider(DataProviderContract):
    """
    Data adapter and in-memory revision repository for manually entered and verified ENAG records.
    """
    provider_name: str = "ENAG_MANUAL"
    provider_version: str = "1.0.0"
    source_quality: SourceTier = SourceTier.TIER_3_AGGREGATOR
    access_status: ProviderAccessStatus = ProviderAccessStatus.GREEN

    def __init__(self) -> None:
        # Storage for verified ENAG records history keyed by (reference_period:value_type) -> List[ManualENAGRecord]
        self._records_history: Dict[str, List[ManualENAGRecord]] = {}

    def ingest_record(self, record: ManualENAGRecord) -> None:
        """
        Ingests a manual ENAG record. Overwrites are forbidden; revisions must link supersedes_record_id.
        """
        if not record.reference_period or not record.value_type:
            raise ValueError("Manual ENAG record must specify reference_period (YYYY-MM) and value_type.")

        key = f"{record.reference_period}:{record.value_type}"
        history = self._records_history.setdefault(key, [])

        if history:
            latest = history[-1]
            if record.supersedes_record_id != latest.id:
                raise ValueError(
                    f"Record for {key} already exists (id={latest.id}). Direct overwrite is forbidden. "
                    f"Set supersedes_record_id={latest.id} to submit a revision."
                )
            history.append(record)
        else:
            history.append(record)

    def verify_record(self, reference_period: str, value_type: str, verified_by: str) -> bool:
        """
        Transitions a PENDING record to VERIFIED status after manual validation.
        Does NOT mutate substantive data (value, reference_period, source_url, published_at).
        """
        key = f"{reference_period}:{value_type}"
        history = self._records_history.get(key)
        if not history:
            return False

        record = history[-1]
        if not record.source_url:
            raise ValueError("Cannot verify ENAG record without a valid source_url.")

        record.verification_status = VerificationStatus.VERIFIED
        record.verified_at = datetime.now(timezone.utc)
        record.verified_by = verified_by
        return True

    def get_latest_record(self, reference_period: str, value_type: str) -> Optional[ManualENAGRecord]:
        key = f"{reference_period}:{value_type}"
        history = self._records_history.get(key)
        return history[-1] if history else None

    def get_record_history(self, reference_period: str, value_type: str) -> List[ManualENAGRecord]:
        key = f"{reference_period}:{value_type}"
        return list(self._records_history.get(key, []))

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        """
        Retrieves a verified ENAG inflation observation.
        """
        val_type = "MONTHLY_PCT"
        if context.provider_symbol and "YOY" in context.provider_symbol:
            val_type = "ANNUAL_PCT"

        target_date = context.effective_date or (context.as_of_time.date() if context.as_of_time else date.today())
        period_str = target_date.strftime("%Y-%m")

        key = f"{period_str}:{val_type}"
        history = self._records_history.get(key)
        t_retrieved = datetime.now(timezone.utc)

        if not history:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=t_retrieved,
                published_at=None,
                effective_date=target_date,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=[f"No manual ENAG record entered for period {period_str} ({val_type})."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        # In point-in-time mode, resolve appropriate revision
        record = history[-1]
        if context.as_of_time:
            # Find latest revision entered on or before as_of_time
            valid_revisions = [r for r in history if r.entered_at <= context.as_of_time]
            if not valid_revisions:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=t_retrieved,
                    published_at=None,
                    effective_date=target_date,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=[f"No ENAG record entered before as-of boundary {context.as_of_time}."],
                    canonical_instrument_id=context.canonical_instrument_id,
                    provider_symbol=context.provider_symbol,
                )
            record = valid_revisions[-1]

        if record.verification_status != VerificationStatus.VERIFIED:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=t_retrieved,
                published_at=record.published_at, # No fabrication of entered_at as publication
                effective_date=target_date,
                status=DataStatus.UNAVAILABLE,
                raw=record,
                warnings=[f"ENAG record for {period_str} is {record.verification_status.value.upper()}; not approved for calculation."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        eff_date = self._parse_period_to_date(record.reference_period) or target_date

        return ProviderResponse(
            provider_name=self.provider_name,
            source_quality=self.source_quality,
            retrieved_at=t_retrieved,
            published_at=record.published_at, # Strict: None if unknown
            effective_date=eff_date,
            observed_at=record.entered_at,
            status=DataStatus.COMPLETE,
            raw={
                "reference_period": record.reference_period,
                "value_type": record.value_type,
                "value": record.value,
                "source_url": record.source_url,
                "verification_status": record.verification_status.value,
                "verified_at": record.verified_at.isoformat() if record.verified_at else None,
                "verified_by": record.verified_by,
                "id": str(record.id),
                "supersedes_record_id": str(record.supersedes_record_id) if record.supersedes_record_id else None,
            },
            warnings=["Non-governmental inflation research data. Not official statistics."],
            canonical_instrument_id=context.canonical_instrument_id,
            provider_symbol=context.provider_symbol,
        )

    def normalize(self, raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise ProviderSchemaError("ENAG raw payload must be dict.")
        return {
            "period": raw.get("reference_period"),
            "value": raw.get("value"),
            "value_type": raw.get("value_type"),
            "source_url": raw.get("source_url"),
            "verification_status": raw.get("verification_status"),
        }

    def validate(self, normalized: Dict[str, Any]) -> List[str]:
        warnings: List[str] = []
        val = normalized.get("value")
        if val is not None and val < -50.0:
            warnings.append(f"Abnormally negative inflation value in ENAG: {val}")
        return warnings

    def provenance(self, response: ProviderResponse) -> ProviderProvenance:
        return ProviderProvenance(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            endpoint="manual://enagrup.org",
            retrieved_at=response.retrieved_at,
            source_quality=self.source_quality,
            canonical_instrument_id=response.canonical_instrument_id,
            provider_symbol=response.provider_symbol,
            effective_date=response.effective_date,
        )

    @staticmethod
    def _parse_period_to_date(period_str: str) -> Optional[date]:
        try:
            if len(period_str) == 7 and "-" in period_str:
                year, month = map(int, period_str.split("-"))
                if month in (1, 3, 5, 7, 8, 10, 12):
                    day = 31
                elif month in (4, 6, 9, 11):
                    day = 30
                else:
                    day = 29 if year % 4 == 0 else 28
                return date(year, month, day)
        except Exception:
            pass
        return None
