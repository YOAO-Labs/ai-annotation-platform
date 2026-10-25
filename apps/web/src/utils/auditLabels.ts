export const AUDIT_ACTION_LABELS: Record<string, string> = {
  "auth.login": "登录",
  "user.invite": "邀请用户",
  "user.register": "完成注册",
  "user.role_change": "变更角色",
  "user.deactivate": "停用账号",
  "user.profile_update": "更新资料",
  "user.password_change": "修改密码",
  "project.create": "创建项目",
  "project.update": "更新项目",
  "project.transfer": "转移项目",
  "project.delete": "删除项目",
  "project.member_add": "添加成员",
  "project.member_remove": "移除成员",
  "dataset.create": "创建数据集",
  "dataset.delete": "删除数据集",
  "storage.fault": "存储访问故障",
  "dataset_source.protection_update": "更新来源保护责任",
  "annotation.update": "编辑标注",
  "annotation.comment": "评论标注",
  "system.bootstrap_admin": "引导管理员",
  "system.settings_update": "更新系统设置",
  "bug_report.created": "提交反馈",
  "bug_report.status_changed": "反馈状态更新",
  "bug_comment.created": "反馈评论",
  // B-5 · AI 相关审计
  "ai.preannotate.triggered": "触发 AI 预标注",
  "video_tracker_job.decision": "审核视频追踪候选",
  "video_mask.keyframe_correct": "保存视频 Mask 纠错帧",
  "video_mask.keyframe_operate": "操作视频 Mask 关键帧",
  "video_correction_job.create": "启动视频 Mask 纠错传播",
  "video_correction_job.cancel": "取消视频 Mask 纠错传播",
  "ml_backend.created": "注册 ML 后端",
  "ml_backend.updated": "更新 ML 后端",
  "ml_backend.deleted": "删除 ML 后端",
  "http.post": "HTTP·写",
  "http.patch": "HTTP·改",
  "http.put": "HTTP·改",
  "http.delete": "HTTP·删",
};

export function auditActionLabel(action: string): string {
  return AUDIT_ACTION_LABELS[action] ?? action;
}

export const AUDIT_BUSINESS_ACTIONS: string[] = Object.keys(AUDIT_ACTION_LABELS).filter(
  (k) => !k.startsWith("http."),
);

export const AUDIT_TARGET_TYPES = [
  "user",
  "project",
  "task",
  "dataset",
  "storage_location",
  "annotation",
  "system",
  "ml_backend",
  "bug_report",
  "video_tracker_job",
];
