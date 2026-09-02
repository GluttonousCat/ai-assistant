# -*- encoding: utf-8 -*-
"""
认证 API
- POST /api/auth/login    登录, 返回 JWT
- POST /api/auth/register 注册 (自助, 默认 user 角色)
- GET  /api/auth/me       当前用户信息 (需登录)
- POST /api/auth/change-password  修改密码 (需登录)
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from core import security
from core.config import get_config
from core.security import TokenPayload
from api.deps import get_current_user, require_admin

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=6, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    invite_code: str = Field(min_length=1, max_length=64)


@auth_router.post("/login")
async def login(req: LoginRequest) -> Dict[str, Any]:
    user = security.authenticate(req.username.strip(), req.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    security.touch_login(user["username"])
    token = security.create_token(user["user_id"], user["username"], user["role"])
    return {
        "token": token,
        "user": {
            "user_id": user["user_id"],
            "username": user["username"],
            "role": user["role"],
            "display_name": user.get("display_name") or user["username"],
        },
    }


@auth_router.post("/register")
async def register(req: RegisterRequest) -> Dict[str, Any]:
    import re
    # 注册码校验 (受控注册; .env AUTH_INVITE_CODE 可改, 默认 123456)
    valid_code = get_config().env("AUTH_INVITE_CODE", "123456")
    if req.invite_code != valid_code:
        raise HTTPException(status_code=403, detail="注册码不正确")
    username = req.username.strip()
    # 用户名仅英文/数字/下划线 (前端同规则; 后端强制, 防绕过)
    if not re.fullmatch(r"[A-Za-z0-9_]{6,64}", username):
        raise HTTPException(
            status_code=400, detail="用户名仅限英文字母/数字/下划线, 至少 6 位")
    if security.get_user(username) is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")
    try:
        user = security.create_user(username, req.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"user_id": user["user_id"], "username": user["username"]}


@auth_router.get("/me")
async def me(current: TokenPayload = Depends(get_current_user)) -> Dict[str, Any]:
    return {
        "user_id": current.user_id,
        "username": current.username,
        "role": current.role,
    }


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6, max_length=128)


@auth_router.post("/change-password")
async def change_password(req: ChangePasswordRequest,
                          current: TokenPayload = Depends(get_current_user)) -> Dict[str, Any]:
    user = security.get_user(current.username)
    if not user or not security.verify_password(req.old_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="原密码错误")
    from storage.pg import PgClient
    with PgClient() as pg:
        pg.execute("UPDATE auth.users SET password_hash=%s WHERE username=%s",
                   (security.hash_password(req.new_password), current.username))
    return {"ok": True}


# ============================================================
# 用户管理 (仅超级管理员 Gluttonouscat): 列表 / 角色变更
# 其他 admin 可看区间看板, 但不能进用户管理
# ============================================================

def _require_super(current: TokenPayload = Depends(require_admin)) -> TokenPayload:
    super_name = get_config().env("SUPER_ADMIN", "Gluttonouscat")
    if current.username != super_name:
        raise HTTPException(status_code=403, detail="仅超级管理员可管理用户")
    return current


@auth_router.get("/users")
async def list_users(current: TokenPayload = Depends(_require_super)):
    """全部账号列表 (admin)"""
    from storage.pg import PgClient
    with PgClient() as pg:
        rows = pg.fetch_all(
            "SELECT user_id, username, role, display_name, is_active, "
            "last_login_at, created_at FROM auth.users ORDER BY user_id")
    for r in rows:
        for k in ("last_login_at", "created_at"):
            if r.get(k):
                r[k] = r[k].strftime("%Y-%m-%d %H:%M")
    return {"items": rows}


class RoleRequest(BaseModel):
    role: str  # admin / user


@auth_router.post("/users/{user_id}/role")
async def set_user_role(user_id: int, req: RoleRequest,
                        current: TokenPayload = Depends(_require_super)) -> Dict[str, Any]:
    """变更账号角色 (仅超级管理员)。防呆: 不能改自己 (避免唯一管理员自降)。"""
    if req.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="role 仅支持 admin / user")
    if user_id == current.user_id:
        raise HTTPException(status_code=400, detail="不能变更自己的角色")
    from storage.pg import PgClient
    with PgClient() as pg:
        row = pg.fetch_one(
            "SELECT username FROM auth.users WHERE user_id=%s", (user_id,))
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        pg.execute("UPDATE auth.users SET role=%s WHERE user_id=%s",
                   (req.role, user_id))
    return {"ok": True, "user_id": user_id, "role": req.role}
