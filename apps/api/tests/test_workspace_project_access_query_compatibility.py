"""Replay the frozen Project-access contract through each current provider."""

from __future__ import annotations

from contextlib import AsyncExitStack
import hashlib
import json
import os
from pathlib import Path

import pytest

from app.config import settings
from tests.workspace_project_access_query_helpers import (
    replay_workspace_project_access,
)
from tests.workspace_project_query_helpers import (
    api_project_query_binding,
    project_query_dsn,
    project_query_provider,
)


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_FIXTURE = Path(__file__).parent / "fixtures/workspace_access_reads_legacy/samples.json"
_FIXTURE_SHA256 = "d5d579a4ac384bc04891d9d7378eebdf01a01397351c7aa2d33d23154270603d"


def _public_http_sample(sample: dict) -> dict:
    response = sample["response"]
    return {
        "label": sample["label"],
        "request": {
            "method": sample["request"]["method"],
            "path": sample["request"]["path"],
            "actor_alias": sample["request"]["actor"]["alias"],
        },
        "response": {
            "status": response["status"],
            "headers": response["headers"],
            "body": response["body"],
            "raw_body": {"byte_length": response["raw_body"]["byte_length"]},
        },
    }


def _resolver_probe(sample: dict) -> dict:
    fields = (
        "status",
        "access",
        "detail",
        "no_autoflush",
        "caller_transaction_open_after_read",
        "visible_from_owner_before_caller_rollback",
        "pending_user_id",
        "caller",
    )
    return {name: sample[name] for name in fields if name in sample}


@pytest.mark.parametrize("binding", ["borrowed", "dedicated", "http"])
async def test_current_project_access_owner_matches_frozen_public_samples(
    test_engine, app_module, monkeypatch, binding
):
    fixture_bytes = _FIXTURE.read_bytes()
    assert hashlib.sha256(fixture_bytes).hexdigest() == _FIXTURE_SHA256
    reference = json.loads(fixture_bytes)
    assert reference["capture_kind"] == "actual-pre-extraction-project-access-http"
    assert len(reference["samples"]) == 16

    for name in (
        "workspace_project_query_url",
        "workspace_project_query_database_url",
        "workspace_project_query_database_url_file",
    ):
        monkeypatch.setattr(settings, name, "")

    async with AsyncExitStack() as stack:
        token = "test-project-query"
        url = dsn = None
        if binding == "dedicated":
            dsn = project_query_dsn(test_engine)
        elif binding == "http":
            # Access queries use the identity actor snapshot in the API; they do
            # not depend on the separate Identity profile/briefs capability.
            url = await stack.enter_async_context(
                project_query_provider(project_query_dsn(test_engine), token)
            )
        await stack.enter_async_context(
            api_project_query_binding(url=url, dsn=dsn, token=token)
        )
        current = await replay_workspace_project_access(
            test_engine,
            app_module,
            monkeypatch,
            reference["samples"],
            token=token,
        )

    expected_http = [_public_http_sample(sample) for sample in reference["samples"]]
    assert current["samples"] == expected_http

    # These two calls deliberately exercise the existing resolver directly.
    # They bypass the HTTP visibility dependency and are not part of the new
    # Project-query HTTP owner trace above.
    expected_resolver = reference["observations"]["resolver_caller_autoflush"]
    assert current["resolver_caller_autoflush"] == {
        "caller": expected_resolver["caller"],
        "default": _resolver_probe(expected_resolver["default"]),
        "no_autoflush": _resolver_probe(expected_resolver["no_autoflush"]),
    }
