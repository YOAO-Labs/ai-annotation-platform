"""同步 Client 与资源命名空间 (公开 API 面)。"""

from __future__ import annotations

import mimetypes
import time
from pathlib import Path
from typing import Any, Callable, Sequence
from uuid import UUID

from ai_annotation._http import HttpTransport
from ai_annotation.config import load_config
from ai_annotation.errors import AAPError, JobFailedError, JobTimeoutError
from ai_annotation.models import (
    Annotation,
    AnnotationBulkUpdateResult,
    AnnotationFeedback,
    AnnotationFeedbackCreate,
    ApiKey,
    ApiKeyCreated,
    Batch,
    BatchDistributeResult,
    BulkBatchActionResult,
    DashboardStats,
    Dataset,
    DatasetItem,
    DatasetUnlinkPreview,
    DatasetUnlinkResult,
    FeedbackAnchorPosition,
    FeedbackAnchorType,
    FeedbackKind,
    FeedbackPage,
    FeedbackSeverity,
    FeedbackStatus,
    ImportResult,
    Job,
    JobPage,
    JobRetryResult,
    LinkResult,
    Me,
    Member,
    MLBackend,
    MLBackendHealth,
    MLBackendUnloadResult,
    MyPerformance,
    Page,
    PersonStat,
    Project,
    ProjectAccess,
    ProjectMemberRolePreview,
    ProjectMLBackend,
    ProjectRole,
    ProjectServicePool,
    ProjectStats,
    ReviewClaim,
    CapabilityDrift,
    ServicePool,
    ServicePoolRuntimeSnapshot,
    ServicePoolTopology,
    Task,
    TaskActionResult,
    TaskPage,
    UploadedItem,
    ZipUploadResult,
)

IdLike = str | UUID

_JOB_TERMINAL = frozenset({"completed", "failed", "cancelled"})


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


class Projects:
    def __init__(self, http: HttpTransport):
        self._http = http

    def list(
        self, status: str | None = None, search: str | None = None
    ) -> list[Project]:
        resp = self._http.request(
            "GET", "/projects", params=_drop_none({"status": status, "search": search})
        )
        return [Project.model_validate(x) for x in resp.json()]

    def create(
        self,
        name: str,
        type_key: str | None = None,
        data_type: str | None = None,
        **kwargs: Any,
    ) -> Project:
        body: dict[str, Any] = {
            "name": name,
            "type_key": type_key,
            "data_type": data_type,
            **kwargs,
        }
        # 后端 ProjectCreate.type_label 必填; 未显式给出时按 type_key/data_type/name 兜底
        body.setdefault("type_label", type_key or data_type or name)
        resp = self._http.request("POST", "/projects", json=_drop_none(body))
        return Project.model_validate(resp.json())

    def get(self, project_id: IdLike) -> Project:
        resp = self._http.request("GET", f"/projects/{project_id}")
        return Project.model_validate(resp.json())

    def update(self, project_id: IdLike, **fields: Any) -> Project:
        resp = self._http.request("PATCH", f"/projects/{project_id}", json=fields)
        return Project.model_validate(resp.json())

    def delete(self, project_id: IdLike) -> None:
        self._http.request("DELETE", f"/projects/{project_id}")

    def stats(self) -> ProjectStats:
        """可见项目聚合统计 (含最近 12 周时间序列)。任意已认证用户可达。"""
        resp = self._http.request("GET", "/projects/stats")
        return ProjectStats.model_validate(resp.json())

    def access(self, project_id: IdLike) -> ProjectAccess:
        """当前账号在该项目上的解析权限 (平台/项目角色 + 固定能力集)。

        管理者返回 ``access_kind`` owner/super_admin 且 membership 字段可能为
        None; 普通成员给出 membership id/version 与自身能力。服务端权威。
        """
        resp = self._http.request("GET", f"/projects/{project_id}/access")
        return ProjectAccess.model_validate(resp.json())


