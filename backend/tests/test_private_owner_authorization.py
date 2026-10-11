"""Phase 25D1B: single-owner + AAL2/TOTP authorization dependency. Synthetic identities only."""
import dataclasses

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from backend.infrastructure import auth
from backend.infrastructure.private_access import OwnerContext, require_private_owner
from backend.tests import jwt_support as js
from backend.tests.jwt_support import DROP

KID = "kid-owner"
NOW = None


class FakeJWKS:
    def __init__(self, doc):
        self.doc, self.error = doc, None

    def __call__(self, url):
        if self.error:
            raise self.error
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
    fake = FakeJWKS(js.jwks_doc(js.public_jwk(key, KID, "ES256")))
    monkeypatch.setattr(auth, "_fetch_jwks_document", fake, raising=False)
    return fake


@pytest.fixture
def client(env):
    app = FastAPI()

    @app.get("/_owner")
    async def owner(ctx: OwnerContext = Depends(require_private_owner)):
        return {"user_id": ctx.user_id, "session_id": ctx.session_id, "aal": ctx.aal}

    @app.post("/_owner")
    async def owner_post(body: dict, ctx: OwnerContext = Depends(require_private_owner)):
        return {"user_id": ctx.user_id}

    @app.get("/_generic")
    async def generic(payload=Depends(auth.verify_jwt)):
        return {"sub": payload["sub"]}

    return TestClient(app)


def bearer(key, **over):
    return {"Authorization": "Bearer " + js.sign(js.claims(**over), key, "ES256", kid=KID)}


def test_owner_with_aal2_and_totp_is_accepted(client, key):
    r = client.get("/_owner", headers=bearer(key))
    assert r.status_code == 200
    assert r.json()["user_id"] == js.OWNER_ID and r.json()["aal"] == "aal2"


def test_non_owner_authenticated_user_is_forbidden(client, key):
    r = client.get("/_owner", headers=bearer(key, sub=js.OTHER_ID))
    assert r.status_code == 403
    assert js.OWNER_ID not in r.text


def test_non_owner_with_aal2_totp_and_owner_looking_metadata_is_forbidden(client, key):
    r = client.get("/_owner", headers=bearer(
        key, sub=js.OTHER_ID,
        user_metadata={"owner": True, "is_owner": True, "sub": js.OWNER_ID, "owner_id": js.OWNER_ID},
        app_metadata={"role": "owner", "owner_id": js.OWNER_ID}))
    assert r.status_code == 403


def test_owner_with_aal1_is_denied_with_mfa_required(client, key):
    r = client.get("/_owner", headers=bearer(key, aal="aal1", amr=[{"method": "password", "timestamp": 1700000000}]))
    assert r.status_code == 403 and r.json()["detail"] == "mfa_required"


@pytest.mark.parametrize("over", [
    {"aal": DROP}, {"aal": "AAL2"}, {"aal": ["aal2"]}, {"aal": None}, {"aal": 2},
    {"amr": DROP}, {"amr": None}, {"amr": "totp"}, {"amr": {"method": "totp"}}, {"amr": []},
    {"amr": ["totp"]}, {"amr": [{"timestamp": 1700000000}]},
    {"amr": [{"method": "totp"}]},
    {"amr": [{"method": "totp", "timestamp": "1700000000"}]},
    {"amr": [{"method": ["totp"], "timestamp": 1700000000}]},
    {"amr": [{"method": "TOTP", "timestamp": 1700000000}]},
    {"amr": [{"method": "otp", "timestamp": 1700000000}, {"method": "password", "timestamp": 1700000000}]},
    {"amr": [{"method": "password", "timestamp": 1700000000}]},
])
def test_aal2_without_valid_totp_evidence_is_denied(client, key, over):
    over = dict(over)
    over.setdefault("aal", "aal2")
    r = client.get("/_owner", headers=bearer(key, **over))
    assert r.status_code == 403 and r.json()["detail"] == "mfa_required"


