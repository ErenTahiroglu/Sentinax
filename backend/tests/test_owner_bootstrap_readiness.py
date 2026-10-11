"""Phase 25D1C0: operator smoke tooling, signup-disabled expectations and auth.users dependency evidence."""
import importlib.util
import io
import json
import pathlib
import re
import sys

import httpx
import pytest

from backend.infrastructure import auth
from backend.tests import jwt_support as js

REPO = pathlib.Path(__file__).resolve().parents[2]
SMOKE_PATH = REPO / "scripts" / "owner_token_smoke.py"
MIGRATIONS = sorted((REPO / "supabase" / "migrations").glob("*.sql"))


def load_smoke():
    spec = importlib.util.spec_from_file_location("owner_token_smoke", SMOKE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


KID = "kid-smoke"


@pytest.fixture
def trust(monkeypatch):
    key = js.new_ec_key()
    monkeypatch.setenv("SUPABASE_URL", js.PROJECT_URL)
    monkeypatch.setenv("SENTINAX_SUPABASE_JWT_MODE", "jwks")
    monkeypatch.setenv("SENTINAX_OWNER_USER_ID", js.OWNER_ID)
    monkeypatch.setattr(auth, "_jwks_caches", {})
    monkeypatch.setattr(auth, "_fetch_jwks_document", lambda url: js.jwks_doc(js.public_jwk(key, KID, "ES256")))
    return key


def token(key, **over):
    return js.sign(js.claims(**over), key, "ES256", kid=KID)


# ── evaluator ──────────────────────────────────────────────────────────────
def test_evaluation_reports_only_non_sensitive_booleans(trust):
    smoke = load_smoke()
    tok = token(trust)
    out = smoke.evaluate_access_token(tok)
    assert out["verifier_accepts"] is True
    assert out["alg"] == "ES256"
    assert out["aal"] == "aal2" and out["totp_evidence"] is True
    assert out["owner_match"] is True and out["owner_authorized"] is True
    assert 0 < out["seconds_until_expiry"] <= 600
    blob = json.dumps(out)
    for forbidden in (tok, js.OWNER_ID, "owner@example.invalid", js.PROJECT_URL):
        assert forbidden not in blob


def test_evaluation_flags_aal1_wrong_owner_and_bad_signature(trust):
    smoke = load_smoke()
    aal1 = smoke.evaluate_access_token(token(trust, aal="aal1", amr=[{"method": "password", "timestamp": 1}]))
    assert aal1["verifier_accepts"] is True and aal1["owner_authorized"] is False and aal1["totp_evidence"] is False
    other = smoke.evaluate_access_token(token(trust, sub=js.OTHER_ID))
    assert other["owner_match"] is False and other["owner_authorized"] is False
    forged = smoke.evaluate_access_token(js.sign(js.claims(), js.new_ec_key(), "ES256", kid=KID))
    assert forged["verifier_accepts"] is False and forged["owner_authorized"] is False


def test_evaluation_unverifiable_token_exposes_no_claims(trust):
    smoke = load_smoke()
    out = smoke.evaluate_access_token("garbage")
    assert out["verifier_accepts"] is False
    assert set(out) <= {"verifier_accepts", "alg", "owner_match", "owner_authorized", "aal", "totp_evidence", "seconds_until_expiry", "error_class"}


# ── full flow against a mocked Auth server ─────────────────────────────────
def test_smoke_flow_signs_in_verifies_totp_prints_no_secret_and_signs_out(trust):
    smoke = load_smoke()
    aal1_tok = token(trust, aal="aal1", amr=[{"method": "password", "timestamp": 1}])
    aal2_tok = token(trust)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.url.query.decode()))
        path = request.url.path
        assert request.headers["apikey"] == "sb_publishable_synthetic"
        if path == "/auth/v1/token":
            return httpx.Response(200, json={"access_token": aal1_tok, "user": {"factors": [
                {"id": "f-unverified", "factor_type": "totp", "status": "unverified"},
                {"id": "f-1", "factor_type": "totp", "status": "verified"}]}})
        if path == "/auth/v1/factors/f-1/challenge":
            assert request.headers["authorization"] == f"Bearer {aal1_tok}"
            return httpx.Response(200, json={"id": "ch-1"})
        if path == "/auth/v1/factors/f-1/verify":
            assert json.loads(request.content) == {"challenge_id": "ch-1", "code": "123456"}
            return httpx.Response(200, json={"access_token": aal2_tok})
        if path == "/auth/v1/logout":
            assert request.headers["authorization"] == f"Bearer {aal2_tok}"
            return httpx.Response(204)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = io.StringIO()
    result = smoke.run_smoke(client, js.PROJECT_URL, "sb_publishable_synthetic", "owner@example.invalid", "pw-synthetic", "123456", out=out)
    assert result["owner_authorized"] is True
    printed = out.getvalue()
    for secret in (aal1_tok, aal2_tok, "pw-synthetic", "123456", js.OWNER_ID, "owner@example.invalid"):
        assert secret not in printed
    assert seen[-1][1] == "/auth/v1/logout", "session must be closed even on success"
    assert not any(p.endswith(("/signup", "/otp", "/admin/users")) for _, p, _ in seen)


