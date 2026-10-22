"""Cold installed-image qualification for screenshot Identity recording consumers."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import uuid
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text

from tests.identity_recording_fixture_helpers import (
    INITIAL_PREFERENCES,
    RECORDER_EMAIL,
    recording_graph,
    recording_snapshot,
)
from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    endpoint_url,
)
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.workspace_import_cli_helpers import WorkspaceImportCli

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_RECORDING_FIXTURE_LIVE") != "1",
        reason="requires the explicitly enabled owned Identity recording verifier",
    ),
]

_IDENTITY_CONSUMER_FUNCTIONS = {
    "read_actor_facts(uuid)",
    "lock_accounts_share(uuid[],boolean)",
    "lock_accounts_update(uuid[],boolean)",
    "lock_accounts_key_share(uuid[],boolean)",
    "check_accepted_api_key(uuid,uuid,text[])",
}
_FIXTURE_PREFERENCE_FUNCTION = "aap_identity_v1.replace_fixture_preferences(uuid,jsonb)"
_IDENTITY_VIEWS = {
    "aap_identity_v1.account_profiles",
    "aap_identity_v1.invitation_audit_targets",
}
_JOB_ASSET_ID = "jobs-retry-recovery"
_REVIEW_REASON = "车辆边界存在偏移，请重新贴合目标外沿。"
_UNRELATED_FEEDBACK = "unrelated recording feedback"


async def _function_set(db, role: str) -> set[str]:
    return set(
        (
            await db.scalars(
                text(
                    "SELECT ns.nspname || '.' || proc.proname || '(' || "
                    "replace(pg_catalog.oidvectortypes(proc.proargtypes), ' ', '') || ')' "
                    "FROM pg_catalog.pg_proc AS proc "
                    "JOIN pg_catalog.pg_namespace AS ns ON ns.oid=proc.pronamespace "
                    "WHERE ns.nspname='aap_identity_v1' "
                    "AND pg_catalog.has_function_privilege(:role, proc.oid, 'EXECUTE')"
                ),
                {"role": role},
            )
        ).all()
    )


async def _assert_identity_profile(test_engine, *, preference_writer: bool = True):
    _verify_postgres(test_engine.url)
    ordinary = {f"aap_identity_v1.{sig}" for sig in _IDENTITY_CONSUMER_FUNCTIONS}
    expected = set(ordinary)
    if preference_writer:
        expected.add(_FIXTURE_PREFERENCE_FUNCTION)
    async with test_engine.connect() as db:
        assert await _function_set(db, "legacy_runtime") == expected
        assert await _function_set(db, "identity_api_consumer") == ordinary
        for role in ("legacy_runtime", "identity_api_consumer"):
            for view in _IDENTITY_VIEWS:
                assert await db.scalar(
                    text(
                        "SELECT pg_catalog.has_table_privilege("
                        "CAST(:role AS name),CAST(:view AS regclass),'SELECT')"
                    ),
                    {"role": role, "view": view},
                ), (role, view)
            assert not await db.scalar(
                text(
                    "SELECT pg_catalog.has_table_privilege(CAST(:role AS name),"
                    "'public.users',"
                    "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
                ),
                {"role": role},
            ), role
            assert not await db.scalar(
                text(
                    "SELECT pg_catalog.has_any_column_privilege(CAST(:role AS name),"
                    "'public.users','SELECT,INSERT,UPDATE,REFERENCES')"
                ),
                {"role": role},
            ), role
        assert not await db.scalar(
            text(
                "SELECT pg_catalog.has_function_privilege('identity_api_consumer',"
                "CAST(:signature AS regprocedure),'EXECUTE')"
            ),
            {"signature": _FIXTURE_PREFERENCE_FUNCTION},
        )
        assert (
            await db.execute(
                text(
                    "SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname='legacy_runtime'"
                )
            )
        ).scalar_one() is False
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM pg_catalog.pg_auth_members "
                    "WHERE member=(SELECT oid FROM pg_catalog.pg_roles "
                    "WHERE rolname='legacy_runtime')"
                )
            )
            == 0
        )


async def _run(cli: WorkspaceImportCli, command: tuple[str, ...]):
    return await asyncio.to_thread(cli.run_command, command)


async def _run_job_fixture(
    cli: WorkspaceImportCli,
    action: str,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    user_email: str | None = None,
):
    command = [
        "python",
        "scripts/screenshot_job_recovery_fixture.py",
        action,
        "--project-id",
        str(project_id),
        "--task-id",
        str(task_id),
    ]
    if user_email is not None:
        command.extend(("--user-email", user_email))
    return await _run(cli, tuple(command))


async def _run_review_fixture(
    cli: WorkspaceImportCli,
    action: str,
    *,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    reviewer_email: str,
):
    return await _run(
        cli,
        (
            "python",
            "scripts/screenshot_review_reject_fixture.py",
            action,
            "--project-id",
            str(project_id),
            "--task-id",
            str(task_id),
            "--reviewer-email",
            reviewer_email,
        ),
    )


async def _run_preferences_fixture(cli: WorkspaceImportCli, user_id: uuid.UUID):
    return await _run(
        cli,
        (
            "python",
            "scripts/restore_screenshot_user_preferences.py",
            "--user-id",
            str(user_id),
            "--preferences-file",
            "/fixture/preferences.json",
        ),
    )


def _job_result(run) -> dict:
    assert not run.timed_out and run.exit_code == 0, run.evidence
    return json.loads(run.stdout.strip())


def _review_result(run, action: str) -> dict:
    assert not run.timed_out and run.exit_code == 0, run.evidence
    prefix = f"[review-reject-fixture] {action} "
    output = run.stdout.strip()
    assert output.startswith(prefix), run.evidence
    return json.loads(output[len(prefix) :])


def _assert_failed(run) -> None:
    assert not run.timed_out and run.exit_code != 0, run.evidence


def _tagged_job_rows(snapshot: dict) -> tuple[list[dict], list[dict]]:
    jobs = [
        row
        for row in snapshot["jobs"]
        if (row["payload"] or {}).get("recording_asset_id") == _JOB_ASSET_ID
    ]
    failed = [
        row
        for row in snapshot["failed"]
        if (row["extra"] or {}).get("recording_asset_id") == _JOB_ASSET_ID
    ]
    return jobs, failed


def _assert_job_fixture_state(snapshot: dict) -> None:
    jobs, failed = _tagged_job_rows(snapshot)
    assert len(jobs) == len(failed) == 1
    assert jobs[0]["kind"] == "batch_predict"
    assert jobs[0]["status"] == "failed"
    assert jobs[0]["user_id"] == "recorder"
    assert jobs[0]["payload"]["task_id"] == "ocr_task"
    assert failed[0]["error_type"] == "BackendTimeout"


def _target_task(snapshot: dict) -> dict:
    return next(row for row in snapshot["tasks"] if row["id"] == "image_task")


def _assert_review_state(snapshot: dict) -> None:
    task = _target_task(snapshot)
    assert task["status"] == "review"
    assert task["review_round_id"] == "review-round"
    assert task["review_submitter_id"] == "annotator"
    assert task["review_contributor_ids"] == ["annotator"]
    assert [row["body"] for row in snapshot["feedback"]] == [_UNRELATED_FEEDBACK]
    assert len(snapshot["notifications"]) == 1
    assert (
        snapshot["notifications"][0]["payload"]["reject_reason"] == _UNRELATED_FEEDBACK
    )


def _assert_preferences_only(
    before: dict, after: dict, expected_preferences: dict
) -> None:
    for section in ("jobs", "failed", "tasks", "feedback", "notifications", "batches"):
        assert after[section] == before[section], section
    before_users = {row["id"]: row for row in before["users"]}
    after_users = {row["id"]: row for row in after["users"]}
    assert set(after_users) == set(before_users)
    for user_id, original in before_users.items():
        expected = dict(original)
        if user_id == "recorder":
            expected["preferences"] = expected_preferences
        assert after_users[user_id] == expected, user_id


def _write_preferences(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


async def _install_preferences_gate(
    test_engine, *, recorder_id: uuid.UUID, application_name: str
) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:12]
    function = f"public.identity_recording_pause_{suffix}"
    trigger = f"identity_recording_pause_{suffix}"
    body = f"""CREATE FUNCTION {function}() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $recording_pause$
