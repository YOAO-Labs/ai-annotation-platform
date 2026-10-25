"""Capture unchanged rename behavior against real, independently held locks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from tests import test_project_class_rename_legacy as legacy
from tests.factory import create_project, create_task, create_user
from tests.labeling_class_query_helpers import (
    api_db_binding,
    assert_owned_test_database,
    clear_local_identity,
    make_request_runtime,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_PROJECT_CLASS_RENAME_LOCK_LEGACY_CAPTURE") != "1",
        reason="requires explicit owned legacy rename lock capture",
    ),
]


async def test_capture_real_legacy_project_class_rename_lock_modes(
    test_engine, app_module, monkeypatch
):
    await assert_owned_test_database(test_engine)
    output = Path(os.environ["AAP_PROJECT_CLASS_RENAME_LOCK_LEGACY_OUTPUT"])
    root = Path(__file__).resolve().parents[3]
    assert output.is_absolute() and output.resolve().parent == root / ".worktree"
    assert not output.exists()
    source = legacy._source_metadata()
    clear_local_identity(monkeypatch)
    for name in (
        "workspace_project_query_url",
        "workspace_project_query_database_url",
        "workspace_project_query_database_url_file",
    ):
        monkeypatch.setattr(settings, name, "")
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_ids, project_ids, task_ids = [], [], []
    runtime = None
    capture = None
    try:
        async with maker.begin() as seed:
            actor = await create_user(
                seed,
                "project_admin",
                f"rename-lock-{uuid.uuid4().hex}@example.test",
                "Rename lock capture",
            )
            actor_ids.append(actor.id)
            project = await create_project(
                seed, owner_id=actor.id, name="Rename lock capture"
            )
            project_ids.append(project.id)
            project.tool_bindings = {"bbox": legacy._binding(["Car"])}
            task = await create_task(seed, project_id=project.id)
            task_ids.append(task.id)
            annotation = Annotation(
                id=uuid.uuid4(),
                project_id=project.id,
                task_id=task.id,
                user_id=actor.id,
                annotation_type="bbox",
                tool_unit_id="bbox",
                source="manual",
                class_name="Car",
                geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                attributes={},
                is_active=True,
            )
            seed.add(annotation)

        runtime, runtime_maker = await make_request_runtime(
            test_engine, application_name="legacy-rename-lock-api"
        )
        aliases = {
            str(actor.id): "$actor_owner",
            actor.email: "$email_owner",
            str(project.id): "$project_target",
            project.display_id: "$display_target",
            str(task.id): "$task_target",
            str(annotation.id): "$annotation_target",
        }
        samples = []
        headers = {
            "Authorization": f"Bearer {create_access_token(subject=str(actor.id), role=actor.role)}"
        }
        scenarios = (
            ("account_update_blocks_changed", "users", "UPDATE", actor.id, "Truck"),
            ("account_update_blocks_noop", "users", "UPDATE", actor.id, "Car"),
            (
                "project_update_blocks_changed",
                "projects",
                "UPDATE",
                project.id,
                "Truck",
            ),
            (
                "project_key_share_allows_noop",
                "projects",
                "KEY SHARE",
                project.id,
                "Car",
            ),
            (
                "project_key_share_blocks_upgrade",
                "projects",
                "KEY SHARE",
                project.id,
                "Truck",
            ),
        )
        async with api_db_binding(app_module, runtime_maker):
            transport = httpx.ASGITransport(app=app_module, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://legacy-rename-lock", timeout=10
            ) as client:
                for label, table, mode, target, new_name in scenarios:
                    # Both identifiers come exclusively from the fixed scenario
                    # inventory above. The row UUID remains a bound parameter.
                    assert table in {"users", "projects"}
                    assert mode in {"UPDATE", "KEY SHARE"}
                    async with test_engine.connect() as holder:
                        async with holder.begin():
                            locked = await holder.scalar(
                                text(
                                    f"SELECT id FROM public.{table} "
                                    f"WHERE id=:id FOR {mode}"
                                ),
                                {"id": target},
                            )
                            assert locked == target
                            state = {"current": {"label": label}}
                            with legacy._observe_runtime(runtime, state) as activity:
                                async with asyncio.timeout(10):
                                    response = await client.post(
                                        f"/api/v1/projects/{project.id}/classes/rename",
                                        headers=headers,
                                        json={
                                            "old_name": "Car",
                                            "new_name": new_name,
                                            "tool_unit_id": "bbox",
                                        },
                                    )
                            expected = (
                                200 if label == "project_key_share_allows_noop" else 409
                            )
                            assert response.status_code == expected, response.text
                            snapshot = await legacy._snapshot(maker, project.id)
                            assert (
                                snapshot["tool_bindings"]["bbox"]["classes"][0]["name"]
                                == "Car"
                            )
                            assert snapshot["annotations"][0]["class_name"] == "Car"
                            assert activity["transaction_events"].count("commit") == 0
                            samples.append(
                                {
                                    "label": label,
                                    "held_row": {"table": table, "mode": mode},
                                    "request": {
                                        "old_name": "Car",
                                        "new_name": new_name,
                                        "tool_unit_id": "bbox",
                                    },
                                    "response": {
                                        "status": response.status_code,
                                        "headers": {
                                            key: response.headers[key]
                                            for key in legacy._HEADERS
                                            if key in response.headers
                                        },
                                        "body": legacy._normalise(
                                            response.json(), aliases
                                        ),
                                    },
                                    "observation": legacy._activity_summary(activity),
                                    "post_response_state": legacy._normalise(
                                        snapshot, aliases
                                    ),
                                }
                            )
        capture = {
            "capture_kind": "actual-pre-extraction-project-class-rename-locks",
            "source": source,
            "lock_driver_sha256": hashlib.sha256(
                Path(__file__).read_bytes()
            ).hexdigest(),
            "samples": samples,
            "limit": "actual lock holders and NOWAIT behavior; no latency or capacity claim",
        }
    finally:
        if runtime is not None:
            await runtime.dispose()
        await legacy._cleanup_owned(
            maker, actor_ids=actor_ids, project_ids=project_ids, task_ids=task_ids
        )

    assert capture is not None and len(capture["samples"]) == 5
    with output.open("x", encoding="utf-8") as stream:
        json.dump(capture, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
