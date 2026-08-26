"""
🧩 Puzzle Parça: Pazar Algılama ve Fon Sınıflandırma
=====================================================
Ticker sembollerinin hangi pazara (US / TR / TEFAS) ait olduğunu tespit eder.
TEFAS fonları için Katılım fon sınıflandırması ve ilk işlem tarihi bilgisi sağlar.

Kullanım:
    from backend.data.market_detector import detect_market, classify_fund
"""

import logging

logger = logging.getLogger(__name__)

# ── Bilinen BIST hisseleri (hızlı eşleşme) ──────────────────────────────────
_BILINEN_BIST = {
    "THYAO", "ASELS", "GARAN", "AKBNK", "YKBNK", "EREGL", "BIMAS",
    "SAHOL", "KCHOL", "SISE", "TUPRS", "FROTO", "TOASO", "TCELL",
    "PGSUS", "TAVHL", "EKGYO", "KOZAL", "SASA", "TTKOM", "AKSA",
    "ARCLK", "DOHOL", "ENKAI", "HALKB", "ISCTR", "MGROS", "PETKM",
    "SOKM", "VESTL", "VAKBN", "KOZAA", "GUBRF", "KRDMD", "AEFES",
    "CIMSA", "CEMTS", "OTKAR", "BRISA", "AGHOL", "TSKB", "ALARK",
    "ISDMR", "NTHOL", "ISGYO", "KLRHO",
}


def detect_market(ticker: str) -> tuple:
    """
    Ticker'ın pazarını otomatik algılar (Network isteği olmadan, Kural Tabanlı).
    
    Desteklenen pazarlar: TR (BIST), TR/TEFAS, US
    Kapsam dışı (kripto vb.): UNKNOWN döner.
    
    Returns:
        (market, fetcher_ticker, is_tefas)
        Örn: ("US", "AAPL", False) veya ("TR", "TP2", True)
    """
    ticker = ticker.upper().strip()
    
    # 0. Kripto Para — Kapsam Dışı
    # Sentinax Private Engine kripto varlıkları kapsamaz.
    if ticker.endswith("USDT") or ticker.endswith("-USD"):
        return "UNKNOWN", ticker, False
    
    # 1. Açıkça BIST formatıysa (.IS soneki)
    if ticker.endswith(".IS"):
        return "TR", ticker, False
    
    # 2. Bilinen BIST hisselerini hızlı eşle
    if ticker in _BILINEN_BIST:
        return "TR", f"{ticker}.IS", False
    
    # 3. Kısa kod kriteri → TEFAS Fonu
    #    Tam 3 harfliyse ve sadece harflerden veya rakamlardan oluşuyorsa (TP2, ZP8 vb.)
    if len(ticker) == 3 and ticker.isalnum():
        return "TR", ticker, True
        
    # 4. Geri kalan → US olarak varsayılan
    return "US", ticker, False



def classify_fund(ticker: str) -> dict:
    """
    TEFAS fonunun Katılım (İslami) fon olup olmadığını tespit eder.
    (Network İsteği Tamamen Kapatılmıştır - Sabit Çıktı Üretir)
    
    Returns:
        {
            "status": "Katılım Fonu Değil",
            "is_etf": True,
            "fund_note": "Açıklama...",
            "fund_start_date": None,
            "fund_age": "",
            "purification_ratio": 0,
            "debt_ratio": 0,
            "holdings_str": ""
        }
    """
    return {
        "status": "Katılım Fonu Değil",
        "is_etf": True,
        "holdings_str": "",
        "purification_ratio": 0,
        "debt_ratio": 0,
        "fund_note": "Fon bilgileri tefas_scraper.py üzerinden sorgulanmalıdır.",
        "fund_start_date": None,
        "fund_age": "",
    }
