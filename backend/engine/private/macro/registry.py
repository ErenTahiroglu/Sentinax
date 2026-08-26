"""
backend/engine/private/macro/registry.py
==========================================
Canonical Macroeconomic Series Registry for Turkey.

Verified Series Catalog:
    - TCMB EVDS: FX Rates (USD/TRY, EUR/TRY), Policy Rate (1-Week Repo Auction Rate)
    - TÜİK SDMX: CPI Index/YoY/MoM, Domestic PPI Index/YoY/MoM (Yİ-ÜFE)
    - ENAG Manual: Monthly and Annual Consumer Inflation Estimates
"""

from typing import Dict, List, Optional

from backend.engine.private.domain import FreshnessBasis, SourceTier
from backend.engine.private.macro.models import (
    MacroCategory,
    MacroFrequency,
    MacroSeriesDefinition,
    MacroUnit,
)


class MacroSeriesRegistry:
    """Central registry of verified macroeconomic series."""

    _DEFINITIONS: Dict[str, MacroSeriesDefinition] = {
        # ─────────────────────────────────────────────────────────────────────
        # 1. TCMB EVDS — Foreign Exchange & Monetary Policy Rates
        # ─────────────────────────────────────────────────────────────────────
        "TR_FX_USDTRY": MacroSeriesDefinition(
            canonical_key="TR_FX_USDTRY",
            provider="TCMB_EVDS",
            provider_series_code="TP.DK.USD.A.YTL",
            category=MacroCategory.FX,
            description="TCMB Gösterge Niteliğindeki ABD Doları Döviz Alış Kuru (TL)",
            unit=MacroUnit.TRY,
            frequency=MacroFrequency.BUSINESS_DAILY,
            freshness_basis=FreshnessBasis.EFFECTIVE_DATE,
            source_tier=SourceTier.TIER_1_REGULATORY,
            expected_release_interval_days=1,
            source_url="https://evds2.tcmb.gov.tr/",
        ),
        "TR_FX_EURTRY": MacroSeriesDefinition(
            canonical_key="TR_FX_EURTRY",
            provider="TCMB_EVDS",
            provider_series_code="TP.DK.EUR.A.YTL",
            category=MacroCategory.FX,
            description="TCMB Gösterge Niteliğindeki Euro Döviz Alış Kuru (TL)",
            unit=MacroUnit.TRY,
            frequency=MacroFrequency.BUSINESS_DAILY,
            freshness_basis=FreshnessBasis.EFFECTIVE_DATE,
            source_tier=SourceTier.TIER_1_REGULATORY,
            expected_release_interval_days=1,
            source_url="https://evds2.tcmb.gov.tr/",
        ),
        "TR_POLICY_RATE": MacroSeriesDefinition(
            canonical_key="TR_POLICY_RATE",
            provider="TCMB_EVDS",
            provider_series_code="TP.APIFON4",
            category=MacroCategory.INTEREST_RATE,
            description="TCMB 1 Hafta Vadeli Repo İhale Faiz Oranı / Politika Faizi (Ağırlıklı Ortalama)",
            unit=MacroUnit.PERCENT,
            frequency=MacroFrequency.BUSINESS_DAILY,
            freshness_basis=FreshnessBasis.EFFECTIVE_DATE,
            source_tier=SourceTier.TIER_1_REGULATORY,
            expected_release_interval_days=1,
            source_url="https://evds2.tcmb.gov.tr/",
        ),

        # ─────────────────────────────────────────────────────────────────────
        # 2. TÜİK SDMX — Official Inflation & Producer Price Indexes (2003=100)
        # ─────────────────────────────────────────────────────────────────────
        "TR_CPI_TUIK_INDEX": MacroSeriesDefinition(
            canonical_key="TR_CPI_TUIK_INDEX",
            provider="TUIK_SDMX",
            provider_series_code="CPI_INDEX_2003",
            category=MacroCategory.INFLATION_CPI,
            description="TÜİK Tüketici Fiyat Endeksi Genel (2003=100)",
            unit=MacroUnit.INDEX_POINTS,
            frequency=MacroFrequency.MONTHLY,
            freshness_basis=FreshnessBasis.PUBLISHED_AT,
            source_tier=SourceTier.TIER_1_REGULATORY,
            expected_release_interval_days=31,
            source_url="https://data.tuik.gov.tr/",
        ),
        "TR_CPI_TUIK_YOY": MacroSeriesDefinition(
            canonical_key="TR_CPI_TUIK_YOY",
            provider="TUIK_SDMX",
            provider_series_code="CPI_YOY_PCT",
            category=MacroCategory.INFLATION_CPI,
            description="TÜİK Tüketici Fiyat Endeksi Yıllık Değişim Oranı (%)",
            unit=MacroUnit.PERCENT,
            frequency=MacroFrequency.MONTHLY,
            freshness_basis=FreshnessBasis.PUBLISHED_AT,
            source_tier=SourceTier.TIER_1_REGULATORY,
            expected_release_interval_days=31,
            source_url="https://data.tuik.gov.tr/",
        ),
        "TR_CPI_TUIK_MOM": MacroSeriesDefinition(
            canonical_key="TR_CPI_TUIK_MOM",
            provider="TUIK_SDMX",
            provider_series_code="CPI_MOM_PCT",
            category=MacroCategory.INFLATION_CPI,
            description="TÜİK Tüketici Fiyat Endeksi Aylık Değişim Oranı (%)",
            unit=MacroUnit.PERCENT,
            frequency=MacroFrequency.MONTHLY,
            freshness_basis=FreshnessBasis.PUBLISHED_AT,
            source_tier=SourceTier.TIER_1_REGULATORY,
            expected_release_interval_days=31,
            source_url="https://data.tuik.gov.tr/",
        ),
        "TR_PPI_TUIK_INDEX": MacroSeriesDefinition(
            canonical_key="TR_PPI_TUIK_INDEX",
            provider="TUIK_SDMX",
            provider_series_code="PPI_INDEX_2003",
            category=MacroCategory.INFLATION_PPI,
            description="TÜİK Yurt İçi Üretici Fiyat Endeksi Genel (Yİ-ÜFE, 2003=100) — Vergi Endeksleme Referansı",
            unit=MacroUnit.INDEX_POINTS,
            frequency=MacroFrequency.MONTHLY,
            freshness_basis=FreshnessBasis.PUBLISHED_AT,
            source_tier=SourceTier.TIER_1_REGULATORY,
            expected_release_interval_days=31,
            source_url="https://data.tuik.gov.tr/",
        ),
        "TR_PPI_TUIK_YOY": MacroSeriesDefinition(
            canonical_key="TR_PPI_TUIK_YOY",
            provider="TUIK_SDMX",
            provider_series_code="PPI_YOY_PCT",
            category=MacroCategory.INFLATION_PPI,
            description="TÜİK Yurt İçi Üretici Fiyat Endeksi Yıllık Değişim Oranı (%)",
            unit=MacroUnit.PERCENT,
            frequency=MacroFrequency.MONTHLY,
            freshness_basis=FreshnessBasis.PUBLISHED_AT,
            source_tier=SourceTier.TIER_1_REGULATORY,
            expected_release_interval_days=31,
            source_url="https://data.tuik.gov.tr/",
        ),
        "TR_PPI_TUIK_MOM": MacroSeriesDefinition(
            canonical_key="TR_PPI_TUIK_MOM",
            provider="TUIK_SDMX",
            provider_series_code="PPI_MOM_PCT",
            category=MacroCategory.INFLATION_PPI,
            description="TÜİK Yurt İçi Üretici Fiyat Endeksi Aylık Değişim Oranı (%)",
            unit=MacroUnit.PERCENT,
            frequency=MacroFrequency.MONTHLY,
            freshness_basis=FreshnessBasis.PUBLISHED_AT,
            source_tier=SourceTier.TIER_1_REGULATORY,
            expected_release_interval_days=31,
            source_url="https://data.tuik.gov.tr/",
        ),

        # ─────────────────────────────────────────────────────────────────────
        # 3. ENAG — Non-Governmental Inflation Estimates (Manual Verified)
        # ─────────────────────────────────────────────────────────────────────
        "TR_INFLATION_ENAG_MOM": MacroSeriesDefinition(
            canonical_key="TR_INFLATION_ENAG_MOM",
            provider="ENAG_MANUAL",
            provider_series_code="ENAG_CPI_MOM",
            category=MacroCategory.INFLATION_CPI,
            description="ENAGrup Tüketici Fiyat Endeksi Aylık Değişim Oranı (%) — Doğrulanmış Manuel Giriş",
            unit=MacroUnit.PERCENT,
            frequency=MacroFrequency.MONTHLY,
            freshness_basis=FreshnessBasis.PUBLISHED_AT,
            source_tier=SourceTier.TIER_3_AGGREGATOR,
            expected_release_interval_days=31,
            source_url="https://enagrup.org/",
        ),
        "TR_INFLATION_ENAG_YOY": MacroSeriesDefinition(
            canonical_key="TR_INFLATION_ENAG_YOY",
            provider="ENAG_MANUAL",
            provider_series_code="ENAG_CPI_YOY",
            category=MacroCategory.INFLATION_CPI,
            description="ENAGrup Tüketici Fiyat Endeksi 12 Aylık Değişim Oranı (%) — Doğrulanmış Manuel Giriş",
            unit=MacroUnit.PERCENT,
            frequency=MacroFrequency.MONTHLY,
            freshness_basis=FreshnessBasis.PUBLISHED_AT,
            source_tier=SourceTier.TIER_3_AGGREGATOR,
            expected_release_interval_days=31,
            source_url="https://enagrup.org/",
        ),
    }

    @classmethod
    def get(cls, canonical_key: str) -> Optional[MacroSeriesDefinition]:
        return cls._DEFINITIONS.get(canonical_key)

    @classmethod
    def list_all(cls) -> List[MacroSeriesDefinition]:
        return list(cls._DEFINITIONS.values())

    @classmethod
    def list_by_provider(cls, provider: str) -> List[MacroSeriesDefinition]:
        return [s for s in cls._DEFINITIONS.values() if s.provider == provider]

    @classmethod
    def get_by_provider_code(cls, provider: str, provider_series_code: str) -> Optional[MacroSeriesDefinition]:
        for s in cls._DEFINITIONS.values():
            if s.provider == provider and s.provider_series_code == provider_series_code:
                return s
        return None
