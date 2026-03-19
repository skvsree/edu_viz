from types import SimpleNamespace

from app.services.access import ROLE_ADMIN, ROLE_SYSTEM_ADMIN, ROLE_USER
from app.services.admin_bootstrap import (
    SYSTEM_ADMIN_BOOTSTRAP_EMAIL,
    bootstrap_system_admin_for_user,
    configured_system_admin_email,
    ensure_system_admin_role,
)


class DummySession:
    def __init__(self):
        self.committed = False
        self.refreshed = False
        self.added = []

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.committed = True

    def refresh(self, value):
        self.refreshed = True


def test_configured_system_admin_email_defaults_to_requested_address():
    assert configured_system_admin_email() == SYSTEM_ADMIN_BOOTSTRAP_EMAIL


def test_ensure_system_admin_role_promotes_user():
    db = DummySession()
    user = SimpleNamespace(email=SYSTEM_ADMIN_BOOTSTRAP_EMAIL, role=ROLE_USER)

    changed = ensure_system_admin_role(db, user)

    assert changed is True
    assert user.role == ROLE_SYSTEM_ADMIN
    assert db.added == [user]


def test_bootstrap_system_admin_for_user_promotes_matching_email_case_insensitively():
    db = DummySession()
    user = SimpleNamespace(email="SKV.SREE@OUTLOOK.COM", role=ROLE_ADMIN)

    changed = bootstrap_system_admin_for_user(db, user)

    assert changed is True
    assert user.role == ROLE_SYSTEM_ADMIN
    assert db.committed is True
    assert db.refreshed is True


def test_bootstrap_system_admin_for_user_ignores_non_matching_email():
    db = DummySession()
    user = SimpleNamespace(email="someone@example.com", role=ROLE_USER)

    changed = bootstrap_system_admin_for_user(db, user)

    assert changed is False
    assert user.role == ROLE_USER
    assert db.committed is False
