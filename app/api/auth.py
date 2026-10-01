"""JWT auth (CLAUDE.md §16.1): HS256, 12 h, roles `dispatcher`/`admin`.

Users live in the environment; auth is applied by the API middleware. When `AUTH_ENABLED` is
false (default for the open MVP dashboard) the middleware is a no-op.

`PyJWT` is imported lazily so the memory-mode MVP keeps working in environments where the
full dependency set is not installed.
"""

from __future__ import annotations

import time

from app.common.config import get_env

ALGORITHM = "HS256"


def _jwt():
    try:
        import jwt  # noqa: PLC0415 - optional dependency, loaded on demand
    except ImportError as exc:  # pragma: no cover - only when auth is actually used
        raise RuntimeError("PyJWT is required for authentication; install the full dependencies") from exc
    return jwt


def verify_credentials(login: str, password: str) -> str | None:
    """Return the role for valid credentials, else None."""
    env = get_env()
    if login == env.dispatcher_login and password == env.dispatcher_password:
        return "dispatcher"
    if login == env.admin_login and password == env.admin_password:
        return "admin"
    return None


def issue_token(login: str, role: str) -> tuple[str, int]:
    env = get_env()
    now = int(time.time())
    exp = now + env.jwt_ttl_h * 3600
    token = _jwt().encode({"sub": login, "role": role, "iat": now, "exp": exp}, env.jwt_secret,
                          algorithm=ALGORITHM)
    return token, exp


def verify_token(token: str) -> dict | None:
    try:
        return _jwt().decode(token, get_env().jwt_secret, algorithms=[ALGORITHM])
    except Exception:
        return None
