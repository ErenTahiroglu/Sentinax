"""Synthetic keys and Supabase-shaped tokens for auth tests. No real secrets, no network."""
import json
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa

PROJECT_URL = "https://abcdefghijklmnopqrst.supabase.co"
ISSUER = f"{PROJECT_URL}/auth/v1"
LEGACY_SECRET = "synthetic-legacy-secret-0123456789-abcdefghij"
OTHER_SECRET = "another-synthetic-secret-0123456789-zzzzzzzzz"
OWNER_ID = "11111111-2222-4333-8444-555555555555"
OTHER_ID = "99999999-8888-4777-8666-555555555555"


def new_ec_key():
    return ec.generate_private_key(ec.SECP256R1())


def new_rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_jwk(private_key, kid, alg):
    algo = jwt.algorithms.ECAlgorithm if alg == "ES256" else jwt.algorithms.RSAAlgorithm
    jwk = json.loads(algo.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "alg": alg, "use": "sig"})
    return jwk


def jwks_doc(*jwks):
    return {"keys": list(jwks)}


def claims(**over):
    now = int(time.time())
    base = {
        "iss": ISSUER,
        "aud": "authenticated",
        "exp": now + 600,
        "iat": now - 10,
        "sub": OWNER_ID,
        "role": "authenticated",
        "aal": "aal2",
        "amr": [
            {"method": "totp", "timestamp": now - 5},
            {"method": "password", "timestamp": now - 60},
        ],
        "session_id": str(uuid.uuid4()),
        "email": "owner@example.invalid",
        "is_anonymous": False,
        "app_metadata": {"provider": "email"},
        "user_metadata": {},
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not _DROP}


_DROP = object()
DROP = _DROP


def sign(payload, key, alg, kid=None, headers=None):
    h = dict(headers or {})
    if kid is not None:
        h["kid"] = kid
    return jwt.encode(payload, key, algorithm=alg, headers=h or None)


def b64(obj):
    import base64
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def unsigned_none_token(payload):
    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(payload)}."
