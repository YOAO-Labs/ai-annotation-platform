import json
import uuid

from cryptography.fernet import Fernet
import httpx
from app.schemas.annotation_feedback import AnnotationFeedbackCreate
from sqlalchemy.engine import URL

import pytest

from tests.test_notifications_module_assembly_live import (
    _batch_predict_terminal_job,
    _feedback_issue,
)
from tests.notifications_module_acceptance_helpers import (
    api_redis_acl_commands,
    kombu_fanout_channel_pattern,
    new_fernet_mail_key,
    notification_publish_database_url,
)


def _database_url(**changes):
    return URL.create(
        "postgresql+asyncpg",
        username="notification_publish_client",
        password="scoped-secret",
        host="127.0.0.1",
        port=55432,
        database="notifications_fixture_test",
    ).set(**changes)


def test_generated_fixture_mail_key_is_accepted_by_fernet():
    key = new_fernet_mail_key()

    assert isinstance(Fernet(key.encode("ascii")), Fernet)


@pytest.mark.asyncio
async def test_feedback_issue_posts_schema_valid_project_anchor():
    project_id = uuid.UUID("60000000-0000-4000-8000-000000000001")
    feedback_id = uuid.UUID("60000000-0000-4000-8000-000000000002")
    captured = {}

    def handle(request):
        body = json.loads(request.content)
        payload = AnnotationFeedbackCreate.model_validate(body)
        assert payload.anchor_type == "project"
        assert payload.task_id is None
        captured["body"] = body
        return httpx.Response(200, json={"id": str(feedback_id)})

    async with httpx.AsyncClient(
        base_url="http://notifications-test",
        transport=httpx.MockTransport(handle),
    ) as client:
        assert (
            await _feedback_issue(
                client, project_id=project_id, body="Project-scoped test issue"
            )
            == feedback_id
        )

    assert captured["body"] == {
        "kind": "issue",
        "anchor_type": "project",
        "project_id": str(project_id),
        "body": "Project-scoped test issue",
    }


def test_batch_predict_terminal_fixture_keeps_project_scope():
    job_id = uuid.UUID("60000000-0000-4000-8000-000000000003")
    user_id = uuid.UUID("60000000-0000-4000-8000-000000000004")
    project_id = uuid.UUID("60000000-0000-4000-8000-000000000005")

    job = _batch_predict_terminal_job(job_id, user_id=user_id, project_id=project_id)

    assert job.kind == "batch_predict"
    assert job.user_id == user_id
    assert job.project_id == project_id


def test_publisher_role_url_stays_on_the_owned_test_database():
    reference = URL.create(
        "postgresql+asyncpg",
        username="owner",
        password="owner-secret",
        host="127.0.0.1",
        port=55432,
        database="notifications_fixture_test",
    )

    bound = notification_publish_database_url(
        _database_url().render_as_string(hide_password=False), reference
    )

    assert bound.drivername == "postgresql"
    assert (
        bound.username,
        bound.password,
        bound.host,
        bound.port,
        bound.database,
    ) == (
        "notification_publish_client",
        "scoped-secret",
        "127.0.0.1",
        55432,
        "notifications_fixture_test",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"username": "owner"},
        {"password": ""},
        {"host": "localhost"},
        {"port": 55433},
        {"database": "production"},
    ],
)
def test_publisher_role_url_rejects_wrong_identity_or_database(changes):
    reference = URL.create(
        "postgresql+asyncpg",
        username="owner",
        password="owner-secret",
        host="127.0.0.1",
        port=55432,
        database="notifications_fixture_test",
    )

    with pytest.raises(ValueError, match="owned test database"):
        notification_publish_database_url(
            _database_url(**changes).render_as_string(hide_password=False), reference
        )


def test_api_redis_acl_keeps_cache_and_queue_with_scoped_fanout_only():
    commands = set(api_redis_acl_commands())

    assert "-@all" in commands
    assert {
        "+@connection",
        "+@read",
        "+@write",
        "+@blocking",
        "+@scripting",
        "+@transaction",
        "+@pubsub",
        "-@admin",
        "-@dangerous",
        "+eval",
        "+evalsha",
        "+script|load",
    } <= commands
    assert "+publish" not in commands


@pytest.mark.parametrize(("database", "expected"), [(None, "&/0.*"), ("12", "&/12.*")])
def test_kombu_fanout_acl_matches_the_selected_redis_database(database, expected):
    assert (
        kombu_fanout_channel_pattern(
            URL.create("redis", host="redis", port=6379, database=database)
        )
        == expected
    )


def test_kombu_fanout_acl_rejects_unsupported_channel_prefixes():
    with pytest.raises(ValueError, match="unsupported Kombu fanout prefix"):
        kombu_fanout_channel_pattern(
            URL.create(
                "redis",
                host="redis",
                port=6379,
                database="0",
                query={"global_keyprefix": "api:"},
            )
        )
