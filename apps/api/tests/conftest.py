"""DB-backed pytest 脚手架（function-scoped engine + dependency_overrides[get_db]）。

提供:
  - test_db_url: 解析一次性测试库连接。显式 TEST_DATABASE_URL 优先；否则跟随本环境
    迁移连接（host/port/账号/驱动），库名固定 annotation_test。两者都不可用时明确报错，
    不回退到任何未获准的默认连接。多 worktree（各连不同 postgres 端口）无需手动设置。
  - apply_migrations: session 级，alembic upgrade head（一次性），并在日志中标明目标库。
  - test_engine: function-scoped，避免 pytest-asyncio function-scope event loop 与
    session-scope engine 冲突。
  - db_session: function-scoped，SAVEPOINT 隔离。
  - super_admin / project_admin / annotator / reviewer：平台身份 fixture（含 JWT token）。
    annotator / reviewer 是字面平台 employee 账号；项目职责必须由测试显式创建
    ProjectMember 行授予，fixture 不创建任何 membership，也不从平台角色推断职责。
  - httpx_client: ASGI 客户端，app.dependency_overrides[get_db] 绑定到 db_session，
    fixture 在事务内写入的数据对 API 可见（与 API 共享同一 SAVEPOINT 事务）。

前置条件:
    连接必须是获准写入的一次性测试库（本地 annotation_test 或本 worktree 的
    aap_wt_*_test）。用 pnpm dev:worktree -- exec --mode test -- <command> 运行；
    开发模式下本文件在 import 阶段直接拒绝执行。

跑法:
    cd apps/api
    pytest -q
"""

from __future__ import annotations

import os

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

if os.environ.get("AAP_WORKTREE_MODE") == "dev":
    raise pytest.UsageError(
        "开发模式不能运行数据库测试；请使用 pnpm dev:worktree -- exec --mode test -- <command>"
    )


# 必须在首次 import app 前开启；否则 app.api.v1.router 不会挂载测试 Seed router。
# 数据库名仍由下方 fixture 固定到一次性测试库，router 自身会再做后缀校验。
os.environ["E2E_SEED_ENABLED"] = "true"


def _validate_test_db_target(url: str) -> str:
    """校验解析出的测试库目标确实是获准的一次性测试库。

    规则：连接串必须可解析；驱动必须是 postgresql；库名必须以 _test 结尾
    （覆盖文档化的历史默认 annotation_test 与 worktree 启动器分配的
    aap_wt_*_test）。显式 TEST_DATABASE_URL 与派生默认一视同仁，不能绕过。

    用户可见错误只包含异常类型名 / 驱动名 / 库名——绝不回显原始 URL 或底层
    异常文本，也不保留异常链，避免畸形连接串里的凭据泄漏到输出。
    """
    from sqlalchemy.engine import make_url

    try:
        parsed = make_url(url)
    except Exception as exc:
        raise RuntimeError(
            f"测试数据库连接串无法解析（{type(exc).__name__}）。"
            "请检查 TEST_DATABASE_URL 或 app 迁移配置指向的连接串。"
        ) from None
    if parsed.get_backend_name() != "postgresql":
        raise RuntimeError(
            f"测试数据库驱动必须是 postgresql，得到 "
            f"{parsed.get_backend_name()!r}（pytest 脚手架按 asyncpg 写表/迁移）。"
        )
    if not parsed.database or not parsed.database.endswith("_test"):
        raise RuntimeError(
            f"拒绝非一次性测试库目标：库名 {parsed.database!r} 不以 _test 结尾。"
            "pytest 必须运行在获准的一次性测试库（annotation_test 或 "
            "aap_wt_*_test）上；开发/生产库一律拒绝。"
        )
    return url


