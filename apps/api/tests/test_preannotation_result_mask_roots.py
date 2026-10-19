"""Mask GC keeps pending ordinary preannotation payloads alive."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[3]
ROOTS_SQL = ROOT / "apps/api/alembic/sql/0231_preannotation_result_mask_roots.sql"


async def _native_call(db, operation: str, payload: dict):
    result = await db.execute(
        text(f"SELECT aap_labeling_v1.{operation}(CAST(:payload AS jsonb))"),
        {"payload": json.dumps(payload)},
    )
    return result.scalar_one()


def _key(digest: str) -> str:
    return f"raster-masks/sha256/{digest[:2]}/{digest[2:4]}/{digest}.json"


async def _seed_pending_payload(db, key: str):
    job_id = uuid4()
    actor_id = uuid4()
    project_id = uuid4()
    task_id = uuid4()
    result_id = uuid4()
    payload_id = uuid4()
    created_at = datetime.now(timezone.utc)
    body = json.dumps({"result": [{"mask": {"object_key": key}}]})
    payload_digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    await db.execute(
        text(
            "INSERT INTO public.batch_result_executions "
            "(job_id, actor_id, project_id, request_digest, state, total, "
            "selection_complete) VALUES "
            "(:job_id, :actor_id, :project_id, :digest, 'running', 1, true)"
        ),
        {
            "job_id": job_id,
            "actor_id": actor_id,
            "project_id": project_id,
            "digest": "a" * 64,
        },
    )
    await db.execute(
        text(
            "INSERT INTO public.batch_result_members "
            "(job_id, ordinal, task_id, failure_id, state, result_id) VALUES "
            "(:job_id, 1, :task_id, :failure_id, 'captured', :result_id)"
        ),
        {
            "job_id": job_id,
            "task_id": task_id,
            "failure_id": uuid4(),
            "result_id": result_id,
        },
    )
    await db.execute(
        text(
            "INSERT INTO public.batch_result_bundles "
            "(result_id, job_id, ordinal, task_id, result_count, raw_digest, "
            "raw_complete, state) VALUES "
            "(:result_id, :job_id, 1, :task_id, 1, :digest, true, 'captured')"
        ),
        {
            "result_id": result_id,
            "job_id": job_id,
            "task_id": task_id,
            "digest": payload_digest,
        },
    )
    await db.execute(
        text(
            "INSERT INTO public.batch_result_payloads "
            "(payload_id, result_id, job_id, ordinal, request_index, prediction_id, "
            "prediction_created_at, stage, sha256, byte_size, part_count, complete, "
            "roots_active, body) VALUES "
            "(:payload_id, :result_id, :job_id, 1, 0, :prediction_id, :created_at, "
            "'raw', :digest, :byte_size, 1, true, true, CAST(:body AS jsonb))"
        ),
        {
            "payload_id": payload_id,
            "result_id": result_id,
            "job_id": job_id,
            "prediction_id": uuid4(),
            "created_at": created_at,
            "digest": payload_digest,
            "byte_size": len(body.encode("utf-8")),
            "body": body,
        },
    )
    return payload_id


async def _stage_old_key(db, key: str):
    worker_id = uuid4()
    claim = await _native_call(
        db,
        "claim_mask_gc_sweep",
        {"worker_id": str(worker_id), "lease_seconds": 30},
    )
    assert claim["ok"] is True, claim
    sweep = claim["data"]["sweep"]
    last_modified = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(
        timespec="microseconds"
    )
    return await _native_call(
        db,
        "stage_mask_gc_page",
        {
            "sweep_id": sweep["sweep_id"],
            "generation": sweep["generation"],
            "lease_token": sweep["lease_token"],
            "after_key": sweep["after_key"],
            "page": {
                "items": [
                    {
                        "object_key": key,
                        "etag": "preannotation-result-test",
                        "last_modified": last_modified,
                        "byte_size": 123,
                    }
                ],
                "after_key": key,
                "complete": True,
            },
        },
    )


async def test_pending_payload_root_blocks_native_mask_gc_claim(db_session):
    key = _key(uuid4().hex * 2)
    payload_id = await _seed_pending_payload(db_session, key)

    root_live, generic_live, generic_expired = (
        await db_session.execute(
            text(
                "SELECT "
                "aap_labeling_v1._mask_gc_root_entry_live("
                "'batch_result_payloads', :payload_id, NULL, true), "
                "aap_labeling_v1._mask_gc_root_entry_live("
                "'predictions', 'legacy-root', NULL, false), "
                "aap_labeling_v1._mask_gc_root_entry_live("
                "'predictions', 'expired-root', "
                "pg_catalog.clock_timestamp() - interval '1 second', false)"
            ),
            {"payload_id": str(payload_id)},
        )
    ).one()
    assert (root_live, generic_live, generic_expired) == (True, True, False)

    roots = await db_session.execute(
        text(
            "SELECT object_key FROM public.mask_gc_roots "
            "WHERE root_kind='batch_result_payloads' AND root_id=:payload_id"
        ),
        {"payload_id": str(payload_id)},
    )
    assert roots.scalars().all() == [key]

    staged = await _stage_old_key(db_session, key)
    assert staged["ok"] is True, staged
    assert staged["data"]["staged"] == 0
    assert staged["data"]["skipped"] == 1
    delete_claim = await _native_call(
        db_session,
        "claim_mask_gc_deletes",
        {"worker_id": str(uuid4()), "limit": 1, "lease_seconds": 30},
    )
    assert delete_claim["ok"] is True, delete_claim
    assert delete_claim["data"]["items"] == []
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.mask_gc_intents "
                "WHERE object_key=:key AND state <> 'completed'"
            ),
            {"key": key},
        )
    ) == 0


async def test_payload_root_transfers_and_releases_with_its_snapshot(db_session):
    old_key = _key(uuid4().hex * 2)
    new_key = _key(uuid4().hex * 2)
    payload_id = await _seed_pending_payload(db_session, old_key)

    await db_session.execute(
        text(
            "UPDATE public.batch_result_payloads SET body=CAST(:body AS jsonb) "
            "WHERE payload_id=:payload_id"
        ),
        {
            "payload_id": payload_id,
            "body": json.dumps({"result": [{"mask": {"object_key": new_key}}]}),
        },
    )
    roots = await db_session.execute(
        text(
            "SELECT object_key FROM public.mask_gc_roots "
            "WHERE root_kind='batch_result_payloads' AND root_id=:payload_id "
            "ORDER BY object_key"
        ),
        {"payload_id": str(payload_id)},
    )
    assert roots.scalars().all() == [new_key]
    assert (await _stage_old_key(db_session, old_key))["data"]["staged"] == 1
    assert (await _stage_old_key(db_session, new_key))["data"]["staged"] == 0

    await db_session.execute(
        text(
            "UPDATE public.batch_result_payloads SET roots_active=false "
            "WHERE payload_id=:payload_id"
        ),
        {"payload_id": payload_id},
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.mask_gc_roots "
                "WHERE root_kind='batch_result_payloads' AND root_id=:payload_id"
            ),
            {"payload_id": str(payload_id)},
        )
    ) == 0
    assert (await _stage_old_key(db_session, new_key))["data"]["staged"] == 1


async def test_terminal_bundle_states_release_payload_roots(db_session):
    for terminal_state in ("applied", "failed", "abandoned"):
        key = _key(uuid4().hex * 2)
        payload_id = await _seed_pending_payload(db_session, key)
        if terminal_state == "applied":
            await db_session.execute(
                text(
                    "UPDATE public.batch_result_bundles "
                    "SET state=:state, prepared_complete=true, "
                    "prepared_digest=:digest, applied_at=pg_catalog.clock_timestamp() "
                    "WHERE result_id=(SELECT result_id FROM "
                    "public.batch_result_payloads WHERE payload_id=:payload_id)"
                ),
                {
                    "state": terminal_state,
                    "digest": "f" * 64,
                    "payload_id": payload_id,
                },
            )
        else:
            await db_session.execute(
                text(
                    "UPDATE public.batch_result_bundles SET state=:state "
                    "WHERE result_id=(SELECT result_id FROM "
                    "public.batch_result_payloads WHERE payload_id=:payload_id)"
                ),
                {"state": terminal_state, "payload_id": payload_id},
            )

        live = await db_session.scalar(
            text(
                "SELECT aap_labeling_v1._mask_gc_root_entry_live("
                "'batch_result_payloads', :payload_id, NULL, false)"
            ),
            {"payload_id": str(payload_id)},
        )
        assert live is False
        staged = await _stage_old_key(db_session, key)
        assert staged["ok"] is True, staged
        assert staged["data"]["staged"] == 1


def test_liveness_extension_preserves_existing_root_kinds_and_lock_order():
    source = ROOTS_SQL.read_text(encoding="utf-8")
    helper = source.split(
        "CREATE OR REPLACE FUNCTION aap_labeling_v1._mask_gc_root_entry_live(", 1
    )[1].split("$function$;", 1)[0]
    assert "p_root_kind = 'batch_result_payloads'" in helper
    assert "public.batch_result_payloads" in helper
    assert "public.batch_result_bundles" in helper
    assert "bundle.state IN ('capturing', 'captured', 'ready')" in helper
    assert "payload.complete" in helper
    assert "payload.roots_active" in helper
    assert "payload.body IS NOT NULL" in helper
    assert "p_root_kind = 'video_tracker_input_scopes'" in helper
    assert "RETURN p_valid_until IS NULL OR p_valid_until >" in helper
    assert "EXCEPTION WHEN OTHERS THEN\n            RETURN p_fail_closed;" in helper
    assert "FOR UPDATE" not in helper
    assert "NOWAIT" not in helper
