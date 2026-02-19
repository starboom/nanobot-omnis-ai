"""Password hashing and JWT token management (stdlib only)."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

_SECRET_PATH = Path.home() / ".nanobot" / "portal_secret.key"
_TOKEN_EXPIRY = 7 * 24 * 3600  # 7 days


def _get_secret() -> str:
    if _SECRET_PATH.exists():
        return _SECRET_PATH.read_text().strip()
    secret = secrets.token_hex(32)
    _SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SECRET_PATH.write_text(secret)
    return secret


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, hash_hex = stored.split("$", 1)
    except ValueError:
        return False
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return hmac.compare_digest(h.hex(), hash_hex)


def _b64url(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _b64url_decode(s: str) -> dict:
    padded = s + "=" * (4 - len(s) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def create_token(user_id: int, username: str, role: str) -> str:
    header = _b64url({"alg": "HS256", "typ": "JWT"})
    payload = _b64url({
        "sub": user_id,
        "usr": username,
        "role": role,
        "exp": int(time.time()) + _TOKEN_EXPIRY,
    })
    msg = f"{header}.{payload}"
    sig = hmac.new(_get_secret().encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}.{sig}"


def verify_token(token: str) -> dict | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    msg = f"{parts[0]}.{parts[1]}"
    expected = hmac.new(_get_secret().encode(), msg.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(parts[2], expected):
        return None
    payload = _b64url_decode(parts[1])
    if payload.get("exp", 0) < time.time():
        return None
    return payload