class Datasets:
    def __init__(self, http: HttpTransport):
        self._http = http

    def list(
        self,
        search: str | None = None,
        data_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[Dataset]:
        resp = self._http.request(
            "GET",
            "/datasets",
            params=_drop_none(
                {
                    "search": search,
                    "data_type": data_type,
                    "limit": limit,
                    "offset": offset,
                }
            ),
        )
        return Page[Dataset].model_validate(resp.json())

    def create(self, name: str, data_type: str = "image", **kwargs: Any) -> Dataset:
        resp = self._http.request(
            "POST", "/datasets", json={"name": name, "data_type": data_type, **kwargs}
        )
        return Dataset.model_validate(resp.json())

    def get(self, dataset_id: IdLike) -> Dataset:
        resp = self._http.request("GET", f"/datasets/{dataset_id}")
        return Dataset.model_validate(resp.json())

    def update(self, dataset_id: IdLike, **fields: Any) -> Dataset:
        resp = self._http.request("PUT", f"/datasets/{dataset_id}", json=fields)
        return Dataset.model_validate(resp.json())

    def delete(self, dataset_id: IdLike) -> None:
        self._http.request("DELETE", f"/datasets/{dataset_id}")

    def list_items(
        self, dataset_id: IdLike, limit: int = 50, offset: int = 0
    ) -> Page[DatasetItem]:
        resp = self._http.request(
            "GET",
            f"/datasets/{dataset_id}/items",
            params={"limit": limit, "offset": offset},
        )
        return Page[DatasetItem].model_validate(resp.json())

    def delete_item(self, dataset_id: IdLike, item_id: IdLike) -> None:
        self._http.request("DELETE", f"/datasets/{dataset_id}/items/{item_id}")

    def list_projects(self, dataset_id: IdLike) -> list[Project]:
        resp = self._http.request("GET", f"/datasets/{dataset_id}/projects")
        return [Project.model_validate(x) for x in resp.json()]

    def upload_files(
        self,
        dataset_id: IdLike,
        paths: Sequence[str | Path],
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> list[UploadedItem]:
        """逐文件三步流: upload-init → PUT 预签名 URL → upload-complete。

        on_progress(done, total, file_name) 在每个文件完成后回调。
        """
        files = [Path(p) for p in paths]
        items: list[UploadedItem] = []
        for i, path in enumerate(files):
            content_type = (
                mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            )
            init = self._http.request(
                "POST",
                f"/datasets/{dataset_id}/items/upload-init",
                json={"file_name": path.name, "content_type": content_type},
            ).json()
            self._http.put_presigned(init["upload_url"], path, content_type)
            done = self._http.request(
                "POST",
                f"/datasets/{dataset_id}/items/upload-complete/{init['item_id']}",
            ).json()
            items.append(UploadedItem(file_name=path.name, **done))
            if on_progress is not None:
                on_progress(i + 1, len(files), path.name)
        return items

    def upload_zip(self, dataset_id: IdLike, zip_path: str | Path) -> ZipUploadResult:
        """multipart 上传单个 ZIP 包, 后端解压入库 (≤200MB / ≤5000 文件)。"""
        path = Path(zip_path)
        with path.open("rb") as f:
            resp = self._http.request(
                "POST",
                f"/datasets/{dataset_id}/items/upload-zip",
                files={"file": (path.name, f, "application/zip")},
            )
        return ZipUploadResult.model_validate(resp.json())

    def link_project(self, dataset_id: IdLike, project_id: IdLike) -> LinkResult:
        resp = self._http.request(
            "POST", f"/datasets/{dataset_id}/link", json={"project_id": str(project_id)}
        )
        return LinkResult.model_validate(resp.json())

    def preview_unlink(
        self, dataset_id: IdLike, project_id: IdLike
    ) -> DatasetUnlinkPreview:
        resp = self._http.request(
            "GET", f"/datasets/{dataset_id}/link/{project_id}/preview-unlink"
        )
        return DatasetUnlinkPreview.model_validate(resp.json())

    def unlink_project(
        self, dataset_id: IdLike, project_id: IdLike
    ) -> DatasetUnlinkResult:
        resp = self._http.request("DELETE", f"/datasets/{dataset_id}/link/{project_id}")
        return DatasetUnlinkResult.model_validate(resp.json())


class Tasks:
    def __init__(self, http: HttpTransport):
        self._http = http

    def list(
        self,
        project_id: IdLike,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        cursor: str | None = None,
    ) -> TaskPage:
        resp = self._http.request(
            "GET",
            "/tasks",
            params=_drop_none(
                {
                    "project_id": str(project_id),
                    "status": status,
                    "limit": limit,
                    "offset": offset,
                    "cursor": cursor,
                }
            ),
        )
        return TaskPage.model_validate(resp.json())

    def get(self, task_id: IdLike) -> Task:
        resp = self._http.request("GET", f"/tasks/{task_id}")
        return Task.model_validate(resp.json())

    def next(self, project_id: IdLike, batch_id: IdLike | None = None) -> Task | None:
        """领取下一个可标注 task; 无可领任务时返回 None。"""
        params = {"project_id": str(project_id)}
        if batch_id is not None:
            params["batch_id"] = str(batch_id)
        resp = self._http.request("GET", "/tasks/next", params=params)
        # 无可领任务时后端返回 null; 空 body 同样按 None 处理
        data = resp.json() if resp.content else None
        return Task.model_validate(data) if data is not None else None

    def submit(self, task_id: IdLike) -> TaskActionResult:
        resp = self._http.request("POST", f"/tasks/{task_id}/submit")
        return TaskActionResult.model_validate(resp.json())

    def skip(
        self, task_id: IdLike, reason: str, note: str | None = None
    ) -> TaskActionResult:
        resp = self._http.request(
            "POST",
            f"/tasks/{task_id}/skip",
            json=_drop_none({"reason": reason, "note": note}),
        )
        return TaskActionResult.model_validate(resp.json())

    def withdraw(self, task_id: IdLike) -> TaskActionResult:
        resp = self._http.request("POST", f"/tasks/{task_id}/withdraw")
        return TaskActionResult.model_validate(resp.json())

    def reopen(self, task_id: IdLike) -> TaskActionResult:
        resp = self._http.request("POST", f"/tasks/{task_id}/reopen")
        return TaskActionResult.model_validate(resp.json())

    def accept_rejection(self, task_id: IdLike) -> TaskActionResult:
        resp = self._http.request("POST", f"/tasks/{task_id}/accept-rejection")
        return TaskActionResult.model_validate(resp.json())

    def claim_review(self, task_id: IdLike) -> ReviewClaim:
        resp = self._http.request("POST", f"/tasks/{task_id}/review/claim")
        return ReviewClaim.model_validate(resp.json())

    def approve_review(self, task_id: IdLike, **fields: Any) -> TaskActionResult:
        resp = self._http.request(
            "POST", f"/tasks/{task_id}/review/approve", json=fields
        )
        return TaskActionResult.model_validate(resp.json())

    def reject_review(
        self, task_id: IdLike, reason_type: str, reason: str
    ) -> TaskActionResult:
        resp = self._http.request(
            "POST",
            f"/tasks/{task_id}/review/reject",
            json={"reason_type": reason_type, "reason": reason},
        )
        return TaskActionResult.model_validate(resp.json())


class Annotations:
    def __init__(self, http: HttpTransport):
        self._http = http

    def list(self, task_id: IdLike) -> list[Annotation]:
        resp = self._http.request("GET", f"/tasks/{task_id}/annotations")
        return [Annotation.model_validate(x) for x in resp.json()]

    def create(
        self,
        task_id: IdLike,
        annotation_type: str,
        geometry: dict[str, Any],
        class_name: str | None = None,
        **kwargs: Any,
    ) -> Annotation:
        body = _drop_none(
            {
                "annotation_type": annotation_type,
                "geometry": geometry,
                "class_name": class_name,
                **kwargs,
            }
        )
        resp = self._http.request("POST", f"/tasks/{task_id}/annotations", json=body)
        return Annotation.model_validate(resp.json())

    def update(
        self, task_id: IdLike, annotation_id: IdLike, **fields: Any
    ) -> Annotation:
        resp = self._http.request(
            "PATCH", f"/tasks/{task_id}/annotations/{annotation_id}", json=fields
        )
        return Annotation.model_validate(resp.json())

    def delete(self, task_id: IdLike, annotation_id: IdLike) -> None:
        self._http.request("DELETE", f"/tasks/{task_id}/annotations/{annotation_id}")

    def bulk_update(
        self, annotation_ids: Sequence[IdLike], **patch: Any
    ) -> AnnotationBulkUpdateResult:
        allowed = {"class_name", "attributes", "z_order", "is_locked", "is_hidden"}
        unknown = set(patch) - allowed
        if unknown:
            raise ValueError(f"unsupported bulk annotation fields: {sorted(unknown)}")
        if not patch:
            raise ValueError("bulk annotation patch must not be empty")
        resp = self._http.request(
            "POST",
            "/annotations/bulk-update",
            json={"ids": [str(value) for value in annotation_ids], "patch": patch},
        )
        return AnnotationBulkUpdateResult.model_validate(resp.json())


class Feedbacks:
    def __init__(self, http: HttpTransport):
        self._http = http

    def create(
        self,
        project_id: IdLike,
        body: str,
        *,
        kind: FeedbackKind = "issue",
        anchor_type: FeedbackAnchorType = "task",
        task_id: IdLike | None = None,
        annotation_id: IdLike | None = None,
        anchor_position: FeedbackAnchorPosition | dict[str, Any] | None = None,
        severity: FeedbackSeverity | None = None,
        title: str | None = None,
        attachments: Sequence[dict[str, Any]] = (),
        thread_parent_id: IdLike | None = None,
    ) -> AnnotationFeedback:
        """Create feedback; the server verifies task permission and video/object boundaries."""
        payload = AnnotationFeedbackCreate.model_validate(
            {
                "kind": kind,
                "anchor_type": anchor_type,
                "project_id": project_id,
                "task_id": task_id,
                "annotation_id": annotation_id,
                "anchor_position": anchor_position,
                "severity": severity,
                "title": title,
                "body": body,
                "attachments": list(attachments),
                "thread_parent_id": thread_parent_id,
            }
        ).model_dump(mode="json", by_alias=True, exclude_none=True)
        # A task-level feedback explicitly carries no pixel anchor.
        payload.setdefault("anchor_position", None)
        resp = self._http.request("POST", "/feedbacks", json=payload)
        return AnnotationFeedback.model_validate(resp.json())

    def list(
        self,
        project_id: IdLike,
        *,
        task_id: IdLike | None = None,
        annotation_id: IdLike | None = None,
        kind: FeedbackKind | None = None,
        anchor_type: FeedbackAnchorType | None = None,
        status: FeedbackStatus | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> FeedbackPage:
        """List visible feedback using items/next_cursor pagination (no total field)."""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer between 1 and 200")
        params = _drop_none(
            {
                "project_id": str(project_id),
                "task_id": str(task_id) if task_id is not None else None,
                "annotation_id": str(annotation_id)
                if annotation_id is not None
                else None,
                "kind": kind,
                "anchor_type": anchor_type,
                "status": status,
                "cursor": cursor,
                "limit": limit,
            }
        )
        resp = self._http.request("GET", "/feedbacks", params=params)
        return FeedbackPage.model_validate(resp.json())


class Predictions:
    def __init__(self, http: HttpTransport):
        self._http = http

    def import_file(
        self,
        project_id: IdLike,
        file_path: str | Path,
        format: str = "aap_json",
        yolo_variant: str | None = None,
        model_version: str | None = None,
        dry_run: bool = False,
        overwrite_existing: bool = False,
    ) -> ImportResult:
        """导入外部预测结果 (aap_json / coco / yolo)。

        注意: 后端 overwrite_existing 缺省为 True, SDK 显式发送、缺省 False (更保守)。
        """
        path = Path(file_path)
        params: dict[str, Any] = {"format": format, "dry_run": dry_run}
        if yolo_variant is not None:
            params["yolo_variant"] = yolo_variant
        data: dict[str, Any] = {
            "overwrite_existing": "true" if overwrite_existing else "false"
        }
        if model_version is not None:
            data["model_version"] = model_version
        with path.open("rb") as f:
            resp = self._http.request(
                "POST",
                f"/projects/{project_id}/predictions/import",
                params=params,
                data=data,
                files={"file": (path.name, f)},
            )
        return ImportResult.model_validate(resp.json())


class Jobs:
    def __init__(self, http: HttpTransport):
        self._http = http

    def list(
        self,
        status: str | Sequence[str] | None = None,
        kind: str | Sequence[str] | None = None,
        project_id: IdLike | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JobPage:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status is not None:
            params["status"] = [status] if isinstance(status, str) else list(status)
        if kind is not None:
            params["kind"] = [kind] if isinstance(kind, str) else list(kind)
        if project_id is not None:
            params["project_id"] = str(project_id)
        resp = self._http.request("GET", "/async-jobs", params=params)
        return JobPage.model_validate(resp.json())

    def get(self, job_id: IdLike) -> Job:
        resp = self._http.request("GET", f"/async-jobs/{job_id}")
        return Job.model_validate(resp.json())

    def cancel(self, job_id: IdLike) -> None:
        """请求软取消一个 job。

        仅对可取消 kind 且 status ∈ {pending, running} 有效; 否则后端返回 400/409。
        取消是协作式的 (worker 在下一条任务边界落 cancelled 终态), 返回不代表已终止,
        终态需后续 get/list 反映。
        """
        self._http.request("POST", f"/async-jobs/{job_id}/cancel")

    def retry_failed(self, job_id: IdLike) -> JobRetryResult:
        resp = self._http.request("POST", f"/async-jobs/{job_id}/retry-failed")
        return JobRetryResult.model_validate(resp.json())

    def wait(
        self,
        job_id: IdLike,
        timeout: float = 600.0,
        poll_interval: float = 2.0,
        on_progress: Callable[[Job], None] | None = None,
    ) -> Job:
        """轮询直到终态。failed/cancelled 抛 JobFailedError; 超时抛 JobTimeoutError。"""
        deadline = time.monotonic() + timeout
        while True:
            job = self.get(job_id)
            if on_progress is not None:
                on_progress(job)
            if job.status == "completed":
                return job
            if job.status in _JOB_TERMINAL:
                raise JobFailedError(job)
            if time.monotonic() >= deadline:
                raise JobTimeoutError(
                    f"async job {job_id} 等待超过 {timeout}s 仍未到终态"
                )
            time.sleep(poll_interval)


class Exports:
    def __init__(self, http: HttpTransport, jobs: Jobs):
        self._http = http
        self._jobs = jobs

    def create(
        self,
        project_id: IdLike,
        targets: list[str],
        include_attributes: bool | None = None,
        **kwargs: Any,
    ) -> str:
        """发起异步导出 (202), 返回 job_id; 参数走 query (与后端端点一致)。"""
        params: dict[str, Any] = {"targets": targets}
        if include_attributes is not None:
            params["include_attributes"] = include_attributes
        params.update(_drop_none(kwargs))
        resp = self._http.request(
            "POST", f"/projects/{project_id}/export", params=params
        )
        return resp.json()["job_id"]

    def wait(
        self,
        job_id: IdLike,
        timeout: float = 600.0,
        poll_interval: float = 2.0,
        on_progress: Callable[[Job], None] | None = None,
    ) -> Job:
        return self._jobs.wait(
            job_id,
            timeout=timeout,
            poll_interval=poll_interval,
            on_progress=on_progress,
        )

    def download(self, job_or_id: Job | IdLike, dest_path: str | Path) -> Path:
        """从 job.result["download_url"] 流式下载导出包到 dest_path。"""
        job = job_or_id if isinstance(job_or_id, Job) else self._jobs.get(job_or_id)
        url = (job.result or {}).get("download_url")
        if not url:
            raise AAPError(
                f"async job {job.id} (status={job.status}) 无 result.download_url, 无法下载"
            )
        return self._http.stream_download(url, Path(dest_path))


class MLBackends:
    """ML Backend 只读监控 (健康状态 + GPU/cache 指标)。

    全局注册表 (ADR-0044) 下, `list` 只返回**本项目已启用**的全局
    backend (非「项目挂载的全部」); `MLBackend.id` 是全局 registry id, 同一物理
    backend 在不同项目里返回同一 id。注册表管理由 `Client.ml_registry` 暴露，
    项目启用管理由本命名空间的 enablement 方法暴露。
    """

    def __init__(self, http: HttpTransport):
        self._http = http

    def list(self, project_id: IdLike) -> list[MLBackend]:
        resp = self._http.request("GET", f"/projects/{project_id}/ml-backends")
        return [MLBackend.model_validate(x) for x in resp.json()]

    def get(self, project_id: IdLike, backend_id: IdLike) -> MLBackend:
        resp = self._http.request(
            "GET", f"/projects/{project_id}/ml-backends/{backend_id}"
        )
        return MLBackend.model_validate(resp.json())

    def list_available(self, project_id: IdLike) -> list[ProjectMLBackend]:
        resp = self._http.request(
            "GET", f"/projects/{project_id}/ml-backends/available"
        )
        return [ProjectMLBackend.model_validate(x) for x in resp.json()["items"]]

    def set_enablement(
        self, project_id: IdLike, backend_id: IdLike, enabled: bool
    ) -> ProjectMLBackend:
        resp = self._http.request(
            "PUT",
            f"/projects/{project_id}/ml-backends/{backend_id}/enablement",
            json={"enabled": enabled},
        )
        return ProjectMLBackend.model_validate(resp.json())

    def check_health(self, project_id: IdLike, backend_id: IdLike) -> MLBackendHealth:
        resp = self._http.request(
            "POST", f"/projects/{project_id}/ml-backends/{backend_id}/health"
        )
        return MLBackendHealth.model_validate(resp.json())

    def list_available_pools(self, project_id: IdLike) -> list[ProjectServicePool]:
        resp = self._http.request(
            "GET", f"/projects/{project_id}/ml-backends/pools/available"
        )
        return [ProjectServicePool.model_validate(x) for x in resp.json()["items"]]

    def set_pool_enablement(
        self, project_id: IdLike, pool_id: IdLike, enabled: bool
    ) -> ProjectServicePool:
        resp = self._http.request(
            "PUT",
            f"/projects/{project_id}/ml-backends/pools/{pool_id}/enablement",
            json={"enabled": enabled},
        )
        return ProjectServicePool.model_validate(resp.json())


class MLRegistry:
    """全局物理 backend registry（super-admin）。"""

    def __init__(self, http: HttpTransport):
        self._http = http

    def list(self) -> list[MLBackend]:
        resp = self._http.request("GET", "/admin/ml-integrations/all")
        return [MLBackend.model_validate(x) for x in resp.json()["items"]]

    def create(self, **fields: Any) -> MLBackend:
        resp = self._http.request(
            "POST", "/admin/ml-integrations/registry", json=fields
        )
        return MLBackend.model_validate(resp.json())

    def update(self, registry_id: IdLike, **fields: Any) -> MLBackend:
        resp = self._http.request(
            "PUT", f"/admin/ml-integrations/registry/{registry_id}", json=fields
        )
        return MLBackend.model_validate(resp.json())

    def delete(self, registry_id: IdLike) -> None:
        self._http.request("DELETE", f"/admin/ml-integrations/registry/{registry_id}")

    def check_health(self, registry_id: IdLike) -> MLBackendHealth:
        resp = self._http.request(
            "POST", f"/admin/ml-integrations/registry/{registry_id}/health"
        )
        return MLBackendHealth.model_validate(resp.json())

    def unload(self, registry_id: IdLike) -> MLBackendUnloadResult:
        resp = self._http.request(
            "POST", f"/admin/ml-integrations/registry/{registry_id}/unload"
        )
        return MLBackendUnloadResult.model_validate(resp.json())


class ServicePools:
    """逻辑服务池与成员路由管理（super-admin）。"""

    def __init__(self, http: HttpTransport):
        self._http = http

    def list(self) -> list[ServicePool]:
        resp = self._http.request("GET", "/admin/ml-integrations/service-pools")
        return [ServicePool.model_validate(x) for x in resp.json()]

    def get(self, pool_id: IdLike) -> ServicePool:
        resp = self._http.request(
            "GET", f"/admin/ml-integrations/service-pools/{pool_id}"
        )
        return ServicePool.model_validate(resp.json())

    def create(
        self, name: str, legacy_instance_id: IdLike | None = None
    ) -> ServicePool:
        body = {"name": name}
        if legacy_instance_id is not None:
            body["legacy_instance_id"] = str(legacy_instance_id)
        resp = self._http.request(
            "POST", "/admin/ml-integrations/service-pools", json=body
        )
        return ServicePool.model_validate(resp.json())

    def update(self, pool_id: IdLike, **fields: Any) -> ServicePool:
        resp = self._http.request(
            "PATCH", f"/admin/ml-integrations/service-pools/{pool_id}", json=fields
        )
        return ServicePool.model_validate(resp.json())

    def delete(self, pool_id: IdLike) -> None:
        self._http.request("DELETE", f"/admin/ml-integrations/service-pools/{pool_id}")

    def add_member(
        self, pool_id: IdLike, registry_id: IdLike, weight: int = 1
    ) -> ServicePool:
        resp = self._http.request(
            "PUT",
            f"/admin/ml-integrations/service-pools/{pool_id}/members/{registry_id}",
            json={"weight": weight},
        )
        return ServicePool.model_validate(resp.json())

    def remove_member(self, pool_id: IdLike, registry_id: IdLike) -> ServicePool:
        resp = self._http.request(
            "DELETE",
            f"/admin/ml-integrations/service-pools/{pool_id}/members/{registry_id}",
        )
        return ServicePool.model_validate(resp.json())

    def drain_member(self, pool_id: IdLike, registry_id: IdLike) -> ServicePool:
        resp = self._http.request(
            "POST",
            f"/admin/ml-integrations/service-pools/{pool_id}/members/{registry_id}/drain",
        )
        return ServicePool.model_validate(resp.json())

    def resume_member(self, pool_id: IdLike, registry_id: IdLike) -> ServicePool:
        resp = self._http.request(
            "POST",
            f"/admin/ml-integrations/service-pools/{pool_id}/members/{registry_id}/resume",
        )
        return ServicePool.model_validate(resp.json())

    def preview_capability_drift(
        self, pool_id: IdLike, registry_id: IdLike
    ) -> CapabilityDrift:
        resp = self._http.request(
            "GET",
            f"/admin/ml-integrations/service-pools/{pool_id}/members/{registry_id}/capability-drift",
        )
        return CapabilityDrift.model_validate(resp.json())

    def accept_capability_drift(
        self,
        pool_id: IdLike,
        registry_id: IdLike,
        expected_fingerprint: str,
        enable_pool: bool = False,
    ) -> ServicePool:
        resp = self._http.request(
            "POST",
            f"/admin/ml-integrations/service-pools/{pool_id}/members/{registry_id}/capability-drift/accept",
            json={
                "expected_candidate_fingerprint": expected_fingerprint,
                "enable_pool": enable_pool,
            },
        )
        return ServicePool.model_validate(resp.json())

    def topology(self) -> ServicePoolTopology:
        resp = self._http.request("GET", "/admin/ml-integrations/topology")
        return ServicePoolTopology.model_validate(resp.json())

    def runtime_snapshot(self) -> ServicePoolRuntimeSnapshot:
        resp = self._http.request("GET", "/admin/ml-integrations/runtime-snapshot")
        return ServicePoolRuntimeSnapshot.model_validate(resp.json())


class Batches:
    """项目批次管理。"""

    def __init__(self, http: HttpTransport):
        self._http = http

    def list(self, project_id: IdLike, status: str | None = None) -> list[Batch]:
        resp = self._http.request(
            "GET",
            f"/projects/{project_id}/batches",
            params=_drop_none({"status": status}),
        )
        return [Batch.model_validate(x) for x in resp.json()]

    def get(self, project_id: IdLike, batch_id: IdLike) -> Batch:
        resp = self._http.request("GET", f"/projects/{project_id}/batches/{batch_id}")
        return Batch.model_validate(resp.json())

    def create(self, project_id: IdLike, name: str, **fields: Any) -> Batch:
        resp = self._http.request(
            "POST", f"/projects/{project_id}/batches", json={"name": name, **fields}
        )
        return Batch.model_validate(resp.json())

    def update(self, project_id: IdLike, batch_id: IdLike, **fields: Any) -> Batch:
        resp = self._http.request(
            "PATCH", f"/projects/{project_id}/batches/{batch_id}", json=fields
        )
        return Batch.model_validate(resp.json())

    def delete(self, project_id: IdLike, batch_id: IdLike, force: bool = False) -> None:
        self._http.request(
            "DELETE",
            f"/projects/{project_id}/batches/{batch_id}",
            params={"force": force},
        )

    def transition(
        self,
        project_id: IdLike,
        batch_id: IdLike,
        target_status: str,
        reason: str | None = None,
    ) -> Batch:
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/batches/{batch_id}/transition",
            json=_drop_none({"target_status": target_status, "reason": reason}),
        )
        return Batch.model_validate(resp.json())

    def reject(self, project_id: IdLike, batch_id: IdLike, feedback: str) -> Batch:
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/batches/{batch_id}/reject",
            json={"feedback": feedback},
        )
        return Batch.model_validate(resp.json())

    def reset(self, project_id: IdLike, batch_id: IdLike, reason: str) -> Batch:
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/batches/{batch_id}/reset",
            json={"reason": reason},
        )
        return Batch.model_validate(resp.json())

    def distribute(self, project_id: IdLike, **options: Any) -> BatchDistributeResult:
        body = dict(options)
        for key in ("annotator_ids", "reviewer_ids"):
            if key in body:
                body[key] = [str(value) for value in body[key]]
        resp = self._http.request(
            "POST", f"/projects/{project_id}/batches/distribute-batches", json=body
        )
        return BatchDistributeResult.model_validate(resp.json())

    def bulk_activate(
        self, project_id: IdLike, batch_ids: Sequence[IdLike]
    ) -> BulkBatchActionResult:
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/batches/bulk-activate",
            json={"batch_ids": [str(value) for value in batch_ids]},
        )
        return BulkBatchActionResult.model_validate(resp.json())

    def bulk_approve(
        self, project_id: IdLike, batch_ids: Sequence[IdLike]
    ) -> BulkBatchActionResult:
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/batches/bulk-approve",
            json={"batch_ids": [str(value) for value in batch_ids]},
        )
        return BulkBatchActionResult.model_validate(resp.json())

    def bulk_reject(
        self, project_id: IdLike, batch_ids: Sequence[IdLike], feedback: str
    ) -> BulkBatchActionResult:
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/batches/bulk-reject",
            json={
                "batch_ids": [str(value) for value in batch_ids],
                "feedback": feedback,
            },
        )
        return BulkBatchActionResult.model_validate(resp.json())

    def bulk_reassign(
        self, project_id: IdLike, batch_ids: Sequence[IdLike], **assignment: Any
    ) -> BulkBatchActionResult:
        unknown = set(assignment) - {"annotator_id", "reviewer_id"}
        if unknown:
            raise ValueError(f"unsupported batch assignment fields: {sorted(unknown)}")
        if not assignment:
            raise ValueError("at least one assignment field is required")
        body = {
            key: str(value) if value is not None else None
            for key, value in assignment.items()
        }
        body["batch_ids"] = [str(value) for value in batch_ids]
        resp = self._http.request(
            "POST", f"/projects/{project_id}/batches/bulk-reassign", json=body
        )
        return BulkBatchActionResult.model_validate(resp.json())

    def export(
        self,
        project_id: IdLike,
        batch_id: IdLike,
        targets: Sequence[str] | None = None,
        **options: Any,
    ) -> str:
        selected_targets = list(targets or ["coco"])
        if "voc" in selected_targets:
            raise ValueError(
                "VOC batch export returns ZIP bytes synchronously and is not supported by this job helper"
            )
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/batches/{batch_id}/export",
            params={"targets": selected_targets, **options},
        )
        return resp.json()["job_id"]


