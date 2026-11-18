# -*- encoding: utf-8 -*-
"""
Authentication & security (JWT + bcrypt)

- Password hashing: bcrypt (cost 12)
- Tokens: HS256 JWT, expiry configurable (config.yaml auth.token_expire_hours,
  default 12h)
- Secret: .env AUTH_SECRET (if unset, a temporary random secret is used and all
  login sessions invalidate on restart)
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import bcrypt
import jwt

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)

_ALGO = "HS256"
_fallback_secret = None
_secret_lock = threading.Lock()

# ============================================================
# Password
# ============================================================


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode(
        "utf-8"
    )


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


# ============================================================
# JWT
# ============================================================


def _secret() -> str:
    """Signing secret: prefer .env AUTH_SECRET; if unset, a per-process random secret (all sessions lost on restart; log a hint)"""
    global _fallback_secret
    secret = get_config().env("AUTH_SECRET", "")
    if secret:
        return secret
    with _secret_lock:
        if _fallback_secret is None:
            logger.warning(
                "AUTH_SECRET 未配置, 使用临时随机密钥 (重启后所有登录态失效)"
            )
            _fallback_secret = "tmp-" + secrets.token_hex(32)
        return _fallback_secret


@dataclass
class TokenPayload:
    user_id: int
    username: str
    role: str
    exp: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sub": str(self.user_id),
            "username": self.username,
            "role": self.role,
            "exp": self.exp,
        }


def create_token(user_id: int, username: str, role: str) -> str:
    hours = int(get_config().get("auth.token_expire_hours", 12))
    exp = datetime.now(timezone.utc) + timedelta(hours=hours)
    return jwt.encode(
        {
            "sub": str(user_id),
            "username": username,
            "role": role,
            "iat": int(time.time()),
            "exp": exp,
        },
        _secret(),
        algorithm=_ALGO,
    )


def decode_token(token: str) -> Optional[TokenPayload]:
    """Verify and parse the JWT; return None when invalid/expired"""
    try:
        data = jwt.decode(token, _secret(), algorithms=[_ALGO])
        return TokenPayload(
            user_id=int(data["sub"]),
            username=data["username"],
            role=data.get("role", "user"),
            exp=int(data["exp"]),
        )
    except (jwt.PyJWTError, KeyError, ValueError):
        return None
