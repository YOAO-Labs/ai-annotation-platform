---
title: 新增数据库迁移
audience: [dev]
type: how-to
status: stable
last_reviewed: 2026-11-13
---

# How-to：Alembic 迁移

## 生成

```bash
cd apps/api
# 1. 改 SQLAlchemy 模型 (app/db/models/...)
# 2. 自动生成迁移
uv run alembic revision --autogenerate -m "add widgets table"
```

打开生成的 `app/migrations/versions/<hash>_add_widgets_table.py`，**人工 review**：

- 检查列类型、约束是否对
- 删除无关变更（Alembic 偶尔误识别）
- 大表加索引时考虑 `op.create_index(..., postgresql_concurrently=True)`

## 应用

```bash
uv run alembic upgrade head        # 升到最新
uv run alembic current             # 查看当前版本
uv run alembic history             # 查看版本历史
```

## Beta 前向迁移资格

Beta 阶段仅不把 **schema downgrade** 作为 Stage F 或 CI 的验收条件；稳定版发布前
重新评估降级资格。**当前 head 的 dump/restore、恢复隔离／quarantine 和现有消费者
恢复套件仍是 T1 必需项**，不因本次政策调整而跳过或延后。事务失败原子性、当前权限
检查、不可变回执和精确资源清理同样不变；不允许通过 `stamp`、删依赖对象、
`CASCADE` 或跳过用例伪造成功。已经发布的迁移和历史 `downgrade()` 保持原样；
发现问题以新的前向迁移修复，不重写历史 SQL。

`scripts/validate_migrations.py` 在独立的一次性数据库执行两个真实阶段：

1. `fresh`：从空库完整 `upgrade head`，核验实际版本、关键列和 server default。
2. `forward`：真实升级到 `0173` 后播种历史账号、成员与邀请；升级到 `0174`
   核验角色转换、停用状态、成员关系和邀请历史，再升级到当前 head 并重新核验。

迁移专属的 live 测试补充代表性的真实升级场景、已安装函数／权限合同，以及升级拒绝时
DDL、数据和版本标记全部回滚的原子性。图片切换测试从独立空库真实升级到 `bo0219`，
验证旧 pending/building writer 和未投递 import/ingest thumbnail 会阻止 `bo0220`；
只在按既定合同排空这些工作后才允许继续升级。上传重试测试从真实前置版本升到当前
head，保留旧尝试，验证只有具备已结算未写入证据的失败可复用键、活动／不确定写入
仍排他，且权限和历史不可变性不变。任何测试都不在完整 head 夹具上原地降级。

```yaml
# backend-test.yml（后端分片 job，Postgres service 已就绪）
- name: Migration validation (Beta fresh / forward)
  working-directory: apps/api
  env:
    AAP_MIGRATION_VALIDATION_OWNER: ci:${{ github.run_id }}
    MIGRATION_DATABASE_URL: ${{ env.DATABASE_URL }}
  run: uv run python ../../scripts/validate_migrations.py
```

本前向 runner 要求一次性本地 PostgreSQL 库和可 `CREATE DATABASE` 的连接角色；
它只创建/删除有归属标记的 `<库名>__mv_{fresh,forward}`，成功与失败都会清理，
绝不改动基础库 schema。它不调用 `pg_dump`／`pg_restore`；这些客户端仍由独立的
当前 head 备份／恢复和消费者恢复验收使用。`--json` 的
`qualification: beta-forward-only` 仅记录这两个前向阶段，不声称降级、备份恢复或
quarantine 已通过，也不能替代仍必需的恢复套件。
`--fail-after {create,fresh,forward}` 保留失败路径清理的注入入口。

`scripts/alembic_migration_policy.py` 只报告完整的 base→head 修订链：默认打印 head，
`--json` 输出升级顺序。多 head、merge、孤儿分支、未知父版本和循环仍 fail closed；
历史不可逆标记不再决定前向升级是否可验收。该脚本不授权 `stamp` 或 downgrade。

本地验证：

```bash
pnpm dev:worktree -- init --mode test
pnpm dev:worktree -- exec --mode test -- \
  bash -lc 'cd apps/api && .venv/bin/python ../../scripts/validate_migrations.py'
```

## 同步数据修复

如果迁移涉及数据迁移（不只 schema），在 `upgrade()` 里写 SQL：

```python
def upgrade() -> None:
    op.add_column("widgets", sa.Column("status", sa.String, nullable=True))
    op.execute("UPDATE widgets SET status = 'active' WHERE status IS NULL")
    op.alter_column("widgets", "status", nullable=False)
```

## 测试

```bash
uv run pytest tests/test_alembic_drift.py    # 已有，校验 ORM <-> 迁移一致
```

## 不允许

- ❌ 破坏性迁移没有明确的数据处置、前向升级保护和失败原子性验证
- ❌ 在迁移里 import ORM 模型（迁移要锁定 schema 时点的 DDL，不依赖运行时模型）
- ❌ 跨多个 PR 才能完成的迁移（一个 PR 一个迁移单元）
