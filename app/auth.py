"""Simple web authentication: a single admin account, session cookie, remember-me.

Password hashing via PBKDF2 (stdlib). Session token = HMAC-signed JSON.
Admin data + secret live in the config (cfg['auth']).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from store import ConfigStore

_PBKDF2_ITERS = 200_000
REMEMBER_AGE = 30 * 24 * 3600      # 30 days
SESSION_AGE = 12 * 3600            # server-side limit without remember-me
COOKIE_NAME = "cacs_session"


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt_b = bytes.fromhex(salt) if salt else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt_b, _PBKDF2_ITERS)
    return salt_b.hex(), dk.hex()


class Auth:
    def __init__(self, store: ConfigStore):
        self.store = store

    # --- admin account -----------------------------------------------------
    def is_configured(self) -> bool:
        a = self.store.get()["auth"]
        return bool(a.get("username") and a.get("pw_hash"))

    def set_admin(self, username: str, password: str) -> None:
        salt, h = hash_password(password)
        a = self.store.get()["auth"]
        secret = a.get("secret") or secrets.token_hex(32)
        self.store.save({"auth": {
            "username": username.strip(), "pw_salt": salt, "pw_hash": h, "secret": secret,
        }})

    def verify_password(self, username: str, password: str) -> bool:
        a = self.store.get()["auth"]
        if not a.get("username") or username.strip() != a["username"]:
            return False
        _, h = hash_password(password, a["pw_salt"])
        return hmac.compare_digest(h, a["pw_hash"])

    # --- session token -----------------------------------------------------
    def _secret(self) -> bytes:
        return (self.store.get()["auth"].get("secret") or "").encode()

    def make_token(self, username: str, remember: bool) -> str:
        payload = {"u": username, "iat": int(time.time()), "r": bool(remember)}
        raw = _b64e(json.dumps(payload, separators=(",", ":")).encode())
        sig = hmac.new(self._secret(), raw.encode(), hashlib.sha256).digest()
        return f"{raw}.{_b64e(sig)}"

    def verify_token(self, token: str | None) -> str | None:
        if not token or "." not in token:
            return None
        raw, _, sig = token.partition(".")
        expected = hmac.new(self._secret(), raw.encode(), hashlib.sha256).digest()
        try:
            if not hmac.compare_digest(_b64d(sig), expected):
                return None
            data: dict[str, Any] = json.loads(_b64d(raw))
        except Exception:
            return None
        max_age = REMEMBER_AGE if data.get("r") else SESSION_AGE
        if int(time.time()) - int(data.get("iat", 0)) > max_age:
            return None
        u = data.get("u")
        if u != self.store.get()["auth"].get("username"):
            return None
        return u
