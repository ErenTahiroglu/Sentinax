"""
backend/engine/private/macro/registry.py
==========================================
Canonical Macroeconomic Series Registry for Turkey and United States.

Verified Series Catalog:
    - TCMB EVDS (TR):
        * TR_FX_USDTRY (TP.DK.USD.A.YTL) -> VERIFIED
        * TR_FX_EURTRY (TP.DK.EUR.A.YTL) -> VERIFIED
        * TR_TCMB_AOFM (TP.APIFON4 - Ağırlıklı Ortalama Fonlama Maliyeti) -> VERIFIED
        * TR_POLICY_RATE -> UNVERIFIED (Disabled pending official EVDS policy rate code verification)
    - TÜİK SDMX (TR):
        * All series marked UNVERIFIED (is_active=False) pending official SDMX codelist catalog discovery
    - ENAG Manual (TR):
        * TR_INFLATION_ENAG_MOM & TR_INFLATION_ENAG_YOY -> VERIFIED (Manual Verified Ingestion)
    - FRED / ALFRED (US):
        * US_CPI_HEADLINE_INDEX (CPIAUCSL) -> VERIFIED
        * US_CPI_CORE_INDEX (CPILFESL) -> VERIFIED
        * US_UNEMPLOYMENT_RATE (UNRATE) -> VERIFIED
        * US_REAL_GDP (GDPC1) -> VERIFIED
        * US_INDUSTRIAL_PRODUCTION (INDPRO) -> VERIFIED
        * US_EFFECTIVE_FED_FUNDS_RATE (DFF) -> VERIFIED
"""

from typing import Dict, List, Optional

from backend.engine.private.domain import FreshnessBasis, SourceTier
from backend.engine.private.macro.models import (
    ContractStatus,
    MacroCategory,
    MacroFrequency,
    MacroSeriesDefinition,
    MacroUnit,
)


