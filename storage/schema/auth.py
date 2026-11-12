"""Authentication schema, separate from hashing/JWT and business transactions."""
from storage.schema.connection import ddl_cursor

DDL_USERS = """
CREATE TABLE IF NOT EXISTS auth.users (
    user_id       SERIAL PRIMARY KEY,
    username      VARCHAR(64) UNIQUE NOT NULL,
    password_hash VARCHAR(128) NOT NULL,
    role          VARCHAR(16) NOT NULL DEFAULT 'user',   -- admin / user
    display_name  VARCHAR(64),
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    last_login_at TIMESTAMP,
    created_at    TIMESTAMP DEFAULT now()
);
"""

def ensure_auth_tables():
    with ddl_cursor() as cur:
        cur.execute("SET lock_timeout='5s'")
        cur.execute("CREATE SCHEMA IF NOT EXISTS auth")
        cur.execute(DDL_USERS)
