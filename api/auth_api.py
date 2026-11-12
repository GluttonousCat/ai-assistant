# -*- encoding: utf-8 -*-
"""
Authentication API
- POST /api/auth/login    Login, returns JWT
- POST /api/auth/register Self-service registration (default role: user)
- GET  /api/auth/me       Current user info (login required)
- POST /api/auth/change-password  Change password (login required)
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core import security
from core.config import get_config
from core.security import TokenPayload
from api.deps import get_current_user, require_admin
from tools.auth import users as accounts

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=6, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    invite_code: str = Field(min_length=1, max_length=64)


@auth_router.post("/login")
def login(req: LoginRequest) -> Dict[str, Any]:
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
            "is_super_admin": user["role"] == "admin" and user["username"] == get_config().env("SUPER_ADMIN", "Gluttonouscat"),
        },
    }


@auth_router.post("/register")
def register(req: RegisterRequest) -> Dict[str, Any]:
    import re
    # Invite-code validation (controlled registration; override via .env AUTH_INVITE_CODE, default 123456)
    valid_code = get_config().env("AUTH_INVITE_CODE", "123456")
    if req.invite_code != valid_code:
        raise HTTPException(status_code=403, detail="注册码不正确")
    username = req.username.strip()
    # Username: letters/digits/underscore only (same rule as frontend; enforced server-side to prevent bypass)
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
def me(current: TokenPayload = Depends(get_current_user)) -> Dict[str, Any]:
    user = security.get_user(current.username)
    if not user or not user.get("is_active"):
        raise HTTPException(status_code=401, detail="账户不可用，请重新登录")
    super_name = get_config().env("SUPER_ADMIN", "Gluttonouscat")
    return {
        "user_id": user["user_id"],
        "username": user["username"],
        "display_name": user.get("display_name") or user["username"],
        "role": user["role"],
        "is_super_admin": user["role"] == "admin" and user["username"] == super_name,
    }


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6, max_length=128)


@auth_router.post("/change-password")
def change_password(req: ChangePasswordRequest,
                          current: TokenPayload = Depends(get_current_user)) -> Dict[str, Any]:
    user = security.get_user(current.username)
    if not user or not security.verify_password(req.old_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="原密码错误")
    accounts.set_password(current.username, security.hash_password(req.new_password))
    return {"ok": True}


# ============================================================
# User management (super admin Gluttonouscat only): list / role change
# Other admins can view admin pages but cannot manage users
# ============================================================

def _require_super(current: TokenPayload = Depends(require_admin)) -> TokenPayload:
    super_name = get_config().env("SUPER_ADMIN", "Gluttonouscat")
    if current.username != super_name:
        raise HTTPException(status_code=403, detail="仅超级管理员可管理用户")
    return current


@auth_router.get("/users")
def list_users(current: TokenPayload = Depends(_require_super)):
    """List all accounts (admin)"""
    rows = accounts.list_users()
    for r in rows:
        for k in ("last_login_at", "created_at"):
            if r.get(k):
                r[k] = r[k].strftime("%Y-%m-%d %H:%M")
    return {"items": rows}


class RoleRequest(BaseModel):
    role: str  # admin / user


@auth_router.post("/users/{user_id}/role")
def set_user_role(user_id: int, req: RoleRequest,
                        current: TokenPayload = Depends(_require_super)) -> Dict[str, Any]:
    """Change account role (super admin only). Guard: cannot change own role (prevents the only admin from demoting themselves)."""
    if req.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="role 仅支持 admin / user")
    if user_id == current.user_id:
        raise HTTPException(status_code=400, detail="不能变更自己的角色")
    if not accounts.set_role(user_id, req.role):
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True, "user_id": user_id, "role": req.role}
