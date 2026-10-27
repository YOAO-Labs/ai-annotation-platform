import asyncio
from logging.config import fileConfig

from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# 导入所有 Model，autogenerate 才能感知表结构
from app.db.base import Base
from app.db.native_schema import include_orm_schema_object
import app.db.models  # noqa: F401 — 触发 User/Project/Task/Annotation 注册到 Base.metadata
from app.config import settings

config = context.config

# 从 pydantic settings 注入真实 URL，覆盖 alembic.ini 中的空值。
# 但若调用方已通过 cfg.set_main_option(...) 显式设置（例如 conftest 注入 test_db_url），
# 不要覆盖 — 否则测试 DB 上的迁移会跑到 dev DB 上。
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", settings.effective_migration_database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        include_object=include_orm_schema_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


# celery-worker 与 celery-beat 同时启动会并发跑 alembic upgrade head, 没有锁的话
# 两边可能同时执行同一条 ALTER, 先到的成功、后到的撞 DuplicateColumnError 直接退出.
# Postgres session-level advisory lock 把 migration 串行化; 后到者等到 head 已就绪
# 再进入, 跑出 no-op upgrade.
_ALEMBIC_LOCK_ID = 727274


def do_run_migrations(connection: Connection) -> None:
    # 第一句 execute 会隐式开启 outer transaction; context.begin_transaction()
    # 因此降级为 SAVEPOINT, 内层 commit 不会推到 DB. asyncpg + NullPool 下连接
    # 归还时无显式 commit → 整段 migration 被静默回滚, alembic_version 不更新
    # 但 "Running upgrade" 日志已打 — 表象就是「跑了但没生效」.
    # 修法: 显式 connection.commit() 把 outer transaction 推进去. 由于 advisory lock
    # 是 session 级 (pg_advisory_lock 而非 pg_advisory_xact_lock), commit 不释放锁.
    connection.execute(
        text("SELECT pg_advisory_lock(:k)").bindparams(k=_ALEMBIC_LOCK_ID)
    )
    try:
        if connection.scalar(text("SELECT to_regclass('public.alembic_version')")):
            revisions = connection.scalars(
                text("SELECT version_num FROM public.alembic_version")
            ).all()
            if any(
                "0190" <= revision <= "0370" for revision in revisions
            ) and connection.scalar(text("SELECT to_regnamespace('aap_workspace_v1')")):
                raise RuntimeError(
                    "This database uses the pre-merge backend-opt revision lineage. "
                    "Numeric 0190-0267 now retain the published main history; "
                    "owner migrations use bo0190-bo0370. Refusing an ambiguous "
                    "upgrade: restore and reconcile a verified backup before "
                    "adopting the merged lineage; do not stamp the database."
                )
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_orm_schema_object,
        )
        with context.begin_transaction():
            context.run_migrations()
        connection.commit()
    except Exception:
        # PostgreSQL marks the transaction aborted after any failed DDL. Roll it
        # back before the session-level unlock, otherwise the unlock itself raises
        # InFailedSQLTransaction and hides the migration's real root cause.
        connection.rollback()
        raise
    finally:
        connection.execute(
            text("SELECT pg_advisory_unlock(:k)").bindparams(k=_ALEMBIC_LOCK_ID)
        )
        connection.commit()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
