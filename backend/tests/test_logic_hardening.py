import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException
from typing import cast
from backend.engine.agent_states import GraphState
from backend.engine.circuit_breaker import evaluate_risk_circuit_breaker
from backend.infrastructure.auth import verify_token_string

# shadow_pnl_tracker moved to deprecated/ — tests using it are individually skipped
try:
    from backend.deprecated.shadow_pnl_tracker import _evaluate_pnl
except ImportError:
    _evaluate_pnl = None  # type: ignore



# ── 1. Circuit Breaker Logic Tests ──────────────────────────────────────────

def test_circuit_breaker_stable_market():
    """Stabil piyasada risk tartışmasının bypass edildiğini doğrular."""
    state = {
        "market_report": {
            "market_data": {"degisim": 2.5},
            "klines": [{"close": 100}] * 11
        },
        "fundamentals_report": {"financials": {"beta": 1.1}},
        "ticker": "AAPL"
    }
    result = evaluate_risk_circuit_breaker(cast(GraphState, state))
    assert result == "bypass_risk_debate"
    assert state["skip_risk_debate"] is True

def test_circuit_breaker_high_volatility():
    """%10 üzeri günlük değişimde risk tartışmasının tetiklendiğini doğrular."""
    state = {
        "market_report": {
            "market_data": {"degisim": 12.0},
            "klines": [{"close": 100}] * 11
        },
        "fundamentals_report": {"financials": {"beta": 1.1}},
        "ticker": "TSLA"
    }
    result = evaluate_risk_circuit_breaker(cast(GraphState, state))
    assert result == "trigger_risk_debate"
    assert "Aşırı günlük fiyat oynaması" in state["circuit_breaker_reason"]

def test_circuit_breaker_ma_deviation():
    """10 günlük hareketli ortalamadan %15 sapmada risk tartışmasını tetikler."""
    state = {
        "market_report": {
            "market_data": {"degisim": 1.0},
            "klines": [{"close": 100}] * 10 + [{"close": 120}] # %20 deviation from avg 100
        },
        "fundamentals_report": {"financials": {"beta": 1.1}},
        "ticker": "BTC"
    }
    result = evaluate_risk_circuit_breaker(cast(GraphState, state))
    assert result == "trigger_risk_debate"
    assert "hareketli ortalamadan sert sapma" in state["circuit_breaker_reason"]

def test_circuit_breaker_high_beta():
    """Beta > 2.0 durumunda risk tartışmasını tetikler."""
    state = {
        "market_report": {
            "market_data": {"degisim": 1.0},
            "klines": [{"close": 100}] * 11
        },
        "fundamentals_report": {"financials": {"beta": 2.5}},
        "ticker": "NVDA"
    }
    result = evaluate_risk_circuit_breaker(cast(GraphState, state))
    assert result == "trigger_risk_debate"
    assert "Beta > 2.0" in state["circuit_breaker_reason"]

# ── 2. Auth Resilience Tests ────────────────────────────────────────────────

@patch("jwt.decode")
@patch("backend.infrastructure.redis_cache.cache_get")
@patch("backend.infrastructure.auth.SUPABASE_JWT_SECRET", "dummy_secret_for_tests")
def test_auth_expired_token(mock_redis, mock_jwt_decode):
    """Süresi dolmuş token ile 401 hatası fırlatır."""
    import jwt
    mock_jwt_decode.side_effect = jwt.ExpiredSignatureError("Signature has expired")
    mock_redis.return_value = None
    
    with pytest.raises(HTTPException) as exc:
        verify_token_string("expired_token")
    assert exc.value.status_code == 401
    assert "Token has expired" in exc.value.detail

@patch("backend.infrastructure.redis_cache.cache_get")
@patch("backend.infrastructure.auth.SUPABASE_JWT_SECRET", "dummy_secret_for_tests")
def test_auth_blacklisted_token(mock_redis):
    """Kara listedeki token ile erişim engellenir."""
    mock_redis.return_value = True # Found in blacklist
    
    with pytest.raises(HTTPException) as exc:
        verify_token_string("revoked_token")
    assert exc.value.status_code == 401
    assert "This session has been signed out" in exc.value.detail

# ── 3. Shadow PnL Winner Logic Tests ────────────────────────────────────────

@pytest.mark.skip(reason="shadow_pnl_tracker deprecated. PnL logic will be rewritten for Private Engine.")
@pytest.mark.asyncio
@patch("yfinance.Ticker")
async def test_shadow_pnl_winner_logic(mock_ticker):
    """Shadow PnL hesaplamasında NEW ve OLD kararlarının performansını karşılaştırır."""
    import pandas as pd
    # Mock yfinance return
    mock_instance = MagicMock()
    # Create a real DataFrame to avoid iloc/mock issues
    df = pd.DataFrame({"Close": [110.0]})
    mock_instance.history.return_value = df
    mock_ticker.return_value = mock_instance
    
    # CASE 1: NEW correctly predicted BUY, OLD predicted SELL
    # Gain: +10. New (BUY) perf: +10. Old (SELL) perf: -10. New Wins.
    price, winner = await _evaluate_pnl("AAPL", 100, "SELL", "BUY")
    assert price == 110.0
    assert winner == "NEW"

    # CASE 2: OLD correctly predicted BUY, NEW predicted SELL
    # Gain: +10. Old (BUY) perf: +10. New (SELL) perf: -10. Old Wins.
    price, winner = await _evaluate_pnl("AAPL", 100, "BUY", "SELL")
    assert winner == "OLD"

    # CASE 3: Both predicted same direction
    # Tie logic (default comparison results in Tie or according to implementation)
    price, winner = await _evaluate_pnl("AAPL", 100, "BUY", "BUY")
    assert winner == "TIE"

