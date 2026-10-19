---
title: 新增 API 端点
audience: [dev]
type: how-to
status: stable
last_reviewed: 2026-10-19
---

# How-to：新增 API 端点

> 本文以真实落地的 `POST /auth/logout` 为线索，串联从 FastAPI 路由、Service、测试、OpenAPI 快照到前端生成的完整链路。

---

## 完整流程（标准版）

```bash
# 1. 后端：路由 + service + schema
apps/api/app/api/v1/auth.py                # 路由
apps/api/app/core/token_blacklist.py       # service
apps/api/app/schemas/user.py               # 复用现有 Token schema

# 2. 后端测试
apps/api/tests/test_auth.py

# 3. 跑测试
cd apps/api && uv run pytest tests/test_auth.py -v -k logout

# 4. 刷新受 Git 跟踪的 OpenAPI snapshot
pnpm openapi:export

# 5. 本地生成前端 TS 类型与 SDK（构建期产物，不提交 Git）
pnpm codegen

# 6. 前端 wrapper + 状态变更
apps/web/src/api/auth.ts
apps/web/src/pages/.../UserMenu.tsx        # 调用方

# 7. 提 PR：路由 + 测试 + openapi.snapshot.json + 前端业务代码一并提交
```

---

## 1. 路由

`POST /auth/logout` 把当前 token 的 jti 加到 Redis 黑名单，TTL = 该 token 剩余有效期。下面的代码块由 `check-doc-snippets.mjs` 锁定到源文件 `apps/api/app/api/v1/auth.py` 中的 `logout` 函数（含装饰器），源码改一字 prebuild 即报错：

<!-- snippet:apps/api/app/api/v1/auth.py#logout -->

```python
@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.core.token_blacklist import blacklist_token

    payload = decode_access_token(credentials.credentials)
    jti = payload.get("jti")
    if jti:
        exp = payload.get("exp", 0)
        remaining = int(exp - datetime.now(timezone.utc).timestamp())
        await blacklist_token(jti, max(remaining, 0))

    current_user.status = "offline"

    await AuditService.log(
        db,
        actor=current_user,
        action=AuditAction.AUTH_LOGOUT,
        target_type="user",
        target_id=str(current_user.id),
        request=request,
        status_code=204,
    )
    await db.commit()
```

<!-- /snippet -->

要点：

- `status_code=204` 显式声明：FastAPI 默认会改 200 OK，无 body 端点应主动指定 204 让 OpenAPI 准确。
- 三个 `Depends`：`HTTPBearer()` 拿原 token、`get_current_user` 校验签名+黑名单+gen、`get_db` 注入 session。`get_current_user` 内部已经 decode 一次 token，路由里再 decode 一次取 `jti` 是为了保持辅助函数职责单一与可读性。
- `AuditService.log` 在 commit 前写：保证审计与业务事务原子性，崩溃要么都生效要么都回滚。

如果你的端点返回 body，模板换成：

```python
@router.post(
    "/widgets",
    response_model=WidgetOut,
    status_code=201,
    summary="创建 Widget",
    responses={
        400: {"description": "参数非法"},
        409: {"description": "重名冲突"},
    },
)
async def create_widget(
    payload: WidgetIn,
    user=Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """业务规则：同一个 owner 下名字唯一；触发权限校验。"""
    return await widget_service.create(db, payload, owner_id=user.id)
```

`summary` 一句话；`description` 用 docstring；`responses={}` 显式列出非默认状态码 → OpenAPI 完整 → 前端类型完整。

---

## 2. Service / Core helper

`logout` 端点把核心逻辑下放到 `apps/api/app/core/token_blacklist.py:21-29`：

```python
async def blacklist_token(jti: str, ttl_seconds: int) -> None:
    if ttl_seconds <= 0:
        return
    r = _get_redis()
    try:
        await r.setex(f"{_KEY_PREFIX}{jti}", ttl_seconds, "1")
    finally:
        await r.aclose()
```

要点：

- 纯逻辑函数，不接 `Request` / `db`：可独立单测，也可被多个端点复用（实际 `logout-all` 的代际号增量逻辑也在同一文件）。
- TTL ≤ 0 提前返回：token 已过期再加黑名单等于永久占 key。
- Redis 客户端用 `try/finally` 关闭：避免跑全套测试时 Redis 连接泄漏导致连接数触顶——**新加 Redis 调用务必关闭连接**。

