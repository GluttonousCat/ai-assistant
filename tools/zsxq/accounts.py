"""Account operations; callers never receive a database connection or cursor."""

from storage.sqlite.account import AccountDatabase


def default_cookie():
    account = AccountDatabase().get_default_account(mask_cookie=False)
    return account["cookie"] if account else None


def create_account(cookie, name=None):
    return AccountDatabase().add_account(cookie, name)


def list_accounts():
    return AccountDatabase().list_accounts()
