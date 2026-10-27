"""Export-specific error policy over shared frozen-selection locks."""

from app.services.task_scope import ids_clause as ids_clause
from app.services.task_scope import lock_task_scope as _lock_task_scope


async def lock_task_scope(db, task_ids):
    return await _lock_task_scope(db, task_ids, error_code="export_scope_changed")
