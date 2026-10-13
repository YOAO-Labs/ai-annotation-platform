"""Pure tests for project-scoped notification delivery gating (B3).

No database: only the payload-to-project extraction contract is exercised here.
The batched membership recheck runs against PostgreSQL in the API suite.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.services.notification import NotificationService, _notification_project_id


def _row(payload, target_type="bug_report", target_id=None):
    return SimpleNamespace(
        payload=payload,
        target_type=target_type,
        target_id=target_id or uuid.uuid4(),
    )


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows=None):
        self._rows = rows or []

    async def execute(self, *_args, **_kwargs):
        return _FakeResult(self._rows)


def _job_row(payload, target_type, target_id):
    row = SimpleNamespace(payload=payload, target_type=target_type, target_id=target_id)
    return row


def test_project_id_extracted_from_payload():
    project_id = uuid.uuid4()
    assert _notification_project_id(_row({"project_id": str(project_id)})) == project_id


def test_discussion_payload_project_scope():
    project_id = uuid.uuid4()
    row = _row({"project_id": str(project_id), "task_id": str(uuid.uuid4())})
    assert _notification_project_id(row) == project_id


def test_non_project_payload_is_ungated():
    assert _notification_project_id(_row({"kind": "audit_archive"})) is None
    assert _notification_project_id(_row({})) is None
    assert _notification_project_id(_row(None)) is None


def test_malformed_project_id_is_ungated():
    assert _notification_project_id(_row({"project_id": "not-a-uuid"})) is None


async def test_payload_project_id_marks_notification_restricted():
    svc = NotificationService(_FakeDb())
    rows = [_row({"project_id": str(uuid.uuid4())})]
    scopes = await svc._resolve_delivery_scopes(rows)
    assert scopes[0][0] is True
    assert scopes[0][1] is not None
    assert scopes[0][2] is False


async def test_missing_project_id_is_not_restricted():
    svc = NotificationService(_FakeDb())
    rows = [_row({"display_id": "B-1"})]
    scopes = await svc._resolve_delivery_scopes(rows)
    assert scopes[0] == (False, None, False)


async def test_export_target_without_resolvable_job_fails_closed():
    svc = NotificationService(_FakeDb())
    target_id = uuid.uuid4()
    rows = [_job_row({"download_url": "https://x"}, "export", target_id)]
    scopes = await svc._resolve_delivery_scopes(rows)
    assert scopes == [(True, None, True)]


async def test_export_target_resolves_job_project_and_export_capability():
    target_id = uuid.uuid4()
    project_id = uuid.uuid4()
    svc = NotificationService(_FakeDb([(target_id, project_id, "export")]))
    rows = [_job_row({"download_url": "https://x"}, "export", target_id)]
    scopes = await svc._resolve_delivery_scopes(rows)
    assert scopes == [(True, project_id, True)]


async def test_global_job_target_is_preserved_as_global():
    target_id = uuid.uuid4()
    svc = NotificationService(_FakeDb([(target_id, None, "audit_archive")]))
    rows = [_job_row({"kind": "audit_archive"}, "async_job", target_id)]
    scopes = await svc._resolve_delivery_scopes(rows)
    assert scopes == [(False, None, False)]


async def test_project_async_job_without_payload_scope_is_restricted():
    """A project async job is restricted even when payload omits project_id."""

    target_id = uuid.uuid4()
    project_id = uuid.uuid4()
    svc = NotificationService(_FakeDb([(target_id, project_id, "batch_predict")]))
    rows = [_job_row({"batch_display_id": "B-1"}, "async_job", target_id)]
    scopes = await svc._resolve_delivery_scopes(rows)
    assert scopes == [(True, project_id, False)]


async def test_missing_async_job_target_fails_closed():
    svc = NotificationService(_FakeDb([]))
    target_id = uuid.uuid4()
    rows = [_job_row({}, "async_job", target_id)]
    scopes = await svc._resolve_delivery_scopes(rows)
    assert scopes == [(True, None, False)]


async def test_export_kind_async_job_requires_export_capability():
    target_id = uuid.uuid4()
    project_id = uuid.uuid4()
    svc = NotificationService(_FakeDb([(target_id, project_id, "export")]))
    rows = [_job_row({}, "async_job", target_id)]
    scopes = await svc._resolve_delivery_scopes(rows)
    assert scopes == [(True, project_id, True)]
