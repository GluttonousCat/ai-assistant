# -*- encoding: utf-8 -*-
"""
Authentication & security (JWT + bcrypt)

- User table: auth.users (created idempotently at startup; default admin account
  seeded from environment variables)
- Password hashing: bcrypt (cost 12)
- Tokens: HS256 JWT, expiry configurable (config.yaml auth.token_expire_hours,
  default 12h)
- Secret: .env AUTH_SECRET (if unset, a temporary random secret is used and all
  login sessions invalidate on restart)
"""
from __future__ import annotations

import secrets
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

from storage.schema.auth import DDL_USERS, ensure_auth_tables
from storage.repositories import auth as auth_store


# ============================================================
# Password
# ============================================================

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


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
    secret = get_config().env("AUTH_SECRET", "")
    if not secret:
        if not getattr(_secret, "_warned", False):
            logger.warning("AUTH_SECRET 未配置, 使用临时随机密钥 (重启后所有登录态失效)")
            _secret._warned = True  # type: ignore[attr-defined]
        secret = "tmp-" + secrets.token_hex(32)
    return secret


@dataclass
class TokenPayload:
    user_id: int
    username: str
    role: str
    exp: int

    def to_dict(self) -> Dict[str, Any]:
        return {"sub": str(self.user_id), "username": self.username,
                "role": self.role, "exp": self.exp}


def create_token(user_id: int, username: str, role: str) -> str:
    hours = int(get_config().get("auth.token_expire_hours", 12))
    exp = datetime.now(timezone.utc) + timedelta(hours=hours)
    return jwt.encode(
        {"sub": str(user_id), "username": username, "role": role,
         "iat": int(time.time()), "exp": exp},
        _secret(), algorithm=_ALGO,
    )


def decode_token(token: str) -> Optional[TokenPayload]:
    """Verify and parse the JWT; return None when invalid/expired"""
    try:
        data = jwt.decode(token, _secret(), algorithms=[_ALGO])
        return TokenPayload(user_id=int(data["sub"]), username=data["username"],
                            role=data.get("role", "user"), exp=int(data["exp"]))
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


# ============================================================
# User storage (auth.users)
# ============================================================

def ensure_auth_schema() -> None:
    """Idempotently create tables + seed the admin on first deployment"""
    ensure_auth_tables()
    cfg = get_config()
    admin_user = cfg.env("AUTH_ADMIN_USER", "admin")
    admin_pwd = cfg.env("AUTH_ADMIN_PASSWORD", "admin123")
    if auth_store.user_count() == 0 and admin_user and admin_pwd:
        create_user(admin_user, admin_pwd, role="admin", display_name="管理员")
        logger.info("首次部署已创建种子管理员")


def get_user(username: str) -> Optional[Dict[str, Any]]:
    return auth_store.find_user(username)


def create_user(username: str, password: str, role: str = "user",
                display_name: Optional[str] = None) -> Dict[str, Any]:
    if len(password) < 6:
        raise ValueError("密码至少 8 位")
    auth_store.insert_user(username, hash_password(password), role, display_name or username)
    return get_user(username) or {}


def authenticate(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Verify username/password; return the user row on success (caller issues the token), None on failure"""
    user = get_user(username)
    if not user or not user.get("is_active"):
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


def touch_login(username: str) -> None:
    auth_store.touch_login(username)
