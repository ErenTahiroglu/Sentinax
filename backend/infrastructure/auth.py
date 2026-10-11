"""Supabase user-token verification (Phase 25D1B).

Authentication only: "this is a genuine, current Supabase user token". It is NOT
authorization to see private financial data; that is `private_access.require_private_owner`.

Trust comes exclusively from operator configuration, never from token contents:
  SUPABASE_URL                    project origin -> expected issuer `<origin>/auth/v1`
                                  and JWKS URL `<origin>/auth/v1/.well-known/jwks.json`
  SENTINAX_SUPABASE_JWT_MODE      REQUIRED, exactly one of:
                                    jwks          asymmetric ES256/RS256 via the project JWKS
                                    legacy_hs256  HS256 with SUPABASE_JWT_SECRET (>= 32 bytes)
  SUPABASE_JWT_SECRET             only read in legacy_hs256 mode

Modes never fall back to each other; unset/unknown mode, missing trust configuration or an
unreachable JWKS all fail closed (503). The two modes accept disjoint algorithm sets.

Revocation / lifetime (no instantaneous-revocation claim is made):
  * An access token stays valid until its `exp`. Sign-out, factor removal or password change
    revoke the refresh token / session at Supabase, not tokens already issued.
  * jwks mode: a revoked/rotated-out key is distrusted once our JWKS cache expires
    (JWKS_CACHE_TTL_SECONDS) AND Supabase's own edge cache (documented as 10 minutes) refreshes;
    plan for up to ~15 minutes after revocation, longer if Supabase's multi-level cache applies.
  * legacy_hs256 mode: whoever holds the shared secret can mint any claims, and there is no
    key-level revocation short of rotating the secret. Supabase documents this mode as
    not recommended; server-side verification through the Auth server is a separate dependency.
  * There is no operational token blocklist: the former cache-backed one had no writer and failed open.
"""
import logging
import os
import threading
import uuid
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit

import httpx
import jwt
from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

# Legacy shared secret (kept as a module attribute for existing callers/tests); only used in legacy_hs256 mode.
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")

JWT_MODE_ENV = "SENTINAX_SUPABASE_JWT_MODE"
MODE_JWKS = "jwks"
MODE_LEGACY_HS256 = "legacy_hs256"

JWKS_CACHE_TTL_SECONDS = 300
JWKS_MIN_REFRESH_INTERVAL_SECONDS = 30
JWKS_FAILURE_BACKOFF_SECONDS = 5
_JWKS_TIMEOUT_SECONDS = 3.0
_JWKS_MAX_BYTES = 65536
_JWKS_MAX_KEYS = 20
_MAX_TOKEN_CHARS = 8192
_LEEWAY_SECONDS = 10
_MIN_LEGACY_SECRET_BYTES = 32
_EXPECTED_AUDIENCE = "authenticated"
_FORBIDDEN_HEADERS = ("jku", "jwk", "x5u", "x5c", "crit")

ERR_INVALID = "Invalid authentication token."
ERR_EXPIRED = "Token has expired. Please sign in again."
ERR_UNAVAILABLE = "Authentication is temporarily unavailable."

_monotonic = __import__("time").monotonic


class _Reject(Exception):
    """Internal rejection. `reason` is a fixed code for logs; `public` is the only text a client sees."""

    def __init__(self, status: int, public: str, reason: str):
        super().__init__(reason)
        self.status, self.public, self.reason = status, public, reason


def _invalid(reason: str) -> _Reject:
    return _Reject(401, ERR_INVALID, reason)


def _unavailable(reason: str) -> _Reject:
    return _Reject(503, ERR_UNAVAILABLE, reason)


def canonical_uuid(value) -> Optional[str]:
    """Return the value only if it is already the canonical lower-case hyphenated UUID string."""
    if not isinstance(value, str):
        return None
    try:
        return value if str(uuid.UUID(value)) == value else None
    except ValueError:
        return None


