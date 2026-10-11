"""Phase 25D1E2: privileged-access containment.

A) GET /api/health is a minimal, unauthenticated, NON-privileged process-liveness probe: no credential, no database, no network, no internals.
B) The legacy check_llm_quota dependency and the API-layer get_supabase_client factory are retired: they perform no privileged I/O and cannot authorise anything.
C) The service-role key is no longer read anywhere in the public API layer (backend/api), so a stray import cannot reintroduce non-owner privileged reads.
Everything runs through the real FastAPI app with synthetic keys and network guards. No live service, credential or financial data is involved.
"""
import ast
import asyncio
import os
import re
import socket
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from backend.api import dependencies
from backend.api.main import app
from backend.infrastructure import auth
from backend.tests import jwt_support as js

ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "backend" / "api"
KID = "kid-owner"
SENTINEL_URL = "https://internal-secret-host.example"
SENTINEL_KEY = "sentinel-service-role-value-should-never-appear"


class FakeJWKS:
    def __init__(self, doc):
        self.doc = doc

    def __call__(self, url):
        return self.doc


@pytest.fixture
def no_io(monkeypatch):
    """Any network connection or httpx request, and any read of the service-role key, is a test failure."""
    seen = {"net": [], "env": []}

    def blocked(*a, **k):
        seen["net"].append(a)
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)                    # real egress transports only: TestClient uses its own ASGI transport
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)
    original = os._Environ.__getitem__

    def spy(self, key):
        seen["env"].append(key)
        return original(self, key)

    monkeypatch.setattr(os._Environ, "__getitem__", spy)
    return seen


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", SENTINEL_URL)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", SENTINEL_KEY)


# --- A. public health: minimal liveness --------------------------------------------------------------------------------------------------------
def test_health_is_anonymous_and_returns_exactly_the_minimal_liveness_body(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_health_responses_are_not_cacheable_and_nosniff(client):
    r = client.get("/api/health")
    assert "no-store" in r.headers["cache-control"].lower()
    assert r.headers.get("x-content-type-options", "").lower() == "nosniff"


def test_health_makes_no_network_or_database_request_and_never_reads_the_service_role_key(client, configured, no_io):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert no_io["net"] == []
    assert "SUPABASE_SERVICE_ROLE_KEY" not in no_io["env"] and "SUPABASE_URL" not in no_io["env"]


def test_health_works_with_no_supabase_or_redis_configuration_and_has_no_privileged_fallback(client, monkeypatch, no_io):
    for name in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_ANON_KEY", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"} and no_io["net"] == []


