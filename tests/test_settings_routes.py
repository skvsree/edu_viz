from __future__ import annotations

from types import SimpleNamespace
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
    user = SimpleNamespace(
        id=uuid4(),
        role=ROLE_ADMIN,
        organization_id=uuid4(),
        email="admin@example.com",
        identity_sub="admin-sub",
    )
    db = SettingsDB(users=[SimpleNamespace(id=uuid4())])

    response = pages.settings_home(make_request(path="/settings"), user=user, db=db)

    body = render_body(response)
    assert response.status_code == 200
    assert "Settings" in body
    assert "Manage users" in body
    assert 'href="/settings/organizations"' not in body


def test_organizations_page_visible_for_system_admin():
    org = SimpleNamespace(
        id=uuid4(),
        name="Northwind",
        is_ai_enabled=True,
        users=[SimpleNamespace(id=uuid4())],
    )
    user = SimpleNamespace(
        id=uuid4(),
        role=ROLE_SYSTEM_ADMIN,
        organization_id=None,
        email="root@example.com",
        identity_sub="root-sub",
    )
    db = SettingsDB(organizations=[org])

    response = pages.organizations_page(
        make_request(path="/settings/organizations"), user=user, db=db
    )

    body = render_body(response)
    assert response.status_code == 200
    assert "Organizations" in body
    assert "Add organization" in body
    assert "Northwind" in body
    assert 'data-organization-name="Northwind"' in body
    assert (
        f'data-organization-update-action="/settings/organizations/{org.id}/update"'
        in body
    )
    assert "const supportsModalDialog =" in body


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
    user = SimpleNamespace(
        id=uuid4(),
        role=ROLE_SYSTEM_ADMIN,
        organization_id=None,
        email="root@example.com",
        identity_sub="root-sub",
    )
    db = SettingsDB(organizations=[org], users=[member], objects={member.id: member})

    response = pages.users_page(make_request(path="/settings/users"), user=user, db=db)

    body = render_body(response)
    assert response.status_code == 200
    assert "Manage users" not in body
    assert "learner@example.com" in body
    assert f'value="{org.id}"' in body
    assert "Save user" in body


class JobsSettingsDB(FakeDB):
    def __init__(self, execute_results=None):
        super().__init__(objects={})
        self.execute_results = list(execute_results or [])

    def execute(self, stmt):
        result = self.execute_results.pop(0) if self.execute_results else []
        if isinstance(result, list) and result and isinstance(result[0], tuple):
            return SimpleNamespace(all=lambda: list(result))
        return FakeResult(result)


def test_jobs_page_shows_retry_for_failed_bulk_job():
    admin = SimpleNamespace(
        id=uuid4(),
        role=ROLE_SYSTEM_ADMIN,
        organization_id=None,
        email="root@example.com",
        identity_sub="root-sub",
    )
    bulk_id = uuid4()
    deck_id = uuid4()
    job = SimpleNamespace(
        id=uuid4(),
        job_type="bulk_ai_upload",
        status="failed",
        processed_items=2,
        total_items=4,
        failed_items=1,
        created_at=None,
        completed_at=None,
        reference_id=bulk_id,
    )
    bulk = SimpleNamespace(
        id=bulk_id,
        filename="batch.zip",
        total_files=4,
        status="failed",
        deck_id=deck_id,
    )
    deck = SimpleNamespace(id=deck_id, name="Deck One")
    db = JobsSettingsDB([[job], [bulk], [(bulk_id, deck)], [deck]])

    response = pages.jobs_page(make_request(path="/settings/jobs"), user=admin, db=db)

    body = render_body(response)
    assert response.status_code == 200
    assert f"/api/v1/bulk-ai-upload/{bulk_id}/resume" in body
    assert ">Retry<" in body


def test_jobs_page_hides_retry_for_running_bulk_job():
    admin = SimpleNamespace(
        id=uuid4(),
        role=ROLE_SYSTEM_ADMIN,
        organization_id=None,
        email="root@example.com",
        identity_sub="root-sub",
    )
    bulk_id = uuid4()
    job = SimpleNamespace(
        id=uuid4(),
        job_type="bulk_ai_upload",
        status="running",
        processed_items=1,
        total_items=4,
        failed_items=0,
        created_at=None,
        completed_at=None,
        reference_id=bulk_id,
    )
    bulk = SimpleNamespace(
        id=bulk_id,
        filename="batch.zip",
        total_files=4,
        status="processing",
        deck_id=None,
    )
    db = JobsSettingsDB([[job], [bulk], [], []])

    response = pages.jobs_page(make_request(path="/settings/jobs"), user=admin, db=db)

    body = render_body(response)
    assert response.status_code == 200
    assert f"/api/v1/bulk-ai-upload/{bulk_id}/resume" not in body
