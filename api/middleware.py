# -*- encoding: utf-8 -*-
"""
全局鉴权中间件

规则:
- 免登录白名单: /api/auth/login, /api/auth/register, /health, /, /docs*, /openapi.json,
  静态资源 (/assets/*, /range, /login, /favicon*)
- 其余 /api/* 与 WebSocket 升级请求必须携带有效 Bearer Token
- 非 API 路径 (前端页面) 放行, 由前端路由引导到登录页
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from core.logger import get_logger
from core.security import decode_token

logger = get_logger(__name__)

# 前缀/精确匹配的免登录路径
OPEN_PATHS = {
    "/", "/health", "/range", "/login", "/favicon.ico",
    "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
}
OPEN_PREFIXES = ("/api/auth/login", "/api/auth/register", "/assets/")


def _is_open(path: str) -> bool:
    if path in OPEN_PATHS:
        return True
    return any(path.startswith(p) for p in OPEN_PREFIXES)


class AuthMiddleware(BaseHTTPMiddleware):
    """API 鉴权: 校验 Authorization: Bearer <jwt>"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        is_api = path.startswith("/api")
        is_ws = request.scope.get("type") == "websocket"

        if not (is_api or is_ws) or _is_open(path):
            return await call_next(request)

        auth = request.headers.get("authorization") or ""
        parts = auth.split(None, 1)
        token = parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else None
        if not token:
            token = request.query_params.get("token")
        if not token or decode_token(token) is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "未登录或登录已失效, 请重新登录"},
            )
        return await call_next(request)
