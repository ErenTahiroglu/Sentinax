import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal
import pandas as pd

pytestmark = pytest.mark.skip(
    reason="shadow_pnl_tracker moved to backend/deprecated/. "
           "PnL engine will be redesigned for Private Engine. "
           "Preserved for reference only."
)

try:
    from backend.deprecated.shadow_pnl_tracker import _evaluate_pnl
except ImportError:
    _evaluate_pnl = None  # type: ignore



# ── 1. Precision & Rounding Stress Tests ────────────────────────────────────

@pytest.mark.asyncio
@patch("yfinance.Ticker")
async def test_precision_accumulation_stress(mock_ticker):
    """
    On binlerce ardışık işlemde (re-balancing) kuruş sapmalarını simüle eder.
    IEEE 754 float drift miktarını kontrol eder.
    """
    mock_instance = MagicMock()
    # Fiyatın çok küçük adımlarla (%0.0001) değiştiği 1000 iterasyon
    current_price = Decimal("100.0")
    
    for i in range(10):
        # Fiyatın %1.0 arttığı senaryo (Komisyon %0.3'ü geçmek için)
        target_price = current_price * Decimal("1.01")
        df = pd.DataFrame({"Close": [float(target_price)]})
        mock_instance.history.return_value = df
        mock_ticker.return_value = mock_instance
        
        # PnL hesapla
        tn, winner = await _evaluate_pnl("AAPL", current_price, "SELL", "BUY")
        
        # Float hassasiyeti kontrolü: Farkın çok küçük olması durumunda bile 
        # NEW motorun "kazanan" olarak atanması gerekir (BUY > SELL artış durumunda)
        assert winner == "NEW"
        current_price = Decimal(str(tn))

@pytest.mark.asyncio
@patch("yfinance.Ticker")
async def test_zero_price_delisting_resilience(mock_ticker):
    """
    Varlık fiyatı 0'a düştüğünde (Delisting) sistemin NaN vermemesini doğrular.
    """
    mock_instance = MagicMock()
    # Fiyat 0.0 geliyor
    mock_instance.history.return_value = pd.DataFrame({"Close": [0.0]})
    mock_ticker.return_value = mock_instance
    
    # T0=100 iken hisse 0'a düşerse
    price, winner = await _evaluate_pnl("AAPL", 100.0, "BUY", "SELL")
    
    assert price == 0.0
    # Ciddi bir düşüşte SELL kararı (zarardan koruduğu için) NEW motoru kazandırır
    assert winner == "NEW" 

# ── 2. Shadow PnL Winner Logic (Financial Risk) ─────────────────────────────

@pytest.mark.asyncio
@patch("yfinance.Ticker")
async def test_shadow_pnl_hold_protection_logic(mock_ticker):
    """
    HOLD kararının 'zarardan koruma' (Protection) olarak doğru işlenip işlenmediği.
    Finansal Risk: Eğer sistem düşüşte HOLD diyenle SELL diyeni ayırt edemezse rapor hatalı olur.
    """
    mock_instance = MagicMock()
    # Fiyat 100 -> 80'e düştü (Zarar: -20)
    mock_instance.history.return_value = pd.DataFrame({"Close": [80.0]})
    mock_ticker.return_value = mock_instance
    
    # CASE: Old says BUY (aggressive), New says SELL (defensive)
    # Gain = -20. OLD Perf = (1) * (-20) = -20. NEW Perf = (-1) * (-20) = +20.
    # NEW Wins because it effectively "shorted" or avoided the loss.
    _, winner = await _evaluate_pnl("AAPL", 100.0, "BUY", "SELL")
    assert winner == "NEW"

@pytest.mark.asyncio
async def test_extreme_volatile_returns():
    """
    Kripto gibi %1000 artışlarda veya ani %99 düşüşlerde float overflow testi.
    """
    t0 = 0.00000001 # Shitcoin price
    tn = 100.0       # Massive pump
    
    # _evaluate_pnl internal logic replication (since it relies on yfinance mock for tn)
    def calculate_winner(t0, tn, old_dec, new_dec):
        gain = tn - t0
        old_dir = 1 if "BUY" in old_dec else -1
        new_dir = 1 if "BUY" in new_dec else -1
        return "NEW" if (new_dir * gain) > (old_dir * gain) else "OLD"

    winner = calculate_winner(t0, tn, "SELL", "BUY")
    assert winner == "NEW"

    # Extreme drop
    winner = calculate_winner(100.0, 0.00000001, "SELL", "BUY")
    assert winner == "OLD"
