"""FastAPI dependency providers.

Phase 25D1E2: this module holds NO privileged credential and performs NO privileged I/O. The legacy LLM-quota dependency and the API-layer
service-role client factory were retired (no production caller existed; both reached the database with the service-role key on behalf of any
merely-authenticated user). Trusted service-role access now lives only in explicitly injected clients of the private engine (e.g. the
PortfolioRepository ledger writer), never in the public API layer, and private routes authorise with require_private_owner.
"""
from fastapi import Request, HTTPException

from backend.services.chat_orchestrator import orchestrator
from backend.infrastructure.auth import verify_jwt

_RETIRED_DETAIL = "This capability is unavailable."


def get_orchestrator():
    """Dependency Injection provider for ChatOrchestrator."""
    return orchestrator


async def check_llm_quota() -> None:
    """RETIRED (Phase 25D1E2). Always fails closed; never queries a database, reads a credential or grants access.

    A route that still depends on it answers 503 for everyone, so a stray import cannot reintroduce non-owner privileged reads.
    """
    raise HTTPException(status_code=503, detail=_RETIRED_DETAIL)


async def get_current_user(request: Request) -> dict:
    """JWT Token'ı doğrular ve içindeki kullanıcı verisini (payload) döner."""
    payload = await verify_jwt(request)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired session.")
    return payload


def get_supabase_client():
    """RETIRED (Phase 25D1E2). The public API layer never creates a service-role client."""
    raise RuntimeError("get_supabase_client is retired: service-role clients are injected only into trusted engine components.")