# ── trusted configuration ──────────────────────────────────────────────────
def _trusted_origin() -> str:
    raw = os.getenv("SUPABASE_URL", "").strip()
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise _unavailable("config_supabase_url")
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or parts.path not in ("", "/") or port not in (None, 443)):
        raise _unavailable("config_supabase_url")
    return f"https://{parts.hostname}"


def _mode() -> str:
    mode = os.getenv(JWT_MODE_ENV, "")
    if mode not in (MODE_JWKS, MODE_LEGACY_HS256):
        raise _unavailable("config_jwt_mode")
    return mode


# ── JWKS ───────────────────────────────────────────────────────────────────
def _fetch_jwks_document(url: str):
    with httpx.Client(timeout=httpx.Timeout(_JWKS_TIMEOUT_SECONDS), follow_redirects=False) as client:
        response = client.get(url, headers={"Accept": "application/json"})
    if response.status_code != 200 or len(response.content) > _JWKS_MAX_BYTES:
        raise OSError("jwks fetch rejected")
    return response.json()


def _load_key(jwk) -> Optional[Tuple[str, str, object]]:
    """(kid, alg, public key) for a usable asymmetric signing key, else None."""
    if not isinstance(jwk, dict):
        return None
    kid, kty = jwk.get("kid"), jwk.get("kty")
    if not isinstance(kid, str) or not kid or len(kid) > 128:
        return None
    if jwk.get("use") not in (None, "sig"):
        return None
    try:
        if kty == "EC" and jwk.get("crv") == "P-256":
            alg, algo = "ES256", jwt.algorithms.ECAlgorithm
        elif kty == "RSA":
            alg, algo = "RS256", jwt.algorithms.RSAAlgorithm
        else:
            return None
        if jwk.get("alg") not in (None, alg):
            return None
        import json as _json
        return kid, alg, algo.from_jwk(_json.dumps(jwk))
    except (jwt.PyJWTError, ValueError, TypeError):
        return None


class _JWKSCache:
    def __init__(self, url: str):
        self.url = url
        self._keys: Dict[str, Tuple[str, object]] = {}
        self._fetched_at: Optional[float] = None
        self._failed_at: Optional[float] = None
        self._lock = threading.Lock()

    def _refresh(self, now: float) -> None:
        if self._failed_at is not None and now - self._failed_at < JWKS_FAILURE_BACKOFF_SECONDS:
            raise _unavailable("jwks_backoff")
        try:
            doc = _fetch_jwks_document(self.url)
            if not isinstance(doc, dict) or not isinstance(doc.get("keys"), list) or len(doc["keys"]) > _JWKS_MAX_KEYS:
                raise ValueError("malformed jwks")
        except (OSError, ValueError, TypeError, httpx.HTTPError):
            self._keys, self._fetched_at, self._failed_at = {}, None, now
            raise _unavailable("jwks_unavailable")
        keys: Dict[str, Tuple[str, object]] = {}
        ambiguous = set()
        for jwk in doc["keys"]:
            loaded = _load_key(jwk)
            if loaded is None:
                continue
            kid, alg, key = loaded
            if kid in keys:
                ambiguous.add(kid)
            keys[kid] = (alg, key)
        for kid in ambiguous:
            keys.pop(kid, None)
        # Replace wholesale: a key removed upstream stops being trusted at the next refresh.
        self._keys, self._fetched_at, self._failed_at = keys, now, None

    def key_for(self, kid: str, alg: str):
        with self._lock:
            now = _monotonic()
            if self._fetched_at is None or now - self._fetched_at >= JWKS_CACHE_TTL_SECONDS:
                self._refresh(now)
            elif kid not in self._keys and now - self._fetched_at >= JWKS_MIN_REFRESH_INTERVAL_SECONDS:
                self._refresh(now)  # rotation: one throttled forced refresh for an unknown kid
            entry = self._keys.get(kid)
        if entry is None or entry[0] != alg:
            raise _invalid("unknown_kid_or_alg_mismatch")
        return entry[1]


