"""Phase 25D1E1: first guarded private read API (GET /private/portfolio/summary).

Exercises the REAL FastAPI application (backend.api.main.app), the real middleware stack, the real JWT verifier and the real
require_private_owner dependency with synthetic test-only keys (no dependency override, no real Supabase, no network, no financial data).
Authenticated and unauthenticated paths are both covered; in this phase only UNAVAILABLE can ever be produced.
"""
import ast
import inspect
import json
import re
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.main import app
from backend.api.routers import buffett
from backend.infrastructure import auth
from backend.tests import jwt_support as js
from backend.tests.jwt_support import DROP

PATH = "/private/portfolio/summary"
KID = "kid-owner"
FORBIDDEN_KEYS = {
    "portfolio_value", "total_value", "net_worth", "market_value", "current_price", "price", "cost_basis", "average_cost", "pnl", "profit", "profit_loss",
    "return", "returns", "performance", "risk_score", "score", "recommendation", "recommendations", "signal", "target_price", "weight", "weights", "allocation",
    "holdings", "cash_balance", "balance", "nav", "yield", "dividend",
}
CONTRACT_KEYS = {"contract_version", "data_status", "reason_code", "reason", "source", "as_of", "generated_at", "warnings", "portfolios", "positions"}


class FakeJWKS:
    def __init__(self, doc):
        self.doc = doc

    def __call__(self, url):
        return self.doc


@pytest.fixture
def key():
    return js.new_ec_key()


@pytest.fixture
def env(monkeypatch, key):
    monkeypatch.setenv("SUPABASE_URL", js.PROJECT_URL)
    monkeypatch.setenv("SENTINAX_SUPABASE_JWT_MODE", "jwks")
    monkeypatch.setenv("SENTINAX_OWNER_USER_ID", js.OWNER_ID)
    monkeypatch.setattr(auth, "_jwks_caches", {}, raising=False)
    monkeypatch.setattr(auth, "_fetch_jwks_document", FakeJWKS(js.jwks_doc(js.public_jwk(key, KID, "ES256"))), raising=False)
    assert not app.dependency_overrides, "the owner dependency must never be overridden: the real verifier is under test"


@pytest.fixture
def client(env):
    return TestClient(app, raise_server_exceptions=False)


def token(key, **over):
    return js.sign(js.claims(**over), key, "ES256", kid=KID)


def bearer(key, **over):
    return {"Authorization": "Bearer " + token(key, **over)}


def walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_keys(v)


def assert_private_headers(r):
    cc = r.headers.get("cache-control", "").lower()
    assert "no-store" in cc and "private" in cc, cc
    assert "public" not in cc and "s-maxage" not in cc and not re.search(r"max-age=[1-9]", cc), cc
    assert r.headers.get("pragma", "").lower() == "no-cache"
    assert "authorization" in r.headers.get("vary", "").lower()
    assert r.headers.get("x-content-type-options", "").lower() == "nosniff"


# --- authentication / authorization through the real verifier --------------------------------------------------------------------------------------
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer"}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer not.a.jwt"}])
def test_missing_or_malformed_credentials_are_401(client, headers):
    r = client.get(PATH, headers=headers)
    assert r.status_code == 401 and "bearer" in r.headers.get("www-authenticate", "").lower()
    assert_private_headers(r)


def test_token_signed_by_another_key_fails(client, key):
    other = js.new_ec_key()
    r = client.get(PATH, headers={"Authorization": "Bearer " + js.sign(js.claims(), other, "ES256", kid=KID)})
    assert r.status_code == 401


@pytest.mark.parametrize("over", [{"exp": 1}, {"iss": "https://evil.example/auth/v1"}, {"aud": "somebody-else"}, {"role": "anon"}, {"role": "service_role"}, {"is_anonymous": True, "aal": "aal1"}])
def test_expired_wrong_issuer_audience_role_or_anonymous_are_401(client, key, over):
    assert client.get(PATH, headers=bearer(key, **over)).status_code == 401


def test_unsigned_none_algorithm_token_is_401(client):
    t = js.unsigned_none_token(js.claims())
    assert client.get(PATH, headers={"Authorization": f"Bearer {t}"}).status_code == 401


def test_foreign_user_with_aal2_totp_is_forbidden(client, key):
    r = client.get(PATH, headers=bearer(key, sub=js.OTHER_ID, user_metadata={"owner": True}, app_metadata={"role": "owner"}))
    assert r.status_code == 403 and js.OWNER_ID not in r.text and js.OTHER_ID not in r.text
    assert_private_headers(r)


def test_owner_with_aal1_is_denied_mfa_required(client, key):
    r = client.get(PATH, headers=bearer(key, aal="aal1", amr=[{"method": "password", "timestamp": 1700000000}]))
    assert r.status_code == 403 and r.json()["message"] == "mfa_required"
    assert_private_headers(r)


