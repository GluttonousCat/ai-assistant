"""Role and account persistence facade; route dependencies retain authorization."""
from storage.repositories.auth import active_role, list_users, set_password, set_role

__all__ = ["active_role", "list_users", "set_password", "set_role"]
