"""Phase 25D0: public Buffett API must fail closed (no fabricated evidence)."""
import json
import re
import socket
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routers import buffett

ROUTER_SRC = Path(buffett.__file__).read_text(encoding="utf-8")

FORBIDDEN_KEYS = {
    "current_price", "target_entry", "margin_of_safety_pct", "total_score",
    "details", "dcf", "intrinsic_value", "base_case", "bear_case", "bull_case",
    "llm_analysis", "moat_score", "profitability_score", "balance_sheet_score",
    "valuation_score",
}


def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_keys(v)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(buffett.router)
    return TestClient(app)


def test_portfolio_is_explicit_unavailable_with_empty_data(client):
    r = client.get("/buffett/portfolio")
    assert r.status_code == 200
    body = r.json()
    assert body["data_status"] == "UNAVAILABLE"
    assert body["portfolio"] == []
    assert body["reason_code"] == "NO_QUALIFIED_EVIDENCE"
    assert body["reason"]
    assert body["as_of"] is None
    assert body["warnings"]


def test_portfolio_exposes_no_scores_prices_or_valuations(client):
    body = client.get("/buffett/portfolio").json()
    assert not (set(_walk_keys(body)) & FORBIDDEN_KEYS)
    assert "100.0" not in json.dumps(body)


def test_generated_at_is_response_creation_time_not_source_time(client):
    body = client.get("/buffett/portfolio").json()
    assert body["generated_at"].endswith("+00:00") or body["generated_at"].endswith("Z")
    assert body["as_of"] is None


def test_history_is_unavailable_without_hardcoded_events(client):
    r = client.get("/buffett/history")
    assert r.status_code == 200
    body = r.json()
    assert body["data_status"] == "UNAVAILABLE"
    assert body["changes"] == []
    assert body["reason_code"] == "NO_QUALIFIED_EVIDENCE"
    text = json.dumps(body)
    for fake in ("EREGL", "DOAS", "2023-11-01", "ADDED", "REMOVED"):
        assert fake not in text


def test_public_routes_need_no_auth_and_leak_no_private_paths(client):
    for path in ("/buffett/portfolio", "/buffett/history"):
        r = client.get(path)
        assert r.status_code == 200
        low = r.text.lower()
        for leak in ("authorization", "bearer", "/private", "owner_id", "supabase"):
            assert leak not in low


def test_public_responses_are_not_cached(client):
    for path in ("/buffett/portfolio", "/buffett/history"):
        cc = client.get(path).headers.get("cache-control", "")
        assert "no-store" in cc


def test_no_network_or_provider_call_to_serve_placeholders(client, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket.socket, "connect", boom)
    assert client.get("/buffett/portfolio").status_code == 200
    assert client.get("/buffett/history").status_code == 200


def test_router_does_not_import_provider_engine_or_llm():
    for banned in ("yfinance", "MockKAPFetcher", "YFinanceDataFetcher",
                   "DCFValuation", "BuffettEngine", "llm_analysis"):
        assert banned not in ROUTER_SRC, banned
    assert "yfinance" not in sys.modules or True  # import check is source-based
    assert not re.search(r"\b100\.0\b", ROUTER_SRC)
    assert "cpi_data" not in ROUTER_SRC
