"""Keep the stable video-retirement rejection visible at both delete APIs."""

import pytest
from fastapi import HTTPException

from app.application.dataset_deletion import _restore_unlink_failure
from app.application.project_deletion import _restore_failure
from app.services.dataset_unlink_contract import DatasetUnlinkFailure
from app.services.project_deletion_contract import ProjectDeletionFailure

_DETAIL = {
    "code": "video_execution_not_retired",
    "message": "存在尚未退役的视频执行，请先取消并完成退役后重试",
}


def test_project_deletion_maps_unretired_video_execution_to_conflict():
    with pytest.raises(HTTPException) as raised:
        _restore_failure(ProjectDeletionFailure("video_execution_not_retired"))

    assert raised.value.status_code == 409
    assert raised.value.detail == _DETAIL


def test_dataset_unlink_maps_unretired_video_execution_to_conflict():
    with pytest.raises(HTTPException) as raised:
        _restore_unlink_failure(DatasetUnlinkFailure("video_execution_not_retired"))

    assert raised.value.status_code == 409
    assert raised.value.detail == _DETAIL
