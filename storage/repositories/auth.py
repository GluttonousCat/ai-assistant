"""Authentication persistence; password hashing and JWT remain in core.security."""
from storage.pg import PgClient


def active_role(user_id):
    with PgClient(read_only=True) as pg:
        return pg.fetch_one("SELECT username, role FROM auth.users WHERE user_id=%s AND is_active", (user_id,))


def find_user(username):
    with PgClient(read_only=True) as pg:
        return pg.fetch_one("SELECT user_id,username,password_hash,role,display_name,is_active,last_login_at "
                            "FROM auth.users WHERE username=%s", (username,))


def user_count():
    with PgClient(read_only=True) as pg:
        return pg.fetch_one("SELECT count(*) AS n FROM auth.users")["n"]


def insert_user(username, password_hash, role, display_name):
    with PgClient() as pg:
        pg.execute("INSERT INTO auth.users(username,password_hash,role,display_name) VALUES (%s,%s,%s,%s)",
                   (username, password_hash, role, display_name))


def touch_login(username):
    with PgClient() as pg:
        pg.execute("UPDATE auth.users SET last_login_at=now() WHERE username=%s", (username,))


def set_password(username, password_hash):
    with PgClient() as pg:
        pg.execute("UPDATE auth.users SET password_hash=%s WHERE username=%s", (password_hash, username))


def list_users():
    with PgClient(read_only=True) as pg:
        return pg.fetch_all("SELECT user_id,username,role,display_name,is_active,last_login_at,created_at "
                            "FROM auth.users ORDER BY user_id")


def set_role(user_id, role):
    with PgClient() as pg:
        return pg.execute("UPDATE auth.users SET role=%s WHERE user_id=%s", (role, user_id)) > 0
