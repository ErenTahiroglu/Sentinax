import pytest
from decimal import Decimal, getcontext
from unittest.mock import patch, MagicMock
import pandas as pd

pytestmark = pytest.mark.skip(
    reason="shadow_pnl_tracker moved to backend/deprecated/. Preserved for reference."
)

try:
    from backend.deprecated.shadow_pnl_tracker import evaluate_pnl_dynamic
except ImportError:
    evaluate_pnl_dynamic = None  # type: ignore

from unittest.mock import patch, MagicMock
import pandas as pd

# 🛡️ Test Setup
getcontext().prec = 28

@pytest.mark.asyncio
@patch("yfinance.Ticker")
async def test_extreme_precision_shitcoin(mock_ticker):
    """
    Kripto dünyasında 0.0000000000000001 gibi fiyatların 
    float truncation'a uğramadığını doğrular.
    """
    mock_instance = MagicMock()
    # Fiyat: 1.0e-16 -> 1.1e-15 (%1000 artış)
    t0 = Decimal("0.0000000000000001")
    tn = Decimal("0.0000000000000011")
    
    # Komisyon: %0.1
    comm = Decimal("0.001")
    slip = Decimal("0.0")

    # Mock Setup: yfinance must return a float Close price that Decimal(str()) can read
    mock_instance.history.return_value = pd.DataFrame({"Close": [float(tn)]})
    mock_ticker.return_value = mock_instance
    
    # Karşılaştırma: NEW (BUY) vs OLD (SELL)
    # Price 1.0e-16 -> 1.1e-15 (1000% gain)
    # New (BUY) perf: 10 - cost. Old (SELL) perf: -10 - cost.
    # New Wins.
    _, winner = await evaluate_pnl_dynamic("PEPE", t0, "SELL", "BUY", comm, slip)
    assert winner == "NEW", "Aşırı düşük fiyatlı varlıkta NEW kazanmalıydı."

@pytest.mark.asyncio
@patch("yfinance.Ticker")
async def test_massive_volume_aggregation(mock_ticker):
    """
    Milyarlarca liralık hacimlerde kuruş kaçıp kaçmadığının testi.
    """
    mock_instance = MagicMock()
    # Fiyat: 10,000,000.00 -> 10,000,005.00
    t0 = Decimal("10000000.00")
    tn = Decimal("10000005.00")
    
    mock_instance.history.return_value = pd.DataFrame({"Close": [float(tn)]})
    mock_ticker.return_value = mock_instance

    # Komisyon: %0.01 (1,000 TL)
    comm = Decimal("0.0001")
    slip = Decimal("0.0")
    # Gain (5) < Cost (1000) => HOLD Wins.
    
    _, winner = await evaluate_pnl_dynamic("HIGH_VOL", t0, "HOLD", "BUY", comm, slip)
    assert winner == "OLD", "Milyonluk hacimde kâr < maliyet süzgeci (Hold) çalışmalı."
