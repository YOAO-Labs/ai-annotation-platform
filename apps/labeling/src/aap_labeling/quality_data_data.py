"""Transaction callback for Mask QC and repair owner commands."""

from collections.abc import Awaitable, Callable

from .quality_data_contract import (
    QualityDataCommand,
    decode_quality_data_reply,
    quality_data_command_json,
)

_MASK_QC_OPERATIONS = {
    "read_source",
    "create_run",
    "read_run",
    "read_run_by_job",
    "find_run",
    "begin_run",
    "progress_run",
    "complete_run",
    "fail_run",
    "cancel_run",
    "reconcile_run",
    "upsert_issue",
    "mark_seen",
    "read_issue",
    "read_issues",
    "list_issues",
    "read_task_run_issues",
    "find_latest_task_run",
    "find_current_completed_task_run",
    "read_open_task_issues",
    "decide_issue",
    "resolve_repaired_issue",
    "read_issue_statuses",
    "issue_versions_current",
    "read_annotation_revision",
    "read_tracker_candidate_context",
    "read_run_summary",
}
_RETENTION_OPERATIONS = {"read_all_references", "check_reference"}


class QualityDataClient:
    def __init__(self, execute: Callable[[str, str], Awaitable[object]]) -> None:
        self._execute = execute

    async def command(self, request: QualityDataCommand) -> object:
        operation = request.operation
        function = (
            "raster_mask_retention_query"
            if operation in _RETENTION_OPERATIONS
            else "mask_qc_command"
            if operation in _MASK_QC_OPERATIONS
            else "mask_repair_command"
        )
        reply = await self._execute(function, quality_data_command_json(request))
        return decode_quality_data_reply(request, reply)
