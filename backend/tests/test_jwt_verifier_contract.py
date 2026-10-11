"""Phase 25D1B: Supabase JWT verifier contract. Synthetic keys; no network; no Redis."""
import asyncio

import pytest
from fastapi import HTTPException, Request

from backend.infrastructure import auth
from backend.tests import jwt_support as js
from backend.tests.jwt_support import DROP

MODE_ENV = "SENTINAX_SUPABASE_JWT_MODE"
KID = "kid-current"


class FakeJWKS:
    def __init__(self, doc=None, error=None):
        self.doc, self.error, self.calls = doc, error, 0

    def __call__(self, url):
        self.calls += 1
        assert url == f"{js.PROJECT_URL}/auth/v1/.well-known/jwks.json", "JWKS URL must derive from trusted config"
        if self.error:
            raise self.error
        return self.doc


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def ec_key():
    return js.new_ec_key()


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(auth, "_monotonic", c, raising=False)
    return c


@pytest.fixture
def jwks_mode(monkeypatch, ec_key, clock):
    monkeypatch.setenv("SUPABASE_URL", js.PROJECT_URL)
    monkeypatch.setenv(MODE_ENV, "jwks")
    monkeypatch.setattr(auth, "_jwks_caches", {}, raising=False)
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", js.LEGACY_SECRET)  # must be ignored in jwks mode
    fake = FakeJWKS(js.jwks_doc(js.public_jwk(ec_key, KID, "ES256")))
    monkeypatch.setattr(auth, "_fetch_jwks_document", fake, raising=False)
    return fake


