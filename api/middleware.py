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

from asyncio import to_thread
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from core.logger import get_logger
from api.deps import resolve_session

logger = get_logger(__name__)

# Login-free paths, matched exactly or by prefix
OPEN_PATHS = {
    "/", "/health", "/login", "/favicon.ico",
    "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
    "/api/auth/login", "/api/auth/register",
}
OPEN_PREFIXES = ("/assets/",)


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
        try:
            current = await to_thread(resolve_session, token) if token else None
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        if current is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "未登录或登录已失效, 请重新登录"},
            )
        request.state.current_user = current
        return await call_next(request)
