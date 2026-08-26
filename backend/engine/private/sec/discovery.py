"""
backend/engine/private/sec/discovery.py
=========================================
Official SEC Ticker-to-CIK Candidate Discovery Service.

Official Endpoint:
    - Base URL: https://www.sec.gov/files/company_tickers_exchange.json
    - Secondary: https://www.sec.gov/files/company_tickers.json

Core Principles:
    - SEC disclaimer: The SEC does not guarantee the completeness or accuracy of ticker/CIK mapping files.
    - Matches are returned as "identity candidates" with explicit provenance and confidence.
    - Never blindly overwrite or mutate canonical `InstrumentRecord.cik` without master validation.
    - Multiple tickers can map to the same issuer CIK (multiple share classes, ADRs).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.engine.private.domain import DataConfidenceLevel
from backend.engine.private.sec.cik import normalize_cik
from backend.engine.private.sec.client import SECEdgarClient

logger = logging.getLogger(__name__)


@dataclass
class SECTickerCandidate:
    """
    Candidate CIK match discovered from official SEC ticker mapping files.
    """
    ticker: str
    cik: str
    company_name: str
    exchange: Optional[str] = None
    confidence_level: DataConfidenceLevel = DataConfidenceLevel.MEDIUM
    provenance_source: str = "SEC_COMPANY_TICKERS_EXCHANGE"
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SECTickerDiscoveryService:
    """
    Service for resolving ticker symbols to SEC CIK candidates.
    """
    TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"

    def __init__(self, client: Optional[SECEdgarClient] = None) -> None:
        self.client = client or SECEdgarClient()

    async def discover_candidate_by_ticker(self, ticker: str) -> Optional[SECTickerCandidate]:
        """
        Queries official SEC company_tickers_exchange.json to find a candidate CIK for a given ticker.
        """
        clean_ticker = ticker.strip().upper()
        if not clean_ticker:
            return None

        payload = await self.client.get_json(self.TICKERS_EXCHANGE_URL)
        data = payload.get("data")
        fields = payload.get("fields")

        if not data or not fields or not isinstance(data, list) or not isinstance(fields, list):
            # Fallback to key-value structure if company_tickers.json was returned
            for entry in payload.values():
                if isinstance(entry, dict) and entry.get("ticker", "").upper() == clean_ticker:
                    return SECTickerCandidate(
                        ticker=clean_ticker,
                        cik=normalize_cik(entry["cik_str"]),
                        company_name=entry.get("title", ""),
                    )
            return None

        # Map field indices
        field_map = {f: idx for idx, f in enumerate(fields)}
        cik_idx = field_map.get("cik")
        ticker_idx = field_map.get("ticker")
        name_idx = field_map.get("name")
        exchange_idx = field_map.get("exchange")

        if cik_idx is None or ticker_idx is None:
            return None

        for row in data:
            if not isinstance(row, list) or len(row) <= max(cik_idx, ticker_idx):
                continue
            row_ticker = str(row[ticker_idx]).strip().upper()
            if row_ticker == clean_ticker:
                raw_cik = row[cik_idx]
                canonical_cik = normalize_cik(raw_cik)
                comp_name = str(row[name_idx]).strip() if name_idx is not None and len(row) > name_idx else ""
                exch = str(row[exchange_idx]).strip() if exchange_idx is not None and len(row) > exchange_idx else None
                return SECTickerCandidate(
                    ticker=clean_ticker,
                    cik=canonical_cik,
                    company_name=comp_name,
                    exchange=exch,
                )

        return None
