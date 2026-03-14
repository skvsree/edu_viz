from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from app.api.routers import pages
from app.services.access import ROLE_ADMIN, ROLE_SYSTEM_ADMIN
from tests.test_dashboard_routes import FakeDB, make_request, render_body


class FakeResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


class SettingsDB(FakeDB):
    def __init__(self, organizations=None, users=None, objects=None):
        super().__init__(objects=objects)
        self.organizations = organizations or []
        self.users = users or []

    def execute(self, stmt):
        text = str(stmt)
        if "FROM organizations" in text:
            return FakeResult(self.organizations)
        if "FROM users" in text:
            return FakeResult(self.users)
        return FakeResult([])


def test_settings_home_visible_for_admin():
    user = SimpleNamespace(id=uuid4(), role=ROLE_ADMIN, organization_id=uuid4(), email="admin@example.com", identity_sub="admin-sub")
    db = SettingsDB(users=[SimpleNamespace(id=uuid4())])

    response = pages.settings_home(make_request(path="/settings"), user=user, db=db)

    body = render_body(response)
    assert response.status_code == 200
    assert "Settings" in body
    assert "Manage users" in body
    assert 'href="/settings/organizations"' not in body


def test_organizations_page_visible_for_system_admin():
    org = SimpleNamespace(id=uuid4(), name="Northwind", is_ai_enabled=True, users=[SimpleNamespace(id=uuid4())])
    user = SimpleNamespace(id=uuid4(), role=ROLE_SYSTEM_ADMIN, organization_id=None, email="root@example.com", identity_sub="root-sub")
    db = SettingsDB(organizations=[org])

    response = pages.organizations_page(make_request(path="/settings/organizations"), user=user, db=db)

    body = render_body(response)
    assert response.status_code == 200
    assert "Organizations" in body
    assert "Add organization" in body
    assert "Northwind" in body
    assert 'data-organization-name="Northwind"' in body
    assert f'data-organization-update-action="/settings/organizations/{org.id}/update"' in body
    assert 'const supportsModalDialog =' in body


def test_users_page_shows_org_assignment_for_system_admin():
    org = SimpleNamespace(id=uuid4(), name="Northwind", is_ai_enabled=True)
    member = SimpleNamespace(
        id=uuid4(),
        email="learner@example.com",
        identity_sub="learner-sub",
        role="user",
        organization_id=org.id,
        organization=org,
        is_test_enabled=True,
    )
    user = SimpleNamespace(id=uuid4(), role=ROLE_SYSTEM_ADMIN, organization_id=None, email="root@example.com", identity_sub="root-sub")
    db = SettingsDB(organizations=[org], users=[member], objects={member.id: member})

    response = pages.users_page(make_request(path="/settings/users"), user=user, db=db)

    body = render_body(response)
    assert response.status_code == 200
    assert "Manage users" not in body
    assert "learner@example.com" in body
    assert f'value="{org.id}"' in body
    assert "Save user" in body
