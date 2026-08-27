"""
backend/engine/private/providers/bist_eod.py
============================================
Official Borsa İstanbul (BIST) Equity EOD & ALTIN.S1 Market Data Adapter.

Access Classification:
    - Provider Access: YELLOW (public bulletin download surface; not an SLA-backed developer API).
    - Source Quality: TIER_2_EXCHANGE (official exchange bulletin).
    - Capabilities: CURRENT_DAILY_PUBLIC, HISTORICAL_PUBLIC_IF_AVAILABLE, HISTORICAL_DATASTORE_RESTRICTED.

Hardening Invariants:
    - Zero float usage: all prices, volumes, and values are pure Decimal.
    - Raw snapshot first: raw payload and SHA-256 hash are recorded before normalization.
    - PIT integrity: trade_date (effective date) is cleanly separated from retrieved_at (network UTC).
    - ALTIN.S1 modeled as COMMODITY_CERTIFICATE (Darphane, 0.01g gold, 0.995 purity, TRY).
    - Market price comes strictly from BIST bulletin (no synthetic fair-value or premium/discount calculation).
    - Non-trading day distinguished from network error or 404.
    - Schema drift fails closed on missing required columns.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

import httpx

from backend.engine.private.bist.constants import (
    BIST_BULLETIN_DIRECT_BASE_URL,
    BIST_DATASTORE_PORTAL_URL,
    BIST_DEFAULT_MIC,
    BIST_OFFICIAL_PORTAL_URL,
    BIST_PROVIDER_NAME,
    BIST_PROVIDER_VERSION,
)
from backend.engine.private.bist.models import (
    BISTBulletinSnapshot,
    BISTCapability,
    BISTEODObservation,
    BISTObservationStatus,
)
from backend.engine.private.bist.parser import (
    BISTBulletinParser,
    BISTSchemaDriftError,
    clean_bist_symbol,
)
from backend.engine.private.domain import (
    DataStatus,
    ProviderAccessStatus,
    SourceTier,
)
from backend.engine.private.exceptions import (
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderServerError,
    ProviderTimeoutError,
)
from backend.engine.private.identity import InstrumentResolverService
from backend.engine.private.provider_contract import (
    DataProviderContract,
    FetchContext,
    ProviderProvenance,
    ProviderResponse,
)
from backend.infrastructure.http_client import get_http_client

logger = logging.getLogger(__name__)


class BISTEODProvider(DataProviderContract):
    """
    Data provider adapter for Borsa İstanbul Equity Market Daily Bulletins & ALTIN.S1.
    """
    provider_name: str = BIST_PROVIDER_NAME
    provider_version: str = BIST_PROVIDER_VERSION
    source_quality: SourceTier = SourceTier.TIER_2_EXCHANGE
    access_status: ProviderAccessStatus = ProviderAccessStatus.YELLOW

    # Access Classification & Metadata Flags
    official_source: bool = True
    developer_api: bool = False
    sla_guaranteed: bool = False

    capabilities: List[BISTCapability] = [
        BISTCapability.CURRENT_DAILY_PUBLIC,
        BISTCapability.HISTORICAL_PUBLIC_IF_AVAILABLE,
        BISTCapability.HISTORICAL_DATASTORE_RESTRICTED,
    ]

    def __init__(
        self,
        http_client: Optional[httpx.AsyncClient] = None,
        resolver: Optional[InstrumentResolverService] = None,
        base_url: Optional[str] = None,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
    ) -> None:
        self._http_client = http_client
        self._resolver = resolver
        self.base_url = base_url or BIST_BULLETIN_DIRECT_BASE_URL
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    def _get_client(self) -> httpx.AsyncClient:
        return self._http_client or get_http_client()

    def get_bulletin_url(self, trade_date: date) -> str:
        """
        Constructs the official downloadable bulletin file URL for a given trade date.
        """
        date_str = trade_date.strftime("%Y%m%d")
        return f"{self.base_url.rstrip('/')}/bulten_{date_str}.zip"

    async def fetch_daily_bulletin(
        self,
        trade_date: date,
    ) -> Tuple[BISTBulletinSnapshot, List[BISTEODObservation]]:
        """
        Fetches and parses the official BIST daily bulletin for a specific trade date.
        
        Distinguishes:
            - Non-trading days (weekends, official holidays)
            - Historical dates restricted to DataStore
            - Transport / network failures
            - Schema drift failures
        """
        # 1. Non-trading day check: weekends
        if trade_date.weekday() >= 5:  # Saturday = 5, Sunday = 6
            now_utc = datetime.now(timezone.utc)
            snap = BISTBulletinSnapshot(
                trade_date=trade_date,
                retrieved_at=now_utc,
                http_status=200,
                payload_hash=hashlib.sha256(b"").hexdigest(),
                content_type="text/plain",
                file_name=None,
                source_url=self.get_bulletin_url(trade_date),
                observations=[],
                raw_bytes=b"",
                parser_version=self.provider_version,
                diagnostics=["NON_TRADING_DAY: Weekend session does not trade on BIST."],
            )
            return snap, []

        url = self.get_bulletin_url(trade_date)
        client = self._get_client()
        attempt = 0
        last_exc: Optional[Exception] = None

        while attempt <= self.max_retries:
            attempt += 1
            try:
                response = await client.get(url, timeout=self.timeout_seconds)
                now_utc = datetime.now(timezone.utc)

                if response.status_code == 200:
                    raw_bytes = response.content
                    if not raw_bytes:
                        # Empty payload on a weekday -> possible holiday / no-session day
                        snap = BISTBulletinSnapshot(
                            trade_date=trade_date,
                            retrieved_at=now_utc,
                            http_status=200,
                            payload_hash=hashlib.sha256(b"").hexdigest(),
                            content_type=response.headers.get("content-type", "text/plain"),
                            file_name=f"bulten_{trade_date.strftime('%Y%m%d')}.zip",
                            source_url=url,
                            observations=[],
                            raw_bytes=raw_bytes,
                            parser_version=self.provider_version,
                            diagnostics=["NON_TRADING_DAY: Empty bulletin payload from exchange (possible official holiday)."],
                        )
                        return snap, []

                    payload_hash = hashlib.sha256(raw_bytes).hexdigest()
                    snap_id = uuid4()

                    # Parse observations from bulletin
                    try:
                        observations = BISTBulletinParser.parse_bulletin_bytes(
                            raw_bytes=raw_bytes,
                            filename=f"bulten_{trade_date.strftime('%Y%m%d')}.zip",
                            filename_date=trade_date,
                            snapshot_id=snap_id,
                            snapshot_hash=payload_hash,
                            retrieved_at=now_utc,
                            resolver=self._resolver,
                        )
                    except BISTSchemaDriftError as drift_err:
                        logger.error("BIST Schema drift detected: %s", drift_err)
                        snap = BISTBulletinSnapshot(
                            id=snap_id,
                            trade_date=trade_date,
                            retrieved_at=now_utc,
                            http_status=200,
                            payload_hash=payload_hash,
                            content_type=response.headers.get("content-type", "application/zip"),
                            file_name=f"bulten_{trade_date.strftime('%Y%m%d')}.zip",
                            source_url=url,
                            observations=[],
                            raw_bytes=raw_bytes,
                            parser_version=self.provider_version,
                            diagnostics=[f"SCHEMA_DRIFT: {drift_err}"],
                        )
                        return snap, []

                    snap = BISTBulletinSnapshot(
                        id=snap_id,
                        trade_date=trade_date,
                        retrieved_at=now_utc,
                        http_status=200,
                        payload_hash=payload_hash,
                        content_type=response.headers.get("content-type", "application/zip"),
                        file_name=f"bulten_{trade_date.strftime('%Y%m%d')}.zip",
                        source_url=url,
                        observations=observations,
                        raw_bytes=raw_bytes,
                        parser_version=self.provider_version,
                        diagnostics=[],
                    )
                    return snap, observations

                elif response.status_code == 404:
                    # 404 Not Found -> Historical data restricted to DataStore or holiday
                    snap = BISTBulletinSnapshot(
                        trade_date=trade_date,
                        retrieved_at=now_utc,
                        http_status=404,
                        payload_hash=hashlib.sha256(b"").hexdigest(),
                        content_type="text/plain",
                        file_name=None,
                        source_url=url,
                        observations=[],
                        raw_bytes=None,
                        parser_version=self.provider_version,
                        diagnostics=[
                            f"HISTORICAL_DATASTORE_RESTRICTED: Bulletin for {trade_date} not found at public URL (HTTP 404). "
                            f"Historical archives are restricted to {BIST_DATASTORE_PORTAL_URL}."
                        ],
                    )
                    return snap, []

                elif response.status_code == 429:
                    if attempt <= self.max_retries:
                        await asyncio.sleep(0.5 * attempt)
                        continue
                    raise ProviderRateLimitError("BIST download rate limit encountered (HTTP 429).", provider_name=self.provider_name)

                elif response.status_code >= 500:
                    if attempt <= self.max_retries:
                        await asyncio.sleep(0.5 * attempt)
                        continue
                    raise ProviderServerError(f"BIST server error (HTTP {response.status_code}).", provider_name=self.provider_name)

                else:
                    snap = BISTBulletinSnapshot(
                        trade_date=trade_date,
                        retrieved_at=now_utc,
                        http_status=response.status_code,
                        payload_hash=hashlib.sha256(b"").hexdigest(),
                        content_type="text/plain",
                        file_name=None,
                        source_url=url,
                        observations=[],
                        raw_bytes=None,
                        parser_version=self.provider_version,
                        diagnostics=[f"HTTP_ERROR_{response.status_code}: Unexpected HTTP status from BIST."],
                    )
                    return snap, []

            except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt <= self.max_retries:
                    await asyncio.sleep(0.5 * attempt)
                    continue
                raise ProviderTimeoutError(f"Timeout connecting to BIST ({self.timeout_seconds}s).", provider_name=self.provider_name) from exc

            except httpx.RequestError as exc:
                last_exc = exc
                if attempt <= self.max_retries:
                    await asyncio.sleep(0.5 * attempt)
                    continue
                raise ProviderServerError(f"Network transport error connecting to BIST: {exc}", provider_name=self.provider_name) from exc

        if last_exc:
            raise ProviderServerError(f"Failed to fetch BIST bulletin after {self.max_retries} retries: {last_exc}", provider_name=self.provider_name)

        raise ProviderServerError("Unknown error fetching BIST bulletin.", provider_name=self.provider_name)

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        """
        Fetches an individual instrument or all observations for a requested context.
        """
        trade_date = context.effective_date or date.today()
        target_symbol = clean_bist_symbol(context.provider_symbol) if context.provider_symbol else None

        try:
            snapshot, observations = await self.fetch_daily_bulletin(trade_date)
        except Exception as exc:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=datetime.now(timezone.utc),
                published_at=None,
                effective_date=trade_date,
                status=DataStatus.UNAVAILABLE,
                raw=None,
                warnings=[f"Failed to fetch BIST bulletin for {trade_date}: {exc}"],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        # Check if non-trading day or restricted
        if not observations:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=snapshot.retrieved_at,
                published_at=None,
                effective_date=trade_date,
                status=DataStatus.UNAVAILABLE,
                raw=snapshot.to_dict(),
                warnings=snapshot.diagnostics or ["No observations available in bulletin."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        if target_symbol:
            matched = [obs for obs in observations if obs.symbol == target_symbol]
            if not matched:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=snapshot.retrieved_at,
                    published_at=None,
                    effective_date=trade_date,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=[f"Symbol '{target_symbol}' not found in BIST bulletin for {trade_date}."],
                    canonical_instrument_id=context.canonical_instrument_id,
                    provider_symbol=context.provider_symbol,
                )
            
            selected_obs = matched[0]
            status = DataStatus.COMPLETE if selected_obs.status == BISTObservationStatus.VALID else (
                DataStatus.PARTIAL if selected_obs.status == BISTObservationStatus.UNRESOLVED_IDENTITY else DataStatus.DEGRADED
            )

            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=snapshot.retrieved_at,
                published_at=None,
                effective_date=trade_date,
                status=status,
                raw=selected_obs.to_dict(),
                warnings=selected_obs.diagnostics,
                canonical_instrument_id=selected_obs.instrument_id or context.canonical_instrument_id,
                provider_symbol=selected_obs.symbol,
            )

        # Multi-instrument / full bulletin fetch
        return ProviderResponse(
            provider_name=self.provider_name,
            source_quality=self.source_quality,
            retrieved_at=snapshot.retrieved_at,
            published_at=None,
            effective_date=trade_date,
            status=DataStatus.COMPLETE,
            raw=[obs.to_dict() for obs in observations],
            warnings=snapshot.diagnostics,
            canonical_instrument_id=context.canonical_instrument_id,
            provider_symbol=None,
        )

    def normalize(self, raw: Any) -> Dict[str, Any]:
        """
        Maps raw payload to canonical field dictionary without data fabrication.
        """
        if isinstance(raw, BISTEODObservation):
            return raw.to_dict()
        if isinstance(raw, dict):
            return raw
        return {"raw": raw}

    def validate(self, normalized: Dict[str, Any]) -> List[str]:
        """
        Validates normalized observation dictionary for schema anomalies or OHLC contradictions.
        Never raises exceptions — all issues are returned as warning strings.
        """
        warnings: List[str] = []
        if not isinstance(normalized, dict):
            return ["Invalid normalized payload type: expected dict."]

        symbol = normalized.get("symbol")
        if not symbol:
            warnings.append("Missing required field 'symbol'.")

        close_str = normalized.get("close")
        if close_str is None:
            warnings.append("Missing required field 'close'.")
        else:
            try:
                c = Decimal(str(close_str))
                if c < 0:
                    warnings.append(f"Negative close price: {c}")
            except Exception:
                warnings.append(f"Malformed close price: {close_str}")

        # Check OHLC integrity if available
        open_str = normalized.get("open")
        high_str = normalized.get("high")
        low_str = normalized.get("low")

        if high_str is not None and low_str is not None:
            try:
                h = Decimal(str(high_str))
                l = Decimal(str(low_str))
                if h < l:
                    warnings.append(f"OHLC integrity violation: High ({h}) < Low ({l})")
                if close_str is not None:
                    c = Decimal(str(close_str))
                    if h < c:
                        warnings.append(f"OHLC integrity violation: High ({h}) < Close ({c})")
                    if l > c:
                        warnings.append(f"OHLC integrity violation: Low ({l}) > Close ({c})")
                if open_str is not None:
                    o = Decimal(str(open_str))
                    if h < o:
                        warnings.append(f"OHLC integrity violation: High ({h}) < Open ({o})")
                    if l > o:
                        warnings.append(f"OHLC integrity violation: Low ({l}) > Open ({o})")
            except Exception:
                pass

        return warnings

    def provenance(self, response: ProviderResponse) -> ProviderProvenance:
        """
        Returns the audit trail for this provider response.
        """
        eff_date = response.effective_date
        url = self.get_bulletin_url(eff_date) if eff_date else self.base_url

        return ProviderProvenance(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            endpoint=url,
            retrieved_at=response.retrieved_at,
            source_quality=self.source_quality,
            canonical_instrument_id=response.canonical_instrument_id,
            provider_symbol=response.provider_symbol,
            effective_date=eff_date,
            metadata={
                "access_status": self.access_status.value,
                "official_source": self.official_source,
                "developer_api": self.developer_api,
                "sla_guaranteed": self.sla_guaranteed,
            },
        )
