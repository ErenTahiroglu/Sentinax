"""
backend/engine/private/providers/bist_kmtp.py
=============================================
Official Borsa İstanbul (BIST) Kıymetli Madenler ve Kıymetli Taşlar Piyasası (KMTP) Market Adapter.

Access Classification:
    - Provider Access: YELLOW (public bulletin download surface; not an SLA-backed developer API).
    - Source Quality: TIER_2_EXCHANGE (official exchange precious metals bulletin).
    - Originating Source: BIST KMTP.

Hardening Invariants:
    - Zero float arithmetic: pure Decimal for all prices, quantities, purities, turnovers.
    - Verified manifest-driven discovery: derives file paths strictly from DataFilePaths.zip manifest.
    - Zero guessed URLs: fails closed if discovery manifest is unavailable or ambiguous.
    - Raw snapshot first: raw payload and SHA-256 hash recorded immutably before normalization.
    - Point-In-Time (PIT) integrity: effective_date decoupled from retrieved_at (UTC).
    - Non-trading day distinguished from discovery failure, network error, or 404.
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

from backend.engine.private.bist.locator import (
    BISTResourceResolutionError,
)
from backend.engine.private.bist.manifest import (
    BISTDirectoryManifest,
    BISTDirectoryManifestCache,
    BISTDirectoryManifestParser,
)
from backend.engine.private.domain import (
    DataStatus,
    ProviderAccessStatus,
    SourceTier,
)
from backend.engine.private.exceptions import (
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
)
from backend.engine.private.precious_metals.constants import (
    BIST_DATA_FILE_PATHS_URL,
    BIST_KMTP_DATA_URL,
    BIST_KMTP_PROVIDER_NAME,
    BIST_KMTP_PROVIDER_VERSION,
    PreciousMetalType,
)
from backend.engine.private.precious_metals.locator import (
    BISTPreciousMetalsBulletinLocator,
)
from backend.engine.private.precious_metals.models import (
    PreciousMetalMarketObservation,
    PreciousMetalObservationStatus,
    PreciousMetalSnapshot,
)
from backend.engine.private.precious_metals.parser import (
    BISTKMTPBulletinParser,
    BISTKMTPSchemaDriftError,
)
from backend.engine.private.provider_contract import (
    DataProviderContract,
    FetchContext,
    ProviderProvenance,
    ProviderResponse,
)
from backend.infrastructure.http_client import get_http_client

logger = logging.getLogger(__name__)


class BISTKMTPProvider(DataProviderContract):
    """
    Data provider adapter for Borsa İstanbul Precious Metals Market (KMTP) Daily Bulletins.
    """
    provider_name: str = BIST_KMTP_PROVIDER_NAME
    provider_version: str = BIST_KMTP_PROVIDER_VERSION
    source_quality: SourceTier = SourceTier.TIER_2_EXCHANGE
    access_status: ProviderAccessStatus = ProviderAccessStatus.YELLOW

    # Access Classification & Metadata Flags
    official_source: bool = True
    developer_api: bool = False
    sla_guaranteed: bool = False

    def __init__(
        self,
        http_client: Optional[httpx.AsyncClient] = None,
        base_host: str = "https://www.borsaistanbul.com",
        landing_page_url: Optional[str] = None,
        manifest_url: str = BIST_DATA_FILE_PATHS_URL,
        manifest_cache: Optional[BISTDirectoryManifestCache] = None,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
    ) -> None:
        self._http_client = http_client
        self.base_host = base_host.rstrip("/")
        self.landing_page_url = landing_page_url or BIST_KMTP_DATA_URL
        self.manifest_url = manifest_url
        self.manifest_cache = manifest_cache or BISTDirectoryManifestCache()
        self.locator = BISTPreciousMetalsBulletinLocator(
            base_host=self.base_host,
            landing_page_url=self.landing_page_url,
        )
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries

    def _get_client(self) -> httpx.AsyncClient:
        return self._http_client or get_http_client()

    async def fetch_directory_manifest(
        self,
        force_refresh: bool = False,
    ) -> Tuple[Optional[BISTDirectoryManifest], bool]:
        """
        Fetches or retrieves from cache the verified BIST DataFilePaths.zip directory manifest.
        Returns (manifest, is_stale).
        """
        if not force_refresh:
            cached, is_stale = self.manifest_cache.get_manifest()
            if cached is not None and not is_stale:
                return cached, False

        client = self._get_client()
        attempt = 0
        last_exc: Optional[Exception] = None

        while attempt <= self.max_retries:
            attempt += 1
            try:
                response = await client.get(self.manifest_url, timeout=self.timeout_seconds)
                if response.status_code == 200:
                    raw_bytes = response.content
                    manifest = BISTDirectoryManifestParser.parse_manifest_bytes(
                        raw_bytes=raw_bytes,
                        source_url=self.manifest_url,
                        retrieved_at=datetime.now(timezone.utc),
                    )
                    self.manifest_cache.set_manifest(manifest)
                    return manifest, False
                elif response.status_code == 429:
                    if attempt <= self.max_retries:
                        await asyncio.sleep(0.5 * attempt)
                        continue
                    raise ProviderRateLimitError("BIST manifest rate limit (HTTP 429).", provider_name=self.provider_name)
                elif response.status_code >= 500:
                    if attempt <= self.max_retries:
                        await asyncio.sleep(0.5 * attempt)
                        continue
                    raise ProviderServerError(f"BIST manifest server error (HTTP {response.status_code}).", provider_name=self.provider_name)
            except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt <= self.max_retries:
                    await asyncio.sleep(0.5 * attempt)
                    continue
                break
            except Exception as exc:
                last_exc = exc
                if attempt <= self.max_retries:
                    await asyncio.sleep(0.5 * attempt)
                    continue
                break

        cached, _ = self.manifest_cache.get_manifest()
        if cached is not None:
            logger.warning("Using stale BIST directory manifest due to fetch failure: %s", last_exc)
            return cached, True

        logger.error("Failed to fetch BIST directory manifest for KMTP: %s", last_exc)
        return None, False

    async def fetch_daily_bulletin(
        self,
        trade_date: date,
    ) -> Tuple[PreciousMetalSnapshot, List[PreciousMetalMarketObservation]]:
        """
        Fetches and parses the official BIST KMTP daily bulletin for a specific trade date.
        """
        now_utc = datetime.now(timezone.utc)

        # 1. Non-trading weekends
        if trade_date.weekday() >= 5:
            snap = PreciousMetalSnapshot(
                trade_date=trade_date,
                retrieved_at=now_utc,
                http_status=200,
                payload_hash=hashlib.sha256(b"").hexdigest(),
                content_type="text/plain",
                file_name=None,
                source_url="",
                resolved_download_url=None,
                observations=[],
                raw_bytes=b"",
                parser_version=self.provider_version,
                diagnostics=["NON_TRADING_DAY: Weekend session does not trade on BIST KMTP."],
            )
            return snap, []

        # 2. Retrieve verified directory manifest
        try:
            manifest, is_stale = await self.fetch_directory_manifest()
        except ProviderRateLimitError:
            raise
        except ProviderServerError:
            raise
        except ProviderTimeoutError:
            raise
        except Exception as exc:
            snap = PreciousMetalSnapshot(
                trade_date=trade_date,
                retrieved_at=now_utc,
                http_status=500,
                payload_hash=hashlib.sha256(b"").hexdigest(),
                content_type="text/plain",
                file_name=None,
                source_url=self.manifest_url,
                resolved_download_url=None,
                observations=[],
                raw_bytes=b"",
                parser_version=self.provider_version,
                diagnostics=[f"DISCOVERY_FETCH_FAILED: {exc}"],
            )
            return snap, []

        if manifest is None:
            snap = PreciousMetalSnapshot(
                trade_date=trade_date,
                retrieved_at=now_utc,
                http_status=503,
                payload_hash=hashlib.sha256(b"").hexdigest(),
                content_type="text/plain",
                file_name=None,
                source_url=self.manifest_url,
                resolved_download_url=None,
                observations=[],
                raw_bytes=b"",
                parser_version=self.provider_version,
                diagnostics=["DISCOVERY_UNAVAILABLE: Could not obtain verified BIST directory manifest for KMTP."],
            )
            return snap, []

        # 3. Resolve exact official bulletin resource
        try:
            resource = self.locator.resolve_bulletin_resource(
                trade_date=trade_date,
                manifest=manifest,
                is_stale_discovery=is_stale,
            )
        except BISTResourceResolutionError as res_err:
            snap = PreciousMetalSnapshot(
                trade_date=trade_date,
                retrieved_at=now_utc,
                http_status=400,
                payload_hash=hashlib.sha256(b"").hexdigest(),
                content_type="text/plain",
                file_name=None,
                source_url=self.manifest_url,
                resolved_download_url=None,
                manifest_hash=manifest.payload_hash,
                is_stale_discovery=is_stale,
                observations=[],
                raw_bytes=b"",
                parser_version=self.provider_version,
                diagnostics=[str(res_err)],
            )
            return snap, []

        # 4. Fetch bulletin content from resolved URL
        url = resource.resolved_download_url
        client = self._get_client()
        attempt = 0
        last_exc: Optional[Exception] = None

        while attempt <= self.max_retries:
            attempt += 1
            try:
                response = await client.get(url, timeout=self.timeout_seconds)
                fetch_time = datetime.now(timezone.utc)

                if response.status_code == 200:
                    raw_bytes = response.content
                    headers = getattr(response, "headers", {})
                    content_type = headers.get("content-type", "application/zip") if hasattr(headers, "get") else "application/zip"

                    if not raw_bytes:
                        snap = PreciousMetalSnapshot(
                            trade_date=trade_date,
                            retrieved_at=fetch_time,
                            http_status=200,
                            payload_hash=hashlib.sha256(b"").hexdigest(),
                            content_type=content_type,
                            file_name=resource.official_filename,
                            source_url=url,
                            resolved_download_url=url,
                            manifest_hash=resource.manifest_hash,
                            is_stale_discovery=resource.is_stale_discovery,
                            observations=[],
                            raw_bytes=raw_bytes,
                            parser_version=self.provider_version,
                            diagnostics=["EMPTY_SOURCE_PAYLOAD: Empty KMTP bulletin payload from exchange."],
                        )
                        return snap, []

                    payload_hash = hashlib.sha256(raw_bytes).hexdigest()
                    snap_id = uuid4()

                    # Parse observations from KMTP bulletin
                    try:
                        observations = BISTKMTPBulletinParser.parse_bulletin_bytes(
                            raw_bytes=raw_bytes,
                            filename=resource.official_filename,
                            trade_date=trade_date,
                            snapshot_id=snap_id,
                            snapshot_hash=payload_hash,
                            retrieved_at=fetch_time,
                        )
                    except BISTKMTPSchemaDriftError as drift_err:
                        logger.error("BIST KMTP Schema drift: %s", drift_err)
                        snap = PreciousMetalSnapshot(
                            id=snap_id,
                            trade_date=trade_date,
                            retrieved_at=fetch_time,
                            http_status=200,
                            payload_hash=payload_hash,
                            content_type=content_type,
                            file_name=resource.official_filename,
                            source_url=url,
                            resolved_download_url=url,
                            manifest_hash=resource.manifest_hash,
                            is_stale_discovery=resource.is_stale_discovery,
                            observations=[],
                            raw_bytes=raw_bytes,
                            parser_version=self.provider_version,
                            diagnostics=[f"SCHEMA_DRIFT: {drift_err}"],
                        )
                        return snap, []

                    diagnostics = []
                    if resource.is_stale_discovery:
                        diagnostics.append("DEGRADED_DISCOVERY: KMTP bulletin resolved using stale cached directory manifest.")

                    snap = PreciousMetalSnapshot(
                        id=snap_id,
                        trade_date=trade_date,
                        retrieved_at=fetch_time,
                        http_status=200,
                        payload_hash=payload_hash,
                        content_type=content_type,
                        file_name=resource.official_filename,
                        source_url=url,
                        resolved_download_url=url,
                        manifest_hash=resource.manifest_hash,
                        is_stale_discovery=resource.is_stale_discovery,
                        observations=observations,
                        raw_bytes=raw_bytes,
                        parser_version=self.provider_version,
                        diagnostics=diagnostics,
                    )
                    return snap, observations

                elif response.status_code == 404:
                    snap = PreciousMetalSnapshot(
                        trade_date=trade_date,
                        retrieved_at=fetch_time,
                        http_status=404,
                        payload_hash=hashlib.sha256(b"").hexdigest(),
                        content_type="text/plain",
                        file_name=resource.official_filename,
                        source_url=url,
                        resolved_download_url=url,
                        manifest_hash=resource.manifest_hash,
                        is_stale_discovery=resource.is_stale_discovery,
                        observations=[],
                        raw_bytes=None,
                        parser_version=self.provider_version,
                        diagnostics=[f"RESOURCE_NOT_FOUND: KMTP bulletin file '{resource.official_filename}' not found (HTTP 404)."],
                    )
                    return snap, []

                elif response.status_code == 429:
                    if attempt <= self.max_retries:
                        await asyncio.sleep(0.5 * attempt)
                        continue
                    raise ProviderRateLimitError("BIST KMTP rate limit encountered (HTTP 429).", provider_name=self.provider_name)

                elif response.status_code >= 500:
                    if attempt <= self.max_retries:
                        await asyncio.sleep(0.5 * attempt)
                        continue
                    raise ProviderServerError(f"BIST KMTP server error (HTTP {response.status_code}).", provider_name=self.provider_name)

            except (httpx.TimeoutException, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt <= self.max_retries:
                    await asyncio.sleep(0.5 * attempt)
                    continue
                raise ProviderTimeoutError(f"Timeout connecting to BIST KMTP ({self.timeout_seconds}s).", provider_name=self.provider_name) from exc
            except httpx.RequestError as exc:
                last_exc = exc
                if attempt <= self.max_retries:
                    await asyncio.sleep(0.5 * attempt)
                    continue
                raise ProviderServerError(f"Network error connecting to BIST KMTP: {exc}", provider_name=self.provider_name) from exc

        if last_exc:
            raise ProviderServerError(f"Failed to fetch BIST KMTP bulletin after {self.max_retries} retries: {last_exc}", provider_name=self.provider_name)

        raise ProviderServerError("Unknown error fetching BIST KMTP bulletin.", provider_name=self.provider_name)

    async def fetch(self, context: FetchContext) -> ProviderResponse:
        """
        Fetches precious metal observation(s) from BIST KMTP for a requested context.
        """
        trade_date = context.effective_date or date.today()
        target_symbol = context.provider_symbol

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
                warnings=[f"Failed to fetch BIST KMTP bulletin for {trade_date}: {exc}"],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        if not observations:
            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=snapshot.retrieved_at,
                published_at=None,
                effective_date=trade_date,
                status=DataStatus.UNAVAILABLE,
                raw=snapshot.to_dict(),
                warnings=snapshot.diagnostics or ["No precious metal observations available in bulletin."],
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=context.provider_symbol,
            )

        if target_symbol:
            matched = [obs for obs in observations if obs.raw_symbol == target_symbol or obs.metal.value == target_symbol.upper()]
            if not matched:
                return ProviderResponse(
                    provider_name=self.provider_name,
                    source_quality=self.source_quality,
                    retrieved_at=snapshot.retrieved_at,
                    published_at=None,
                    effective_date=trade_date,
                    status=DataStatus.UNAVAILABLE,
                    raw=None,
                    warnings=[f"Symbol/Metal '{target_symbol}' not found in BIST KMTP bulletin for {trade_date}."],
                    canonical_instrument_id=context.canonical_instrument_id,
                    provider_symbol=context.provider_symbol,
                )

            selected_obs = matched[0]
            status = DataStatus.COMPLETE if selected_obs.status == PreciousMetalObservationStatus.VALID else DataStatus.DEGRADED

            return ProviderResponse(
                provider_name=self.provider_name,
                source_quality=self.source_quality,
                retrieved_at=snapshot.retrieved_at,
                published_at=None,
                effective_date=trade_date,
                status=status,
                raw=selected_obs.to_dict(),
                warnings=selected_obs.diagnostics,
                canonical_instrument_id=context.canonical_instrument_id,
                provider_symbol=selected_obs.raw_symbol or target_symbol,
            )

        # Multi-observation aggregate
        has_invalid = any(
            obs.status in (PreciousMetalObservationStatus.INVALID_OBSERVATION, PreciousMetalObservationStatus.CONFLICT_QUARANTINED)
            for obs in observations
        )
        status = DataStatus.DEGRADED if has_invalid else DataStatus.COMPLETE

        return ProviderResponse(
            provider_name=self.provider_name,
            source_quality=self.source_quality,
            retrieved_at=snapshot.retrieved_at,
            published_at=None,
            effective_date=trade_date,
            status=status,
            raw=[obs.to_dict() for obs in observations],
            warnings=snapshot.diagnostics,
            canonical_instrument_id=context.canonical_instrument_id,
            provider_symbol=None,
        )

    def normalize(self, raw: Any) -> Dict[str, Any]:
        if isinstance(raw, PreciousMetalMarketObservation):
            return raw.to_dict()
        if isinstance(raw, dict):
            return raw
        return {"raw": raw}

    def validate(self, normalized: Dict[str, Any]) -> List[str]:
        warnings: List[str] = []
        if not isinstance(normalized, dict):
            return ["Invalid normalized payload: expected dict."]

        metal = normalized.get("metal")
        if not metal:
            warnings.append("Missing required field 'metal'.")

        price = normalized.get("price")
        if price is None:
            warnings.append("Missing required field 'price'.")
        else:
            try:
                p = Decimal(str(price))
                if p <= 0:
                    warnings.append(f"Non-positive precious metal price: {p}")
            except Exception:
                warnings.append(f"Malformed precious metal price: {price}")

        return warnings

    def provenance(self, response: ProviderResponse) -> ProviderProvenance:
        eff_date = response.effective_date or date.today()
        return ProviderProvenance(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            endpoint=self.manifest_url,
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
                "landing_page_url": self.landing_page_url,
                "manifest_url": self.manifest_url,
            },
        )