def _resolve_test_db_url() -> str:
    """解析并校验测试库连接：显式 TEST_DATABASE_URL 优先，其次从迁移连接派生
    annotation_test 库。测试 fixture 需要运行 Alembic 并直接写表，因此分离数据库
    角色时使用 schema owner；单角色环境仍回退 DATABASE_URL。

    任一步骤失败都抛出带原因的 RuntimeError——绝不回退到硬编码默认连接串，
    避免把配置错误变成指向未获准数据库的连接失败。返回前经
    _validate_test_db_target 校验目标身份。
    """
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return _validate_test_db_target(explicit)
    try:
        from sqlalchemy.engine import make_url

        from app.config import settings

        derived = (
            make_url(settings.effective_migration_database_url)
            .set(database="annotation_test")
            .render_as_string(hide_password=False)
        )
    except Exception as exc:
        raise RuntimeError(
            f"无法解析默认测试数据库（{type(exc).__name__}），且未设置 "
            "TEST_DATABASE_URL。请显式导出 TEST_DATABASE_URL 指向获准的"
            "一次性测试库（库名以 _test 结尾）。"
        ) from None
    return _validate_test_db_target(derived)


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Keep request limits active while isolating the shared ASGI client IP."""
    from app.core.ratelimit import limiter

    limiter.reset()
    try:
        yield
    finally:
        limiter.reset()


@pytest.fixture(scope="session")
def test_db_url() -> str:
    return _resolve_test_db_url()


@pytest.fixture(scope="session")
def apply_migrations(test_db_url: str):
    """在整个 session 中运行一次 alembic upgrade head。

    保持 session-scope：迁移只跑一次，但下面的 test_engine 是 function-scope，
    不再共享同一 engine，迁移结果是 DDL，commit 后对所有连接可见。
    目标库身份已由 _resolve_test_db_url / _validate_test_db_target 守卫校验；
    下面打印库名仅作人工核对，不构成安全边界。
    """
    from alembic.config import Config
    from alembic import command
    from sqlalchemy.engine import make_url

    db_name = make_url(test_db_url).database
    print(f"\n[conftest] alembic upgrade head -> test db {db_name!r} (guard-validated)")
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", test_db_url)
    command.upgrade(alembic_cfg, "head")
    yield
    # 迁移保留，便于失败时检查


@pytest.fixture
async def test_engine(test_db_url: str, apply_migrations):
    """Function-scoped engine：与 pytest-asyncio 默认 function-scope event loop 兼容。"""
    engine = create_async_engine(test_db_url, echo=False)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(test_engine):
    """Per-test 数据库 session（SAVEPOINT 隔离）。"""
    conn = await test_engine.connect()
    trans = await conn.begin()
    maker = async_sessionmaker(conn, class_=AsyncSession, expire_on_commit=False)
    session = maker()

    await conn.begin_nested()
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        # 模块级进程缓存的服务（如 SystemSettingsService）需在每 test 清理，
        # 否则上一个测试的 PATCH 值会泄漏到下一个测试（DB SAVEPOINT 已回滚但缓存未失效）。
        # cleanup 失败必须可见，不能用静默 except 掩盖缓存污染。
        from app.services.system_settings_service import SystemSettingsService

        SystemSettingsService.invalidate()


@pytest.fixture(scope="session")
def app_module():
    from app.main import app

    return app


@pytest.fixture
async def httpx_client(app_module, db_session: AsyncSession):
    """ASGI httpx client，dependency_overrides[get_db] 绑定到 db_session。

    fixture 在 db_session 写入的数据对 API 可见（fixture 与 API 共享同一 SAVEPOINT 事务）。
    所有 API 集成测试统一使用本 fixture；需要真实独立事务/跨连接可见性时，
    另建 engine/connection（参考 test_worker_signals.py、test_discussion_notifications_commit.py）。
    """
    from app.deps import get_db

    async def _override():
        yield db_session

    app_module.dependency_overrides[get_db] = _override
    transport = httpx.ASGITransport(app=app_module)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            yield client
    finally:
        app_module.dependency_overrides.pop(get_db, None)


# ── 用户 Fixtures ────────────────────────────────────────────────────


def _make_user(role: str, email: str, name: str) -> dict:
    from tests.factory import make_user_dict

    return make_user_dict(role, email, name)


async def _create_user(db: AsyncSession, role: str, email: str, name: str):
    from app.db.models.user import User

    data = _make_user(role, email, name)
    user = User(**data)
    db.add(user)
    await db.flush()

    from app.core.security import create_access_token

    token = create_access_token(subject=str(user.id), role=role)
    return user, token


@pytest.fixture
async def super_admin(db_session: AsyncSession):
    return await _create_user(db_session, "super_admin", "admin@test.local", "Admin")


@pytest.fixture
async def project_admin(db_session: AsyncSession):
    return await _create_user(db_session, "project_admin", "pm@test.local", "PM")


@pytest.fixture
async def annotator(db_session: AsyncSession):
    """Literal platform employee; project authority requires an explicit membership."""

    return await _create_user(db_session, "employee", "anno@test.local", "Annotator")


@pytest.fixture
async def reviewer(db_session: AsyncSession):
    """Literal platform employee; project authority requires an explicit membership."""

    return await _create_user(db_session, "employee", "qa@test.local", "Reviewer")


@pytest.fixture
def auth_headers(super_admin) -> dict[str, str]:
    _, token = super_admin
    return {"Authorization": f"Bearer {token}"}


# ADR-0050 · 测试辅助: 创建 registry + 其 singleton 服务池 + active 成员。
# 项目启用关联 / 项目主绑定都基于 pool id (ProjectMLBackendPool.pool_id / Project.ml_backend_pool_id)。
# 测试不再直接 new ProjectMLBackend(project_id, registry_id); 改用本 helper 得到 pool 再建关联。
async def create_registry_with_pool(
    db: AsyncSession,
    *,
    name: str = "test-backend",
    url: str | None = None,
    state: str = "connected",
    is_interactive: bool = True,
    enabled_pool: bool = False,
    **registry_kwargs,
):
    """Create a MLBackendRegistry row + its singleton service pool + active member.

    Returns (registry, pool). The pool's legacy_instance_id points at the registry.
    Pass enabled_pool=True to mark the pool enabled (rare; off mode leaves it false
    and project enablement is expressed via ProjectMLBackendPool.enabled).
    """
    from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
    from app.db.models.ml_backend_registry import MLBackendRegistry

    url = url or f"http://{name}.test:9999"
    registry = MLBackendRegistry(
        name=name,
        url=url,
        state=state,
        is_interactive=is_interactive,
        source="manual",
        **registry_kwargs,
    )
    db.add(registry)
    await db.flush()
    pool = MLBackendServicePool(
        name=registry.name,
        enabled=enabled_pool,
        routing_policy="smooth_weighted_round_robin",
        legacy_instance_id=registry.id,
        routing_generation=1,
    )
    db.add(pool)
    await db.flush()
    db.add(
        MLBackendPoolMember(
            pool_id=pool.id,
            registry_id=registry.id,
            traffic_state="active",
            weight=1,
        )
    )
    await db.flush()
    return registry, pool