def test_smoke_flow_without_verified_factor_stops_and_signs_out(trust):
    smoke = load_smoke()
    tok = token(trust, aal="aal1", amr=[{"method": "password", "timestamp": 1}])
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/auth/v1/token":
            return httpx.Response(200, json={"access_token": tok, "user": {"factors": []}})
        return httpx.Response(204)

    out = io.StringIO()
    result = smoke.run_smoke(httpx.Client(transport=httpx.MockTransport(handler)), js.PROJECT_URL, "k", "e@x.invalid", "pw", "123456", out=out)
    assert result["owner_authorized"] is False and result["aal"] == "aal1"
    assert "/auth/v1/logout" in paths and not any("factors" in p for p in paths)


def test_smoke_refuses_non_https_or_wrong_project_url(trust):
    smoke = load_smoke()
    for bad in ("http://abcdefghijklmnopqrst.supabase.co", "https://evil.example/", "https://u:p@abcdefghijklmnopqrst.supabase.co"):
        with pytest.raises(SystemExit):
            smoke.run_smoke(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))), bad, "k", "e", "p", "123456", out=io.StringIO())


def test_smoke_script_never_prints_or_logs_tokens_by_construction():
    src = SMOKE_PATH.read_text(encoding="utf-8")
    assert "getpass" in src
    assert not re.search(r"print\([^)]*(token|password|code|secret)", src, re.I)
    assert "logging" not in src
    assert "sys.argv" not in src, "secrets must not travel on the command line"


# ── auth.users dependency evidence (deleted-user risk analysis) ────────────
def _auth_user_references():
    refs = []
    for m in MIGRATIONS:
        text = re.sub(r"--[^\n]*", "", m.read_text(encoding="utf-8"))
        for stmt in re.finditer(r"CREATE TABLE[^;]*?public\.(\w+)\s*\((.*?)\n\);", text, re.S | re.I):
            table, body = stmt.group(1), stmt.group(2)
            for col in re.finditer(r"(\w+)\s+UUID[^,\n]*REFERENCES\s+auth\.users\s*\(id\)\s*(ON DELETE (\w+(?: \w+)?))?", body, re.I):
                refs.append((table, col.group(1), (col.group(3) or "NO ACTION").upper()))
    return sorted(refs)


def test_only_known_tables_reference_auth_users_and_ledger_owner_is_restrict():
    assert _auth_user_references() == [("portfolios", "owner_id", "RESTRICT"), ("user_profiles", "user_id", "CASCADE")]


def test_other_owner_tables_are_bound_to_portfolios_not_to_auth_users():
    text = "\n".join(m.read_text(encoding="utf-8") for m in MIGRATIONS)
    for table in ("portfolio_accounts", "cash_buckets", "investment_goals", "planned_contributions", "portfolio_transactions"):
        body = re.search(rf"CREATE TABLE[^;]*?public\.{table}\s*\((.*?)\n\);", text, re.S | re.I).group(1)
        assert re.search(r"FOREIGN KEY \(portfolio_id, owner_id\)\s+REFERENCES public\.portfolios", body, re.I), table
