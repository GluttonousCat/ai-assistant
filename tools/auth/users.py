"""Account services and persistence; cryptographic primitives remain in core."""

from storage.repositories.auth import active_role, list_users, set_password, set_role


from core.config import get_config
from core.logger import get_logger
from core.security import hash_password, verify_password
from storage.schema.auth import ensure_auth_tables
from storage.repositories import auth as auth_store
from typing import Any, Dict, Optional

logger = get_logger(__name__)
__all__ = [
    "active_role",
    "list_users",
    "set_password",
    "set_role",
    "ensure_auth_schema",
    "get_user",
    "create_user",
    "authenticate",
    "touch_login",
]

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


def create_user(
    username: str, password: str, role: str = "user", display_name: Optional[str] = None
) -> Dict[str, Any]:
    if len(password) < 8:
        raise ValueError("密码至少 8 位")
    auth_store.insert_user(
        username, hash_password(password), role, display_name or username
    )
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
