---
audience: [dev]
type: how-to
status: stable
last_reviewed: 2026-10-14
---

# How-to：Alembic 迁移

## 生成

```bash
cd apps/api
# 1. 改 SQLAlchemy 模型 (app/db/models/...)
# 2. 自动生成迁移
uv run alembic revision --autogenerate -m "add widgets table"
```

打开生成的 `alembic/versions/<hash>_add_widgets_table.py`，**人工 review**：

- 检查列类型、约束是否对
- 删除无关变更（Alembic 偶尔误识别）
- 大表加索引时考虑 `op.create_index(..., postgresql_concurrently=True)`

## 应用

```bash
uv run alembic upgrade head        # 升到最新
uv run alembic downgrade -1        # 回滚一步
uv run alembic current             # 查看当前版本
uv run alembic history             # 查看版本历史
```

## CI 校验

`scripts/validate_migrations.py` 在一次性数据库上执行真实验证，不使用 `alembic stamp`：

1. `fresh`：新库完整 `upgrade head`。
2. `reversible`：新库先 `upgrade <floor>`（真实 schema，不是 stamp），再真实
   `downgrade base` 并重新 `upgrade head`；`<floor>` 是唯一不可逆迁移之下
   的最新版本，因此不可逆迁移的 `downgrade()` 从不执行。
3. `suffix`：不可逆迁移之后存在可逆后缀时，执行 `head → 不可逆版本 → head`，
   核对实际降级步数、版本边界和已转换的历史数据。没有后缀时跳过此阶段。
4. `forward`：在 `<floor>` 播种真实历史行，升级到不可逆版本后做数据断言（角色转换、
   历史行保留、新 server default、不伪造成员关系），证明该版本的 `downgrade()` 会拒绝，
   再升级到 head 并复核数据。
5. `restore`：把转换前的 `pg_dump` 快照恢复到独立数据库，证明恢复路径真实可用。

```yaml
# ci.yml（pytest job，Postgres service 已就绪）
- name: Migration validation (fresh / reversible / suffix / forward / restore)
  working-directory: apps/api
  env:
    AAP_MIGRATION_VALIDATION_OWNER: ci:${{ github.run_id }}
    MIGRATION_DATABASE_URL: ${{ env.DATABASE_URL }}
  run: uv run python ../../scripts/validate_migrations.py
```

运行要求：目标是一次性本地库且连接角色可 `CREATE DATABASE`；宿主机有
`pg_dump`/`pg_restore`，或 Postgres 运行在可被 `docker ps` 发现的容器中（
容器镜像自带客户端）。脚本只创建/删除 `<库名>__mv_{fresh,reversible,suffix,forward,restore}`
并做归属标记，成功与失败都会清理，绝不改动基础库 schema。

确实无法无损回滚的迁移（如 `0174` 员工角色切换）在迁移文件里声明模块级
`IRREVERSIBLE = True` 并让 `downgrade()` 抛错。`scripts/alembic_reversible_floor.py`
只用于**报告**可逆边界：默认打印 floor，`--json` 输出完整策略；它不授权 `stamp`。
`--json` 中的 `reversible_suffix` 单独列出不可逆版本之上的可逆修订。
修订图出现多 head、merge、孤儿分支、缺失祖先、环、多个不可逆迁移或基线处不可逆时，
脚本会 fail closed（非零退出），而不是给出一个会漏检的 floor。

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

- ❌ 直接 `op.execute("DROP TABLE ...")` 不写 downgrade
- ❌ 在迁移里 import ORM 模型（迁移要锁定 schema 时点的 DDL，不依赖运行时模型）
- ❌ 跨多个 PR 才能完成的迁移（一个 PR 一个迁移单元）