class MacroSeriesRegistry:
    """Central registry of verified macroeconomic series."""

    _DEFINITIONS: Dict[str, MacroSeriesDefinition] = {
        # ─────────────────────────────────────────────────────────────────────
        # 1. TCMB EVDS (Turkey) — Foreign Exchange & Funding Cost
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
            geography="TR",
            contract_status=ContractStatus.VERIFIED,
            expected_release_interval_days=1,
            source_url="https://evds2.tcmb.gov.tr/",
            verification_source="TCMB EVDS Web Servis Kullanım Kılavuzu & Series Metadata",
            verification_notes="Official daily indicative USD/TRY exchange rate.",
            is_active=True,
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
            geography="TR",
            contract_status=ContractStatus.VERIFIED,
            expected_release_interval_days=1,
            source_url="https://evds2.tcmb.gov.tr/",
            verification_source="TCMB EVDS Web Servis Kullanım Kılavuzu & Series Metadata",
            verification_notes="Official daily indicative EUR/TRY exchange rate.",
            is_active=True,
        ),
        "TR_TCMB_AOFM": MacroSeriesDefinition(
            canonical_key="TR_TCMB_AOFM",
            provider="TCMB_EVDS",
            provider_series_code="TP.APIFON4",
            category=MacroCategory.INTEREST_RATE,
            description="TCMB Ağırlıklı Ortalama Fonlama Maliyeti (AOFM) (%)",
            unit=MacroUnit.PERCENT,
            frequency=MacroFrequency.BUSINESS_DAILY,
            freshness_basis=FreshnessBasis.EFFECTIVE_DATE,
            source_tier=SourceTier.TIER_1_REGULATORY,
            geography="TR",
            contract_status=ContractStatus.VERIFIED,
            expected_release_interval_days=1,
            source_url="https://evds2.tcmb.gov.tr/",
            verification_source="TCMB EVDS Metadata (Açık Piyasa İşlemleri Fonlama İstatistikleri)",
            verification_notes="TCMB Ağırlıklı Ortalama Fonlama Maliyeti. Not the statutory 1-week repo policy rate.",
            is_active=True,
        ),
        "TR_POLICY_RATE": MacroSeriesDefinition(
            canonical_key="TR_POLICY_RATE",
            provider="TCMB_EVDS",
            provider_series_code="UNVERIFIED",
            category=MacroCategory.INTEREST_RATE,
            description="TCMB 1 Hafta Vadeli Repo İhale Faiz Oranı / Politika Faizi (%) — DOĞRULANMAMIŞ",
            unit=MacroUnit.PERCENT,
            frequency=MacroFrequency.BUSINESS_DAILY,
            freshness_basis=FreshnessBasis.EFFECTIVE_DATE,
            source_tier=SourceTier.TIER_1_REGULATORY,
            geography="TR",
            contract_status=ContractStatus.UNVERIFIED,
            expected_release_interval_days=1,
            source_url="https://evds2.tcmb.gov.tr/",
            verification_notes="Policy rate EVDS series code not yet officially verified. Disabled to prevent incorrect proxy data.",
            is_active=False,
        ),

        # ─────────────────────────────────────────────────────────────────────
        # 2. TÜİK SDMX (Turkey) — Official Inflation (Unverified catalog)
        # ─────────────────────────────────────────────────────────────────────
        "TR_CPI_TUIK_INDEX": MacroSeriesDefinition(
            canonical_key="TR_CPI_TUIK_INDEX",
            provider="TUIK_SDMX",
            provider_series_code="CPI_INDEX_2003",
            category=MacroCategory.INFLATION_CPI,
            description="TÜİK Tüketici Fiyat Endeksi Genel (2003=100) — DOĞRULANMAMIŞ DATAFLOW",
            unit=MacroUnit.INDEX_POINTS,
            frequency=MacroFrequency.MONTHLY,
            freshness_basis=FreshnessBasis.PUBLISHED_AT,
            source_tier=SourceTier.TIER_1_REGULATORY,
            geography="TR",
            contract_status=ContractStatus.UNVERIFIED,
            expected_release_interval_days=31,
            source_url="https://data.tuik.gov.tr/",
            verification_notes="TÜİK SDMX dataflow codelist and exact series keys require official data portal catalog discovery verification.",
            is_active=False,
        ),
        "TR_CPI_TUIK_YOY": MacroSeriesDefinition(
            canonical_key="TR_CPI_TUIK_YOY",
            provider="TUIK_SDMX",
            provider_series_code="CPI_YOY_PCT",
            category=MacroCategory.INFLATION_CPI,
            description="TÜİK Tüketici Fiyat Endeksi Yıllık Değişim Oranı (%) — DOĞRULANMAMIŞ DATAFLOW",
            unit=MacroUnit.PERCENT,
            frequency=MacroFrequency.MONTHLY,
            freshness_basis=FreshnessBasis.PUBLISHED_AT,
            source_tier=SourceTier.TIER_1_REGULATORY,
            geography="TR",
            contract_status=ContractStatus.UNVERIFIED,
            expected_release_interval_days=31,
            source_url="https://data.tuik.gov.tr/",
            verification_notes="TÜİK SDMX dataflow codelist and exact series keys require official data portal catalog discovery verification.",
            is_active=False,
        ),
        "TR_CPI_TUIK_MOM": MacroSeriesDefinition(
            canonical_key="TR_CPI_TUIK_MOM",
            provider="TUIK_SDMX",
            provider_series_code="CPI_MOM_PCT",
            category=MacroCategory.INFLATION_CPI,
            description="TÜİK Tüketici Fiyat Endeksi Aylık Değişim Oranı (%) — DOĞRULANMAMIŞ DATAFLOW",
            unit=MacroUnit.PERCENT,
            frequency=MacroFrequency.MONTHLY,
            freshness_basis=FreshnessBasis.PUBLISHED_AT,
            source_tier=SourceTier.TIER_1_REGULATORY,
            geography="TR",
            contract_status=ContractStatus.UNVERIFIED,
            expected_release_interval_days=31,
            source_url="https://data.tuik.gov.tr/",
            verification_notes="TÜİK SDMX dataflow codelist and exact series keys require official data portal catalog discovery verification.",
            is_active=False,
        ),
        "TR_PPI_TUIK_INDEX": MacroSeriesDefinition(
            canonical_key="TR_PPI_TUIK_INDEX",
            provider="TUIK_SDMX",
            provider_series_code="PPI_INDEX_2003",
            category=MacroCategory.INFLATION_PPI,
            description="TÜİK Yurt İçi Üretici Fiyat Endeksi Genel (Yİ-ÜFE) — DOĞRULANMAMIŞ DATAFLOW",
            unit=MacroUnit.INDEX_POINTS,
            frequency=MacroFrequency.MONTHLY,
            freshness_basis=FreshnessBasis.PUBLISHED_AT,
            source_tier=SourceTier.TIER_1_REGULATORY,
            geography="TR",
            contract_status=ContractStatus.UNVERIFIED,
            expected_release_interval_days=31,
            source_url="https://data.tuik.gov.tr/",
            verification_notes="TÜİK SDMX dataflow codelist and exact series keys require official data portal catalog discovery verification.",
            is_active=False,
        ),
        "TR_PPI_TUIK_YOY": MacroSeriesDefinition(
            canonical_key="TR_PPI_TUIK_YOY",
            provider="TUIK_SDMX",
            provider_series_code="PPI_YOY_PCT",
            category=MacroCategory.INFLATION_PPI,
            description="TÜİK Yurt İçi Üretici Fiyat Endeksi Yıllık Değişim Oranı (%) — DOĞRULANMAMIŞ DATAFLOW",
            unit=MacroUnit.PERCENT,
            frequency=MacroFrequency.MONTHLY,
            freshness_basis=FreshnessBasis.PUBLISHED_AT,
            source_tier=SourceTier.TIER_1_REGULATORY,
            geography="TR",
            contract_status=ContractStatus.UNVERIFIED,
            expected_release_interval_days=31,
            source_url="https://data.tuik.gov.tr/",
            verification_notes="TÜİK SDMX dataflow codelist and exact series keys require official data portal catalog discovery verification.",
            is_active=False,
        ),
        "TR_PPI_TUIK_MOM": MacroSeriesDefinition(
            canonical_key="TR_PPI_TUIK_MOM",
            provider="TUIK_SDMX",
            provider_series_code="PPI_MOM_PCT",
            category=MacroCategory.INFLATION_PPI,
            description="TÜİK Yurt İçi Üretici Fiyat Endeksi Aylık Değişim Oranı (%) — DOĞRULANMAMIŞ DATAFLOW",
            unit=MacroUnit.PERCENT,
            frequency=MacroFrequency.MONTHLY,
            freshness_basis=FreshnessBasis.PUBLISHED_AT,
            source_tier=SourceTier.TIER_1_REGULATORY,
            geography="TR",
            contract_status=ContractStatus.UNVERIFIED,
            expected_release_interval_days=31,
            source_url="https://data.tuik.gov.tr/",
            verification_notes="TÜİK SDMX dataflow codelist and exact series keys require official data portal catalog discovery verification.",
            is_active=False,
        ),

        # ─────────────────────────────────────────────────────────────────────
        # 3. ENAG (Turkey) — Non-Governmental Inflation Estimates
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
            geography="TR",
            contract_status=ContractStatus.VERIFIED,
            expected_release_interval_days=31,
            source_url="https://enagrup.org/",
            verification_source="ENAGrup Resmi Aylık Bültenleri",
            verification_notes="Manual verified ingestion lifecycle enforced.",
            is_active=True,
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
            geography="TR",
            contract_status=ContractStatus.VERIFIED,
            expected_release_interval_days=31,
            source_url="https://enagrup.org/",
            verification_source="ENAGrup Resmi Aylık Bültenleri",
            verification_notes="Manual verified ingestion lifecycle enforced.",
            is_active=True,
        ),

        # ─────────────────────────────────────────────────────────────────────
        # 4. FRED / ALFRED (United States) — Official US Macroeconomic Data
        # ─────────────────────────────────────────────────────────────────────
        "US_CPI_HEADLINE_INDEX": MacroSeriesDefinition(
            canonical_key="US_CPI_HEADLINE_INDEX",
            provider="FRED_ALFRED",
            provider_series_code="CPIAUCSL",
            category=MacroCategory.INFLATION_CPI,
            description="Consumer Price Index for All Urban Consumers: All Items (Index 1982-1984=100, SA)",
            unit=MacroUnit.INDEX_POINTS,
            frequency=MacroFrequency.MONTHLY,
            freshness_basis=FreshnessBasis.PUBLISHED_AT,
            source_tier=SourceTier.TIER_1_REGULATORY,
            geography="US",
            provider_native_units="Index 1982-1984=100",
            seasonal_adjustment="Seasonally Adjusted",
            origin_source="U.S. Bureau of Labor Statistics",
            release_name="Consumer Price Index",
            contract_status=ContractStatus.VERIFIED,
            expected_release_interval_days=31,
            source_url="https://fred.stlouisfed.org/series/CPIAUCSL",
            verification_source="Federal Reserve Bank of St. Louis FRED API Series Metadata",
            verification_notes="Headline US CPI from Bureau of Labor Statistics (BLS).",
            is_active=True,
        ),
        "US_CPI_CORE_INDEX": MacroSeriesDefinition(
            canonical_key="US_CPI_CORE_INDEX",
            provider="FRED_ALFRED",
            provider_series_code="CPILFESL",
            category=MacroCategory.INFLATION_CPI,
            description="Consumer Price Index for All Urban Consumers: All Items Less Food and Energy (Index 1982-1984=100, SA)",
            unit=MacroUnit.INDEX_POINTS,
            frequency=MacroFrequency.MONTHLY,
            freshness_basis=FreshnessBasis.PUBLISHED_AT,
            source_tier=SourceTier.TIER_1_REGULATORY,
            geography="US",
            provider_native_units="Index 1982-1984=100",
            seasonal_adjustment="Seasonally Adjusted",
            origin_source="U.S. Bureau of Labor Statistics",
            release_name="Consumer Price Index",
            contract_status=ContractStatus.VERIFIED,
            expected_release_interval_days=31,
            source_url="https://fred.stlouisfed.org/series/CPILFESL",
            verification_source="Federal Reserve Bank of St. Louis FRED API Series Metadata",
            verification_notes="Core US CPI (excluding Food and Energy) from BLS.",
            is_active=True,
        ),
        "US_UNEMPLOYMENT_RATE": MacroSeriesDefinition(
            canonical_key="US_UNEMPLOYMENT_RATE",
            provider="FRED_ALFRED",
            provider_series_code="UNRATE",
            category=MacroCategory.LABOR,
            description="Unemployment Rate (Percent, SA)",
            unit=MacroUnit.PERCENT,
            frequency=MacroFrequency.MONTHLY,
            freshness_basis=FreshnessBasis.PUBLISHED_AT,
            source_tier=SourceTier.TIER_1_REGULATORY,
            geography="US",
            provider_native_units="Percent",
            seasonal_adjustment="Seasonally Adjusted",
            origin_source="U.S. Bureau of Labor Statistics",
            release_name="Employment Situation",
            contract_status=ContractStatus.VERIFIED,
            expected_release_interval_days=31,
            source_url="https://fred.stlouisfed.org/series/UNRATE",
            verification_source="Federal Reserve Bank of St. Louis FRED API Series Metadata",
            verification_notes="US Civilian Unemployment Rate from BLS.",
            is_active=True,
        ),
        "US_REAL_GDP": MacroSeriesDefinition(
            canonical_key="US_REAL_GDP",
            provider="FRED_ALFRED",
            provider_series_code="GDPC1",
            category=MacroCategory.OUTPUT,
            description="Real Gross Domestic Product (Billions of Chained 2017 Dollars, SAAR)",
            unit=MacroUnit.BILLIONS_USD,
            frequency=MacroFrequency.QUARTERLY,
            freshness_basis=FreshnessBasis.PUBLISHED_AT,
            source_tier=SourceTier.TIER_1_REGULATORY,
            geography="US",
            provider_native_units="Billions of Chained 2017 Dollars",
            seasonal_adjustment="Seasonally Adjusted Annual Rate",
            origin_source="U.S. Bureau of Economic Analysis",
            release_name="Gross Domestic Product",
            contract_status=ContractStatus.VERIFIED,
            expected_release_interval_days=92,
            source_url="https://fred.stlouisfed.org/series/GDPC1",
            verification_source="Federal Reserve Bank of St. Louis FRED API Series Metadata",
            verification_notes="US Real GDP from Bureau of Economic Analysis (BEA). Subject to major ALFRED revisions.",
            is_active=True,
        ),
        "US_INDUSTRIAL_PRODUCTION": MacroSeriesDefinition(
            canonical_key="US_INDUSTRIAL_PRODUCTION",
            provider="FRED_ALFRED",
            provider_series_code="INDPRO",
            category=MacroCategory.INDUSTRIAL_ACTIVITY,
            description="Industrial Production: Total Index (Index 2017=100, SA)",
            unit=MacroUnit.INDEX_POINTS,
            frequency=MacroFrequency.MONTHLY,
            freshness_basis=FreshnessBasis.PUBLISHED_AT,
            source_tier=SourceTier.TIER_1_REGULATORY,
            geography="US",
            provider_native_units="Index 2017=100",
            seasonal_adjustment="Seasonally Adjusted",
            origin_source="Board of Governors of the Federal Reserve System",
            release_name="G.17 Industrial Production and Capacity Utilization",
            contract_status=ContractStatus.VERIFIED,
            expected_release_interval_days=31,
            source_url="https://fred.stlouisfed.org/series/INDPRO",
            verification_source="Federal Reserve Bank of St. Louis FRED API Series Metadata",
            verification_notes="US Industrial Production Index from Federal Reserve Board.",
            is_active=True,
        ),
        "US_EFFECTIVE_FED_FUNDS_RATE": MacroSeriesDefinition(
            canonical_key="US_EFFECTIVE_FED_FUNDS_RATE",
            provider="FRED_ALFRED",
            provider_series_code="DFF",
            category=MacroCategory.INTEREST_RATE,
            description="Federal Funds Effective Rate (Percent, NSA)",
            unit=MacroUnit.PERCENT,
            frequency=MacroFrequency.DAILY,
            freshness_basis=FreshnessBasis.EFFECTIVE_DATE,
            source_tier=SourceTier.TIER_1_REGULATORY,
            geography="US",
            provider_native_units="Percent",
            seasonal_adjustment="Not Seasonally Adjusted",
            origin_source="Federal Reserve Bank of New York / Board of Governors",
            release_name="H.15 Selected Interest Rates",
            contract_status=ContractStatus.VERIFIED,
            expected_release_interval_days=1,
            source_url="https://fred.stlouisfed.org/series/DFF",
            verification_source="Federal Reserve Bank of St. Louis FRED API Series Metadata",
            verification_notes="Daily Effective Federal Funds Rate.",
            is_active=True,
        ),
    }

    @classmethod
    def get(cls, canonical_key: str) -> Optional[MacroSeriesDefinition]:
        return cls._DEFINITIONS.get(canonical_key)

    @classmethod
    def list_all(cls) -> List[MacroSeriesDefinition]:
        return list(cls._DEFINITIONS.values())

    @classmethod
    def list_verified_active(cls) -> List[MacroSeriesDefinition]:
        return [s for s in cls._DEFINITIONS.values() if s.is_active and s.contract_status == ContractStatus.VERIFIED]

    @classmethod
    def list_by_provider(cls, provider: str) -> List[MacroSeriesDefinition]:
        return [s for s in cls._DEFINITIONS.values() if s.provider == provider]

    @classmethod
    def list_by_geography(cls, geography: str) -> List[MacroSeriesDefinition]:
        return [s for s in cls._DEFINITIONS.values() if s.geography == geography]

    @classmethod
    def get_by_provider_code(cls, provider: str, provider_series_code: str) -> Optional[MacroSeriesDefinition]:
        for s in cls._DEFINITIONS.values():
            if s.provider == provider and s.provider_series_code == provider_series_code:
                return s
        return None
