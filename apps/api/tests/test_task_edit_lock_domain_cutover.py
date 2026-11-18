"""Pure guards for the task edit-lock advisory-domain cutover.

The installed PostgreSQL behavior is exercised by
``test_task_edit_lock_domain_live.py``.  These checks pin the forward revision,
the absence of a dual/legacy acquisition path, and the frozen history.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQL = ROOT / "alembic" / "sql"
VERSIONS = ROOT / "alembic" / "versions"
PACKAGED = Path(__file__).resolve().parents[2] / "labeling" / "src" / "yoao_labeling"

CUTOVER = (SQL / "0466_task_edit_lock_domain.sql").read_text(encoding="utf-8")
FROZEN = {
    "task_edit_lock_command": SQL / "0334_labeling_task_locks.sql",
    "lock_video_input_manifest_member": SQL / "0224_video_input_selection.sql",
    "lock_identity_offboarding_work": SQL / "0248_identity_offboarding_support.sql",
    "lock_membership_work": SQL / "0253_workspace_membership_labeling.sql",
}


def test_cutover_revision_follows_the_current_head_and_is_forward_only():
    migration = (VERSIONS / "0466_task_edit_lock_domain.py").read_text()
    assert 'revision = "bo0466"' in migration
    assert 'down_revision = "bo0465"' in migration
    assert "0466_task_edit_lock_domain.sql" in migration
    assert "NotImplementedError" in migration
    assert "Task edit-lock advisory domain cutover is forward-only" in migration


def test_all_four_entry_points_acquire_only_the_current_domain():
    assert not re.search(
        r"pg_(?:try_)?advisory_xact_lock(?:_shared)?\(\s*"
        r"(?:pg_catalog\.)?hashtextextended\(\s*'aap:task-edit-lock:",
        CUTOVER,
    )
    expected_current = {
        "task_edit_lock_command": 7,
        "lock_video_input_manifest_member": 1,
        "lock_identity_offboarding_work": 1,
        "lock_membership_work": 1,
    }
    expected_legacy = {
        "task_edit_lock_command": 6,
        "lock_video_input_manifest_member": 2,
        "lock_identity_offboarding_work": 2,
        "lock_membership_work": 2,
    }
    for function_name, count in expected_current.items():
        match = re.search(
            rf"CREATE OR REPLACE FUNCTION aap_labeling_v1\.{function_name}\(.*?"
            r"AS \$function\$(.*?)\$function\$;",
            CUTOVER,
            re.DOTALL,
        )
        assert match is not None, function_name
        body = match.group(1)
        assert body.count("'task-edit-lock:'") == count, function_name
        assert body.count("'aap:task-edit-lock:'") == expected_legacy[function_name]


def test_every_entry_point_checks_the_legacy_domain_before_locking():
    for function_name in FROZEN:
        match = re.search(
            rf"CREATE OR REPLACE FUNCTION aap_labeling_v1\.{function_name}\(.*?"
            r"AS \$function\$(.*?)\$function\$;",
            CUTOVER,
            re.DOTALL,
        )
        assert match is not None, function_name
        body = match.group(1)
        legacy_positions = [
            index
            for index, line in enumerate(body.splitlines())
            if "'aap:task-edit-lock:'" in line
        ]
        current_positions = [
            index
            for index, line in enumerate(body.splitlines())
            if "'task-edit-lock:'" in line
        ]
        assert legacy_positions, function_name
        assert current_positions, function_name
        assert min(legacy_positions) < max(current_positions), function_name


def test_cutover_guard_drains_writers_and_rejects_active_legacy_locks():
    assert "Drain all task edit-lock writers before advisory-domain cutover" in CUTOVER
    assert "An active task edit-lock advisory key remains" in CUTOVER
    assert "pg_catalog.pg_locks AS advisory_lock" in CUTOVER
    assert "pg_catalog.pg_stat_activity AS activity" in CUTOVER
    assert "has_function_privilege" in CUTOVER
    assert "ERRCODE = '55006'" in CUTOVER
    assert "v_expected_legacy integer[] := ARRAY[7, 1, 1, 1]" in CUTOVER
    assert "v_expected_legacy integer[] := ARRAY[6, 2, 2, 2]" in CUTOVER


def test_frozen_history_and_packaged_copy_are_preserved():
    for path in FROZEN.values():
        assert "'aap:task-edit-lock:'" in path.read_text(encoding="utf-8")
    assert (PACKAGED / "sql" / "0466_task_edit_lock_domain.sql").read_text(
        encoding="utf-8"
    ) == CUTOVER
    assert (PACKAGED / "sql" / "0334_labeling_task_locks.sql").read_text(
        encoding="utf-8"
    ) == FROZEN["task_edit_lock_command"].read_text(encoding="utf-8")


def test_python_consumers_use_the_shared_prefix_without_a_legacy_fallback():
    task_lock = (ROOT / "app" / "services" / "task_lock.py").read_text()
    active_learning = (
        ROOT / "app" / "services" / "labeling_active_learning_sources.py"
    ).read_text()
    assert "TASK_LOCK_SCOPE_PREFIX" in task_lock
    assert "aap:task-edit-lock:" not in task_lock
    assert "_lock_task_scope(task_id, nowait=True)" in active_learning
    assert "aap:task-edit-lock:" not in active_learning
