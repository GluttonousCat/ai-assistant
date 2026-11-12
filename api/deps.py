# -*- encoding: utf-8 -*-
"""
Shared API dependencies: login-state validation

Usage:
    from api.deps import get_current_user
    @router.get("/xxx")
    async def xxx(current: TokenPayload = Depends(get_current_user)): ...

Auth scope is enforced by the global middleware in app.py (whitelist: login/
register/health check/static assets), so routes generally need no explicit
Depends; these dependencies are for endpoints that need user identity/role.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, WebSocket
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.security import TokenPayload, decode_token

_bearer = HTTPBearer(auto_error=False)


def _extract_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> TokenPayload:
    """Validate the Bearer token; raises 401 on failure"""
    token = credentials.credentials if credentials else None
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    payload = decode_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="登录已失效, 请重新登录")
    return payload


def require_admin(current: TokenPayload = Depends(get_current_user)) -> TokenPayload:
    """Admin check: the live DB role is authoritative (token role is not trusted, so demotion takes effect immediately)"""
    from storage.pg import PgClient
    try:
        with PgClient() as pg:
            row = pg.fetch_one(
                "SELECT role FROM auth.users WHERE user_id=%s AND is_active",
                (current.user_id,))
    except Exception:
        row = None
    if not row or row.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    current.role = "admin"
    return current


async def ws_auth(websocket: WebSocket) -> Optional[TokenPayload]:
    """WebSocket auth: prefer the Authorization header, then the ?token= query parameter"""
    token = _extract_token(websocket.headers.get("authorization"))
    if not token:
        token = websocket.query_params.get("token")
    if not token:
        return None
    return decode_token(token)