@pytest.fixture
def legacy_mode(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", js.PROJECT_URL)
    monkeypatch.setenv(MODE_ENV, "legacy_hs256")
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", js.LEGACY_SECRET)
    monkeypatch.setattr(auth, "_jwks_caches", {}, raising=False)

    def boom(url):
        raise AssertionError("legacy mode must never fetch JWKS")
    monkeypatch.setattr(auth, "_fetch_jwks_document", boom, raising=False)


def es(ec_key, **over):
    return js.sign(js.claims(**over), ec_key, "ES256", kid=KID)


def hs(secret=js.LEGACY_SECRET, **over):
    return js.sign(js.claims(**over), secret, "HS256")


def reject(token, status=401):
    with pytest.raises(HTTPException) as e:
        auth.verify_token_string(token)
    assert e.value.status_code == status, e.value.detail
    return e.value


# ── valid tokens ───────────────────────────────────────────────────────────
def test_valid_es256_token_in_jwks_mode(jwks_mode, ec_key):
    assert auth.verify_token_string(es(ec_key))["sub"] == js.OWNER_ID


def test_valid_rs256_token_in_jwks_mode(jwks_mode, monkeypatch):
    rsa_key = js.new_rsa_key()
    jwks_mode.doc = js.jwks_doc(js.public_jwk(rsa_key, "kid-rsa", "RS256"))
    tok = js.sign(js.claims(), rsa_key, "RS256", kid="kid-rsa")
    assert auth.verify_token_string(tok)["sub"] == js.OWNER_ID


def test_valid_hs256_token_in_explicit_legacy_mode(legacy_mode):
    assert auth.verify_token_string(hs())["sub"] == js.OWNER_ID


# ── mode / trust configuration fails closed, never guesses ─────────────────
@pytest.mark.parametrize("mode", [None, "", "auto", "HS256", "jwks,legacy_hs256"])
def test_unset_or_unknown_mode_fails_closed_even_with_secret_set(monkeypatch, mode):
    monkeypatch.setenv("SUPABASE_URL", js.PROJECT_URL)
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", js.LEGACY_SECRET)
    if mode is None:
        monkeypatch.delenv(MODE_ENV, raising=False)
    else:
        monkeypatch.setenv(MODE_ENV, mode)
    reject(hs(), 503)


@pytest.mark.parametrize("secret", ["", "short"])
def test_legacy_mode_requires_a_strong_secret(monkeypatch, secret):
    monkeypatch.setenv("SUPABASE_URL", js.PROJECT_URL)
    monkeypatch.setenv(MODE_ENV, "legacy_hs256")
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", secret)
    reject(js.sign(js.claims(), "x" * 40, "HS256"), 503)


@pytest.mark.parametrize("url", [None, "", "http://abcdefghijklmnopqrst.supabase.co",
                                 "https://u:p@abcdefghijklmnopqrst.supabase.co",
                                 "https://abcdefghijklmnopqrst.supabase.co/rest/v1", "not a url"])
def test_untrusted_project_url_fails_closed(jwks_mode, monkeypatch, ec_key, url):
    if url is None:
        monkeypatch.delenv("SUPABASE_URL", raising=False)
    else:
        monkeypatch.setenv("SUPABASE_URL", url)
    reject(es(ec_key), 503)
    assert jwks_mode.calls == 0


def test_modes_are_separated_hs256_rejected_in_jwks_mode(jwks_mode):
    reject(hs(), 401)


def test_modes_are_separated_asymmetric_rejected_in_legacy_mode(legacy_mode, ec_key):
    reject(es(ec_key), 401)


# ── claim validation ───────────────────────────────────────────────────────
@pytest.mark.parametrize("over", [
    {"iss": "https://evil.supabase.co/auth/v1"},
    {"iss": "supabase"},
    {"iss": DROP},
    {"aud": "anon"},
    {"aud": ["authenticated", "anon"]},
    {"aud": DROP},
    {"exp": DROP},
    {"exp": 1},
    {"iat": DROP},
    {"iat": 4102444800},
    {"nbf": 4102444800},
    {"sub": DROP},
    {"sub": ""},
    {"sub": "not-a-uuid"},
    {"sub": "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"},
    {"sub": js.OWNER_ID.replace("-", "")},
    {"sub": 12345},
    {"role": "service_role"},
    {"role": "anon"},
    {"role": DROP},
    {"is_anonymous": True},
    {"is_anonymous": "false"},
    {"is_anonymous": DROP},
    {"session_id": DROP},
    {"session_id": "not-a-uuid"},
])
def test_bad_claims_rejected(jwks_mode, ec_key, over):
    reject(es(ec_key, **over))


def test_service_role_style_token_is_never_a_user_token(jwks_mode, legacy_mode, ec_key):
    svc = {"iss": "supabase", "ref": "abcdefghijklmnopqrst", "role": "service_role", "iat": 1, "exp": 4102444800}
    reject(js.sign(svc, js.LEGACY_SECRET, "HS256"))


def test_service_role_with_forged_user_claims_rejected(jwks_mode, ec_key):
    reject(es(ec_key, role="service_role"))


# ── signature / algorithm ──────────────────────────────────────────────────
def test_alg_none_rejected(jwks_mode):
    reject(js.unsigned_none_token(js.claims()))


def test_alg_none_rejected_in_legacy_mode(legacy_mode):
    reject(js.unsigned_none_token(js.claims()))


def test_wrong_project_key_same_kid_rejected(jwks_mode):
    reject(js.sign(js.claims(), js.new_ec_key(), "ES256", kid=KID))


def test_tampered_payload_rejected(jwks_mode, ec_key):
    head, _, sig = es(ec_key).split(".")
    forged = js.b64(js.claims(sub=js.OTHER_ID))
    reject(f"{head}.{forged}.{sig}")


def test_wrong_hs256_secret_rejected(legacy_mode):
    reject(hs(js.OTHER_SECRET))


def test_algorithm_confusion_hs256_signed_with_public_key_material(jwks_mode, ec_key):
    pem = ec_key.public_key().public_bytes(
        __import__("cryptography.hazmat.primitives.serialization", fromlist=["x"]).Encoding.PEM,
        __import__("cryptography.hazmat.primitives.serialization", fromlist=["x"]).PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    import jwt as pyjwt
    try:
        tok = pyjwt.encode(js.claims(), pem, algorithm="HS256", headers={"kid": KID})
    except Exception:
        tok = js.b64({"alg": "HS256", "kid": KID}) + "." + js.b64(js.claims()) + ".AAAA"
    reject(tok)


def test_header_alg_must_match_the_key_type_for_kid(jwks_mode, ec_key):
    rsa_key = js.new_rsa_key()
    tok = js.sign(js.claims(), rsa_key, "RS256", kid=KID)  # kid maps to an EC key
    reject(tok)


def test_rs256_header_signed_by_different_rsa_key_rejected(jwks_mode):
    jwks_mode.doc = js.jwks_doc(js.public_jwk(js.new_rsa_key(), "kid-rsa", "RS256"))
    reject(js.sign(js.claims(), js.new_rsa_key(), "RS256", kid="kid-rsa"))


@pytest.mark.parametrize("hdr", [{"jku": "https://evil.example/jwks.json"},
                                 {"jwk": {"kty": "oct", "k": "AAAA"}},
                                 {"x5u": "https://evil.example/c.pem"}])
def test_key_injection_headers_rejected(jwks_mode, ec_key, hdr):
    reject(js.sign(js.claims(), ec_key, "ES256", kid=KID, headers=hdr))


def test_missing_kid_rejected_in_jwks_mode(jwks_mode, ec_key):
    reject(js.sign(js.claims(), ec_key, "ES256"))


@pytest.mark.parametrize("tok", ["", "abc", "a.b", "a.b.c", "a.b.c.d", "x" * 20000])
def test_malformed_tokens_rejected(jwks_mode, tok):
    reject(tok)


# ── JWKS: rotation, unknown kid, revocation, availability ──────────────────
def test_rotation_unknown_kid_triggers_one_refresh_then_accepts(jwks_mode, ec_key, clock):
    assert auth.verify_token_string(es(ec_key))
    new_key = js.new_ec_key()
    jwks_mode.doc = js.jwks_doc(js.public_jwk(ec_key, KID, "ES256"), js.public_jwk(new_key, "kid-new", "ES256"))
    clock.t += 60  # past the forced-refresh throttle
    tok = js.sign(js.claims(), new_key, "ES256", kid="kid-new")
    before = jwks_mode.calls
    assert auth.verify_token_string(tok)["sub"] == js.OWNER_ID
    assert jwks_mode.calls == before + 1


def test_unknown_kid_denied_and_refresh_is_throttled(jwks_mode):
    bad = js.sign(js.claims(), js.new_ec_key(), "ES256", kid="ghost")
    reject(bad)
    calls = jwks_mode.calls
    for _ in range(5):
        reject(bad)
    assert jwks_mode.calls == calls, "attacker-chosen kids must not amplify into JWKS fetches"


def test_revoked_kid_is_denied_once_cache_expires(jwks_mode, ec_key, clock):
    tok = es(ec_key)
    assert auth.verify_token_string(tok)
    jwks_mode.doc = js.jwks_doc(js.public_jwk(js.new_ec_key(), "kid-other", "ES256"))
    clock.t += auth.JWKS_CACHE_TTL_SECONDS + 1
    reject(tok)


def test_revoked_kid_window_is_bounded_by_ttl(jwks_mode, ec_key, clock):
    tok = es(ec_key)
    assert auth.verify_token_string(tok)
    jwks_mode.doc = js.jwks_doc(js.public_jwk(js.new_ec_key(), "kid-other", "ES256"))
    clock.t += auth.JWKS_CACHE_TTL_SECONDS - 1
    assert auth.verify_token_string(tok), "documented revocation window: cached key valid until TTL"
    assert auth.JWKS_CACHE_TTL_SECONDS <= 300


def test_jwks_unavailable_with_empty_cache_fails_closed_503(jwks_mode, ec_key):
    jwks_mode.error = OSError("connection refused to 10.0.0.1 secret-host")
    e = reject(es(ec_key), 503)
    assert "10.0.0.1" not in str(e.detail)


def test_jwks_unavailable_after_ttl_fails_closed_no_stale_trust(jwks_mode, ec_key, clock):
    tok = es(ec_key)
    assert auth.verify_token_string(tok)
    jwks_mode.error = OSError("down")
    clock.t += auth.JWKS_CACHE_TTL_SECONDS + 1
    reject(tok, 503)


@pytest.mark.parametrize("doc", [None, {}, {"keys": "x"}, {"keys": [{"kty": "oct", "k": "AAAA", "kid": KID}]}, {"keys": []}])
def test_malformed_or_unusable_jwks_never_trusted(jwks_mode, ec_key, doc):
    jwks_mode.doc = doc
    with pytest.raises(HTTPException) as e:
        auth.verify_token_string(es(ec_key))
    assert e.value.status_code in (401, 503)


def test_symmetric_jwk_in_jwks_never_accepted_for_hs256(jwks_mode):
    import base64
    k = base64.urlsafe_b64encode(js.LEGACY_SECRET.encode()).rstrip(b"=").decode()
    jwks_mode.doc = {"keys": [{"kty": "oct", "kid": "sym", "alg": "HS256", "k": k}]}
    tok = js.sign(js.claims(), js.LEGACY_SECRET, "HS256", kid="sym")
    with pytest.raises(HTTPException) as e:
        auth.verify_token_string(tok)
    assert e.value.status_code in (401, 503)


# ── external errors are generic ────────────────────────────────────────────
def test_errors_are_generic_and_never_leak_token_secret_or_internals(jwks_mode, ec_key, monkeypatch):
    allowed = {auth.ERR_INVALID, auth.ERR_EXPIRED, auth.ERR_UNAVAILABLE}
    cases = [
        es(ec_key, exp=1), es(ec_key, iss="x"), es(ec_key, aud="anon"), "garbage.token.value",
        js.sign(js.claims(), js.new_ec_key(), "ES256", kid=KID), js.unsigned_none_token(js.claims()),
    ]
    for tok in cases:
        e = reject(tok)
        assert e.detail in allowed
        for leaked in (tok, js.LEGACY_SECRET, "Signature", "kid", "audience", "issuer"):
            assert leaked not in e.detail
    monkeypatch.setenv(MODE_ENV, "bogus")
    e = reject(es(ec_key), 503)
    assert e.detail == auth.ERR_UNAVAILABLE
    assert "SENTINAX" not in e.detail and "SECRET" not in e.detail.upper()


def test_expired_token_keeps_compatible_message(jwks_mode, ec_key):
    e = reject(es(ec_key, exp=1))
    assert "Token has expired" in e.detail


# ── revocation: no fake guarantees ─────────────────────────────────────────
def test_redis_blacklist_is_not_consulted_and_not_a_revocation_claim(jwks_mode, ec_key, monkeypatch):
    import backend.infrastructure.redis_cache as rc
    monkeypatch.setattr(rc, "cache_get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("redis must not be consulted")))
    assert auth.verify_token_string(es(ec_key))
    src = open(auth.__file__, encoding="utf-8").read()
    assert "jwt_blacklist" not in src
    assert "redis" not in src.lower().replace("no redis", "")


def test_auth_module_has_no_swallowing_except_in_verification():
    import ast
    tree = ast.parse(open(auth.__file__, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            assert not (len(node.body) == 1 and isinstance(node.body[0], ast.Pass)), "no fail-open except/pass"


# ── verify_jwt (generic dependency) compatibility ──────────────────────────
def _request(authorization=None, query=""):
    headers = [(b"authorization", authorization.encode())] if authorization is not None else []
    return Request({"type": "http", "headers": headers, "query_string": query.encode(), "method": "GET", "path": "/"})


@pytest.mark.parametrize("hdr", [None, "", "Bearer", "Bearer ", "Basic abc", "Bearer a b", "bearer x"])
def test_verify_jwt_rejects_missing_or_malformed_header(jwks_mode, hdr):
    with pytest.raises(HTTPException) as e:
        asyncio.run(auth.verify_jwt(_request(hdr)))
    assert e.value.status_code == 401


def test_verify_jwt_ignores_token_in_query_string(jwks_mode, ec_key):
    with pytest.raises(HTTPException) as e:
        asyncio.run(auth.verify_jwt(_request(None, query=f"token={es(ec_key)}&access_token={es(ec_key)}")))
    assert e.value.status_code == 401


def test_verify_jwt_sets_request_state_user_for_existing_consumers(jwks_mode, ec_key):
    req = _request(f"Bearer {es(ec_key)}")
    payload = asyncio.run(auth.verify_jwt(req))
    assert req.state.user is payload and payload["sub"] == js.OWNER_ID


def test_dependencies_get_current_user_still_works(jwks_mode, ec_key):
    from backend.api.dependencies import get_current_user
    out = asyncio.run(get_current_user(_request(f"Bearer {es(ec_key)}")))
    assert out["sub"] == js.OWNER_ID
