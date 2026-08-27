"""
backend/engine/private/bist/constants.py
========================================
Official Borsa İstanbul (BIST) market data constants, schema definitions,
and reference metadata facts.
"""

from decimal import Decimal
from typing import Dict, List

from backend.engine.private.domain import AssetClass, Currency, InstrumentType

# Official Source Pages and Endpoints
BIST_OFFICIAL_PORTAL_URL: str = "https://www.borsaistanbul.com/tr/sayfa/141/bulten-verileri"
BIST_EQUITY_DATA_URL: str = "https://www.borsaistanbul.com/tr/sayfa/25/pay-piyasasi-verileri"
BIST_BULLETIN_DIRECT_BASE_URL: str = "https://www.borsaistanbul.com/data/bulten/"
BIST_DATASTORE_PORTAL_URL: str = "https://datastore.borsaistanbul.com"

# Provider Classification Metadata
BIST_PROVIDER_NAME: str = "BIST_EOD"
BIST_PROVIDER_VERSION: str = "1.0.0"
BIST_DEFAULT_MIC: str = "XIST"

# ALTIN.S1 Official Reference Metadata Facts
ALTIN_S1_SYMBOL: str = "ALTIN.S1"
ALTIN_S1_CANONICAL_NAME: str = "Darphane Altın Sertifikası"
ALTIN_S1_ISSUER: str = "T.C. Hazine ve Maliye Bakanlığı Darphane ve Damga Matbaası"
ALTIN_S1_UNDERLYING: str = "gold"
ALTIN_S1_CERTIFICATE_REPRESENTATION_GRAMS: Decimal = Decimal("0.01")  # 1 certificate = 0.01g gold
ALTIN_S1_PURITY: Decimal = Decimal("0.995")  # 995/1000 purity
ALTIN_S1_ASSET_CLASS: AssetClass = AssetClass.COMMODITY
ALTIN_S1_INSTRUMENT_TYPE: InstrumentType = InstrumentType.COMMODITY_CERTIFICATE
ALTIN_S1_CURRENCY: Currency = Currency.TRY

# Header Normalization Mappings for BISTECH Bulletin CSV/TXT/ZIP
# Maps diverse Turkish and English header variants to canonical column names.
BIST_HEADER_MAPPINGS: Dict[str, str] = {
    # Trade Date
    "BULTEN_TARIHI": "trade_date",
    "BULTEN TARIHI": "trade_date",
    "BÜLTEN TARİHİ": "trade_date",
    "TARIH": "trade_date",
    "TARİH": "trade_date",
    "DATE": "trade_date",
    "TRADE_DATE": "trade_date",
    # Symbol / Instrument Code
    "HISSE_KODU": "symbol",
    "HISSE KODU": "symbol",
    "HİSSE KODU": "symbol",
    "MENKUL_KIYMET_KODU": "symbol",
    "MENKUL KIYMET KODU": "symbol",
    "INSTRUMENT_CODE": "symbol",
    "SYMBOL": "symbol",
    "KOD": "symbol",
    "TICKER": "symbol",
    # Market Segment
    "PAZAR": "market_segment",
    "PAZAR_KODU": "market_segment",
    "SEKTOR": "market_segment",
    "MARKET": "market_segment",
    "MARKET_SEGMENT": "market_segment",
    "GRUP_KODU": "market_segment",
    # Previous Close
    "ONCEKI_KAPANIS_FIYATI": "previous_close",
    "ONCEKI KAPANIS FIYATI": "previous_close",
    "ÖNCEKİ KAPANIŞ FİYATI": "previous_close",
    "ONCEKI_KAPANIS": "previous_close",
    "PREVIOUS_CLOSE": "previous_close",
    "PREV_CLOSE": "previous_close",
    # Open Price
    "ACILIS_FIYATI": "open",
    "ACILIS FIYATI": "open",
    "AÇILIŞ FİYATI": "open",
    "ACILIS": "open",
    "OPEN_PRICE": "open",
    "OPEN": "open",
    "ILK_FIYAT": "open",
    # High Price
    "EN_YUKSEK_FIYAT": "high",
    "EN YUKSEK FIYAT": "high",
    "EN YÜKSEK FİYAT": "high",
    "EN_YUKSEK": "high",
    "HIGH_PRICE": "high",
    "HIGH": "high",
    "MAX_PRICE": "high",
    # Low Price
    "EN_DUSUK_FIYAT": "low",
    "EN DUSUK FIYAT": "low",
    "EN DÜŞÜK FİYAT": "low",
    "EN_DUSUK": "low",
    "LOW_PRICE": "low",
    "LOW": "low",
    "MIN_PRICE": "low",
    # Close Price
    "KAPANIS_FIYATI": "close",
    "KAPANIS FIYATI": "close",
    "KAPANIŞ FİYATI": "close",
    "KAPANIS": "close",
    "CLOSE_PRICE": "close",
    "CLOSE": "close",
    "SON_FIYAT": "close",
    # Weighted Average Price
    "AGIRLIKLI_ORTALAMA_FIYAT": "weighted_average",
    "AGIRLIKLI ORTALAMA FIYAT": "weighted_average",
    "AĞIRLIKLI ORTALAMA FİYAT": "weighted_average",
    "AOF": "weighted_average",
    "AOF_FIYAT": "weighted_average",
    "WEIGHTED_AVERAGE_PRICE": "weighted_average",
    "WAP": "weighted_average",
    # Volume (Number of Shares / Units)
    "ISLEM_MIKTARI": "volume",
    "ISLEM MIKTARI": "volume",
    "İŞLEM MİKTARI": "volume",
    "TOPLAM_ISLEM_MIKTARI": "volume",
    "VOLUME": "volume",
    "TOTAL_VOLUME": "volume",
    "LOT": "volume",
    # Turnover (Monetary Value in TRY)
    "ISLEM_HACMI": "turnover",
    "ISLEM HACMI": "turnover",
    "İŞLEM HACMİ": "turnover",
    "TOPLAM_ISLEM_HACMI": "turnover",
    "ISLEM_HACMI_TL": "turnover",
    "TURNOVER": "turnover",
    "TOTAL_TURNOVER": "turnover",
    # Trade Count
    "SOZLESME_SAYISI": "trade_count",
    "SOZLESME SAYISI": "trade_count",
    "SÖZLEŞME SAYISI": "trade_count",
    "ISLEM_ADEDI": "trade_count",
    "ISLEM_SAYISI": "trade_count",
    "TRADE_COUNT": "trade_count",
    "NUM_TRADES": "trade_count",
}

# Required Canonical Columns for a Valid Bulletin Row
REQUIRED_BULLETIN_COLUMNS: List[str] = ["trade_date", "symbol", "close"]
