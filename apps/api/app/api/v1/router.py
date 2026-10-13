from fastapi import APIRouter
from app.api.v1 import (
    admin_alias_freq,
    admin_analytics,
    admin_ml_integrations,
    admin_preannotate,
    admin_system_health,
    api_keys,
    async_jobs,
    auth,
    avatars,
    internal,
    audit_logs,
    annotation_comments,
    annotation_feedbacks,
    annotation_history,
    annotations,
    batches,
    bug_reports,
    dashboard,
    data_manager,
    datasets,
    files,
    groups,
    guide_assets,
    invitations,
    invitations_admin,
    me,
    ml_backends,
    ml_capabilities,
    mask_qc,
    point_cloud_quality,
    mask_formats,
    notifications,
    predictions,
    projects,
    project_performance,
    project_pipelines,
    project_templates,
    task_views,
    scenes,
    search,
    storage,
    storage_connections,
    system_settings,
    tasks,
    users,
    video_tracker_jobs,
    videos,
)

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(invitations.router, prefix="/auth", tags=["auth"])
api_router.include_router(
    invitations_admin.router, prefix="/invitations", tags=["invitations"]
)
api_router.include_router(me.router, prefix="/auth/me", tags=["me"])
api_router.include_router(api_keys.router, prefix="/me/api-keys", tags=["api-keys"])
# 头像图片读取。免鉴权(浏览器 <img> 不带 Authorization 头),详见 avatars.py 模块注释。
api_router.include_router(avatars.router, prefix="/avatars", tags=["avatars"])
api_router.include_router(
    admin_ml_integrations.router,
    prefix="/admin/ml-integrations",
    tags=["admin-ml-integrations"],
)
# v0.9.6 · /admin/preannotate-queue (注意: 端点路径已含 /admin/preannotate-queue, 这里 prefix 留空)
api_router.include_router(
    admin_preannotate.router,
    prefix="",
    tags=["admin-preannotate"],
)
# v0.9.7 · /admin/projects/:id/alias-frequency
api_router.include_router(
    admin_alias_freq.router,
    prefix="",
    tags=["admin-alias-freq"],
)
api_router.include_router(projects.router, prefix="/projects", tags=["projects"])
api_router.include_router(project_performance.router, tags=["project-performance"])
api_router.include_router(task_views.router, tags=["task-views"])
api_router.include_router(data_manager.router, tags=["data-manager"])
# v0.10.13 · E1 · 项目标注指引图片资源端点 (与 datasets items upload 独立, 不污染 dataset_items 表)
api_router.include_router(
    guide_assets.router, prefix="/projects", tags=["guide-assets"]
)
# v0.10.14 · E2 · 项目模板库
api_router.include_router(
    project_templates.router, prefix="/project-templates", tags=["project-templates"]
)
api_router.include_router(
    project_pipelines.router, prefix="/project-pipelines", tags=["project-pipelines"]
)
# /tasks 前缀已下放至 tasks 包内部聚合(见 app/api/v1/tasks/__init__.py);
# 此处仅施加 tag,避免与包内前缀叠加成 /tasks/tasks。
api_router.include_router(tasks.router, tags=["tasks"])
api_router.include_router(videos.router, prefix="/videos", tags=["videos"])
api_router.include_router(
    video_tracker_jobs.router,
    prefix="/video-tracker-jobs",
    tags=["video-tracker-jobs"],
)
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(groups.router, prefix="/groups", tags=["groups"])
api_router.include_router(dashboard.router, prefix="/dashboard", tags=["dashboard"])
api_router.include_router(
    ml_backends.router,
    prefix="/projects/{project_id}/ml-backends",
    tags=["ml-backends"],
)
# v0.14.11 · 协议级能力目录 (与 backend 注册解耦)
api_router.include_router(
    ml_capabilities.router,
    prefix="/ml-capabilities",
    tags=["ml-capabilities"],
)
api_router.include_router(files.router, prefix="/files", tags=["files"])
api_router.include_router(datasets.router, prefix="/datasets", tags=["datasets"])
api_router.include_router(scenes.router, prefix="/scenes", tags=["scenes"])
api_router.include_router(storage.router, prefix="/storage", tags=["storage"])
api_router.include_router(
    storage_connections.router,
    prefix="/storage-connections",
    tags=["storage-connections"],
)
api_router.include_router(audit_logs.router, prefix="/audit-logs", tags=["audit"])
api_router.include_router(system_settings.router, prefix="/settings", tags=["settings"])
api_router.include_router(
    batches.router, prefix="/projects/{project_id}/batches", tags=["batches"]
)
api_router.include_router(annotation_comments.router, tags=["annotation-comments"])
api_router.include_router(annotation_history.router, tags=["annotation-history"])
# I12 · Object Group 与批量编辑
api_router.include_router(annotations.router, tags=["annotations"])
# I18 · AnnotationFeedback 统一反馈表
api_router.include_router(annotation_feedbacks.router, tags=["annotation-feedbacks"])
api_router.include_router(search.router, prefix="/search", tags=["search"])
api_router.include_router(bug_reports.router, tags=["bug-reports"])
api_router.include_router(notifications.router, tags=["notifications"])
# v0.8.6 F6 · 失败预测管理 + 重试
api_router.include_router(predictions.router, tags=["predictions"])
# v0.10.16 · 统一异步任务表
api_router.include_router(async_jobs.router, tags=["async-jobs"])
api_router.include_router(mask_qc.router, tags=["mask-qc"])
api_router.include_router(point_cloud_quality.router, tags=["point-cloud-quality"])
api_router.include_router(mask_formats.router, tags=["mask-formats"])
# v0.10.16 · DuckDB 离线分析面板（super_admin only）
api_router.include_router(
    admin_analytics.router, prefix="/admin/analytics", tags=["admin-analytics"]
)
# v0.10.58 · super_admin 系统健康聚合面板
api_router.include_router(
    admin_system_health.router,
    prefix="/admin/system-health",
    tags=["admin-system-health"],
)

# v0.11.19 · 内部服务发现 router (/internal/metrics-targets, Prometheus http_sd)。
# include_in_schema=False, 不进 OpenAPI 公开 schema; 所有环境暴露 (监控用)。
api_router.include_router(internal.router, prefix="/internal", tags=["internal"])

# _test_seed router 仅在非 production 且显式开启 E2E_SEED_ENABLED 时挂载；
# router 内还有数据库名守卫，避免测试造数端点误连开发库。
from app.config import settings as _settings  # noqa: E402


def _e2e_seed_routes_enabled() -> bool:
    return _settings.environment != "production" and _settings.e2e_seed_enabled


if _e2e_seed_routes_enabled():
    from app.api.v1 import _test_seed  # noqa: E402

    api_router.include_router(_test_seed.router, prefix="/__test", tags=["_test_seed"])