_jwks_caches: Dict[str, _JWKSCache] = {}
_jwks_caches_lock = threading.Lock()


def _jwks_cache(url: str) -> _JWKSCache:
    with _jwks_caches_lock:
        cache = _jwks_caches.get(url)
        if cache is None:
            cache = _jwks_caches[url] = _JWKSCache(url)
        return cache


# ── verification ───────────────────────────────────────────────────────────
def _resolve_trust() -> Tuple[Tuple[str, ...], str, Callable[[dict], object]]:
    """(allowed algorithms, expected issuer, key resolver(header)) from trusted configuration only."""
    mode = _mode()
    issuer = _trusted_origin() + "/auth/v1"
    if mode == MODE_LEGACY_HS256:
        secret = SUPABASE_JWT_SECRET
        if not isinstance(secret, str) or len(secret.encode("utf-8")) < _MIN_LEGACY_SECRET_BYTES:
            raise _unavailable("config_legacy_secret")
        return ("HS256",), issuer, lambda header: secret
    cache = _jwks_cache(issuer + "/.well-known/jwks.json")

    def resolve(header: dict):
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise _invalid("missing_kid")
        return cache.key_for(kid, header["alg"])

    return ("ES256", "RS256"), issuer, resolve


def _decode(token) -> dict:
    allowed, issuer, resolve_key = _resolve_trust()
    if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_CHARS or token.count(".") != 2:
        raise _invalid("malformed")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise _invalid("malformed_header")
    if not isinstance(header, dict) or any(h in header for h in _FORBIDDEN_HEADERS):
        raise _invalid("forbidden_header")
    if header.get("alg") not in allowed:
        raise _invalid("alg_not_allowed")
    key = resolve_key(header)
    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=[header["alg"]],
            audience=_EXPECTED_AUDIENCE,
            issuer=issuer,
            leeway=_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "sub", "aud", "iss"], "verify_signature": True},
        )
    except jwt.ExpiredSignatureError:
        raise _Reject(401, ERR_EXPIRED, "expired")
    except (jwt.PyJWTError, ValueError, TypeError):
        raise _invalid("jwt_rejected")
    return _check_user_claims(payload)


def _check_user_claims(payload: dict) -> dict:
    # PyJWT accepts an `aud` list that merely contains the audience; require the exact user audience.
    if payload.get("aud") != _EXPECTED_AUDIENCE:
        raise _invalid("aud")
    if canonical_uuid(payload.get("sub")) is None:
        raise _invalid("sub")
    if payload.get("role") != "authenticated":
        raise _invalid("role")
    if payload.get("is_anonymous") is not False:
        raise _invalid("is_anonymous")
    if canonical_uuid(payload.get("session_id")) is None:
        raise _invalid("session_id")
    return payload


def verify_token_string(token: str) -> dict:
    """Verify a raw Supabase user access token. Sync (used by WebSocket and the HTTP dependency)."""
    try:
        return _decode(token)
    except _Reject as r:
        logger.warning("Zero Trust: token rejected (%s).", r.reason)
        headers = {"WWW-Authenticate": "Bearer"} if r.status == 401 else None
        raise HTTPException(status_code=r.status, detail=r.public, headers=headers)


async def verify_jwt(request: Request):
    """Authorization: Bearer <token> -> verified claims (authentication, not owner authorization)."""
    authorization = request.headers.get("Authorization")
    token = authorization[len("Bearer "):] if authorization and authorization.startswith("Bearer ") else ""
    if not token or any(c.isspace() for c in token):
        logger.warning("Zero Trust: missing or malformed Authorization header.")
        raise HTTPException(
            status_code=401,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = await run_in_threadpool(verify_token_string, token)
    request.state.user = payload
    return payload
