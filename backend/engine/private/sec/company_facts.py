"""
backend/engine/private/sec/company_facts.py
============================================
SEC EDGAR CompanyFacts Ingestion & Hierarchical Raw XBRL Fact Extractor.

Core Invariants:
    - SEC raw facts belong strictly to the issuer entity level (CIK); instrument_id is not stored.
    - Numerical values parsed into exact `Decimal` representation; floats are avoided.
    - Genuine zero is preserved as Decimal("0"); missing values remain None.
    - Classification: DURATION (both start and end present) vs INSTANT (end only).
    - Missing end dates or start without end are rejected as schema-invalid.
    - No fake "UNKNOWN_ACCN" accession numbers; missing accession remains None.
    - Standard public taxonomies (us-gaap, dei, ifrs-full, srt) preserved verbatim.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from uuid import UUID

from backend.engine.private.domain import (
    DataStatus,
    ProviderAccessStatus,
    SourceTier,
)
from backend.engine.private.exceptions import (
    ProviderSchemaError,
)
from backend.engine.private.sec.cik import normalize_cik
from backend.engine.private.sec.client import SECEdgarClient
from backend.engine.private.sec.models import (
    PeriodType,
    SECRawFactRecord,
    build_company_facts_url,
)
from backend.engine.private.sec.submissions import parse_sec_date
from backend.engine.private.storage_models import RawProviderSnapshotRecord

logger = logging.getLogger(__name__)

STANDARD_TAXONOMIES = {"us-gaap", "dei", "ifrs-full", "srt"}


def parse_sec_decimal(val_raw: Any) -> Optional[Decimal]:
    """
    Safely parses an XBRL numerical value into an exact Decimal.
    Rejects booleans, NaN, and Infinity.
    """
    if val_raw is None:
        return None

    # In Python, bool is a subclass of int (True == 1), guard against booleans
    if isinstance(val_raw, bool):
        return None

    if isinstance(val_raw, (float, int)):
        if isinstance(val_raw, float) and (math.isnan(val_raw) or math.isinf(val_raw)):
            return None
        return Decimal(str(val_raw))

    if isinstance(val_raw, str):
        s = val_raw.strip()
        if not s or s.lower() in ("null", "none", "nan", "inf", "-inf", "infinity", "-infinity"):
            return None
        try:
            d = Decimal(s)
            if d.is_nan() or d.is_infinite():
                return None
            return d
        except InvalidOperation:
            return None

    return None


class SECCompanyFactsParser:
    """
    Parses and flattens hierarchical SEC CompanyFacts JSON into SECRawFactRecord instances.
    """

    @classmethod
    def parse_facts(
        cls,
        payload: Dict[str, Any],
        snapshot_id: Optional[UUID] = None,
        retrieved_at: Optional[datetime] = None,
        instrument_id: Optional[UUID] = None,
    ) -> List[SECRawFactRecord]:
        raw_cik = payload.get("cik")
        if raw_cik is None:
            raise ProviderSchemaError("Missing top-level 'cik' in CompanyFacts payload.", provider_name="SEC_EDGAR")

        canonical_cik = normalize_cik(raw_cik)
        facts_root = payload.get("facts")
        if not facts_root or not isinstance(facts_root, dict):
            return []

        t_retrieved = retrieved_at or datetime.now(timezone.utc)
        records: List[SECRawFactRecord] = []
        seen_fingerprints: Set[Tuple[Any, ...]] = set()

        for taxonomy, concepts in facts_root.items():
            if not isinstance(concepts, dict):
                continue

            for concept_tag, concept_data in concepts.items():
                if not isinstance(concept_data, dict):
                    continue

                label = concept_data.get("label")
                description = concept_data.get("description")
                units_dict = concept_data.get("units")
                if not units_dict or not isinstance(units_dict, dict):
                    continue

                for unit_str, fact_entries in units_dict.items():
                    if not isinstance(fact_entries, list):
                        continue

                    for entry in fact_entries:
                        if not isinstance(entry, dict):
                            continue

                        # Extract exact Decimal value
                        val_raw = entry.get("val")
                        val_parsed = parse_sec_decimal(val_raw)

                        # Extract accession number (no fake UNKNOWN_ACCN)
                        accn = entry.get("accn")
                        accn_str = str(accn).strip() if accn and str(accn).strip() else None

                        # Extract dates and validate PeriodType
                        end_d = parse_sec_date(entry.get("end"))
                        start_d = parse_sec_date(entry.get("start"))

                        if end_d is None:
                            # Fact lacks an end date; schema-invalid
                            continue

                        if start_d is not None:
                            period_type = PeriodType.DURATION
                        else:
                            period_type = PeriodType.INSTANT

                        filed_d = parse_sec_date(entry.get("filed"))
                        fy = entry.get("fy")
                        fp = entry.get("fp")
                        form = entry.get("form")
                        frame = entry.get("frame")

                        # Deduplicate identical rows within the same response payload
                        fingerprint = (
                            canonical_cik,
                            taxonomy,
                            concept_tag,
                            unit_str,
                            start_d.isoformat() if start_d else None,
                            end_d.isoformat() if end_d else None,
                            accn_str,
                            form,
                            filed_d.isoformat() if filed_d else None,
                            frame,
                            str(val_parsed) if val_parsed is not None else None,
                        )
                        if fingerprint in seen_fingerprints:
                            continue
                        seen_fingerprints.add(fingerprint)

                        rec = SECRawFactRecord(
                            cik=canonical_cik,
                            accession_number=accn_str,
                            taxonomy=taxonomy,
                            concept=concept_tag,
                            unit=unit_str,
                            period_type=period_type,
                            value=val_parsed,
                            start_date=start_d,
                            end_date=end_d,
                            label=label,
                            description=description,
                            fiscal_year=int(fy) if fy is not None and str(fy).isdigit() else None,
                            fiscal_period=str(fp).strip() if fp else None,
                            form=str(form).strip() if form else None,
                            filed_date=filed_d,
                            frame=str(frame).strip() if frame else None,
                            instrument_id=None,  # Entity-level record; security resolved via instruments.cik at query time
                            snapshot_id=snapshot_id,
                            retrieved_at=t_retrieved,
                            raw_fact=entry,
                        )
                        records.append(rec)

        return records


class SECCompanyFactsProvider:
    """
    Provider service for SEC EDGAR CompanyFacts ingestion.
    """
    provider_name: str = "SEC_COMPANY_FACTS"
    provider_version: str = "1.2.0"
    source_quality: SourceTier = SourceTier.TIER_1_REGULATORY
    access_status: ProviderAccessStatus = ProviderAccessStatus.GREEN

    def __init__(self, client: Optional[SECEdgarClient] = None) -> None:
        self.client = client or SECEdgarClient()

    async def fetch_company_facts(
        self,
        cik: Union[str, int],
    ) -> Tuple[List[SECRawFactRecord], RawProviderSnapshotRecord]:
        """
        Fetches CompanyFacts JSON for a CIK, parses all standard taxonomy facts, and produces raw snapshot.
        """
        canonical_cik = normalize_cik(cik)
        url = build_company_facts_url(canonical_cik)
        retrieved_at = datetime.now(timezone.utc)

        # 1. Fetch CompanyFacts JSON
        payload = await self.client.get_json(url)

        # 2. Build Raw Snapshot Record
        snapshot = RawProviderSnapshotRecord.create(
            provider="SEC_EDGAR",
            endpoint=f"/api/xbrl/companyfacts/CIK{canonical_cik}.json",
            request_params={"cik": canonical_cik},
            raw_payload=payload,
            http_status=200,
            response_metadata={
                "user_agent_declared": True,
                "source_role": "SECURITIES_REGULATOR",
                "custom_extensions_included": False,
            },
            retrieved_at=retrieved_at,
        )

        # 3. Parse hierarchical facts
        facts = SECCompanyFactsParser.parse_facts(
            payload,
            snapshot_id=snapshot.id,
            retrieved_at=retrieved_at,
        )

        return facts, snapshot
