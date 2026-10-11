#!/usr/bin/env python3
"""Operator-run, read-only compatibility smoke test of the REAL Supabase token contract (Phase 25D1C0).

Signs the owner in against the real Auth server from the operator's own machine, completes the TOTP
challenge with an already-verified factor, runs the repository's JWT verifier and owner dependency on the
resulting access token, prints ONLY non-sensitive booleans, then closes the session (scope=local).

It never prints, logs or writes the token, password, TOTP code, user id or e-mail. Secrets are read with
getpass (never argv/environment echo). It creates no account, factor or data row.

Required environment (names only; no secret values belong in shell history or the repository):
  SUPABASE_URL                 https project origin
  SENTINAX_SUPABASE_JWT_MODE   jwks   (this project's active key is ES256)
  SENTINAX_OWNER_USER_ID       the owner UUID
  SUPABASE_PUBLISHABLE_KEY     publishable (sb_publishable_...) key, same value the browser uses
Run from the repository root:  python scripts/owner_token_smoke.py
"""
import getpass
import json
import os
import pathlib
import sys
import time

import httpx
import jwt

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402

from backend.infrastructure import auth  # noqa: E402
from backend.infrastructure import private_access  # noqa: E402

_TIMEOUT = httpx.Timeout(10.0)


def evaluate_access_token(access_token: str) -> dict:
    """Run the production verifier + owner authorization; return non-sensitive facts only."""
    out = {"verifier_accepts": False, "owner_match": False, "owner_authorized": False}
    try:
        header = jwt.get_unverified_header(access_token)
        if isinstance(header.get("alg"), str):
            out["alg"] = header["alg"]
    except jwt.PyJWTError:
        pass
    try:
        claims = auth.verify_token_string(access_token)
    except HTTPException as exc:
        out["error_class"] = f"http_{exc.status_code}"
        return out
    out["verifier_accepts"] = True
    out["aal"] = claims.get("aal") if isinstance(claims.get("aal"), str) else None
    out["totp_evidence"] = "totp" in private_access._totp_methods(claims)
    out["seconds_until_expiry"] = int(claims["exp"] - time.time())
    try:
        out["owner_match"] = auth.canonical_uuid(claims.get("sub")) == private_access.configured_owner_id()
        private_access.authorize_private_owner(claims)
        out["owner_authorized"] = True
    except HTTPException as exc:
        out["error_class"] = f"http_{exc.status_code}"
    return out


def _bearer(key: str, access: str) -> dict:
    return {"apikey": key, "Authorization": f"Bearer {access}"}


def run_smoke(client: httpx.Client, project_url: str, publishable_key: str,
              email: str, password: str, totp_code: str, out=sys.stdout) -> dict:
    try:
        origin = auth._trusted_origin()
    except Exception:
        raise SystemExit("SUPABASE_URL is not a trusted https project origin")
    if project_url.rstrip("/") != origin:
        raise SystemExit("project URL does not match trusted SUPABASE_URL")
    base = f"{origin}/auth/v1"
    access = None
    try:
        r = client.post(f"{base}/token", params={"grant_type": "password"}, headers={"apikey": publishable_key},
                        json={"email": email, "password": password}, timeout=_TIMEOUT)
        if r.status_code != 200:
            raise SystemExit("sign-in failed")
        body = r.json()
        access = body["access_token"]
        factors = [f for f in (body.get("user") or {}).get("factors") or []
                   if f.get("factor_type") == "totp" and f.get("status") == "verified"]
        if factors:
            fid = factors[0]["id"]
            c = client.post(f"{base}/factors/{fid}/challenge", headers=_bearer(publishable_key, access), timeout=_TIMEOUT)
            if c.status_code != 200:
                raise SystemExit("challenge failed")
            v = client.post(f"{base}/factors/{fid}/verify", headers=_bearer(publishable_key, access),
                            json={"challenge_id": c.json()["id"], "code": totp_code}, timeout=_TIMEOUT)
            if v.status_code != 200:
                raise SystemExit("verification failed")
            access = v.json()["access_token"]
        result = evaluate_access_token(access)
    finally:
        if access:
            try:
                client.post(f"{base}/logout", params={"scope": "local"}, headers=_bearer(publishable_key, access), timeout=_TIMEOUT)
            except httpx.HTTPError:
                pass  # best effort: the session also expires on its own
    out.write(json.dumps(result, sort_keys=True) + "\n")
    return result


def main() -> None:
    key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
    url = os.environ.get("SUPABASE_URL", "")
    if not key or not url:
        raise SystemExit("set SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY")
    email = getpass.getpass("Owner e-mail (hidden): ")
    password = getpass.getpass("Password (hidden): ")
    code = getpass.getpass("Current TOTP code (hidden): ")
    with httpx.Client(follow_redirects=False) as client:
        run_smoke(client, url, key, email, password, code)


if __name__ == "__main__":
    main()