@pytest.mark.parametrize("over", [
    {"amr": [{"method": "password", "timestamp": 1700000000}]}, {"amr": [{"method": "otp", "timestamp": 1700000000}]},
    {"amr": DROP}, {"amr": None}, {"amr": "totp"}, {"amr": {"method": "totp"}}, {"amr": []}, {"amr": ["totp"]},
    {"amr": [{"method": "totp"}]}, {"amr": [{"method": "totp", "timestamp": "1700000000"}]}, {"amr": [{"method": "TOTP", "timestamp": 1700000000}]},
    {"aal": DROP}, {"aal": "AAL2"}, {"aal": 2},
])
def test_aal2_without_valid_totp_evidence_or_malformed_claims_are_403(client, key, over):
    over = dict(over)
    over.setdefault("aal", "aal2")
    r = client.get(PATH, headers=bearer(key, **over))
    assert r.status_code == 403 and r.json()["message"] == "mfa_required"


@pytest.mark.parametrize("value", [None, "", "   ", "not-a-uuid", "00000000-0000-0000-0000-000000000000"])
def test_missing_or_invalid_owner_configuration_fails_closed(client, key, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SENTINAX_OWNER_USER_ID", raising=False)
    else:
        monkeypatch.setenv("SENTINAX_OWNER_USER_ID", value)
    r = client.get(PATH, headers=bearer(key))
    assert r.status_code == 503 and js.OWNER_ID not in r.text
    assert_private_headers(r)


def test_owner_identity_is_never_taken_from_the_request(client, key):
    h = dict(bearer(key, sub=js.OTHER_ID), **{"X-Owner-Id": js.OWNER_ID, "X-User-Id": js.OWNER_ID, "Cookie": f"owner_id={js.OWNER_ID}"})
    assert client.get(f"{PATH}?owner_id={js.OWNER_ID}&user_id={js.OWNER_ID}", headers=h).status_code == 403


def test_token_in_query_or_cookie_is_ignored(client, key):
    t = token(key)
    assert client.get(f"{PATH}?access_token={t}&token={t}").status_code == 401
    assert client.get(PATH, headers={"Cookie": f"access_token={t}; sb-access-token={t}"}).status_code == 401


def test_error_responses_never_leak_secrets_or_identities(client, key, monkeypatch):
    t = token(key, sub=js.OTHER_ID)
    for r in (client.get(PATH, headers={"Authorization": f"Bearer {t}"}), client.get(PATH, headers={"Authorization": "Bearer x.y.z"}), client.get(PATH)):
        text = r.text
        for secret in (t, js.OWNER_ID, js.OTHER_ID, js.PROJECT_URL, "SENTINAX_OWNER_USER_ID", "SUPABASE_SERVICE_ROLE_KEY"):
            assert secret not in text, secret


# --- the success contract ---------------------------------------------------------------------------------------------------------------------------
def test_valid_synthetic_owner_aal2_totp_gets_the_explicit_unavailable_envelope(client, key):
    before = datetime.now(timezone.utc)
    r = client.get(PATH, headers=bearer(key))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == CONTRACT_KEYS
    assert body["contract_version"] == "private_portfolio_summary.v1"
    assert body["data_status"] == "UNAVAILABLE"
    assert body["reason_code"] == "NO_QUALIFIED_SOURCE_CONNECTED" and body["reason"]
    assert body["source"] is None and body["as_of"] is None
    assert body["portfolios"] == [] and body["positions"] == []
    assert body["warnings"] and all(isinstance(w, str) and w for w in body["warnings"])
    generated = datetime.fromisoformat(body["generated_at"])
    assert generated.tzinfo is not None and before - timedelta(seconds=5) <= generated <= datetime.now(timezone.utc) + timedelta(seconds=5)
    assert_private_headers(r)


def test_the_success_body_contains_no_financial_value_or_fabricated_evidence(client, key):
    body = client.get(PATH, headers=bearer(key)).json()
    assert not ({k.lower() for k in walk_keys(body)} & FORBIDDEN_KEYS)
    stripped = {k: v for k, v in body.items() if k not in ("generated_at", "contract_version")}
    assert not re.search(r"\d", json.dumps(stripped, ensure_ascii=False)), "no number may appear: nothing is available"
    assert "owner" not in json.dumps(body).lower() and js.OWNER_ID not in json.dumps(body)


def test_response_is_not_a_function_of_the_caller_identity(client, key):
    a = client.get(PATH, headers=bearer(key)).json()
    b = client.get(PATH, headers=bearer(key, session_id="00000000-0000-4000-8000-0000000000aa", email="x@example.invalid")).json()
    assert {k: v for k, v in a.items() if k != "generated_at"} == {k: v for k, v in b.items() if k != "generated_at"}


# --- EMPTY / AVAILABLE can never be produced without a qualified source ------------------------------------------------------------------------------
def test_empty_and_available_require_a_checked_source_and_as_of():
    from backend.api.routers.private_portfolio import PrivatePortfolioSummaryV1
    now = datetime.now(timezone.utc)
    base = dict(contract_version="private_portfolio_summary.v1", reason_code="X", reason="x", generated_at=now, warnings=["w"], portfolios=[], positions=[])
    for status in ("EMPTY", "AVAILABLE"):
        with pytest.raises(Exception):
            PrivatePortfolioSummaryV1(data_status=status, source=None, as_of=None, **base)         # no connection != confirmed-empty
        with pytest.raises(Exception):
            PrivatePortfolioSummaryV1(data_status=status, source="ledger", as_of=None, **base)
    with pytest.raises(Exception):
        PrivatePortfolioSummaryV1(data_status="UNAVAILABLE", source="ledger", as_of=now, **base)   # UNAVAILABLE carries no source/as_of
    with pytest.raises(Exception):
        PrivatePortfolioSummaryV1(data_status="EMPTY", source="ledger", as_of=now, **dict(base, positions=[{"x": 1}]))   # EMPTY means no holdings
    with pytest.raises(Exception):
        PrivatePortfolioSummaryV1(data_status="UNAVAILABLE", source=None, as_of=None, **dict(base, positions=[{"x": 1}]))


def test_production_builder_can_only_return_unavailable():
    from backend.api.routers import private_portfolio
    out = private_portfolio.build_summary()
    assert out.data_status == "UNAVAILABLE" and out.positions == [] and out.portfolios == []
    assert not [n for n in dir(private_portfolio) if re.search(r"mock|fixture|synthetic|sample|demo", n, re.I)], "no test data provider may live in the production module"


# --- HTTP surface: GET only, public/private separation, no I/O ----------------------------------------------------------------------------------------
def http_surface(prefix):
    """{path: methods} from the application's OpenAPI model (FastAPI wraps included routers lazily, so app.routes cannot be walked flatly)."""
    paths = app.openapi()["paths"]
    return {p: {m.upper() for m in ops} for p, ops in paths.items() if p == prefix or p.startswith(prefix + "/")}


def test_private_namespace_has_only_get_routes_and_exactly_one_route():
    assert http_surface("/private") == {PATH: {"GET"}}


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_no_private_write_verb_exists_even_with_a_valid_owner_token(client, key, method):
    r = getattr(client, method)(PATH, headers=bearer(key), **({"json": {"x": 1}} if method != "delete" else {}))
    assert r.status_code in (404, 405) and r.status_code != 200
    r2 = getattr(client, method)("/private/portfolio/transactions", headers=bearer(key))
    assert r2.status_code in (404, 405)


def test_public_buffett_routes_are_unchanged_unauthenticated_and_independent(client):
    assert sorted(http_surface("/buffett")) == sorted(r.path for r in buffett.router.routes)
    assert all(methods == {"GET"} for methods in http_surface("/buffett").values())
    for p in ("/buffett/portfolio", "/buffett/history"):
        r = client.get(p)
        assert r.status_code == 200 and r.json()["data_status"] == "UNAVAILABLE"
        assert r.headers["cache-control"] == "no-store"
    src = Path(buffett.__file__).read_text(encoding="utf-8")
    assert not re.search(r"private_portfolio|private_access|require_private_owner|verify_jwt", src), "public Buffett must stay independent of the private API"


def test_the_endpoint_makes_no_network_connection_and_no_financial_write(client, key, monkeypatch):
    calls = []

    def guard(self, *a, **k):
        calls.append(a)
        raise AssertionError("network connection attempted")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket.socket, "connect_ex", guard)
    r = client.get(PATH, headers=bearer(key))
    assert r.status_code == 200 and calls == []


def test_private_router_source_is_pure_no_io_and_has_no_secret_or_provider_imports():
    from backend.api.routers import private_portfolio
    src = inspect.getsource(private_portfolio)
    tree = ast.parse(src)
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imported.add((n.module or "").split(".")[0] if not (n.module or "").startswith("backend") else n.module)
    assert imported <= {"datetime", "typing", "fastapi", "pydantic", "backend.infrastructure.private_access", "backend.api.routers", "__future__"}, imported
    assert not re.search(r"os\.environ|getenv|httpx|requests|supabase|SERVICE_ROLE|cursor|\.execute\(|\.insert\(|\.rpc\(|open\(", src)
    assert "require_private_owner" in src and "Depends(" in src
    assert not re.search(r"@router\.(post|put|patch|delete)", src)
