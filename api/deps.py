# -*- encoding: utf-8 -*-
"""
API 公共依赖: 登录态校验

用法:
    from api.deps import get_current_user
    @router.get("/xxx")
    async def xxx(current: TokenPayload = Depends(get_current_user)): ...

鉴权范围由 app.py 的全局中间件控制 (白名单: 登录/注册/健康检查/静态资源),
路由内部一般无需再显式 Depends; 本依赖供需要用户身份/角色判断的端点使用。
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
    """校验 Bearer Token, 失败抛 401"""
    token = credentials.credentials if credentials else None
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    payload = decode_token(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="登录已失效, 请重新登录")
    return payload


def require_admin(current: TokenPayload = Depends(get_current_user)) -> TokenPayload:
    """管理员校验: 以数据库实时角色为准 (不信 token 内 role, 降权即时生效)"""
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
    """WebSocket 鉴权: 优先 header Authorization, 其次 query 参数 ?token="""
    token = _extract_token(websocket.headers.get("authorization"))
    if not token:
        token = websocket.query_params.get("token")
    if not token:
        return None
    return decode_token(token)