class Members:
    """项目成员管理。"""

    def __init__(self, http: HttpTransport):
        self._http = http

    def list(self, project_id: IdLike) -> list[Member]:
        resp = self._http.request("GET", f"/projects/{project_id}/members")
        return [Member.model_validate(x) for x in resp.json()]

    def add(self, project_id: IdLike, user_id: IdLike, role: ProjectRole) -> Member:
        """添加项目成员。

        ``role`` 是项目角色 (annotator / reviewer / viewer); 创建会校验账号
        平台角色与项目角色的兼容性 (viewer 账号只能获得 viewer 成员身份)。
        """
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/members",
            json={"user_id": str(user_id), "role": role},
        )
        return Member.model_validate(resp.json())

    def preview_role_change(
        self,
        project_id: IdLike,
        member_id: IdLike,
        project_role: ProjectRole,
        *,
        replacement_annotator_id: IdLike | None = None,
        replacement_reviewer_id: IdLike | None = None,
    ) -> ProjectMemberRolePreview:
        """角色变更预检 (只读): 阻塞项、受影响资源快照与一次性 token。

        写入必须使用返回的 ``preview_token`` 和 ``current_version``; 预检后
        新产生的作业 / 角色 / 归属变化会在写入时以 409 拒绝并要求重新预检。
        """
        body = _drop_none(
            {
                "project_role": project_role,
                "replacement_annotator_id": (
                    str(replacement_annotator_id)
                    if replacement_annotator_id is not None
                    else None
                ),
                "replacement_reviewer_id": (
                    str(replacement_reviewer_id)
                    if replacement_reviewer_id is not None
                    else None
                ),
            }
        )
        resp = self._http.request(
            "POST",
            f"/projects/{project_id}/members/{member_id}/role/preview",
            json=body,
        )
        return ProjectMemberRolePreview.model_validate(resp.json())

    def change_role(
        self,
        project_id: IdLike,
        member_id: IdLike,
        project_role: ProjectRole,
        *,
        expected_version: int,
        preview_token: str,
        reason: str,
        replacement_annotator_id: IdLike | None = None,
        replacement_reviewer_id: IdLike | None = None,
    ) -> Member:
        """按预检结果变更成员项目角色 (CAS + 原子交接)。

        必须带上预检返回的 ``preview_token`` 与 ``expected_version``; 需要交接
        未完成工作时给出显式接替人。版本陈旧 / 快照失效 / 资源占用返回 409。
        """
        body: dict[str, Any] = {
            "project_role": project_role,
            "expected_version": expected_version,
            "preview_token": preview_token,
            "reason": reason,
        }
        if replacement_annotator_id is not None:
            body["replacement_annotator_id"] = str(replacement_annotator_id)
        if replacement_reviewer_id is not None:
            body["replacement_reviewer_id"] = str(replacement_reviewer_id)
        resp = self._http.request(
            "PATCH",
            f"/projects/{project_id}/members/{member_id}/role",
            json=body,
        )
        return Member.model_validate(resp.json())

    def remove(self, project_id: IdLike, member_id: IdLike) -> None:
        self._http.request("DELETE", f"/projects/{project_id}/members/{member_id}")