如果你的端点逻辑复杂（PG 多表事务、多个 ML backend 调用、外发 SMTP），抽到 `app/services/<feature>.py` 的 service 类：

```python
class WidgetService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(self, payload: WidgetIn, *, owner_id: int) -> Widget:
        existing = await self.db.scalar(
            select(Widget).where(Widget.owner_id == owner_id, Widget.name == payload.name)
        )
        if existing:
            raise HTTPException(409, "name already exists")
        widget = Widget(owner_id=owner_id, **payload.model_dump())
        self.db.add(widget)
        await self.db.flush()
        return widget
```

约定：service 只 `flush`，不 `commit`——commit 由路由层负责，便于在路由内外完整事务（如审计 + 业务在同事务）。

---

## 3. 测试

`logout` 在 `apps/api/tests/test_auth.py` 里覆盖三条：

```python
async def test_logout_blacklists_jti(client, user_factory):
    user = await user_factory(email="t@x.com", password="Aa12345678")
    r1 = await client.post("/api/v1/auth/login",
                           json={"email": "t@x.com", "password": "Aa12345678"})
    token = r1.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r2 = await client.post("/api/v1/auth/logout", headers=headers)
    assert r2.status_code == 204

    # 再用同一 token 调 /auth/me 必须 401
    r3 = await client.get("/api/v1/auth/me", headers=headers)
    assert r3.status_code == 401


async def test_logout_writes_audit_log(client, db, user_factory):
    user = await user_factory(email="audit@x.com", password="Aa12345678")
    # ... 登录 + logout ...
    rows = await db.execute(
        select(AuditLog).where(AuditLog.action == "auth.logout",
                               AuditLog.actor_email == "audit@x.com")
    )
    assert rows.scalar_one_or_none() is not None


async def test_logout_idempotent_when_token_expired(...):
    # token TTL ≤ 0 不应抛错
    ...
```

至少要有 1 条正常路径 + 1 条错误路径。能加上「副作用断言」（这里是审计行）更好——它能抓出业务变更后忘改副作用的常见 bug。

---

## 4. OpenAPI Snapshot 与前端类型生成

```bash
# 导出最新 OpenAPI 快照并校验契约一致
pnpm openapi:export
pnpm openapi:check

# 生成前端 TypeScript 类型与 SDK（输出至 apps/web/src/api/generated/，该目录被 Git 忽略）
pnpm codegen
grep -A 5 "logout" apps/web/src/api/generated/sdk.gen.ts | head
```

CI 会执行 `pnpm openapi:check` 与 `pnpm docs:gen`（刷新路由索引 `_routes.generated.md`），因此修改后端接口后必须将更新后的 `apps/api/openapi.snapshot.json`（及受影响的文档产物）一并提交。

---

## 5. 前端 wrapper

来源：`apps/web/src/api/auth.ts`：

```ts
import { authLogout } from "./generated/sdk.gen";

export async function logout(): Promise<void> {
  await authLogout();
  // 清本地存储
  localStorage.removeItem("anno_token");
  // 跳登录页
  window.location.assign("/login");
}
```

调用方在 `UserMenu.tsx` 之类的组件：

```tsx
<DropdownMenuItem onClick={() => logout()}>退出登录</DropdownMenuItem>
```

要点：

- 优先复用 `generated/sdk.gen` 与 `types.gen` 中的契约定义（参见 [ADR-0003](/dev/adr/archive/0003-openapi-client-codegen) 与 [前后端契约边界](../concepts/api-schema-boundary)）。
- 业务副作用（清 localStorage、跳页）放 wrapper 里；组件只负责调用。

---

## 6. PR 检查清单

- [ ] `pnpm openapi:check` 通过（`apps/api/openapi.snapshot.json` 已更新并提交）
- [ ] `pnpm docs:gen` 已同步刷新 `docs-site/api/guides/_routes.generated.md`
- [ ] 后端测试覆盖正常路径 + 至少 1 个错误路径 + 至少 1 个副作用断言
- [ ] 端点声明了 `status_code` / `summary` / 非默认 `responses`
- [ ] 关键业务变更写了 `AuditService.log`（如果端点有副作用）
- [ ] 本地运行 `pnpm codegen` 与 `pnpm typecheck` 通过
- [ ] 若新增端点需暴露给 Python SDK / CLI，同步评估并更新 [Python SDK 参考](../sdk/python-client)
