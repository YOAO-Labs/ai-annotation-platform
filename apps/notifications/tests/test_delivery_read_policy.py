from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest

from aap_notifications.delivery_read import (
    GLOBAL_ASYNC_JOB_KINDS,
    NotificationDeliveryReadPolicy,
    notification_project_id,
    unrestricted_delivery_indices,
)
from aap_notifications.publication_contract import PublicationAuthorization


def _row(
    *,
    user_id=None,
    target_type="task",
    target_id=None,
    payload=None,
):
    return SimpleNamespace(
        user_id=user_id or uuid.uuid4(),
        target_type=target_type,
        target_id=target_id or uuid.uuid4(),
        payload=payload,
    )


class _Data:
    def __init__(self):
        self.calls = []
        self.jobs = {}
        self.member_result = []
        self.reviewer_result = []
        self.owner_result = []
        self.super_result = []

    async def job_scopes(self, job_ids):
        self.calls.append(("jobs", set(job_ids)))
        return [
            (job_id, project_id, kind)
            for job_id, (project_id, kind) in self.jobs.items()
            if job_id in job_ids
        ]

    async def member_pairs(self, pairs):
        self.calls.append(("members", set(pairs)))
        return self.member_result

    async def reviewer_pairs(self, pairs):
        self.calls.append(("reviewers", set(pairs)))
        return self.reviewer_result

    async def owner_pairs(self, pairs):
        self.calls.append(("owners", set(pairs)))
        return self.owner_result

    async def super_pairs(self, pairs):
        self.calls.append(("super", set(pairs)))
        return self.super_result


@pytest.mark.asyncio
async def test_payload_scope_keeps_python_dict_key_and_uuid_parsing_semantics():
    project_id = uuid.uuid4()
    parsed_forms = [
        str(project_id).upper(),
        "{" + str(project_id) + "}",
        project_id.hex,
    ]
    rows = [
        _row(payload=None),
        _row(payload=[]),
        _row(payload=["project_id"]),
        _row(payload="project_id"),
        _row(payload=17),
        _row(payload={}),
        _row(payload={"project_id": None}),
        _row(payload={"project_id": "not-a-uuid"}),
        _row(payload={"project_id": 10**40}),
        *(_row(payload={"project_id": value}) for value in parsed_forms),
    ]
    policy = NotificationDeliveryReadPolicy(_Data())

    scopes = await policy.resolve_scopes(rows)

    assert scopes == [
        (False, None, False),
        (False, None, False),
        (False, None, False),
        (False, None, False),
        (False, None, False),
        (False, None, False),
        (True, None, False),
        (True, None, False),
        (True, None, False),
        (True, project_id, False),
        (True, project_id, False),
        (True, project_id, False),
    ]
    assert notification_project_id(_row(payload={"project_id": project_id.hex})) == (
        project_id
    )


@pytest.mark.asyncio
async def test_actual_jobs_override_payload_and_unknown_or_missing_jobs_fail_closed():
    (
        global_id,
        import_id,
        unknown_id,
        scoped_id,
        export_id,
        async_export_id,
        missing_id,
    ) = [uuid.uuid4() for _ in range(7)]
    scoped_project, forged_project, export_project = [uuid.uuid4() for _ in range(3)]
    rows = [
        _row(
            target_type="async_job", target_id=global_id, payload={"project_id": "bad"}
        ),
        _row(target_type="async_job", target_id=import_id, payload={}),
        _row(target_type="async_job", target_id=unknown_id, payload={}),
        _row(
            target_type="async_job",
            target_id=scoped_id,
            payload={"project_id": str(forged_project)},
        ),
        _row(target_type="export", target_id=export_id, payload={}),
        _row(target_type="async_job", target_id=async_export_id, payload={}),
        _row(target_type="async_job", target_id=missing_id, payload={}),
    ]
    data = _Data()
    data.jobs = {
        global_id: (None, "audit_archive"),
        import_id: (None, "dataset_import"),
        unknown_id: (None, "future_job_kind"),
        scoped_id: (scoped_project, "mask_qc"),
        export_id: (export_project, "export"),
        async_export_id: (export_project, "export"),
    }
    policy = NotificationDeliveryReadPolicy(data)

    scopes = await policy.resolve_scopes(rows)

    assert scopes == [
        (False, None, False),
        (False, None, False),
        (True, None, False),
        (True, scoped_project, False),
        (True, export_project, True),
        (True, export_project, True),
        (True, None, False),
    ]
    assert data.calls == [
        (
            "jobs",
            {
                global_id,
                import_id,
                unknown_id,
                scoped_id,
                export_id,
                async_export_id,
                missing_id,
            },
        )
    ]
    assert GLOBAL_ASYNC_JOB_KINDS == {"audit_archive", "dataset_import"}


