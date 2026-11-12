# -*- encoding: utf-8 -*-
"""
Global auth middleware

Rules:
- Login-free whitelist: /api/auth/login, /api/auth/register, /health, /, /docs*, /openapi.json,
  static assets (/assets/*, /login, /favicon*)
- All other /api/* paths and WebSocket upgrade requests must carry a valid Bearer token
- Non-API paths (frontend pages) pass through; the frontend router redirects to the login page
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from core.logger import get_logger
from core.security import decode_token

logger = get_logger(__name__)

# Login-free paths, matched exactly or by prefix
OPEN_PATHS = {
    "/", "/health", "/login", "/favicon.ico",
    "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
}
OPEN_PREFIXES = ("/api/auth/login", "/api/auth/register", "/assets/")


def _is_open(path: str) -> bool:
    if path in OPEN_PATHS:
        return True
    return any(path.startswith(p) for p in OPEN_PREFIXES)


class AuthMiddleware(BaseHTTPMiddleware):
    """API auth: validate Authorization: Bearer <jwt>"""

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