@pytest.mark.parametrize("value", [None, "", "   ", "not-a-uuid", "00000000-0000-0000-0000-000000000000"])
def test_owner_config_absent_or_invalid_fails_closed(client, key, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SENTINAX_OWNER_USER_ID", raising=False)
    else:
        monkeypatch.setenv("SENTINAX_OWNER_USER_ID", value)
    r = client.get("/_owner", headers=bearer(key))
    assert r.status_code == 503
    assert js.OWNER_ID not in r.text


def test_owner_config_with_noncanonical_uuid_form_still_matches_canonical_subject(client, key, monkeypatch):
    monkeypatch.setenv("SENTINAX_OWNER_USER_ID", js.OWNER_ID.upper())
    assert client.get("/_owner", headers=bearer(key)).status_code == 200


def test_owner_identity_is_never_taken_from_request(client, key):
    other = bearer(key, sub=js.OTHER_ID)
    owner = bearer(key)
    assert client.get(f"/_owner?owner_id={js.OWNER_ID}&user_id={js.OWNER_ID}", headers=other).status_code == 403
    assert client.post("/_owner", json={"owner_id": js.OWNER_ID, "user_id": js.OWNER_ID}, headers=other).status_code == 403
    h = dict(other, **{"X-Owner-Id": js.OWNER_ID, "X-User-Id": js.OWNER_ID, "Cookie": f"owner_id={js.OWNER_ID}"})
    assert client.get("/_owner", headers=h).status_code == 403
    r = client.get(f"/_owner?owner_id={js.OTHER_ID}", headers=owner)
    assert r.status_code == 200 and r.json()["user_id"] == js.OWNER_ID


@pytest.mark.parametrize("hdr", [None, "Bearer", "Basic x"])
def test_missing_credentials_are_401_with_challenge(client, hdr):
    h = {} if hdr is None else {"Authorization": hdr}
    r = client.get("/_owner", headers=h)
    assert r.status_code == 401 and "bearer" in r.headers.get("www-authenticate", "").lower()


@pytest.mark.parametrize("over", [
    {"is_anonymous": True, "aal": "aal1"}, {"role": "anon"}, {"role": "service_role"}, {"aud": "anon"},
    {"exp": 1}, {"iss": "supabase"}, {"sub": "nope"},
])
def test_anonymous_anon_service_role_expired_wrong_issuer_are_401(client, key, over):
    assert client.get("/_owner", headers=bearer(key, **over)).status_code == 401


def test_service_role_token_is_not_an_owner_token(client, key):
    svc = {"iss": "supabase", "ref": "abcdefghijklmnopqrst", "role": "service_role", "iat": 1, "exp": 4102444800}
    tok = js.sign(svc, js.LEGACY_SECRET, "HS256")
    assert client.get("/_owner", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_jwks_unavailable_is_503_not_access(client, key, env):
    env.error = OSError("down")
    assert client.get("/_owner", headers=bearer(key)).status_code == 503


def test_token_in_query_or_body_is_ignored(client, key):
    tok = js.sign(js.claims(), key, "ES256", kid=KID)
    assert client.get(f"/_owner?token={tok}&access_token={tok}").status_code == 401


def test_owner_context_is_immutable_and_carries_only_verified_fields(client, key):
    ctx = OwnerContext(user_id=js.OWNER_ID, session_id="s", aal="aal2", amr_methods=("totp",), expires_at=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.user_id = "x"  # type: ignore[misc]


def test_generic_verify_jwt_is_not_owner_only(client, key):
    r = client.get("/_generic", headers=bearer(key, sub=js.OTHER_ID, aal="aal1", amr=[{"method": "password", "timestamp": 1}]))
    assert r.status_code == 200 and r.json()["sub"] == js.OTHER_ID


def test_unauthorized_owner_path_does_not_run_handler_side_effects(env, key):
    app = FastAPI()
    ran = []

    @app.get("/_x")
    async def x(ctx=Depends(require_private_owner)):
        ran.append(1)
        return {}
    c = TestClient(app)
    for h in (bearer(key, sub=js.OTHER_ID), bearer(key, aal="aal1"), {}):
        c.get("/_x", headers=h)
    assert ran == []
