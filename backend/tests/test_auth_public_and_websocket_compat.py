"""Phase 25D1B: public Buffett stays unauthenticated; /ws/prices keeps working through the stricter verifier."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.api import websocket as ws
from backend.api.routers import buffett
from backend.infrastructure import auth
from backend.tests import jwt_support as js

KID = "kid-ws"


@pytest.fixture
def ws_app(monkeypatch):
    key = js.new_ec_key()
    monkeypatch.setenv("SUPABASE_URL", js.PROJECT_URL)
    monkeypatch.setenv("SENTINAX_SUPABASE_JWT_MODE", "jwks")
    monkeypatch.setattr(auth, "_jwks_caches", {}, raising=False)
    monkeypatch.setattr(auth, "_fetch_jwks_document", lambda url: js.jwks_doc(js.public_jwk(key, KID, "ES256")), raising=False)
    monkeypatch.setattr(ws, "POLYGON_API_KEY", "synthetic")
    monkeypatch.setattr(ws, "_polygon_connected", True)  # never start the upstream reader
    app = FastAPI()
    ws.register_websocket_routes(app)
    return TestClient(app), key


def _tok(key, **over):
    return js.sign(js.claims(**over), key, "ES256", kid=KID)


def test_ws_accepts_valid_user_token(ws_app):
    client, key = ws_app
    with client.websocket_connect(f"/ws/prices?token={_tok(key)}") as sock:
        sock.send_text('{"action": "ping"}')
    ws._clients.clear()


@pytest.mark.parametrize("make", [
    lambda k: None,
    lambda k: "garbage",
    lambda k: _tok(k, exp=1),
    lambda k: _tok(k, role="service_role"),
    lambda k: _tok(k, aud="anon"),
    lambda k: js.sign(js.claims(), js.new_ec_key(), "ES256", kid=KID),
])
def test_ws_rejects_bad_tokens_with_policy_violation(ws_app, make):
    client, key = ws_app
    tok = make(key)
    url = "/ws/prices" if tok is None else f"/ws/prices?token={tok}"
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(url):
            pass
    assert e.value.code == 1008


def test_ws_rejects_when_verifier_trust_config_missing(ws_app, monkeypatch):
    client, key = ws_app
    monkeypatch.delenv("SENTINAX_SUPABASE_JWT_MODE")
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(f"/ws/prices?token={_tok(key)}"):
            pass
    assert e.value.code == 1008


def test_public_buffett_remains_unauthenticated_and_unchanged(monkeypatch):
    for var in ("SUPABASE_URL", "SENTINAX_SUPABASE_JWT_MODE", "SENTINAX_OWNER_USER_ID"):
        monkeypatch.delenv(var, raising=False)
    assert buffett.router.dependencies == []
    app = FastAPI()
    app.include_router(buffett.router)
    c = TestClient(app)
    for path in ("/buffett/portfolio", "/buffett/history"):
        r = c.get(path)
        assert r.status_code == 200 and r.json()["data_status"] == "UNAVAILABLE"
    src = open(buffett.__file__, encoding="utf-8").read()
    assert "infrastructure.auth" not in src and "private_access" not in src
