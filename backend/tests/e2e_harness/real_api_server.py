"""TEST-ONLY harness (Phase 25D1E4): the REAL FastAPI application behind a loopback port, for the connected browser tests.

What is real: backend.api.main.app with its real middleware, the real JWT verifier (backend.infrastructure.auth) and the real require_private_owner dependency.
What is synthetic and lives ONLY here (no production module is changed, no bypass flag exists in production code):
  * a freshly generated ES256 key whose JWKS is injected by replacing the verifier's JWKS *fetch* function (the same technique the unit tests use), so no Supabase call is made;
  * the configured owner UUID (an obviously synthetic value) and the project URL (a public identifier used only to build the expected issuer);
  * `/__harness/*` routes guarded by a per-run secret header: mint a token (valid or deliberately broken), read the log of what the real app answered, change the configured owner.
Outgoing sockets to anything but loopback are refused, so nothing can reach Supabase, Oracle or Cloudflare even by accident. Never import this module from production code.
"""
import json
import os
import socket
import sys
import time
import uuid
from typing import Any, Dict, List

SECRET_ENV = "SENTINAX_HARNESS_SECRET"
PROJECT_URL_ENV = "SENTINAX_HARNESS_PROJECT_URL"
KID = "harness-kid"
AUDIENCE = "authenticated"


def _refuse_non_loopback_egress() -> None:
    original = socket.socket.connect
    allowed = {"127.0.0.1", "::1", "localhost"}

    def guarded(self, address, *a, **k):
        host = address[0] if isinstance(address, tuple) else address
        if self.family != getattr(socket, "AF_UNIX", None) and host not in allowed:
            raise ConnectionRefusedError(f"harness refuses non-loopback egress to {host!r}")
        return original(self, address, *a, **k)

    socket.socket.connect = guarded  # type: ignore[assignment]


def build_app():
    secret = os.environ.get(SECRET_ENV, "")
    project = os.environ.get(PROJECT_URL_ENV, "")
    if len(secret) < 16 or not project.startswith("https://"):
        raise SystemExit(f"{SECRET_ENV} (>=16 chars) and {PROJECT_URL_ENV} (https origin) are required")
    _refuse_non_loopback_egress()

    from backend.tests import jwt_support as js

    # Environment of the process under test: only what the verifier and the owner dependency read. No service-role or provider credentials exist here.
    for name in ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_JWT_SECRET", "GEMINI_API_KEY", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN", "TELEGRAM_BOT_TOKEN"):
        os.environ.pop(name, None)
    os.environ["SUPABASE_URL"] = project
    os.environ["SENTINAX_SUPABASE_JWT_MODE"] = "jwks"
    os.environ["SENTINAX_OWNER_USER_ID"] = js.OWNER_ID

    from backend.infrastructure import auth

    good_key = js.new_ec_key()
    foreign_key = js.new_ec_key()  # signs the "wrong signature" variant; never published in the JWKS
    jwks = js.jwks_doc(js.public_jwk(good_key, KID, "ES256"))
    auth._jwks_caches = {}
    auth._fetch_jwks_document = lambda url: jwks  # test-only injection point, identical to the unit tests'

    from backend.api.main import app as real_app

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    log: List[Dict[str, Any]] = []

    class RecordingApp:
        """Pure ASGI wrapper: records the status the REAL application answered for /private and /buffett requests. It changes nothing."""

        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http" or not (scope["path"].startswith("/private") or scope["path"].startswith("/buffett")):
                return await self.inner(scope, receive, send)
            entry = {"path": scope["path"], "method": scope["method"], "has_authorization": any(k == b"authorization" for k, _ in scope["headers"]),
                     "has_cookie": any(k == b"cookie" for k, _ in scope["headers"]), "status": None, "set_cookie": False, "query": scope.get("query_string", b"").decode()}

            async def spy(message):
                if message["type"] == "http.response.start":
                    entry["status"] = message["status"]
                    entry["set_cookie"] = any(k.lower() == b"set-cookie" for k, _ in message["headers"])
                await send(message)

            try:
                await self.inner(scope, receive, spy)
            finally:
                log.append(entry)

    def guard(request: Request):
        return request.headers.get("x-harness-secret") == secret

    async def sign(request: Request):
        if not guard(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = await request.json()
        variant = body.get("variant", "valid")
        aal = body.get("aal", "aal2")
        now = int(time.time())
        amr = [{"method": "password", "timestamp": now - 60}]
        if aal == "aal2" and variant != "no_totp":
            amr = [{"method": "totp", "timestamp": now - 5}] + amr
        claims = js.claims(sub=body.get("sub", js.OWNER_ID), iss=f"{project}/auth/v1", aal=aal, amr=amr, session_id=body.get("session_id", str(uuid.uuid4())))
        key = good_key
        if variant == "wrong_signature":
            key = foreign_key
        elif variant == "expired":
            claims.update(exp=now - 3600, iat=now - 7200)
        elif variant == "wrong_issuer":
            claims["iss"] = "https://evil.example/auth/v1"
        elif variant == "wrong_audience":
            claims["aud"] = "anon"
        elif variant == "malformed_amr":
            claims["amr"] = "totp"
        return JSONResponse({"token": js.sign(claims, key, "ES256", kid=KID)})

    async def get_log(request: Request):
        if not guard(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if request.query_params.get("clear") == "1":
            log.clear()
        return JSONResponse({"log": log})

    async def set_owner(request: Request):
        if not guard(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        value = (await request.json()).get("owner")
        if value is None:
            os.environ.pop("SENTINAX_OWNER_USER_ID", None)
        else:
            os.environ["SENTINAX_OWNER_USER_ID"] = value or js.OWNER_ID
        return JSONResponse({"ok": True})

    async def ready(request: Request):
        return JSONResponse({"ready": True})

    return Starlette(routes=[
        Route("/__harness/ready", ready),
        Route("/__harness/sign", sign, methods=["POST"]),
        Route("/__harness/log", get_log),
        Route("/__harness/owner", set_owner, methods=["POST"]),
        Mount("/", app=RecordingApp(real_app)),
    ])


if __name__ == "__main__":
    import uvicorn

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    uvicorn.run(build_app(), host="127.0.0.1", port=port, log_level="warning")