BEGIN
    IF NEW.id = '{recorder_id}'::uuid
       AND NEW.preferences ->> 'recording_cancel_gate' = '{application_name}' THEN
        PERFORM pg_catalog.set_config('application_name', '{application_name}', true);
        PERFORM pg_catalog.pg_sleep(10);
    END IF;
    RETURN NEW;
END
$recording_pause$"""
    async with test_engine.begin() as db:
        await db.execute(text(body))
        await db.execute(
            text(
                f"CREATE TRIGGER {trigger} AFTER UPDATE OF preferences ON public.users "
                f"FOR EACH ROW EXECUTE FUNCTION {function}()"
            )
        )
    return function, trigger


async def _drop_preferences_gate(test_engine, gate: tuple[str, str]) -> None:
    function, trigger = gate
    async with test_engine.begin() as db:
        await db.execute(text(f"DROP TRIGGER IF EXISTS {trigger} ON public.users"))
        await db.execute(text(f"DROP FUNCTION IF EXISTS {function}()"))


async def _gate_activity(test_engine, application_name: str) -> dict | None:
    async with test_engine.connect() as db:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT pid,usename,application_name,host(client_addr) AS client_host,"
                        "state,wait_event_type,wait_event,"
                        "position('replace_fixture_preferences' in query)>0 AS native_call "
                        "FROM pg_catalog.pg_stat_activity WHERE datname=current_database() "
                        "AND usename='legacy_runtime' AND application_name=:tag"
                    ),
                    {"tag": application_name},
                )
            )
            .mappings()
            .first()
        )
    return None if row is None else dict(row)


async def _wait_for_preferences_gate(
    test_engine,
    cli: WorkspaceImportCli,
    pending: asyncio.Task,
    application_name: str,
) -> tuple[str, dict]:
    deadline = asyncio.get_running_loop().time() + 25
    while cli.container_id is None and not pending.done():
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail("preference CLI did not create its owned container")
        await asyncio.sleep(0.02)
    if pending.done():
        pytest.fail(f"preference CLI exited before its UPDATE gate: {pending.result()}")

    while asyncio.get_running_loop().time() < deadline:
        activity = await _gate_activity(test_engine, application_name)
        if (
            activity is not None
            and activity["state"] == "active"
            and activity["wait_event_type"] == "Timeout"
            and activity["wait_event"] == "PgSleep"
            and activity["native_call"] is True
        ):
            address = await asyncio.to_thread(cli.running_database_client_address)
            assert activity["client_host"] == address
            return address, activity
        if pending.done():
            pytest.fail(
                f"preference CLI exited before its UPDATE gate: {pending.result()}"
            )
        await asyncio.sleep(0.04)
    pytest.fail("preference CLI did not reach the owned native-update gate")


async def _wait_for_backend_exit(test_engine, backend_pid: int) -> None:
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        async with test_engine.connect() as db:
            alive = await db.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity WHERE pid=:pid)"
                ),
                {"pid": backend_pid},
            )
        if not alive:
            return
        await asyncio.sleep(0.05)
    pytest.fail("stopped preference writer remains in pg_stat_activity")


async def _finish_cli_task(task: asyncio.Task) -> None:
    await asyncio.wait_for(asyncio.shield(task), timeout=35)


async def test_installed_recording_consumers_use_fenced_identity_capabilities(
    test_engine, tmp_path, record_property
):
    _verify_postgres(test_engine.url)
    resources, _main_bucket, _owner_s3 = await assert_workspace_identity(test_engine)
    endpoint = urlsplit(endpoint_url())
    assert endpoint.scheme in {"http", "https"} and endpoint.hostname
    authority_host = (
        f"[{endpoint.hostname}]" if ":" in endpoint.hostname else endpoint.hostname
    )
    access_key = os.environ["AAP_MODULE_DATA_ASSET_IMPORT_S3_ACCESS_KEY"]
    secret_key = os.environ["AAP_MODULE_DATA_ASSET_IMPORT_S3_SECRET_KEY"]
    source_root = tmp_path / "identity-recording-fixture-source"
    source_root.mkdir()
    preferences_path = source_root / "preferences.json"
    cli = None
    pending_gate = None
    preference_gate = None

    async with recording_graph(test_engine) as ids:
        base = await recording_snapshot(test_engine)
        recorder = next(row for row in base["users"] if row["id"] == "recorder")
        assert recorder["email"] == RECORDER_EMAIL
        assert recorder["role"] == "viewer" and recorder["is_active"] is False
        assert recorder["preferences"] == INITIAL_PREFERENCES
        await _assert_identity_profile(test_engine)

        try:
            cli = WorkspaceImportCli(
                test_engine,
                tmp_path,
                source_root=source_root,
                buckets=resources["buckets"],
                minio_authority=f"{authority_host}:{endpoint.port or 9000}",
                minio_use_ssl=endpoint.scheme == "https",
                s3_access_key=access_key,
                s3_secret_key=secret_key,
            )
            await asyncio.to_thread(cli.start)
            await _assert_identity_profile(test_engine)

            wrong_project = uuid.uuid4()
            bad_job_scope = await _run_job_fixture(
                cli,
                "seed",
                project_id=wrong_project,
                task_id=ids["ocr_task"],
                user_email=RECORDER_EMAIL,
            )
            record_property(
                "identity_recording_job_bad_scope",
                bad_job_scope.evidence,
            )
            _assert_failed(bad_job_scope)
            assert await recording_snapshot(test_engine) == base

            job_seed = await _run_job_fixture(
                cli,
                "seed",
                project_id=ids["ocr_project"],
                task_id=ids["ocr_task"],
                user_email=RECORDER_EMAIL,
            )
            record_property("identity_recording_job_seed", job_seed.evidence)
            job_receipt = _job_result(job_seed)
            assert job_receipt["asset_id"] == _JOB_ASSET_ID
            job_state = await recording_snapshot(test_engine)
            _assert_job_fixture_state(job_state)
            for section in ("tasks", "feedback", "notifications", "users", "batches"):
                assert job_state[section] == base[section], section

            missing_job_user = await _run_job_fixture(
                cli,
                "seed",
                project_id=ids["ocr_project"],
                task_id=ids["ocr_task"],
                user_email="missing-recording-user@fixture.test",
            )
            record_property(
                "identity_recording_job_missing_user_rollback",
                missing_job_user.evidence,
            )
            _assert_failed(missing_job_user)
            assert await recording_snapshot(test_engine) == job_state

            bad_job_scope_after_seed = await _run_job_fixture(
                cli,
                "seed",
                project_id=wrong_project,
                task_id=ids["ocr_task"],
                user_email=RECORDER_EMAIL,
            )
            record_property(
                "identity_recording_job_bad_scope_rollback",
                bad_job_scope_after_seed.evidence,
            )
            _assert_failed(bad_job_scope_after_seed)
            assert await recording_snapshot(test_engine) == job_state

            repeated_job = await _run_job_fixture(
                cli,
                "seed",
                project_id=ids["ocr_project"],
                task_id=ids["ocr_task"],
                user_email=RECORDER_EMAIL,
            )
            record_property("identity_recording_job_repeat", repeated_job.evidence)
            repeated_job_result = _job_result(repeated_job)
            assert repeated_job_result["async_job_id"] != job_receipt["async_job_id"]
            assert await recording_snapshot(test_engine) == job_state

            job_cleanup = await _run_job_fixture(
                cli,
                "cleanup",
                project_id=ids["ocr_project"],
                task_id=ids["ocr_task"],
            )
            record_property("identity_recording_job_cleanup", job_cleanup.evidence)
            cleanup_result = _job_result(job_cleanup)
            assert cleanup_result["async_jobs"] == 1
            assert cleanup_result["failed_predictions"] == 1
            assert await recording_snapshot(test_engine) == base

            repeated_cleanup = await _run_job_fixture(
                cli,
                "cleanup",
                project_id=ids["ocr_project"],
                task_id=ids["ocr_task"],
            )
            record_property(
                "identity_recording_job_cleanup_repeat",
                repeated_cleanup.evidence,
            )
            assert _job_result(repeated_cleanup)["async_jobs"] == 0
            assert _job_result(repeated_cleanup)["failed_predictions"] == 0
            assert await recording_snapshot(test_engine) == base

            bad_review_scope = await _run_review_fixture(
                cli,
                "prepare",
                project_id=ids["image_project"],
                task_id=ids["ocr_task"],
                reviewer_email=RECORDER_EMAIL,
            )
            record_property(
                "identity_recording_review_bad_scope",
                bad_review_scope.evidence,
            )
            _assert_failed(bad_review_scope)
            assert await recording_snapshot(test_engine) == base

            missing_reviewer = await _run_review_fixture(
                cli,
                "prepare",
                project_id=ids["image_project"],
                task_id=ids["image_task"],
                reviewer_email="missing-recording-reviewer@fixture.test",
            )
            record_property(
                "identity_recording_review_missing_user",
                missing_reviewer.evidence,
            )
            _assert_failed(missing_reviewer)
            assert await recording_snapshot(test_engine) == base

            review_prepare = await _run_review_fixture(
                cli,
                "prepare",
                project_id=ids["image_project"],
                task_id=ids["image_task"],
                reviewer_email=RECORDER_EMAIL,
            )
            record_property(
                "identity_recording_review_prepare",
                review_prepare.evidence,
            )
            prepare_result = _review_result(review_prepare, "prepare")
            assert prepare_result["status"] == "review"
            assert prepare_result["feedbacks"] == 1
            assert prepare_result["notifications"] == 1
            review_state = await recording_snapshot(test_engine)
            _assert_review_state(review_state)
            assert review_state["users"] == base["users"]
            assert review_state["jobs"] == base["jobs"]
            assert review_state["failed"] == base["failed"]
            assert review_state["tasks"] != base["tasks"]

            repeated_prepare = await _run_review_fixture(
                cli,
                "prepare",
                project_id=ids["image_project"],
                task_id=ids["image_task"],
                reviewer_email=RECORDER_EMAIL,
            )
            record_property(
                "identity_recording_review_prepare_repeat",
                repeated_prepare.evidence,
            )
            repeated_prepare_result = _review_result(repeated_prepare, "prepare")
            assert repeated_prepare_result["feedbacks"] == 0
            assert repeated_prepare_result["notifications"] == 0
            assert await recording_snapshot(test_engine) == review_state

            review_cleanup = await _run_review_fixture(
                cli,
                "cleanup",
                project_id=ids["image_project"],
                task_id=ids["image_task"],
                reviewer_email=RECORDER_EMAIL,
            )
            record_property(
                "identity_recording_review_cleanup",
                review_cleanup.evidence,
            )
            cleanup_review_result = _review_result(review_cleanup, "cleanup")
            assert cleanup_review_result["feedbacks"] == 0
            assert cleanup_review_result["notifications"] == 0
            assert await recording_snapshot(test_engine) == review_state

            rich_preferences = {
                "fixture_unknown": {
                    "unicode": "记录员 🚙 雪",
                    "empty_array": [],
                    "empty_object": {},
                    "nested": {"false": False, "zero": 0, "null": None},
                },
                "unrecognized_top_level": {"kept_verbatim": ["新值", 7]},
            }
            _write_preferences(preferences_path, rich_preferences)
            preference_write = await _run_preferences_fixture(cli, ids["recorder"])
            record_property(
                "identity_recording_preferences_replace",
                preference_write.evidence,
            )
            assert not preference_write.timed_out and preference_write.exit_code == 0
            preference_state = await recording_snapshot(test_engine)
            _assert_preferences_only(review_state, preference_state, rich_preferences)

            repeated_preferences = await _run_preferences_fixture(cli, ids["recorder"])
            record_property(
                "identity_recording_preferences_replace_repeat",
                repeated_preferences.evidence,
            )
            assert (
                not repeated_preferences.timed_out
                and repeated_preferences.exit_code == 0
            )
            assert await recording_snapshot(test_engine) == preference_state

            _write_preferences(preferences_path, {})
            empty_preferences = await _run_preferences_fixture(cli, ids["recorder"])
            record_property(
                "identity_recording_preferences_empty_object",
                empty_preferences.evidence,
            )
            assert not empty_preferences.timed_out and empty_preferences.exit_code == 0
            empty_state = await recording_snapshot(test_engine)
            _assert_preferences_only(review_state, empty_state, {})

            _write_preferences(preferences_path, INITIAL_PREFERENCES)
            preferences_reset = await _run_preferences_fixture(cli, ids["recorder"])
            record_property(
                "identity_recording_preferences_reset",
                preferences_reset.evidence,
            )
            assert not preferences_reset.timed_out and preferences_reset.exit_code == 0
            assert await recording_snapshot(test_engine) == review_state

            missing_preferences_user = await _run_preferences_fixture(cli, uuid.uuid4())
            record_property(
                "identity_recording_preferences_missing_user",
                missing_preferences_user.evidence,
            )
            _assert_failed(missing_preferences_user)
            assert await recording_snapshot(test_engine) == review_state

            _write_preferences(preferences_path, rich_preferences)
            async with test_engine.begin() as db:
                await db.execute(
                    text(
                        f"REVOKE EXECUTE ON FUNCTION {_FIXTURE_PREFERENCE_FUNCTION} "
                        "FROM legacy_runtime"
                    )
                )
            try:
                await _assert_identity_profile(test_engine, preference_writer=False)
                denied_preferences = await _run_preferences_fixture(
                    cli, ids["recorder"]
                )
                record_property(
                    "identity_recording_preferences_capability_denied",
                    denied_preferences.evidence,
                )
                _assert_failed(denied_preferences)
                assert "permission denied for function replace_fixture_preferences" in (
                    denied_preferences.stderr.lower()
                ), denied_preferences.stderr
                assert await recording_snapshot(test_engine) == review_state
            finally:
                async with test_engine.begin() as db:
                    await db.execute(
                        text(
                            f"GRANT EXECUTE ON FUNCTION {_FIXTURE_PREFERENCE_FUNCTION} "
                            "TO legacy_runtime"
                        )
                    )

            await _assert_identity_profile(test_engine)
            recovered_preferences = await _run_preferences_fixture(cli, ids["recorder"])
            record_property(
                "identity_recording_preferences_capability_recovery",
                recovered_preferences.evidence,
            )
            assert not recovered_preferences.timed_out
            assert recovered_preferences.exit_code == 0
            recovered_state = await recording_snapshot(test_engine)
            _assert_preferences_only(review_state, recovered_state, rich_preferences)
            _write_preferences(preferences_path, INITIAL_PREFERENCES)
            recovered_reset = await _run_preferences_fixture(cli, ids["recorder"])
            assert not recovered_reset.timed_out and recovered_reset.exit_code == 0
            assert await recording_snapshot(test_engine) == review_state

            application_name = f"aap-recording-pref-{uuid.uuid4().hex[:24]}"
            gated_preferences = {
                "recording_cancel_gate": application_name,
                "unknown": {"unicode": "取消 🚲", "empty": []},
            }
            _write_preferences(preferences_path, gated_preferences)
            preference_gate = await _install_preferences_gate(
                test_engine,
                recorder_id=ids["recorder"],
                application_name=application_name,
            )
            pending_gate = asyncio.create_task(
                asyncio.to_thread(
                    cli.run_command,
                    (
                        "python",
                        "scripts/restore_screenshot_user_preferences.py",
                        "--user-id",
                        str(ids["recorder"]),
                        "--preferences-file",
                        "/fixture/preferences.json",
                    ),
                )
            )
            try:
                client_address, activity = await _wait_for_preferences_gate(
                    test_engine, cli, pending_gate, application_name
                )
                backend_pid = activity["pid"]
                assert activity["usename"] == "legacy_runtime"
                assert activity["application_name"] == application_name
                assert activity["client_host"] == client_address
                assert activity["native_call"] is True
                record_property(
                    "identity_recording_preferences_cancellation_gate",
                    {
                        "role": activity["usename"],
                        "application_name": activity["application_name"],
                        "client_host": activity["client_host"],
                        "backend_pid": backend_pid,
                        "wait_event_type": activity["wait_event_type"],
                        "wait_event": activity["wait_event"],
                        "native_call": activity["native_call"],
                    },
                )
                assert await asyncio.to_thread(cli.stop_current_container) is True
                stopped = await asyncio.wait_for(
                    asyncio.shield(pending_gate), timeout=35
                )
                record_property(
                    "identity_recording_preferences_physical_cancellation",
                    stopped.evidence,
                )
                assert stopped.exit_code in {137, 143}, stopped.evidence
                assert stopped.externally_stopped is True
                assert stopped.timed_out is False
                await _wait_for_backend_exit(test_engine, backend_pid)
                assert await recording_snapshot(test_engine) == review_state
            finally:
                await _finish_cli_task(pending_gate)
                if preference_gate is not None:
                    await _drop_preferences_gate(test_engine, preference_gate)
                    preference_gate = None

            recovered_gate_write = await _run_preferences_fixture(cli, ids["recorder"])
            record_property(
                "identity_recording_preferences_cancellation_recovery",
                recovered_gate_write.evidence,
            )
            assert not recovered_gate_write.timed_out
            assert recovered_gate_write.exit_code == 0
            gated_state = await recording_snapshot(test_engine)
            _assert_preferences_only(review_state, gated_state, gated_preferences)
            _write_preferences(preferences_path, INITIAL_PREFERENCES)
            final_reset = await _run_preferences_fixture(cli, ids["recorder"])
            assert not final_reset.timed_out and final_reset.exit_code == 0
            assert await recording_snapshot(test_engine) == review_state

            await _assert_identity_profile(test_engine)
            record_property(
                "identity_recording_scope",
                "Qualified the installed screenshot job/review recording helpers "
                "and fixture-only preference replacement through Identity's public "
                "profile and fixed Native write capability. No ML inference or "
                "complete recording deployment was exercised.",
            )
        finally:
            try:
                if pending_gate is not None:
                    await _finish_cli_task(pending_gate)
            finally:
                try:
                    if preference_gate is not None:
                        await _drop_preferences_gate(test_engine, preference_gate)
                finally:
                    if cli is not None:
                        await asyncio.to_thread(cli.close)
