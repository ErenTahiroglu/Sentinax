import pytest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException
from typing import cast
from backend.engine.agent_states import GraphState
from backend.engine.circuit_breaker import evaluate_risk_circuit_breaker
from backend.infrastructure.auth import verify_token_string




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

def _legacy_auth_env(monkeypatch):
    from backend.infrastructure import auth
    monkeypatch.setenv("SUPABASE_URL", "https://abcdefghijklmnopqrst.supabase.co")
    monkeypatch.setenv("SENTINAX_SUPABASE_JWT_MODE", "legacy_hs256")
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", "synthetic-legacy-secret-0123456789-abcdefghij")


def test_auth_expired_token(monkeypatch):
    """Süresi dolmuş token ile 401 hatası fırlatır (Phase 25D1B: gerçek imzalı token)."""
    from backend.tests import jwt_support as js
    _legacy_auth_env(monkeypatch)
    token = js.sign(js.claims(exp=1), js.LEGACY_SECRET, "HS256")
    with pytest.raises(HTTPException) as exc:
        verify_token_string(token)
    assert exc.value.status_code == 401
    assert "Token has expired" in exc.value.detail


def test_auth_has_no_operational_blacklist(monkeypatch):
    """Phase 25D1B: jwt_blacklist had no writer and failed open; it is no longer consulted or claimed."""
    from backend.tests import jwt_support as js
    _legacy_auth_env(monkeypatch)
    monkeypatch.setattr("backend.infrastructure.redis_cache.cache_get", lambda *a, **k: True)
    token = js.sign(js.claims(), js.LEGACY_SECRET, "HS256")
    assert verify_token_string(token)["sub"] == js.OWNER_ID