class Dashboard:
    """看板 / 绩效只读查询。多数端点有角色门控 (admin/people 限 super_admin/project_admin)。"""

    def __init__(self, http: HttpTransport):
        self._http = http

    def admin(self) -> DashboardStats:
        """全局管理仪表盘 (super_admin)。字段经 extra 透传。"""
        resp = self._http.request("GET", "/dashboard/admin")
        return DashboardStats.model_validate(resp.json())

    def reviewer(self) -> DashboardStats:
        """审核员仪表盘 (super_admin / project_admin, 或任一项目 reviewer 的 employee)。

        员工按其在各项目的 reviewer 成员身份过滤工作负载; 没有该身份的
        项目返回空工作集, 不再依赖账号全局角色。
        """
        resp = self._http.request("GET", "/dashboard/reviewer")
        return DashboardStats.model_validate(resp.json())

    def annotator(self) -> DashboardStats:
        """标注员仪表盘 (super_admin / project_admin, 或任一项目 annotator 的 employee)。

        员工按其在各项目的 annotator 成员身份过滤工作负载; 没有该身份的
        项目返回空工作集, 不再依赖账号全局角色。
        """
        resp = self._http.request("GET", "/dashboard/annotator")
        return DashboardStats.model_validate(resp.json())

    def people(
        self,
        role: str | None = None,
        project: IdLike | None = None,
        period: str | None = None,
        sort: str | None = None,
        q: str | None = None,
    ) -> list[PersonStat]:
        """全员绩效卡片 (super_admin / project_admin; project_admin 须传 project)。"""
        params = _drop_none(
            {
                "role": role,
                "project": str(project) if project is not None else None,
                "period": period,
                "sort": sort,
                "q": q,
            }
        )
        resp = self._http.request("GET", "/dashboard/admin/people", params=params)
        data = resp.json()
        items = data.get("items", []) if isinstance(data, dict) else data
        return [PersonStat.model_validate(x) for x in items]

    def me_performance(self, period: str | None = None) -> MyPerformance:
        """当前用户自助绩效 (任意已认证, 强制 self)。"""
        resp = self._http.request(
            "GET", "/dashboard/me/performance", params=_drop_none({"period": period})
        )
        return MyPerformance.model_validate(resp.json())


