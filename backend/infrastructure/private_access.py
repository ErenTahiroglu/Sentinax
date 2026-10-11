"""Single-owner authorization for private routes (Phase 25D1B).

Valid authentication is necessary but never sufficient. The owner is the one operator-configured
UUID in SENTINAX_OWNER_USER_ID; it is never taken from the request, user metadata or the browser.
Private access additionally requires `aal2` with Supabase TOTP evidence in the signed `amr` claim.
This module protects the FastAPI layer only; PostgREST/RLS and service-role paths are separate.
"""
import hmac
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Tuple

from fastapi import HTTPException, Request

from backend.infrastructure.auth import ERR_UNAVAILABLE, canonical_uuid, verify_jwt

logger = logging.getLogger(__name__)

OWNER_ENV = "SENTINAX_OWNER_USER_ID"
_REQUIRED_AAL = "aal2"
_REQUIRED_METHOD = "totp"


@dataclass(frozen=True)
class OwnerContext:
    user_id: str
    session_id: str
    aal: str
    amr_methods: Tuple[str, ...]
    expires_at: int


def configured_owner_id() -> str:
    """Canonical owner UUID from operator configuration; any problem fails closed (503)."""
    raw = os.getenv(OWNER_ENV, "").strip()
    try:
        parsed = uuid.UUID(raw)
    except ValueError:
        logger.error("Private access disabled: %s absent or not a UUID.", OWNER_ENV)
        raise HTTPException(status_code=503, detail=ERR_UNAVAILABLE)
    if parsed.int == 0:
        logger.error("Private access disabled: %s is the nil UUID.", OWNER_ENV)
        raise HTTPException(status_code=503, detail=ERR_UNAVAILABLE)
    return str(parsed)


def _totp_methods(claims: dict) -> Tuple[str, ...]:
    amr = claims.get("amr")
    if not isinstance(amr, list):
        return ()
    methods = []
    for entry in amr:
        if isinstance(entry, dict) and isinstance(entry.get("method"), str) and type(entry.get("timestamp")) is int:
            methods.append(entry["method"])
    return tuple(methods)


def authorize_private_owner(claims: dict) -> OwnerContext:
    """Owner + aal2 + TOTP check on already-verified claims."""
    owner = configured_owner_id()
    subject = canonical_uuid(claims.get("sub"))
    if subject is None or not hmac.compare_digest(subject.encode(), owner.encode()):
        raise HTTPException(status_code=403, detail="forbidden")
    methods = _totp_methods(claims)
    if claims.get("aal") != _REQUIRED_AAL or _REQUIRED_METHOD not in methods:
        raise HTTPException(status_code=403, detail="mfa_required")
    return OwnerContext(
        user_id=subject,
        session_id=claims["session_id"],
        aal=_REQUIRED_AAL,
        amr_methods=methods,
        expires_at=int(claims["exp"]),
    )


async def require_private_owner(request: Request) -> OwnerContext:
    """FastAPI dependency for owner-only routes. Authenticates first, then checks configuration."""
    claims = await verify_jwt(request)
    context = authorize_private_owner(claims)
    request.state.owner_context = context
    return context
