"""Compatibility exports for the Workspace project deletion contract."""

from aap_workspace.project_deletion_contract import (
    PROJECT_DELETION_PROTOCOL,
    ProjectDeletionCommand,
    ProjectDeletionFailure,
    ProjectDeletionReceipt,
    decode_project_deletion_command,
    decode_project_deletion_reply,
    project_deletion_schema,
)

__all__ = [
    "PROJECT_DELETION_PROTOCOL",
    "ProjectDeletionCommand",
    "ProjectDeletionFailure",
    "ProjectDeletionReceipt",
    "decode_project_deletion_command",
    "decode_project_deletion_reply",
    "project_deletion_schema",
]