class ApiKeys:
    def __init__(self, http: HttpTransport):
        self._http = http

    def list(self) -> list[ApiKey]:
        resp = self._http.request("GET", "/me/api-keys")
        return [ApiKey.model_validate(x) for x in resp.json()]

    def create(
        self,
        name: str,
        scopes: Sequence[str] | None = None,
        expires_in_days: int | None = None,
    ) -> ApiKeyCreated:
        body: dict[str, Any] = {"name": name, "scopes": list(scopes or [])}
        if expires_in_days is not None:
            body["expires_in_days"] = expires_in_days
        resp = self._http.request("POST", "/me/api-keys", json=body)
        return ApiKeyCreated.model_validate(resp.json())

    def update(
        self,
        key_id: IdLike,
        *,
        name: str | None = None,
        scopes: Sequence[str] | None = None,
        expires_in_days: int | None = None,
    ) -> ApiKey:
        """部分更新 name / scopes / 有效期。仅传入的字段生效。

        expires_in_days 传 None 时不发送该字段（不改有效期）。如需改回永不过期，
        请直接走 REST PATCH 显式传 null（SDK 当前不暴露该语义，保持调用简单）。
        """
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if scopes is not None:
            body["scopes"] = list(scopes)
        if expires_in_days is not None:
            body["expires_in_days"] = expires_in_days
        resp = self._http.request("PATCH", f"/me/api-keys/{key_id}", json=body)
        return ApiKey.model_validate(resp.json())

    def rotate(self, key_id: IdLike) -> ApiKeyCreated:
        """轮换：返回新一次性明文，旧明文立即失效。"""
        resp = self._http.request("POST", f"/me/api-keys/{key_id}/rotate")
        return ApiKeyCreated.model_validate(resp.json())

    def revoke(self, key_id: IdLike) -> None:
        self._http.request("DELETE", f"/me/api-keys/{key_id}")


