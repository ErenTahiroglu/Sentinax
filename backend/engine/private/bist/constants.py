"""
backend/engine/private/bist/constants.py
========================================
Official Borsa İstanbul (BIST) market data constants, schema definitions,
and reference metadata facts.

Documented Source:
    - Borsa İstanbul Pay Piyasası Gün Sonu Kapanış Verileri (PAY_BULTEN_YYYYAAGG.csv)
    - Delimiter: ';' (semicolon)
    - Decimal Symbol: '.' (dot)
    - Header: 2 header rows (Row 1: Turkish, Row 2: English, Row 3+: Observations)
"""

from decimal import Decimal
from typing import Dict, List

from backend.engine.private.domain import AssetClass, Currency, InstrumentType

# Official Source Pages & Discovery
BIST_OFFICIAL_PORTAL_URL: str = "https://www.borsaistanbul.com/tr/sayfa/141/bulten-verileri"
BIST_EQUITY_DATA_URL: str = "https://www.borsaistanbul.com/tr/sayfa/25/pay-piyasasi-verileri"
BIST_BULLETIN_DIRECT_BASE_URL: str = "https://www.borsaistanbul.com/data/bulten/"
BIST_DATASTORE_PORTAL_URL: str = "https://datastore.borsaistanbul.com"

# Provider Classification Metadata
BIST_PROVIDER_NAME: str = "BIST_EOD"
BIST_PROVIDER_VERSION: str = "1.1.0"
BIST_DEFAULT_MIC: str = "XIST"

# Official Filename Pattern: PAY_BULTEN_YYYYAAGG.csv (or PAY_BULTEN_YYYYMMDD.csv)
BIST_PAY_BULTEN_PREFIX: str = "PAY_BULTEN_"

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

