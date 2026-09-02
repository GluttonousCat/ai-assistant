# -*- encoding: utf-8 -*-
"""
认证与安全 (JWT + bcrypt)

- 用户表: auth.users (启动时幂等建表; 默认管理员账号从环境变量 seed)
- 密码哈希: bcrypt (cost 12)
- 令牌: HS256 JWT, 过期时间可配 (config.yaml auth.token_expire_hours, 默认 12h)
- 密钥: .env AUTH_SECRET (未配置时用临时随机密钥, 重启即全部失效)
"""
from __future__ import annotations

import os
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

DDL_USERS = """
CREATE TABLE IF NOT EXISTS auth.users (
    user_id       SERIAL PRIMARY KEY,
    username      VARCHAR(64) UNIQUE NOT NULL,
    password_hash VARCHAR(128) NOT NULL,
    role          VARCHAR(16) NOT NULL DEFAULT 'user',   -- admin / user
    display_name  VARCHAR(64),
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    last_login_at TIMESTAMP,
    created_at    TIMESTAMP DEFAULT now()
);
"""


# ============================================================
# 密码
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
    """签名密钥: 优先 .env AUTH_SECRET; 未配置则进程内随机 (重启全失效, 提示配置)"""
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
    """校验并解析 JWT, 无效/过期返回 None"""
    try:
        data = jwt.decode(token, _secret(), algorithms=[_ALGO])
        return TokenPayload(user_id=int(data["sub"]), username=data["username"],
                            role=data.get("role", "user"), exp=int(data["exp"]))
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


# ============================================================
# 用户存储 (auth.users)
# ============================================================

def ensure_auth_schema() -> None:
    """幂等建表 + 首次部署种子管理员"""
    from storage.pg import PgClient
    with PgClient() as pg:
        pg.execute("CREATE SCHEMA IF NOT EXISTS auth")
        pg.execute(DDL_USERS)

        # 种子管理员仅在【用户表完全为空】时创建（首次部署引导用）。
        # 不再"admin 不存在就重建"——那会导致删除弱口令账号后重启自动复活。
        # 日常管理员任命走 POST /api/auth/users/{id}/role（admin 操作）。
        cfg = get_config()
        admin_user = cfg.env("AUTH_ADMIN_USER", "admin")
        admin_pwd = cfg.env("AUTH_ADMIN_PASSWORD", "admin123")
        n = pg.fetch_one("SELECT COUNT(*) AS n FROM auth.users")["n"]
        if n == 0 and admin_user and admin_pwd:
            create_user(admin_user, admin_pwd, role="admin",
                        display_name="管理员")
            logger.info(f"首次部署已创建种子管理员: {admin_user} (请尽快修改默认密码)")


def get_user(username: str) -> Optional[Dict[str, Any]]:
    from storage.pg import PgClient
    with PgClient() as pg:
        return pg.fetch_one(
            "SELECT user_id, username, password_hash, role, display_name, "
            "is_active, last_login_at FROM auth.users WHERE username=%s",
            (username,),
        )


def create_user(username: str, password: str, role: str = "user",
                display_name: Optional[str] = None) -> Dict[str, Any]:
    if len(password) < 6:
        raise ValueError("密码至少 8 位")
    from storage.pg import PgClient
    with PgClient() as pg:
        pg.execute(
            "INSERT INTO auth.users (username, password_hash, role, display_name) "
            "VALUES (%s, %s, %s, %s)",
            (username, hash_password(password), role, display_name or username),
        )
    return get_user(username) or {}


def authenticate(username: str, password: str) -> Optional[Dict[str, Any]]:
    """校验用户名密码; 成功返回用户行 (含 token), 失败返回 None"""
    user = get_user(username)
    if not user or not user.get("is_active"):
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


def touch_login(username: str) -> None:
    from storage.pg import PgClient
    with PgClient() as pg:
        pg.execute(
            "UPDATE auth.users SET last_login_at=now() WHERE username=%s",
            (username,),
        )