@pytest.mark.asyncio
async def test_allowed_indices_preserve_pairs_and_query_phase_order():
    user_a, user_b, user_c, user_d, user_e, user_f = [uuid.uuid4() for _ in range(6)]
    project_a, project_b, project_c, project_d, project_e = [
        uuid.uuid4() for _ in range(5)
    ]
    rows = [
        _row(user_id=user_a, payload={"project_id": str(project_a)}),
        _row(user_id=user_b, payload={"project_id": str(project_a)}),
        _row(user_id=user_c, target_type="export"),
        _row(user_id=user_d, payload={"project_id": str(project_c)}),
        _row(user_id=user_e, payload={"project_id": str(project_d)}),
        _row(user_id=user_f),
    ]
    scopes = [
        (True, project_a, False),
        (True, project_a, False),
        (True, project_b, True),
        (True, project_c, False),
        (True, project_d, False),
        (False, None, False),
    ]
    pairs = {
        (project_a, user_a),
        (project_a, user_b),
        (project_b, user_c),
        (project_c, user_d),
        (project_d, user_e),
    }
    data = _Data()
    data.member_result = [(project_a, user_a)]
    data.reviewer_result = [(project_b, user_c)]
    data.owner_result = [(project_c, user_d)]
    data.super_result = []
    policy = NotificationDeliveryReadPolicy(data)

    allowed = await policy.allowed_indices(rows, scopes)

    assert allowed == {0, 2, 3, 5}
    assert [name for name, _pairs in data.calls] == [
        "members",
        "reviewers",
        "owners",
        "super",
    ]
    assert all(requested == pairs for _name, requested in data.calls)


@pytest.mark.asyncio
async def test_authorization_facts_keep_four_ordered_reads_and_do_not_touch_unrestricted_users():
    user = uuid.uuid4()
    project = uuid.uuid4()
    rows = [
        _row(user_id=user, payload={"project_id": str(project)}),
        _row(payload={}),
        _row(payload={"project_id": None}),
    ]
    scopes = [
        (True, project, True),
        (False, None, False),
        (True, None, False),
    ]
    data = _Data()
    data.member_result = [(project, user)]
    data.reviewer_result = []
    data.owner_result = []
    data.super_result = []
    policy = NotificationDeliveryReadPolicy(data)

    facts = await policy.authorization_facts(rows, scopes)

    assert facts == [
        PublicationAuthorization(
            restricted=True,
            project_id=project,
            export_required=True,
            member=True,
            reviewer=False,
            owner=False,
            super_admin=False,
        ),
        PublicationAuthorization(
            restricted=False,
            project_id=None,
            export_required=False,
            member=False,
            reviewer=False,
            owner=False,
            super_admin=False,
        ),
        PublicationAuthorization(
            restricted=True,
            project_id=None,
            export_required=False,
            member=False,
            reviewer=False,
            owner=False,
            super_admin=False,
        ),
    ]
    assert [name for name, _pairs in data.calls] == [
        "members",
        "reviewers",
        "owners",
        "super",
    ]


@pytest.mark.asyncio
async def test_unresolved_authorization_facts_do_not_read_recipient_or_query_pairs():
    class NoRecipient:
        target_type = "task"
        target_id = uuid.uuid4()
        payload = {}

        @property
        def user_id(self):
            raise AssertionError("unrestricted/unresolved facts must not read user_id")

    rows = [NoRecipient(), NoRecipient()]
    scopes = [(False, None, False), (True, None, False)]
    data = _Data()
    policy = NotificationDeliveryReadPolicy(data)

    facts = await policy.authorization_facts(rows, scopes)

    assert len(facts) == 2
    assert not facts[0].restricted
    assert facts[1].restricted and facts[1].project_id is None
    assert data.calls == []


@pytest.mark.asyncio
async def test_empty_or_unresolved_pair_sets_short_circuit_authority_reads():
    data = _Data()
    policy = NotificationDeliveryReadPolicy(data)
    unscoped_rows = [_row(payload={}), _row(payload=["project_id"])]

    scopes = await policy.resolve_scopes(unscoped_rows)
    assert await policy.allowed_indices(unscoped_rows, scopes) == {0, 1}
    assert data.calls == []

    restricted_rows = [_row(payload={"project_id": None})]
    restricted_scopes = await policy.resolve_scopes(restricted_rows)
    assert await policy.allowed_indices(restricted_rows, restricted_scopes) == set()
    assert data.calls == []


def test_database_failure_fallback_allows_only_unrestricted_non_job_rows():
    rows = [
        _row(),
        _row(target_type="async_job", payload={}),
        _row(target_type="export", payload={}),
        _row(payload={"project_id": None}),
        _row(payload={"project_id": "bad"}),
        _row(payload=["project_id"]),
        _row(payload="project_id"),
    ]

    assert unrestricted_delivery_indices(rows) == {0, 5, 6}
