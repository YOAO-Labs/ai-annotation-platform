from __future__ import annotations

import json

from yoao_jobs.notification_scope_contract import NotificationScopeFact


def test_notification_scope_keeps_unknown_job_kinds_and_null_scope():
    fact = NotificationScopeFact.model_validate_json(
        json.dumps(
            {
                "id": "3af0d5e6-fb54-45e6-ae26-c38dbf54f0cc",
                "kind": "future_unrecognized_job_kind",
                "scope_project_id": None,
            }
        )
    )

    assert fact.kind == "future_unrecognized_job_kind"
    assert fact.scope_project_id is None
