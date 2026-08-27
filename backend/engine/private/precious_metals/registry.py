"""
backend/engine/private/precious_metals/registry.py
==================================================
Registry of verified precious metal market reference series contracts (BIST KMTP & TCMB EVDS).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional

from backend.engine.private.domain import Currency
from backend.engine.private.precious_metals.constants import (
    PreciousMetalPriceType,
    PreciousMetalType,
    PreciousMetalUnit,
)
from backend.engine.private.precious_metals.models import (
    PreciousMetalSeriesDefinition,
    SeriesVerificationStatus,
)

_PREDEFINED_SERIES: List[PreciousMetalSeriesDefinition] = [
    # ─────────────────────────────────────────────────────────────────────────
    # Borsa İstanbul KMTP Benchmark References (Originating Exchange Source)
    # Summary benchmarks from 'Fiyatlar' do not define explicit purity/settlement; purity=None, settlement=None
    # ─────────────────────────────────────────────────────────────────────────
    PreciousMetalSeriesDefinition(
        series_code="BIST_KMTP_GOLD_REF_TRY_KG",
        canonical_name="BIST KMTP Standart Altın Referans Fiyatı (TRY/KG)",
        metal=PreciousMetalType.GOLD,
        provider="BIST_KMTP",
        originating_source="BIST",
        frequency="DAILY",
        value_unit="TRY/KG",
        currency=Currency.TRY,
        quantity_unit=PreciousMetalUnit.KG,
        price_type=PreciousMetalPriceType.REFERENCE,
        purity=None,
        settlement_term=None,
        verification_status=SeriesVerificationStatus.VERIFIED,
        notes="BIST KMTP daily official standard gold reference benchmark price in TRY per KG.",
        is_active=True,
        verified_at=date(2026, 8, 27),
        source_catalog_url="https://www.borsaistanbul.com/tr/sayfa/141/bulten-verileri",
    ),
    PreciousMetalSeriesDefinition(
        series_code="BIST_KMTP_GOLD_PRICE_TRY_KG",
        canonical_name="BIST KMTP Altın Metal Fiyatı (TRY/KG)",
        metal=PreciousMetalType.GOLD,
        provider="BIST_KMTP",
        originating_source="BIST",
        frequency="DAILY",
        value_unit="TRY/KG",
        currency=Currency.TRY,
        quantity_unit=PreciousMetalUnit.KG,
        price_type=PreciousMetalPriceType.METAL_PRICE,
        purity=None,
        settlement_term=None,
        verification_status=SeriesVerificationStatus.VERIFIED,
        notes="BIST KMTP daily gold metal price benchmark in TRY per KG.",
        is_active=True,
        verified_at=date(2026, 8, 27),
        source_catalog_url="https://www.borsaistanbul.com/tr/sayfa/141/bulten-verileri",
    ),
    PreciousMetalSeriesDefinition(
        series_code="BIST_KMTP_GOLD_PRICE_USD_OZ",
        canonical_name="BIST KMTP Altın Metal Fiyatı (USD/ONS)",
        metal=PreciousMetalType.GOLD,
        provider="BIST_KMTP",
        originating_source="BIST",
        frequency="DAILY",
        value_unit="USD/ONS",
        currency=Currency.USD,
        quantity_unit=PreciousMetalUnit.TROY_OZ,
        price_type=PreciousMetalPriceType.METAL_PRICE,
        purity=None,
        settlement_term=None,
        verification_status=SeriesVerificationStatus.VERIFIED,
        notes="BIST KMTP daily gold metal price benchmark in USD per Troy Ounce.",
        is_active=True,
        verified_at=date(2026, 8, 27),
        source_catalog_url="https://www.borsaistanbul.com/tr/sayfa/141/bulten-verileri",
    ),
    PreciousMetalSeriesDefinition(
        series_code="BIST_KMTP_GOLD_PRICE_EUR_OZ",
        canonical_name="BIST KMTP Altın Metal Fiyatı (EUR/ONS)",
        metal=PreciousMetalType.GOLD,
        provider="BIST_KMTP",
        originating_source="BIST",
        frequency="DAILY",
        value_unit="EUR/ONS",
        currency=Currency.EUR,
        quantity_unit=PreciousMetalUnit.TROY_OZ,
        price_type=PreciousMetalPriceType.METAL_PRICE,
        purity=None,
        settlement_term=None,
        verification_status=SeriesVerificationStatus.VERIFIED,
        notes="BIST KMTP daily gold metal price benchmark in EUR per Troy Ounce.",
        is_active=True,
        verified_at=date(2026, 8, 27),
        source_catalog_url="https://www.borsaistanbul.com/tr/sayfa/141/bulten-verileri",
    ),
    PreciousMetalSeriesDefinition(
        series_code="BIST_KMTP_GOLD_AOF_USD_OZ",
        canonical_name="BIST KMTP Standart Altın AOF (USD/ONS)",
        metal=PreciousMetalType.GOLD,
        provider="BIST_KMTP",
        originating_source="BIST",
        frequency="DAILY",
        value_unit="USD/ONS",
        currency=Currency.USD,
        quantity_unit=PreciousMetalUnit.TROY_OZ,
        price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
        purity=None,
        settlement_term=None,
        verification_status=SeriesVerificationStatus.VERIFIED,
        notes="BIST KMTP daily volume-weighted average price (AOF) for standard gold in USD per Troy Ounce.",
        is_active=True,
        verified_at=date(2026, 8, 27),
        source_catalog_url="https://www.borsaistanbul.com/tr/sayfa/141/bulten-verileri",
    ),
    PreciousMetalSeriesDefinition(
        series_code="BIST_KMTP_GOLD_AOF_TRY_KG",
        canonical_name="BIST KMTP Standart Altın AOF (TRY/KG)",
        metal=PreciousMetalType.GOLD,
        provider="BIST_KMTP",
        originating_source="BIST",
        frequency="DAILY",
        value_unit="TRY/KG",
        currency=Currency.TRY,
        quantity_unit=PreciousMetalUnit.KG,
        price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
        purity=None,
        settlement_term=None,
        verification_status=SeriesVerificationStatus.VERIFIED,
        notes="BIST KMTP daily volume-weighted average price (AOF) for standard gold in TRY per KG.",
        is_active=True,
        verified_at=date(2026, 8, 27),
        source_catalog_url="https://www.borsaistanbul.com/tr/sayfa/141/bulten-verileri",
    ),
    # Gümüş (Silver) BIST KMTP Benchmarks
    PreciousMetalSeriesDefinition(
        series_code="BIST_KMTP_SILVER_REF_TRY_KG",
        canonical_name="BIST KMTP Standart Gümüş Referans Fiyatı (TRY/KG)",
        metal=PreciousMetalType.SILVER,
        provider="BIST_KMTP",
        originating_source="BIST",
        frequency="DAILY",
        value_unit="TRY/KG",
        currency=Currency.TRY,
        quantity_unit=PreciousMetalUnit.KG,
        price_type=PreciousMetalPriceType.REFERENCE,
        purity=None,
        settlement_term=None,
        verification_status=SeriesVerificationStatus.VERIFIED,
        notes="BIST KMTP daily official standard silver reference benchmark price in TRY per KG.",
        is_active=True,
        verified_at=date(2026, 8, 27),
        source_catalog_url="https://www.borsaistanbul.com/tr/sayfa/141/bulten-verileri",
    ),
    PreciousMetalSeriesDefinition(
        series_code="BIST_KMTP_SILVER_PRICE_TRY_KG",
        canonical_name="BIST KMTP Gümüş Metal Fiyatı (TRY/KG)",
        metal=PreciousMetalType.SILVER,
        provider="BIST_KMTP",
        originating_source="BIST",
        frequency="DAILY",
        value_unit="TRY/KG",
        currency=Currency.TRY,
        quantity_unit=PreciousMetalUnit.KG,
        price_type=PreciousMetalPriceType.METAL_PRICE,
        purity=None,
        settlement_term=None,
        verification_status=SeriesVerificationStatus.VERIFIED,
        notes="BIST KMTP daily silver metal price benchmark in TRY per KG.",
        is_active=True,
        verified_at=date(2026, 8, 27),
        source_catalog_url="https://www.borsaistanbul.com/tr/sayfa/141/bulten-verileri",
    ),
    PreciousMetalSeriesDefinition(
        series_code="BIST_KMTP_SILVER_PRICE_USD_OZ",
        canonical_name="BIST KMTP Gümüş Metal Fiyatı (USD/ONS)",
        metal=PreciousMetalType.SILVER,
        provider="BIST_KMTP",
        originating_source="BIST",
        frequency="DAILY",
        value_unit="USD/ONS",
        currency=Currency.USD,
        quantity_unit=PreciousMetalUnit.TROY_OZ,
        price_type=PreciousMetalPriceType.METAL_PRICE,
        purity=None,
        settlement_term=None,
        verification_status=SeriesVerificationStatus.VERIFIED,
        notes="BIST KMTP daily silver metal price benchmark in USD per Troy Ounce.",
        is_active=True,
        verified_at=date(2026, 8, 27),
        source_catalog_url="https://www.borsaistanbul.com/tr/sayfa/141/bulten-verileri",
    ),

    # ─────────────────────────────────────────────────────────────────────────
    # TCMB EVDS Dissemination Series (BIST-Originating Market Series)
    # Marked UNVERIFIED & is_active=False pending authenticated official metadata verification
    # ─────────────────────────────────────────────────────────────────────────
    PreciousMetalSeriesDefinition(
        series_code="TP.MK.G.ALTIN.USD",
        canonical_name="TCMB EVDS Altın Piyasası (BİST) AOF (USD/ONS)",
        metal=PreciousMetalType.GOLD,
        provider="TCMB_EVDS",
        originating_source="BIST",
        frequency="DAILY",
        value_unit="USD/ONS",
        currency=Currency.USD,
        quantity_unit=PreciousMetalUnit.TROY_OZ,
        price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
        purity=None,
        settlement_term=None,
        verification_status=SeriesVerificationStatus.UNVERIFIED,
        verification_notes="EVDS precious-metal series unverified pending authenticated official TCMB metadata response.",
        notes="TCMB EVDS dissemination candidate of BIST Gold Market Weighted Average Price in USD per Troy Ounce.",
        is_active=False,
        verified_at=None,
        source_catalog_url="https://evds3.tcmb.gov.tr/",
    ),
    PreciousMetalSeriesDefinition(
        series_code="TP.MK.G.ALTIN.TRY",
        canonical_name="TCMB EVDS Altın Piyasası (BİST) AOF (TRY/KG)",
        metal=PreciousMetalType.GOLD,
        provider="TCMB_EVDS",
        originating_source="BIST",
        frequency="DAILY",
        value_unit="TRY/KG",
        currency=Currency.TRY,
        quantity_unit=PreciousMetalUnit.KG,
        price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
        purity=None,
        settlement_term=None,
        verification_status=SeriesVerificationStatus.UNVERIFIED,
        verification_notes="EVDS precious-metal series unverified pending authenticated official TCMB metadata response.",
        notes="TCMB EVDS dissemination candidate of BIST Gold Market Weighted Average Price in TRY per KG.",
        is_active=False,
        verified_at=None,
        source_catalog_url="https://evds3.tcmb.gov.tr/",
    ),
    PreciousMetalSeriesDefinition(
        series_code="TP.MK.G.GUMUS.USD",
        canonical_name="TCMB EVDS Gümüş Piyasası (BİST) AOF (USD/ONS)",
        metal=PreciousMetalType.SILVER,
        provider="TCMB_EVDS",
        originating_source="BIST",
        frequency="DAILY",
        value_unit="USD/ONS",
        currency=Currency.USD,
        quantity_unit=PreciousMetalUnit.TROY_OZ,
        price_type=PreciousMetalPriceType.WEIGHTED_AVERAGE,
        purity=None,
        settlement_term=None,
        verification_status=SeriesVerificationStatus.UNVERIFIED,
        verification_notes="EVDS precious-metal series unverified pending authenticated official TCMB metadata response.",
        notes="TCMB EVDS dissemination candidate of BIST Silver Market Weighted Average Price in USD per Troy Ounce.",
        is_active=False,
        verified_at=None,
        source_catalog_url="https://evds3.tcmb.gov.tr/",
    ),
]


class PreciousMetalSeriesRegistry:
    """
    In-memory registry of verified precious metal market reference series definitions.
    """
    _registry: Dict[str, PreciousMetalSeriesDefinition] = {s.series_code: s for s in _PREDEFINED_SERIES}

    @classmethod
    def register(cls, definition: PreciousMetalSeriesDefinition) -> None:
        cls._registry[definition.series_code] = definition

    @classmethod
    def get(cls, series_code: str) -> Optional[PreciousMetalSeriesDefinition]:
        return cls._registry.get(series_code)

    @classmethod
    def get_verified(cls, series_code: str) -> Optional[PreciousMetalSeriesDefinition]:
        defn = cls._registry.get(series_code)
        if defn and defn.verification_status == SeriesVerificationStatus.VERIFIED and defn.is_active:
            return defn
        return None

    @classmethod
    def is_verified(cls, series_code: str) -> bool:
        return cls.get_verified(series_code) is not None

    @classmethod
    def list_by_metal(cls, metal: PreciousMetalType) -> List[PreciousMetalSeriesDefinition]:
        return [s for s in cls._registry.values() if s.metal == metal and s.is_active]

    @classmethod
    def list_all(cls) -> List[PreciousMetalSeriesDefinition]:
        return list(cls._registry.values())

