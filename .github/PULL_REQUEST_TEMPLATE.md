## 变更说明

<!-- 一句话描述本 PR 的目的与做了什么 -->

## 关联

<!-- Issue / Bug ID / Plan 文件路径，例如：B-12、docs/plans/archive/2026-05-08-xxx.md -->

## 文档影响清单（CLAUDE.md §5）

- [ ] 不涉及文档（仅内部重构 / 测试 / CI）
- [ ] 新增/修改 API → 已更新 `apps/api/openapi.snapshot.json`（并在 `YOAO-Labs/YOAO-Docs` 更新 `api/guides/`）
- [ ] 新增/修改功能 → 已在 `YOAO-Labs/YOAO-Docs` 更新 `user-guide/{annotator|admin|superadmin}/`
- [ ] AI 能力 / 路由 / Job / 候选流变更 → 已检查用户任务、数据生命周期、API/协议、部署恢复、权限审计；不适用项与原因：
- [ ] 架构变更 → 已在 `YOAO-Labs/YOAO-Docs` 更新 `dev/concepts/`，必要时新增 `docs/adr/`
- [ ] 环境变量变更 → 已更新 `.env.example` 与 `DEV.md`
- [ ] 引入新踩坑 → 已在 `YOAO-Labs/YOAO-Docs` 新增 `dev/troubleshooting/*.md`
- [ ] 发版 → 已更新 `CHANGELOG.md`（含版本号与日期）

## 测试

- [ ] 单元 / 集成测试已通过
- [ ] 端到端验证步骤：
  1.
  2.

## 备注

<!-- Docker 是否需要 rebuild？Celery worker 是否需要 restart？数据库是否有 alembic migration？ -->
