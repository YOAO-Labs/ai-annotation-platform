"""Errors returned by managed storage purpose owners."""

from __future__ import annotations

AVATAR_MESSAGES = {
    "avatar_storage_unpinned": "头像尚未保存存储归属，请重新上传",
    "avatar_storage_invalid_key": "头像对象键与上传记录不匹配",
    "avatar_storage_invalid_receipt": "头像内容回执不完整或不合法",
    "avatar_storage_binding_unproven": "头像存储绑定尚未登记或无法核实",
}


class AvatarStorageError(ValueError):
    def __init__(self, code, message=None, *, status_code=409):
        super().__init__(
            message
            if message is not None
            else AVATAR_MESSAGES.get(code, "头像存储身份与当前端点不匹配")
        )
        self.code = code
        self.status_code = status_code


BUG_ATTACHMENT_MESSAGES = {
    "bug_attachment_storage_identity_changed": "附件存储身份与当前端点不匹配",
    "bug_attachment_storage_unpinned": "附件尚未保存存储归属，请重新上传",
    "bug_attachment_storage_invalid_key": "附件对象键与上传记录不匹配",
    "bug_attachment_storage_invalid_receipt": "附件内容回执或保存期限不合法",
    "bug_attachment_storage_invalid_file_name": "附件文件名不合法",
    "bug_attachment_storage_invalid_image": "附件必须是完整的 PNG、JPEG 或 WebP 图片，最多 10 MiB 和 4000 万像素",
    "bug_attachment_storage_binding_unproven": "附件存储绑定尚未登记或无法核实",
    "bug_attachment_storage_integrity_mismatch": "附件字节与独立内容回执不一致",
    "bug_attachment_storage_missing": "已保存的附件对象不存在",
    "bug_attachment_storage_budget": "附件核验超过读取预算",
    "bug_attachment_storage_unavailable": "附件存储暂不可用",
}


class BugAttachmentStorageError(ValueError):
    def __init__(self, code, *, status_code=409):
        super().__init__(BUG_ATTACHMENT_MESSAGES.get(code, "附件存储暂不可用"))
        self.code = code
        self.status_code = status_code


AUDIT_ARCHIVE_MESSAGES = {
    "audit_archive_storage_identity_changed": "归档存储身份与已保存的归属不匹配",
    "audit_archive_storage_unpinned": "归档尚未保存独立存储归属",
    "audit_archive_storage_invalid_key": "归档对象键与归档记录不匹配",
    "audit_archive_storage_invalid_receipt": "归档身份或内容回执不合法",
    "audit_archive_storage_binding_unproven": "归档存储绑定尚未登记或无法核实",
    "audit_archive_storage_integrity_mismatch": "归档字节与独立内容回执不一致",
    "audit_archive_storage_missing": "已保存的归档对象不存在",
    "audit_archive_storage_budget": "归档核验超过读取预算",
    "audit_archive_storage_unavailable": "归档存储暂不可用",
}


class AuditArchiveStorageError(ValueError):
    def __init__(self, code, *, status_code=409):
        super().__init__(AUDIT_ARCHIVE_MESSAGES.get(code, "归档存储暂不可用"))
        self.code = code
        self.status_code = status_code


EXPORT_MESSAGES = {
    "export_storage_unpinned": "导出候选尚未保存存储归属",
    "export_storage_invalid_key": "导出对象键与候选记录不匹配",
    "export_storage_invalid_receipt": "导出内容回执或保存期限不合法",
    "export_storage_binding_unproven": "导出存储绑定尚未登记或无法核实",
    "export_storage_integrity_mismatch": "导出字节与独立内容回执不一致",
    "export_storage_missing": "已保存的导出对象不存在",
    "export_storage_expired": "导出产物已超过固定保存期限",
    "export_storage_budget": "导出核验超过读取预算",
    "export_storage_unavailable": "导出存储暂不可用",
}


class ExportStorageError(ValueError):
    def __init__(self, code, message=None, *, status_code=409):
        super().__init__(
            message
            if message is not None
            else EXPORT_MESSAGES.get(code, "导出存储身份与当前端点不匹配")
        )
        self.code = code
        self.status_code = status_code


class ImportStorageError(ValueError):
    def __init__(self, code, message=None, *, status_code=409):
        super().__init__(message or "导入存储归属或目标能力无法核实")
        self.code, self.status_code = code, status_code


class MediaCacheStorageError(ValueError):
    def __init__(self, code="media_cache_storage_identity_changed", status_code=409):
        super().__init__("媒体缓存存储归属或目标能力无法核实")
        self.code, self.status_code = code, status_code


class AnnotationsStorageError(ValueError):
    def __init__(self, code, message=None, *, status_code=409):
        super().__init__(message or "标注文件存储归属或目标能力无法核实")
        self.code, self.status_code = code, status_code


class DatasetStorageError(ValueError):
    def __init__(self, code, message=None, *, status_code=409):
        super().__init__(message or "数据集原件存储归属或目标能力无法核实")
        self.code, self.status_code = code, status_code


class TrainingDatasetError(ValueError):
    def __init__(self, code: str, message: str, *, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