# Header Normalization Mappings for Documented Official PAY_BULTEN Schema
# Explicit mappings for official Turkish (Row 1) and English (Row 2) columns.
BIST_HEADER_MAPPINGS: Dict[str, str] = {
    # 1. Market Segment
    "PAZAR KODU": "market_segment",
    "PAZAR_KODU": "market_segment",
    "PAZAR": "market_segment",
    "MARKET SEGMENT": "market_segment",
    "MARKET_SEGMENT": "market_segment",

    # 2. Symbol / Instrument Code
    "PAY KODU": "symbol",
    "PAY_KODU": "symbol",
    "HISSE KODU": "symbol",
    "HISSE_KODU": "symbol",
    "HİSSE KODU": "symbol",
    "MENKUL KIYMET KODU": "symbol",
    "MENKUL_KIYMET_KODU": "symbol",
    "INSTRUMENT CODE": "symbol",
    "INSTRUMENT_CODE": "symbol",
    "SYMBOL": "symbol",
    "KOD": "symbol",

    # 3. Instrument Name
    "PAY ADI": "instrument_name",
    "PAY_ADI": "instrument_name",
    "HİSSE ADI": "instrument_name",
    "HISSE ADI": "instrument_name",
    "MENKUL KIYMET ADI": "instrument_name",
    "INSTRUMENT NAME": "instrument_name",
    "INSTRUMENT_NAME": "instrument_name",

    # 4. Previous Close
    "ONCEKI KAPANIS FIYATI": "previous_close",
    "ONCEKI_KAPANIS_FIYATI": "previous_close",
    "ÖNCEKİ KAPANIŞ FİYATI": "previous_close",
    "ONCEKI KAPANIS": "previous_close",
    "PREVIOUS CLOSING PRICE": "previous_close",
    "PREVIOUS_CLOSING_PRICE": "previous_close",
    "PREVIOUS_CLOSE": "previous_close",

    # 5. Open Price
    "ACILIS FIYATI": "open",
    "ACILIS_FIYATI": "open",
    "AÇILIŞ FİYATI": "open",
    "ACILIS": "open",
    "OPENING PRICE": "open",
    "OPENING_PRICE": "open",
    "OPEN_PRICE": "open",
    "OPEN": "open",

    # 6. Low Price
    "EN DUSUK FIYAT": "low",
    "EN_DUSUK_FIYAT": "low",
    "EN DÜŞÜK FİYAT": "low",
    "EN_DUSUK": "low",
    "LOWEST PRICE": "low",
    "LOWEST_PRICE": "low",
    "LOW_PRICE": "low",
    "LOW": "low",

    # 7. High Price
    "EN YUKSEK FIYAT": "high",
    "EN_YUKSEK_FIYAT": "high",
    "EN YÜKSEK FİYAT": "high",
    "EN_YUKSEK": "high",
    "HIGHEST PRICE": "high",
    "HIGHEST_PRICE": "high",
    "HIGH_PRICE": "high",
    "HIGH": "high",

    # 8. Close Price
    "KAPANIS FIYATI": "close",
    "KAPANIS_FIYATI": "close",
    "KAPANIŞ FİYATI": "close",
    "KAPANIS": "close",
    "CLOSING PRICE": "close",
    "CLOSING_PRICE": "close",
    "CLOSE_PRICE": "close",
    "CLOSE": "close",

    # 9. Change (%)
    "DEGISIM(%)": "change_pct",
    "DEĞİŞİM(%)": "change_pct",
    "CHANGE(%)": "change_pct",
    "CHANGE (%)": "change_pct",

    # 10. Weighted Average Price
    "GUNLUK AGIRLIKLI ORTALAMA FIYAT": "weighted_average",
    "GUNLUK_AGIRLIKLI_ORTALAMA_FIYAT": "weighted_average",
    "GÜNLÜK AĞIRLIKLI ORTALAMA FİYAT": "weighted_average",
    "AGIRLIKLI ORTALAMA FIYAT": "weighted_average",
    "AOF": "weighted_average",
    "WAP": "weighted_average",
    "DAILY WEIGHTED AVERAGE PRICE": "weighted_average",
    "WEIGHTED_AVERAGE_PRICE": "weighted_average",

    # 11. Total Trade Value / Turnover (TRY)
    "TOPLAM ISLEM HACMI": "turnover",
    "TOPLAM_ISLEM_HACMI": "turnover",
    "TOPLAM İŞLEM HACMİ": "turnover",
    "ISLEM HACMI": "turnover",
    "TOTAL TRADE VALUE": "turnover",
    "TOTAL_TRADE_VALUE": "turnover",
    "TURNOVER": "turnover",

    # 12. Total Traded Quantity / Volume (Shares/Units)
    "TOPLAM ISLEM ADEDI": "volume",
    "TOPLAM_ISLEM_ADEDI": "volume",
    "TOPLAM İŞLEM ADEDİ": "volume",
    "ISLEM MIKTARI": "volume",
    "TOTAL TRADE QUANTITY": "volume",
    "TOTAL_TRADE_QUANTITY": "volume",
    "VOLUME": "volume",

    # 13. Total Trade Count / Number of Trades
    "TOPLAM SOZLESME SAYISI": "trade_count",
    "TOPLAM_SOZLESME_SAYISI": "trade_count",
    "TOPLAM SÖZLEŞME SAYISI": "trade_count",
    "SOZLESME SAYISI": "trade_count",
    "TOTAL NUMBER OF TRADES": "trade_count",
    "TOTAL_NUMBER_OF_TRADES": "trade_count",
    "TRADE_COUNT": "trade_count",

    # Optional / Legacy Trade Date column if present in non-standard exports
    "BULTEN TARIHI": "trade_date",
    "BULTEN_TARIHI": "trade_date",
    "BÜLTEN TARİHİ": "trade_date",
    "TARIH": "trade_date",
    "DATE": "trade_date",
}

# Required Canonical Columns for a Valid PAY_BULTEN Row
# Note: trade_date comes from the verified bulletin filename / request context.
REQUIRED_BULLETIN_COLUMNS: List[str] = ["symbol", "close"]