def test_health_cannot_leak_internal_errors_even_if_dependencies_would_fail(client, configured, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError(f"db down at {SENTINEL_URL} key={SENTINEL_KEY} owner={js.OWNER_ID}")

    monkeypatch.setattr(httpx.AsyncClient, "get", boom, raising=False)
    monkeypatch.setattr(socket.socket, "connect", boom)
    from backend.api import main
    monkeypatch.setattr(main, "cache_is_redis_active", boom)
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    for secret in (SENTINEL_URL, SENTINEL_KEY, js.OWNER_ID, "portfolio", "redis", "supabase", "error", "Traceback"):
        assert secret.lower() not in r.text.lower(), secret


def test_health_body_has_no_portfolio_owner_or_dependency_information(client, configured):
    body = client.get("/api/health").json()
    assert set(body) == {"status"}
    assert not re.search(r"portfolio|owner|count|redis|supabase|key|url|token", str(body), re.I)


def test_health_stays_compatible_with_legitimate_probes(client):
    assert client.get("/api/health").status_code == 200                                    # render healthCheckPath, keep-alive cron, frontend client
    head = client.head("/api/health")                                                       # wget --spider (docker-compose healthcheck) issues HEAD first
    assert head.status_code == 200 and head.content == b""
    assert client.post("/api/health").status_code == 405
    root = ROOT
    assert 'healthCheckPath: "/api/health"' in (root / "render.yaml").read_text(encoding="utf-8")
    assert "/api/health" in (root / "docker-compose.yml").read_text(encoding="utf-8")
    helper = (root / "frontend/js/network/api_helpers.js").read_text(encoding="utf-8")
    assert not re.search(r"\.(redis|supabase)\b", helper), "no consumer may depend on dependency-status fields"


def test_health_handler_source_is_pure_no_io():
    from backend.api import main
    src = Path(main.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name == "health_check")
    body = "\n".join(ast.get_source_segment(src, n) for n in fn.body[1:])                  # skip the docstring: prose may name what the endpoint deliberately does not do
    assert not any(isinstance(n, (ast.Await, ast.AsyncWith)) for n in ast.walk(fn)), "liveness must not await any I/O"
    assert not re.search(r"getenv|environ|httpx|SERVICE_ROLE|supabase|rest/v1|portfolios|str\(e\)|cache_", body, re.I), body


# --- B. retired legacy quota dependency and API-layer service-role factory ---------------------------------------------------------------------------
def test_check_llm_quota_has_no_production_caller_or_route():
    callers = []
    for path in (ROOT / "backend").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("backend/tests/") or rel == "backend/api/dependencies.py":
            continue
        if re.search(r"\bcheck_llm_quota\b|\bget_supabase_client\b", path.read_text(encoding="utf-8", errors="ignore")):
            callers.append(rel)
    assert callers == [], callers
    assert not app.dependency_overrides


def test_retired_quota_dependency_fails_closed_without_any_privileged_io(configured, no_io):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as err:
        asyncio.run(dependencies.check_llm_quota())
    assert err.value.status_code == 503
    assert SENTINEL_KEY not in str(err.value.detail) and SENTINEL_URL not in str(err.value.detail)
    assert no_io["net"] == [] and "SUPABASE_SERVICE_ROLE_KEY" not in no_io["env"] and "SUPABASE_URL" not in no_io["env"]
    assert not hasattr(dependencies, "httpx"), "the dependencies module must not carry an HTTP client any more"


def test_retired_service_client_factory_creates_nothing_and_reads_no_secret(configured, no_io, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "supabase", None)                  # any attempt to import the client library would raise ImportError, not RuntimeError
    with pytest.raises(RuntimeError):
        dependencies.get_supabase_client()
    assert "SUPABASE_SERVICE_ROLE_KEY" not in no_io["env"] and "SUPABASE_URL" not in no_io["env"]


@pytest.fixture
def jwks_env(monkeypatch):
    key = js.new_ec_key()
    monkeypatch.setenv("SUPABASE_URL", js.PROJECT_URL)
    monkeypatch.setenv("SENTINAX_SUPABASE_JWT_MODE", "jwks")
    monkeypatch.setenv("SENTINAX_OWNER_USER_ID", js.OWNER_ID)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", SENTINEL_KEY)
    monkeypatch.setattr(auth, "_jwks_caches", {}, raising=False)
    monkeypatch.setattr(auth, "_fetch_jwks_document", FakeJWKS(js.jwks_doc(js.public_jwk(key, KID, "ES256"))), raising=False)
    return key


def _accidental_app():
    """What a careless future route would look like: it imports the legacy quota dependency."""
    a = FastAPI()

    @a.get("/_accidental")
    async def accidental(user_id=Depends(dependencies.check_llm_quota)):
        return {"granted_to": user_id}

    return a


@pytest.mark.parametrize("who", ["owner", "non_owner"])
def test_a_route_that_accidentally_depends_on_the_retired_quota_never_grants_access(jwks_env, no_io, who):
    sub = js.OWNER_ID if who == "owner" else js.OTHER_ID
    tok = js.sign(js.claims(sub=sub, user_metadata={"subscription_tier": "pro", "billing_cycle_start": "2099-01-01T00:00:00"}, app_metadata={"tier": "pro"}), jwks_env, "ES256", kid=KID)
    r = TestClient(_accidental_app(), raise_server_exceptions=False).get("/_accidental?tier=pro&billing_cycle_start=2099-01-01&user_id=" + js.OWNER_ID, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 503 and "granted_to" not in r.text and sub not in r.text
    assert no_io["net"] == [] and "SUPABASE_SERVICE_ROLE_KEY" not in no_io["env"]


def test_unauthenticated_request_to_such_a_route_is_also_denied(jwks_env, no_io):
    assert TestClient(_accidental_app(), raise_server_exceptions=False).get("/_accidental").status_code in (401, 503)


# --- C. the public API layer no longer touches the service-role credential --------------------------------------------------------------------------------
def test_backend_api_layer_does_not_use_the_service_role_credential_or_postgrest():
    offenders = []
    for path in API_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"SUPABASE_SERVICE_ROLE_KEY|service_role|rest/v1|create_client", text):
            offenders.append((path.relative_to(ROOT).as_posix(), text[max(0, m.start() - 40): m.end() + 20].replace("\n", " ")))
    # the only permitted mention is the start-up presence check list in main.validate_critical_env (a name, never a value use)
    assert [o for o in offenders if not (o[0] == "backend/api/main.py" and "critical_vars" in o[1])] == [], offenders


def test_valid_private_owner_authorization_is_unaffected(jwks_env, no_io):
    c = TestClient(app, raise_server_exceptions=False)
    ok = js.sign(js.claims(), jwks_env, "ES256", kid=KID)
    assert c.get("/private/portfolio/summary", headers={"Authorization": f"Bearer {ok}"}).json()["data_status"] == "UNAVAILABLE"
    other = js.sign(js.claims(sub=js.OTHER_ID), jwks_env, "ES256", kid=KID)
    assert c.get("/private/portfolio/summary", headers={"Authorization": f"Bearer {other}"}).status_code == 403


def test_service_role_inventory_document_classifies_every_known_privileged_entry_point():
    doc = (ROOT / "docs" / "PHASE_25D1E2_PRIVILEGED_ACCESS_CONTAINMENT.md").read_text(encoding="utf-8")
    for name in ("/api/health", "check_llm_quota", "get_supabase_client", "backend/infrastructure/scheduler.py", "oracle_worker", "supabase/functions/analyze",
                 "PortfolioRepository", "render.yaml", "/api/metrics"):
        assert name in doc, name