class Client:
    """AI 标注平台同步客户端。

    认证: Authorization: Bearer <api_key> (ak_ 开头的 API key 或 JWT, SDK 不区分)。
    base_url / api_key 缺省时依次回落环境变量 AAP_BASE_URL / AAP_API_KEY
    与 ~/.config/ai-annotation/config.toml (见 ai_annotation.config)。
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
    ):
        base_url, api_key = load_config(base_url=base_url, api_key=api_key)
        if not base_url:
            raise AAPError(
                "缺少 base_url: 请传参或设置 AAP_BASE_URL / ~/.config/ai-annotation/config.toml"
            )
        self._http = HttpTransport(base_url, api_key, timeout=timeout)
        self.projects = Projects(self._http)
        self.datasets = Datasets(self._http)
        self.tasks = Tasks(self._http)
        self.annotations = Annotations(self._http)
        self.feedbacks = Feedbacks(self._http)
        self.predictions = Predictions(self._http)
        self.jobs = Jobs(self._http)
        self.exports = Exports(self._http, self.jobs)
        self.ml_backends = MLBackends(self._http)
        self.ml_registry = MLRegistry(self._http)
        self.service_pools = ServicePools(self._http)
        self.batches = Batches(self._http)
        self.members = Members(self._http)
        self.dashboard = Dashboard(self._http)
        self.api_keys = ApiKeys(self._http)

    def me(self) -> Me:
        """当前认证主体 (GET /auth/me)。用于自检凭据 / 角色感知。"""
        resp = self._http.request("GET", "/auth/me")
        return Me.model_validate(resp.json())

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
