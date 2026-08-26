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
    from backend.deprecated.shadow_pnl_tracker import evaluate_pnl_dynamic
except ImportError:
    evaluate_pnl_dynamic = None  # type: ignore


# ── 1. Multi-User Isolation Mock Data ───────────────────────────────────────

@pytest.mark.asyncio
@patch("yfinance.Ticker")
async def test_multi_user_pnl_isolation(mock_ticker):
    """
    User A (%0.1), User B (%2.0) ve User C (%0.2) için 
    aynı piyasa verisinde farklı kazananlar olduğunu doğrular.
    """
    mock_instance = MagicMock()
    t0 = Decimal("100.0")
    
    # 1. User A Senaryosu: %5 Kazanç
    mock_instance.history.return_value = pd.DataFrame({"Close": [105.0]})
    mock_ticker.return_value = mock_instance
    
    old_dec = "SELL" # Old said sell early
    new_dec = "BUY"  # New said buy
    
    # User A (%0.1 Komisyon + %0.05 Kayma = %0.15 friction, %0.3 round-trip)
    # New Perf: 5% - 0.3% = 4.7%
    # Old Perf (SELL): -5% - 0.15% = -5.15%
    comm_a = Decimal("0.001")
    slip_a = Decimal("0.0005")
    _, winner_a = await evaluate_pnl_dynamic("AAPL", t0, old_dec, new_dec, comm_a, slip_a)
    assert winner_a == "NEW"

    # 2. User B Senaryosu: %1 Kazanç, %3 Maliyet
    mock_instance.history.return_value = pd.DataFrame({"Close": [101.0]})
    # New Perf (BUY): 1% - 6% = -5%
    # Old Perf (SELL): -1% - 3% = -4%
    # -4% > -5% => OLD Wins (Losing less is winning).
    comm_b = Decimal("0.02")
    slip_b = Decimal("0.01")
    _, winner_b = await evaluate_pnl_dynamic("AAPL", t0, old_dec, new_dec, comm_b, slip_b)
    assert winner_b == "OLD"

@pytest.mark.asyncio
@patch("yfinance.Ticker")
async def test_profit_cost_shield_logic(mock_ticker):
    """
    Maliyet Kalkanı: Eğer kâr (%0.3), maliyetten (%1.0) düşükse 
    SELL sinyalinin HOLD'dan daha kötü performans verdiğini doğrular.
    """
    mock_instance = MagicMock()
    t0 = Decimal("100.0")
    comm = Decimal("0.005") # %0.5
    slip = Decimal("0.005") # %0.5 (%1.0 friction)

    # Fiyat 99.7 (%0.3 düşüş).
    mock_instance.history.return_value = pd.DataFrame({"Close": [99.7]})
    mock_ticker.return_value = mock_instance
    
    # SELL: 0.003 (avoided loss) - 0.01 (fee) = -0.007
    # HOLD: -0.003 (actual loss)
    # HOLD Wins.
    _, winner = await evaluate_pnl_dynamic("AAPL", t0, "HOLD", "SELL", comm, slip)
    assert winner == "OLD"
