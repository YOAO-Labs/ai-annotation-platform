from __future__ import annotations

from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.schemas.bug_report import BugReportCreate
from app.services.bug_report import BugReportService
from tests.factory import create_project, create_task


async def test_bug_reports_detach_only_named_source_less_tasks_in_project(
    db_session, super_admin
):
    reporter, _ = super_admin
    project = await create_project(db_session, owner_id=reporter.id)
    other_project = await create_project(db_session, owner_id=reporter.id)
    first, second, unlisted, linked = [
        await create_task(db_session, project_id=project.id) for _ in range(4)
    ]
    foreign = await create_task(db_session, project_id=other_project.id)

    dataset = Dataset(
        display_id=f"D-{linked.id.hex[:12]}",
        name="linked images",
        created_by=reporter.id,
    )
    db_session.add(dataset)
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name=linked.file_name,
        file_path=linked.file_path,
        file_type=linked.file_type,
    )
    db_session.add_all(
        [item, ProjectDataset(project_id=project.id, dataset_id=dataset.id)]
    )
    await db_session.flush()
    linked.dataset_item_id = item.id

    service = BugReportService(db_session)
    expected = {}
    for task, remaining_link in (
        (first, None),
        (second, None),
        (unlisted, unlisted.id),
        (linked, linked.id),
        (foreign, foreign.id),
    ):
        report = await service.create(
            reporter.id,
            BugReportCreate(
                project_id=task.project_id,
                task_id=task.id,
                title="Incorrect label",
                description="Check the annotation on this task.",
            ),
            route=f"/tasks/{task.id}",
        )
        assert report.task_id == task.id
        expected[report.id] = (task.project_id, remaining_link)

    await service.detach_task_references(
        project.id, [first.id, second.id, linked.id, foreign.id]
    )

    for report_id, expected_links in expected.items():
        saved = await service.get_with_comments(report_id, reporter.id)
        assert (saved.project_id, saved.task_id) == expected_links
