# 头像上传与内置像素头像

> 状态：关键决策已确认（§1.1），尚未实施。
> 日期：2026-09-15。
> 范围：用户头像的数据模型、上传与规范化链路、内置像素头像资产、设置页选择器，以及核心身份面 / 成员与用户列表的头像展示。
> 目标：用户可上传自定义头像或从内置像素头像中选择；展示位置在深/浅两套主题下都清晰、可缓存、可回退到现有首字母；所有图片内容由服务端校验与规范化，不引入第三方运行时依赖。

## 1. 推荐方向

1. **一个字段承载身份**：`users.avatar_ref`（可空字符串）是唯一真值，取值只有两种语法——`preset:<slug>`（内置像素头像）和 `upload:<32位十六进制 token>`（用户上传）。`NULL` = 保持现有首字母圆片。读取契约在所有用户身份 payload 上只加这一个字段，前端统一由一个解析函数还原为 URL。
2. **上传走 API 直传 + 服务端规范化**：`POST /api/v1/auth/me/avatar`（multipart，≤2 MB）→ Pillow 校验真实格式与像素规模 → EXIF 转正 → 居中方裁 → 缩放 → WebP → 写对象存储 → 更新 `avatar_ref` 并清理旧对象。
3. **内置头像构建期预生成、随仓库提交**：用 DiceBear `pixel-art`（CC0 1.0，可商用无署名义务）在构建期生成 32 张静态 SVG 到 `apps/web/public/avatars/pixel/`，附带 provenance manifest。运行期零依赖，产物可 review、可离线、CSP 友好。
4. **上传头像通过同源能力 URL 展示**：`GET /api/v1/avatars/{token}` 免鉴权、可强缓存、按 token 直取对象（已确认采用）。URL 是 `avatar_ref` 的纯函数，因此不需要在十几处 payload 里回填签名 URL，也不会有预签名 URL 每 10 分钟变化导致的缓存抖动。安全边界见 §4.6。
5. **头像引用只由服务端写入**：客户端的 `PATCH` 只接受 `preset:` 与 `null`，`upload:` 只能由上传端点产生，因此无法引用他人的图片。
6. **独立 `avatars` 桶**（已确认）：键为 `{token}.webp`，永久保留、纳入备份、不挂 lifecycle；完整接线清单见 §4.3.2。

本次范围之外的展示位置（评论、Issue、通知、审计日志）保持纯文字/首字母，见 §12。

### 1.1 已确认决策

| 问题                                 | 结论                                                                                              |
| ------------------------------------ | ------------------------------------------------------------------------------------------------- |
| `GET /api/v1/avatars/{token}` 匿名读 | **接受**。按 §4.6 实现，不再作为待评审项；风险与边界记录在 §11。                                  |
| 头像对象存储位置                     | **新建独立 `avatars` 桶**。按 §4.3.2 的完整接线清单实施（含生产 Caddy 路径白名单与 MinIO 策略）。 |
| 内置头像数量                         | **本次 32 张**，上线后再评估是否追加风格或数量。                                                  |

## 2. 现状证据（代码事实）

| 现状                                                                                                                                          | 影响                                                   | 处理方向                                                                                                           |
| --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| `apps/web/src/components/ui/Avatar.tsx` 只渲染首字母圆片，注释明确「纯首字母，无图片加载」                                                    | 没有任何图片头像能力                                   | 保留为首字母回退原语，另加图片头像组件（§6.1）                                                                     |
| `apps/web/src/components/shadcn/ui/avatar.tsx` 已存在（Radix Root/Image/Fallback）但全仓无人 import                                           | 死代码                                                 | 曾计划复用；实测入口包预算已满（700 KB 用掉 699.8 KB），Radix 原语约 +3 KB 会超预算，改为自实现回退（§6.1、§14.3） |
| `apps/api/app/schemas/user.py` 中只有 `UserBrief` 带 `avatar_initial`；`UserOut` / `MeResponse` / `ProjectMemberOut` 无头像字段               | 头像只能靠名字首字母，同一人多处显示不一致             | 新增 `avatar_ref` 并扩展到读取契约（§3.3）                                                                         |
| `apps/api/app/db/models/user.py` 无头像列；`users.preferences` JSONB 是「偏好」而非身份                                                       | 无存储位置                                             | 新增独立列，不复用 preferences（§3.1）                                                                             |
| `apps/web/src/components/shell/TopBar.tsx:184` 用 `user?.name?.[0]`，其余 20+ 处前端各自算首字母                                              | 逻辑重复、无图片头像、无法跨设备一致                   | 统一到 `UserAvatar` + `resolveAvatarUrl`（§6）                                                                     |
| `apps/api/app/api/v1/me.py` 的 `PATCH /auth/me` 只改姓名，`/auth/me` 返回 `UserOut`（`auth.py:230`）                                          | 头像需要独立端点，避免把 `ProfileUpdate.name` 改成可选 | 独立 `POST/PATCH/DELETE /auth/me/avatar`（§4.1）                                                                   |
| 现有上传全部走「presigned PUT 直传对象存储」（评论附件 `annotation_comments.py:505`、Bug 截图 `bug_reports.py:95`）                           | 服务端拿不到字节，无法校验/裁剪内容                    | 本次改为 API 承载字节；预签名路径仍保留给大附件（§4.2）                                                            |
| `apps/api/app/middleware/upload_body_limits.py` 只对 frame / mask 路径做预解析体量上限                                                        | 普通端点没有上传体量上限                               | 为新端点补一条预解析上限（§4.5）                                                                                   |
| `apps/api/app/services/storage.py` 的 `annotations` 桶已混装任务源文件、`comment-attachments/`、`projects/*/guide/`，lifecycle 规则按前缀限定 | 桶名称与实际职责已不匹配                               | 头像以独立 `avatars` 桶隔离（§4.3）                                                                                |
| CSP 两处均为 `img-src 'self' data: blob: https:`（`infra/docker/nginx.conf:41`、`apps/api/app/middleware/security_headers.py:29`）            | 同源静态 SVG 与同源 API 图片均可直接使用               | 无需改 CSP                                                                                                         |
| 仓库已有手绘像素风先例：`apps/web/src/pages/Workbench/shell/pet/PixelHumanSprite.tsx`（整型坐标 SVG + `shapeRendering="crispEdges"`）         | 像素风与产品调性一致                                   | 内置头像采样同一视觉取向（见 §5.6 的取舍）                                                                         |

未在真实浏览器中验证本方案；§10 的验收要求实施者在深浅两套主题下逐项确认。

## 3. 数据模型与读取契约

### 3.1 `users.avatar_ref`

新迁移（Alembic head 为 `0171`，本迁移取 `0172`）：

```python
op.add_column("users", sa.Column("avatar_ref", sa.String(80), nullable=True))
```

- 可空，无默认值，**不回填历史数据**：存量用户继续走首字母，迁移是纯加列，不需要数据订正，也不需要停机。
- 长度 80 足够容纳 `preset:` + ≤40 字符 slug，或 `upload:` + 32 位 token。
- 不复用 `preferences` JSONB：`UserPreferences` 是 `extra="forbid"` 的强类型偏好树，头像是身份而非偏好；放进 JSONB 会让「谁有头像」无法用 SQL 直接查、也无法在硬删除账号时直接拿到对象 key。

### 3.2 引用语法

| 取值             | 含义           | 谁能写                     | 语法校验                   |
| ---------------- | -------------- | -------------------------- | -------------------------- |
| `NULL` / 缺省    | 首字母圆片     | —                          | —                          |
| `preset:<slug>`  | 内置像素头像   | 用户自助                   | `^preset:[a-z0-9-]{1,40}$` |
| `upload:<token>` | 用户上传的图片 | **仅上传端点**，客户端只读 | `^upload:[0-9a-f]{32}$`    |

服务端用一个 `app/services/avatar.py` 收纳 `parse_ref` / `build_preset_ref` / `build_upload_ref` / `object_key_for`，避免语法散落在端点、服务与 schema 里。严格白名单同时挡掉路径穿越（`/`、`..`、`?`、`#`）与任意字符串进入 `<img src>`。

### 3.3 读取契约扩展

只加一个可选字段，且只加在「需要展示头像」的 payload 上：

| Schema / 类型                                                              | 文件                                                         | 变更                                     |
| -------------------------------------------------------------------------- | ------------------------------------------------------------ | ---------------------------------------- |
| `UserBrief`（任务/批次/仪表盘/历史时间线/反馈共用）                        | `apps/api/app/schemas/user.py:552`、`services/user_brief.py` | `+ avatar_ref: str \| None = None`       |
| `UserOut`（`/auth/me`、用户管理、角色变更、停用/恢复等全部返回用户的端点） | `apps/api/app/schemas/user.py`                               | `+ avatar_ref: str \| None = None`       |
| `ProjectMemberOut`                                                         | `apps/api/app/schemas/project.py:366`                        | `+ avatar_ref: str \| None = None`       |
| `ProjectOut.owner_*` / 管理端项目列表                                      | `apps/api/app/schemas/project.py`、`schemas/dashboard.py`    | `+ owner_avatar_ref: str \| None = None` |
| `AdminPersonItem` / `ProjectMemberPerformance`                             | `apps/api/app/schemas/dashboard.py:146`、项目绩效 schema     | `+ avatar_ref`                           |

`UserOut` 是 `from_attributes = True` 且直接由 ORM 对象构造，加列后自动带上 `avatar_ref`，无需改几十个调用点。`UserBrief` 在 `services/user_brief.py::_to_brief` 一处补字段即可覆盖任务列表、批次、仪表盘与标注历史时间线。

**不新增 `avatar_url` 字段**：URL 是 `avatar_ref` 的纯函数（见 §3.4），后端返回 URL 会迫使 schema 依赖 `storage_service`（签名）或把前端静态路径写进 API，二者都是错的。

### 3.4 为什么只有一个字段（含被否方案）

| 方案                                     | 结论                                                                                                                                                                                                                                                |
| ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 单一 `avatar_ref` + 前端解析（**推荐**） | payload 只加一个字段，十几处端点零改动；URL 稳定因此缓存友好；不需要在 schema 里做签名                                                                                                                                                              |
| 后端返回 `avatar_url`（预签名）          | 需要在 `UserOut`/`UserBrief` 构造处回填签名 URL；`generate_download_url` 的 Expires 按 10 分钟网格变化（`storage.py::_aligned_expires_in`），头像遍布列表会导致 URL 频繁变化、缓存失效。若走这条路必须新增「按天对齐」的签名 helper，复杂度高于收益 |
| 两列 `avatar_kind` + `avatar_key`        | 语义更显式，但两列可能互相矛盾，且读取契约要传两个字段；单列 + 前缀在同一处解析更省                                                                                                                                                                 |
| 复用 `preferences.ui`                    | 见 §3.1，身份数据不该藏在偏好 JSONB 里                                                                                                                                                                                                              |

## 4. 上传与展示链路

### 4.1 端点

| 方法     | 路径                      | 作用                                                                                                      |
| -------- | ------------------------- | --------------------------------------------------------------------------------------------------------- |
| `POST`   | `/api/v1/auth/me/avatar`  | multipart `file`：校验 → 规范化 → 落对象存储 → 写 `avatar_ref` → 返回 `UserOut`                           |
| `PATCH`  | `/api/v1/auth/me/avatar`  | JSON `{"avatar_ref": "preset:pixel-07" \| null}`：选内置头像或恢复首字母；若原值是 `upload:` 则删除旧对象 |
| `DELETE` | `/api/v1/auth/me/avatar`  | 等价于 `PATCH {"avatar_ref": null}`，给「恢复默认」一个语义明确的动词                                     |
| `GET`    | `/api/v1/avatars/{token}` | 按 `upload:` token 返回图片字节（免鉴权、强缓存，见 §4.6）                                                |

`PATCH`/`DELETE` 通过 `app/services/avatar.py` 校验：拒绝任何 `upload:` 前缀（即便语法合法），并校验 `preset:` slug 语法。HTTP 400 + 中文 detail，失败不改库。

### 4.2 服务端规范化流程

`app/services/avatar_image.py`（纯函数、可单测，不碰 IO）：

1. `content_type` 白名单 `image/png` / `image/jpeg` / `image/webp`；**不信任**它，仅作早退。
2. `Image.open` + `img.verify()` 确认是真实图片；用 `Image.MAX_IMAGE_PIXELS` 与显式像素上限（如 ≤ 4000 万像素）挡解压炸弹；宽高比与最小边（如 ≥ 32 px）校验。
3. `ImageOps.exif_transpose` 消除方向信息；转 `RGB`（丢弃 alpha，按 `bg-muted` 同色或白色合成，避免透明 PNG 变黑）。
4. 居中方裁（取短边）→ `LANCZOS` 缩放到 256×256。
5. 编码 WebP（质量 ~90），返回字节 + 尺寸。

端点侧：`put_object` 到 `avatars` 桶的 `{token}.webp`（`ContentType=image/webp`，`CacheControl="public, max-age=86400"`）→ `users.avatar_ref = f"upload:{token}"` → 尽力删除旧 `upload:` 对象（失败只记日志，不回滚本次成功）。

**提交顺序**为先写对象再提交 DB：崩溃只会留下无引用的孤儿对象（§11），不会出现「DB 指向不存在的对象」这种用户可见故障。

### 4.3 存储位置：独立 `avatars` 桶（已确认）

```
MINIO_AVATARS_BUCKET=avatars
avatars/{token}.webp        # token = uuid4().hex，32 位小写十六进制
```

桶内不再分层：token 本身已是不可枚举的随机量，加 `{user_id}/` 前缀只会让「按引用取对象」多一次查库（§4.6 的读路由刻意不查库）。

**策略**：永久保留、纳入备份、**不挂任何 lifecycle 规则**（头像不可重生）。`avatars` 桶不进 `MEDIA_CACHE_PREFIXES`，也不进 `_ensure_lifecycle` 的规则列表——新增桶时最危险的动作就是给整桶加 `Prefix: ""` 过期规则（`docs-site/dev/reference/storage-buckets.md` 记录的 playback 404 事故即同类问题）。

#### 4.3.1 为什么选独立桶

`annotations` 桶已混装任务源文件、`comment-attachments/`、`projects/*/guide/`，名称与实际职责早已不符。头像带来的是**新的一类持久身份数据**：不可重生、需备份、需长期保留、与任务数据无任何生命周期关系。仓库既有原则是「按生命周期 + 敏感度分桶」，因此按该原则放独立桶，而不是继续往 `annotations` 里加前缀。

代价是需要一次完整接线（下表），收益是：备份/迁移策略独立、存储管理页可单独观测头像体量、将来若要给头像加 CDN 或公开读，只影响一个桶。

#### 4.3.2 接线清单（缺一项就会在开发或生产出错）

| 位置                                                      | 改动                                                                                       |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `apps/api/app/config.py`                                  | `minio_avatars_bucket: str = "avatars"`                                                    |
| `apps/api/app/services/storage.py`                        | `self.avatars_bucket`；加入 `ensure_all_buckets()` 与 `list_all_buckets()`                 |
| `apps/api/app/api/v1/storage.py::bucket_roles`            | 加 `storage_service.avatars_bucket: "avatars"`，否则存储管理页看不到该桶                   |
| `.env.example`                                            | `MINIO_AVATARS_BUCKET=avatars`（注释：永久保留 / 纳入备份），随后 `pnpm docs:gen-env-vars` |
| `apps/web/src/api/storage.ts`                             | `BucketSummary.role` 联合类型加 `"avatars"`                                                |
| `apps/web/src/pages/Storage/StoragePage.tsx::ROLE_LABELS` | 加 `avatars: "用户头像"`                                                                   |
| `docs-site/dev/reference/storage-buckets.md`              | 桶清单加一行；桶数从「共 5 个」改为实际数量，并补上漏写的 `import` / `export`              |
| `docs-site/ops/deploy/docker-compose.md`                  | 变量表加 `MINIO_AVATARS_BUCKET` 行                                                         |
| `docs-site/ops/deploy/lan-production.md`                  | 四处「七个桶 / 七个桶变量」改为八个；变量表加 `prod-avatars`；预建桶与备份范围同步         |
| `infra/docker/lan-production-minio-policy.json`           | 三处资源列表各加 `arn:aws:s3:::prod-avatars`（生命周期那一段不加，本桶无 lifecycle 规则）  |
| `infra/docker/Caddyfile.lan-prod:24`                      | `not path` 白名单加 `/minio/prod-avatars/*`，否则边缘代理对该桶直接 404                    |
| `docker-compose.yml`                                      | 默认值走 `config.py`，仅当需要显式固定桶名时才在 api/worker/beat 的 `environment` 补       |
| `DEV.md` 与 `docs-site/dev/reference/env-vars.md`         | 前者如列桶清单则补一行；后者由 `pnpm docs:gen-env-vars` 生成，不手改                       |

生产验收必须包含：`ensure_all_buckets` 对新桶执行 `head_bucket` 成功、存储管理页出现「用户头像」卡片、上传后头像在前端可访问（证明 Caddy 路径白名单与 MinIO 策略都已生效）。

#### 4.3.3 被否方案

**复用 `annotations` 桶的 `avatars/` 前缀**：接线成本几乎为零（该桶 lifecycle 规则按前缀限定，头像天然永久，且已纳入备份），但继续加剧桶职责混乱，头像体量也无法单独观测；既然本次已确认要独立桶，就不再采用该方案。

### 4.4 旧对象清理与孤儿

- 换成 `preset:`、恢复默认、再次上传时，通过旧 `avatar_ref` 直接算出对象 key 并删除（无需列表操作）。
- 账号删除/停用**不删头像**：`DELETE /users/{id}` 是软删除（`is_active=False`），用户行与头像对象都保留，管理员在用户管理里仍能看到头像；这一条是本仓库的实际实现，不要按「硬删除」设想加清理逻辑。
- 上传成功后 DB 提交失败的孤儿对象不再有引用：量级为「每次失败一个几十 KB 对象」，不做实时 GC。若要清理，运维按「`avatars/` 前缀下存在、但 `users.avatar_ref` 中不存在」比对后删除即可。本方案只要求把该风险写进 §11，不新增清理任务。

### 4.5 请求体上限

`apps/api/app/middleware/upload_body_limits.py` 新增：

```python
MAX_AVATAR_FILE_BYTES = 2 * 1024 * 1024
MAX_AVATAR_MULTIPART_BODY_BYTES = MAX_AVATAR_FILE_BYTES + 256 * 1024
is_avatar = path == "/api/v1/auth/me/avatar"
```

与该文件既有做法一致：在 FastAPI 解析 multipart 之前按 `name="file"` 精确量出该 part 的字节数，超限直接 413，避免整段 body 进内存。端点再限频：`limiter` 定义在 `apps/api/app/core/ratelimit.py`，既有用法见 `apps/api/app/api/v1/users.py:855`（`@limiter.limit("3/minute")`）——新端点需接收 `request: Request` 才能挂 `@limiter.limit("10/minute")`。

### 4.6 展示路由 `GET /api/v1/avatars/{token}`

新建 `apps/api/app/api/v1/avatars.py`（`router.py` 注册 `prefix="/avatars"`）：

- 路径参数校验 `^[0-9a-f]{32}$`，不合法直接 404，不可能穿越到桶内其它 key。
- **免鉴权、不查库**（已确认采用）：按 token 拼 `{token}.webp` 从 `avatars` 桶流式返回；不存在则 404。
- 响应头：`Content-Type: image/webp`、`Cache-Control: public, max-age=86400, stale-while-revalidate=604800`、`ETag: "<token>"`，命中 `If-None-Match` 返回 304。
- 为什么 `<img>` 必须免鉴权：前端用 Bearer token（localStorage），浏览器加载 `<img>` 不会带 `Authorization` 头；仓库现有做法是预签名 URL，而预签名 URL 会周期性变化，不适合高频重复出现的头像。若改走浏览器 Cookie 会话则与本仓认证模型不一致，代价更大。

**安全边界（实现时按此收口）**：URL 是 128 位随机 token 的「能力 URL」——持有 URL 者不需要登录即可获取该图片，但 token 不可枚举、URL 中不含用户 ID 或任何 PII、响应只有 `image/webp` 字节、无写操作、无 DB 查询。头像本身对全部登录用户可见，因此新增的暴露面是「头像图片可被未登录者读取」而非「身份或权限数据泄露」。该结论需要写进 `docs-site/dev/reference/storage-buckets.md` 或接口文档，避免后续维护者误判。若将来要求收紧，方向是改为后端签发按天对齐的预签名 `avatar_url`（代价：每个用户身份 payload 都要回填签名 URL，浏览器缓存命中率下降；§3.4 已列）。

## 5. 内置像素头像

### 5.1 来源与许可（已核实）

| 项目     | 值                                                                                  |
| -------- | ----------------------------------------------------------------------------------- |
| 风格     | DiceBear `pixel-art`（半身像、低分辨率向量、方形像素）                              |
| 作者     | DiceBear                                                                            |
| 许可     | **CC0 1.0**（放弃版权，可复制、修改、商用、无需署名）                               |
| 风格主页 | https://www.dicebear.com/styles/pixel-art/                                          |
| 许可总览 | https://www.dicebear.com/licenses/（该页逐风格列许可，`Pixel Art` 列在 CC0 1.0 组） |
| 软件许可 | DiceBear 代码 MIT；与本项目只消费生成产物无关                                       |

同类候选（`Pixel Art Neutral`、`Pixelbot`、`Voxel Art`）同为 CC0，需要更多数量时可直接追加风格；OpenGameArt 的 CC0 像素素材风格零散、需逐件核对，不采用。

### 5.2 生成方式

- 生成脚本：`apps/web/scripts/gen-pixel-avatars.mjs`，`@dicebear/core` + `@dicebear/styles` 作为 **devDependency**（仅生成期需要，不进产物）。
- 命令：`pnpm --filter @anno/web gen:avatars`（新增 npm script）。
- 产物**提交进仓库**：运行期零依赖、可离线、不新增 CSP/网络来源、可 code review、构建期不需要网络。
- 脚本用 `--check` 模式供人工复核（重跑后 diff 为空），**manifest 不写生成时间戳**，保证重复执行是无差异的 no-op。

不用 CLI（`dicebear pixel-art --count 32`）：`--count` 与 `--seed` 互斥，产出名固定为 `pixel-art-N.svg`，无法给出稳定 id、也无法逐个指定配色；用 JS 库一次进程内生成更可控。

### 5.3 产出清单

```
apps/web/public/avatars/pixel/
  manifest.json          # 唯一真值：id → 标签/选项 + 许可与来源
  pixel-01.svg … pixel-32.svg
```

`manifest.json` 形状（示意，字段为最终契约）：

```json
{
  "style": "dicebear/pixel-art",
  "generator": "@dicebear/core + @dicebear/styles",
  "sourceUrl": "https://www.dicebear.com/styles/pixel-art/",
  "creator": "DiceBear",
  "license": "CC0 1.0",
  "licenseUrl": "https://creativecommons.org/publicdomain/zero/1.0/",
  "command": "pnpm --filter @anno/web gen:avatars",
  "items": [
    {
      "id": "pixel-01",
      "label": "像素头像 01",
      "options": { "hairVariant": "short04", "skinColor": "f4c7a1" }
    }
  ]
}
```

`options` 里显式固定每个头像的 `hairVariant` / `clothesVariant` / `eyesVariant` / `mouthVariant` 与 `skinColor` / `hairColor` / `clothingColor`，不依赖 seed 撞运气：32 张全部可预测、可复核、可单独手改。配色从三张小调色板（6 种肤色、8 种发色、8 种衣色）按索引组合，帽子/眼镜/胡须按约 1/3、1/4、1/5 的比例分配，保证形象多样而不怪诞。

数量取 32（本期固定，后续按 §5.4 只追加）：8 列 × 4 行的选择网格刚好一屏，单张 SVG 约 2–4 KB，总体约 100 KB，无缩略图生成成本（浏览器直接缩放同一份 SVG）。

### 5.4 命名与 id 稳定性

`pixel-01`…`pixel-32` 是**永久 id**。已入库的 `avatar_ref` 指向这些 id，因此：删除某张要同时清掉所有引用它的 `avatar_ref`；新增只能追加 `pixel-33` 起的编号，不能重排或复用。该约束写入实施说明与 manifest 顶部注释字段。

### 5.5 被否方案

| 方案                                               | 否掉的原因                                                                                                             |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| 运行期调 DiceBear HTTP API                         | 引入外部依赖与隐私外泄（每次渲染都会把用户标识发给第三方）、离线/内网部署不可用、CSP `img-src https:` 虽允许但不该依赖 |
| 运行期打包 `@dicebear/core` + `@dicebear/styles`   | 为 32 张固定图片引入数百 KB 运行期依赖与首帧 JS 开销，收益只是省下提交 100 KB 静态产物                                 |
| 手绘 32 个形象（对齐桌宠 `PixelHumanSprite` 风格） | 零许可负担，但美术工作量最大且质量不可控；`PixelHumanSprite` 只有 1 个形象，撑不起「供选择」的多样性                   |

### 5.6 主题对比度

内置头像保持透明背景，沿用现有 `bg-muted` 圆片底 + 中性描边环，因此深浅两套主题下都有一致的中性基底。发色/衣色调色板刻意避开极暗与极亮两端，实施时须在深浅两套主题下逐张目视确认（§10），并对同一 `Avatar` 尺寸（`sm`/`md`/`lg`）检查像素边缘是否清晰（SVG 用整型坐标，`shape-rendering` 由 DiceBear 输出的 `shape-rendering="crispEdges"` 保留，勿在优化阶段删掉）。

## 6. 前端改动

### 6.1 组件

| 文件                                              | 作用                                                                                                                                                                                         |
| ------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `apps/web/src/utils/avatar.ts`（新）              | `resolveAvatarUrl(ref)`：`preset:` → `/avatars/pixel/<slug>.svg`；`upload:` → `/api/v1/avatars/<token>`；非法/空 → `null`                                                                    |
| `apps/web/src/components/ui/UserAvatar.tsx`（新） | 图片优先、失败回退首字母：`<img onError>` + 常驻首字母底层（不引入 Radix Avatar 原语，见 §14.3）；props = `{ user: { id?, name?, email?, avatar_initial?, avatar_ref? }, size, className? }` |
| `apps/web/src/components/ui/Avatar.tsx`           | 保持首字母原语不变（纯文字场景与审计 actor 等无 ref 的位置继续使用）                                                                                                                         |

`UserAvatar` 的尺寸档位沿用现有 `sm/md/lg` 类名表（`avatarSizes.ts` 单一来源），视觉与 `Avatar` 完全一致，唯一差别是 `resolveAvatarUrl` 返回 URL 时叠加 `<img>`（`alt=""`，`aria-hidden`，因为它与旁边或 tooltip 中的姓名重复）。首字母常驻底层，因此图片加载中/失败都能自然回退，无需额外状态。

手写的 TS 类型同步补 `avatar_ref?: string | null`：`apps/web/src/api/auth.ts::MeResponse`、`apps/web/src/types/index.ts::UserBrief`（+ `AssigneeAvatarStack` 内的 `AssigneeBrief`）；其余 payload 类型由 `pnpm codegen` 从 OpenAPI 快照生成，不手改。

### 6.2 设置页

`apps/web/src/pages/Settings/SettingsPage.tsx::ProfileSection` 的「基本资料」卡内新增头像行（自上而下：当前头像预览 + 「上传图片」「选择内置头像」「恢复默认」三个动作），新增组件：

- `apps/web/src/components/users/AvatarSettingsCard.tsx`（或就地内联，取决于最终布局）：上传用 `<input type="file" accept="image/png,image/jpeg,image/webp">`，客户端先做类型/大小预检并提示中文原因，再上传；上传中显示进度与禁用态。
- `apps/web/src/components/users/AvatarPickerDialog.tsx`：`Dialog` + 8 列网格，每格是 `pixel-NN.svg`；当前选中项有明确选中态；底部「恢复默认」；`preset` 与 `upload` 都能被替换。
- 上传沿用仓库既有 multipart 写法：**不能用 `apiClient`**（它固定 `Content-Type: application/json`，见 `apps/web/src/api/client.ts`）。参考 `apps/web/src/api/ml-backends.ts:356` 的 `fetch` + `FormData` 写法；若要做进度条，用 `apps/web/src/api/datasets.ts:173` 的 `XMLHttpRequest` 先例。

新增 hooks：`apps/web/src/hooks/useMe.ts` 补 `useUploadAvatar` / `useSetAvatarRef`；成功后 `setUser(updatedUser)`（`authStore` 已持久化，顶栏立即更新）并失效 `["me"]`。

内置头像列表由 `useAvatarPresets()` 拉取 `/avatars/pixel/manifest.json`（React Query，`staleTime: Infinity`）；manifest 拉取失败时选择器显示空态与错误提示，不影响上传与恢复默认。

### 6.3 铺开清单

按已确认范围（核心身份面 + 成员/用户列表），把「自算首字母」换成 `UserAvatar`：

| 位置                                                                         | 数据来源                                              |
| ---------------------------------------------------------------------------- | ----------------------------------------------------- |
| `components/shell/TopBar.tsx:184`                                            | auth store 的 `MeResponse`（新增 `avatar_ref`）       |
| `components/ui/AssigneeAvatarStack.tsx:56`（任务/批次责任人）                | `UserBrief`（已在 §3.3 覆盖，`AssigneeBrief` 加字段） |
| `components/AnnotationHistoryTimeline.tsx:122`                               | `HistoryEntry.actor: UserBrief`（免费获得）           |
| `pages/Projects/sections/MembersSection.tsx:71`                              | `ProjectMemberOut`                                    |
| `pages/Projects/sections/BulkReassignModal.tsx:164`                          | `ProjectMemberOut`                                    |
| `components/projects/ProjectDistributeBatchesModal.tsx:317`                  | `ProjectMemberOut`                                    |
| `components/projects/BatchAssignmentModal.tsx:229`                           | `ProjectMemberOut`                                    |
| `components/projects/AssignMemberModal.tsx:131`、`steps/Step6Members.tsx:99` | 用户列表 `UserOut`                                    |
| `pages/Users/UsersPage.tsx:696/998/1093`                                     | `UserResponse`（`UserOut`）                           |
| `pages/Dashboard/ProjectFilterPanel.tsx:197`                                 | `UserResponse`                                        |
| `pages/Admin/AdminPeoplePage.tsx:331`                                        | `AdminPersonItem`（§3.3 覆盖）                        |
| `pages/Projects/data-manager/ProjectMembersPerformance.tsx:1011`             | `ProjectMemberPerformance`（§3.3 覆盖）               |

保留纯首字母的位置：评论/Issue/通知/审计（payload 只有 `author_name` / `actor_email`）、`OffboardingDialog`（临时弹窗）、`CommandPalette`（结果既有用户也有项目）。项目 owner（`ProjectGrid` / `AdminProjectsDashboard` / `OwnerSection` / `DashboardPage` 活动流）若 §3.3 的 `owner_avatar_ref` 落地则一并切换，否则保持首字母——这是本阶段唯一可裁剪项。

### 6.4 状态与缓存

- 自己的头像：`useAuthStore.user` 更新即全局生效（顶栏、个人资料预览）。
- 他人头像：`avatar_ref` 随 payload 返回，URL 稳定，因此不需要为改头像做全局失效；列表/成员数据原本的重取节奏足够。
- 头像 URL 是 128 位 token，不具备语义，不做任何前端持久化缓存。

### 6.5 设计系统约束

- 只用语义 token：`bg-muted`、`border-border`、`text-muted-foreground`、`rounded-full`；不引入裸色/任意值色（`apps/web` 的 CSS token 检查会拦）。
- 选择器网格使用现有 `Dialog` 与紧凑字号（`text-xs` / `text-2xs`），不新增图标体系（Lucide 现有 `upload` / `user` / `refresh` 足够）。
- 改动后跑 `pnpm --filter @anno/web lint`（含 `lint:css-tokens`）。

## 7. 后端改动清单

| 文件                                                       | 改动                                                                                                                               |
| ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `apps/api/alembic/versions/0172_*.py`（新）                | `users.avatar_ref` 加列                                                                                                            |
| `apps/api/app/db/models/user.py`                           | `avatar_ref` 列映射                                                                                                                |
| `apps/api/app/schemas/user.py`                             | `UserBrief.avatar_ref`、`UserOut.avatar_ref`                                                                                       |
| `apps/api/app/schemas/me.py`                               | `AvatarRefUpdate`（`extra="forbid"`，`avatar_ref: str \| None`）                                                                   |
| `apps/api/app/schemas/project.py`、`schemas/dashboard.py`  | 成员/绩效/管理端人物与项目 owner 的 `avatar_ref`                                                                                   |
| `apps/api/app/services/avatar.py`（新）                    | 引用语法解析与构造、对象 key、旧对象清理                                                                                           |
| `apps/api/app/services/avatar_image.py`（新）              | 纯函数：校验 + 转正 + 方裁 + 缩放 + WebP 编码                                                                                      |
| `apps/api/app/services/user_brief.py`                      | `_to_brief` 补 `avatar_ref`                                                                                                        |
| `apps/api/app/api/v1/me.py`                                | `POST/PATCH/DELETE /avatar`                                                                                                        |
| `apps/api/app/api/v1/avatars.py`（新）+ `api/v1/router.py` | `GET /avatars/{token}`                                                                                                             |
| `apps/api/app/middleware/upload_body_limits.py`            | 新端点预解析上限                                                                                                                   |
| `apps/api/app/services/audit.py`                           | 复用 `USER_PROFILE_UPDATE`，`detail={"field": "avatar", "avatar_ref": ...}`（不新增 action，避免 `auditLabels.ts` 与审计筛选联动） |
| `apps/api/app/api/v1/users.py`                             | **无需改动**：账号删除是软删除（`is_active=False`），按 §4.4 保留头像                                                              |

独立 `avatars` 桶的接线（config / storage / 存储管理页 / 环境变量 / 生产 Caddy 与 MinIO 策略 / 运维手册）见 §4.3.2 的清单，实施时逐项打勾。

## 8. 生成物与文档

| 类别       | 动作                                                                                                                                                                                                     |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| API 契约   | `pnpm openapi:export` 重新生成 `apps/api/openapi.snapshot.json`（CI 有 `openapi:check`）                                                                                                                 |
| 前端类型   | `pnpm codegen` 在本地重新生成 `apps/web/src/api/generated/types.gen.ts`；该目录**被 gitignore**（唯一版本化契约是 openapi snapshot），不需要也不应该提交                                                 |
| Python SDK | `packages/python-sdk/src/ai_annotation/models.py` 的 `UserBrief`（`:554` 附近）补 `avatar_ref`；确认 `tests/test_openapi_contract.py` 与 `api-coverage.toml` 仍通过；不改 SDK 版本                       |
| 用户手册   | `docs-site/user-guide/reference/settings.md`「个人资料」增补头像说明（上传限制、内置头像、恢复默认）                                                                                                     |
| 开发者参考 | `docs-site/dev/reference/storage-buckets.md`：新增 `avatars` 桶行；桶数从「共 5 个」改为实际数量，并补上漏写的 `import` / `export`；写明头像桶永久保留、纳入备份、不挂 lifecycle，以及 §4.6 的可见性边界 |
| 运维手册   | `docs-site/ops/deploy/docker-compose.md` 变量表加 `MINIO_AVATARS_BUCKET`；`docs-site/ops/deploy/lan-production.md` 的「七个桶」全部改为八个并加 `prod-avatars` 行（预建桶、备份范围、边缘代理说明同步）  |
| 生成物登记 | `docs-site/dev/reference/generated-artifacts.md`：登记 `apps/web/public/avatars/pixel/**`（真值源 = 生成脚本 + manifest，Git 跟踪）                                                                      |
| 变更日志   | `CHANGELOG.md` 的 `## [Unreleased] / ### Added` 增条目（用户可感知：可上传头像、可选用内置像素头像）                                                                                                     |
| 文档截图   | 个人资料页新增头像行会影响 `docs-site/user-guide/images/settings/profile.png`。已按 §2 环境重截并复核，但因截图清单要求完整矩阵运行而回退，转为后续工作（§14.4）                                         |

## 9. 分阶段实施

| 阶段 | 内容                                                                                                                                    | 独立可交付                     |
| ---- | --------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------ |
| P1   | 数据模型 + 读取契约 + `avatars` 桶接线（§4.3.2）+ 三个 `/auth/me/avatar` 端点 + `GET /avatars/{token}` + 中间件上限 + 单测 + 契约生成物 | 是：API 可用，前端仍显示首字母 |
| P2   | 生成脚本 + 32 张 SVG + manifest + 一致性测试 + 许可/来源文档                                                                            | 是：静态资产可独立 review      |
| P3   | `resolveAvatarUrl` + `UserAvatar` + 设置页上传/选择/恢复 + hooks + 组件测试                                                             | 是：单个用户端到端可用         |
| P4   | 铺开到成员/用户/绩效列表（§6.3）+ 文档 + 截图刷新 + e2e                                                                                 | 是：协作场景可见               |

P1 与 P2 无依赖，可并行；P3 依赖 P1、P2；P4 依赖 P3。

## 10. 验收标准

**后端单测**（`apps/api/tests/test_me_avatar.py`、`test_avatars_route.py`，`storage_service` 按仓库既有方式打桩）

- 上传合法 PNG/JPEG/WebP → 200，`avatar_ref` 变 `upload:<32hex>`，对象写入 `avatars` 桶的 `<token>.webp`，响应 `ContentType=image/webp`。
- 非图片（伪造 `image/png` 的文本）、超大像素（解压炸弹）、过小尺寸、超过 2 MB、不支持的格式 → 4xx，且**不改库、不写对象**。
- `PATCH` 接受合法 `preset:` 与 `null`；拒绝 `upload:...`、`preset:../../x`、`preset:` 大写与超长 → 400。
- 重复上传/切到 preset/恢复默认会删除旧 `upload:` 对象；删除失败不影响本次 200。
- 账号软删除（`DELETE /users/{id}`）不改动头像引用（§4.4）。
- `GET /avatars/{token}`：合法 token 200 + `Cache-Control`；非法 token（含 `..`、非 hex、超长）与不存在 token → 404；`If-None-Match` 命中 → 304。
- 桶接线：`ensure_all_buckets()` 覆盖 `avatars`；`storage_service.list_all_buckets()` 含该桶；`GET /api/v1/storage/buckets` 返回 `role="avatars"`。

**前端测试**

- `utils/avatar.test.ts`：三种 ref 的解析、`null`/畸形 ref 返回 `null`。
- `UserAvatar.test.tsx`：有 URL 渲染 `<img>`、`onError` 回退首字母、无 ref 直接首字母。
- `AvatarPickerDialog.test.tsx`：选中某项触发 mutation、pending 中禁用重复提交、失败有提示。
- manifest 一致性测试（`node:fs` 读目录）：manifest 的 id 与实际 SVG 文件 1:1、id 唯一、命名连续。

**浏览器手工验收**（深/浅两套主题，1440 与 390 宽度）

- 设置页：上传一张竖图 → 预览为方裁 256×256、方向正确、无变形；选内置头像 → 顶栏立即更新；恢复默认 → 回到首字母；刷新后保持。
- 内置头像网格：32 张全部加载、无 404、无破图；深浅主题下边缘清晰、不糊、对比度可辨；键盘可达（Tab/Enter/Esc）。
- 任务/批次责任人头像组、项目成员列表、用户管理列表：有头像者显示图片，无头像者显示首字母，混合排列不错位、不抖动。
- 评论/审计等未铺开处保持原样（回归：首字母仍正确）。

**检查命令**

- `pnpm lint`、`pnpm typecheck`、`pnpm test`、`pnpm --filter @anno/web lint:css-tokens`
- `pnpm openapi:check`、`pnpm codegen`（无 diff）
- `cd apps/api && uv run pytest tests/test_me_avatar.py tests/test_avatars_route.py`
- `git diff --check`

## 11. 风险与回滚

| 风险                                   | 应对                                                                                                                                                     |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 头像 URL 泄露后被长期匿名读取          | 已确认接受（§1.1/§4.6）；token 128 位不可枚举、不含用户 ID；`Cache-Control` 限 24h；边界写进开发文档                                                     |
| 新桶接线漏项导致生产不可用             | 最容易漏的是 `infra/docker/Caddyfile.lan-prod` 白名单（漏则头像 404）与 `lan-production-minio-policy.json`（漏则 403）；§4.3.2 清单 + §10 的生产验收覆盖 |
| 误给 `avatars` 桶加整桶 lifecycle 规则 | §4.3 的显式警告；实施时复查 `_ensure_lifecycle` 只包含原有四个桶                                                                                         |
| 上传成功但 DB 提交失败留下孤儿对象     | 量级极小（每次失败一个几十 KB）；§4.4 记录，不做实时 GC                                                                                                  |
| 生成头像重跑产生无意义 diff            | manifest 不含时间戳、选项全部显式固定；脚本提供 `--check` 模式                                                                                           |
| 已入库的 preset id 被删除或重排        | §5.4：id 永久、只能追加；删除需同时清理 `avatar_ref`                                                                                                     |
| 深色主题下个别头像对比度不足           | §5.6 调色板避开两端 + §10 的双主题逐张验收                                                                                                               |
| 个人资料页截图与 e2e 视觉基线失效      | P4 明确重截并走媒体复核；`pnpm --filter @anno/web screenshots:regression:update` 只在确认视觉正确后执行                                                  |

回滚：前端回退即恢复首字母；`avatar_ref` 列可空、可继续保留（不影响旧代码）；`avatars` 桶可保留（不占空间、无 lifecycle 会过期）或按 §4.4 清理。无数据迁移，无停机。

## 12. 明确不在本次范围

- 评论、Issue、通知、审计日志、Dashboard 活动流的头像（它们的 payload 只有 `author_name` / `actor_email`，需要另外扩展，属后续独立计划）。
- 客户端裁剪/缩放编辑器（本版为服务端居中方裁；需要用户自选构图时另做）。
- 动态头像、按项目区分头像、API key 头像、Gravatar/外链头像。
- 新用户自动分配随机内置头像（会牺牲「恢复默认 = 首字母」的可预期性）。
- 图片内容审核（涉黄涉政检测）；当前只做格式与规模校验。
- 追加内置头像风格（`Pixel Art Neutral` / `Pixelbot` / `Voxel Art`）与数量扩充：本期固定 32 张，上线后按反馈决定（§5.4 的 id 只追加规则已为此留好空间）。

## 13. 决策记录

1. `GET /api/v1/avatars/{token}` 匿名能力 URL：**已确认接受**（§4.6 按此实现）。
2. 头像存储：**已确认新建独立 `avatars` 桶**（§4.3.2 完整接线清单）。
3. 内置头像：**本期 32 张**，含帽子/眼镜/胡须的多样性；是否追加风格或数量留待上线后按反馈评估。

## 14. 实施记录（与计划的差异）

计划已按 P1–P4 全部落地，并在真实浏览器与隔离截图环境完成验收。实施中发现并修正了几处计划假设与仓库接线缺口：

| 计划假设                                           | 实际情况                                                                                                          | 处理                                                                                |
| -------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| 账号硬删除时清理头像对象                           | `DELETE /users/{id}` 是**软删除**（`is_active=False`），全仓没有硬删除路径                                        | 删除该改动项；账号删除保留头像，写入 §4.4 与开发文档                                |
| 需要提交 `apps/web/src/api/generated/types.gen.ts` | 该目录被 `apps/web/.gitignore` 忽略，唯一版本化契约是 `apps/api/openapi.snapshot.json`                            | 只提交 snapshot；`pnpm codegen` 本地生成即可                                        |
| §4.3.2 接线清单**漏了工作树启动器**                | `scripts/worktree_env.py::BUCKETS` 只覆盖 7 个桶 → 新桶不隔离、`destroy` 不清理，违反「按模式隔离 bucket」契约    | 补 `MINIO_AVATARS_BUCKET`；桶数断言 7 → 8；工作树文档同步（`8557ee5f`）             |
| 可能需要在 docker-compose 中显式声明新桶变量       | 生产叠加文件用 `env_file: .env.production`（由 `.env.example` 复制）注入全部 `MINIO_*`；dev 走 `config.py` 默认值 | compose 文件无需改动；`docker-compose.md` 变量表与 `lan-production.md` 桶清单已同步 |
| `test_audit_logs.py::test_pagination` 是稳定测试   | 该用例传 `limit=5`，但列表端点分页契约是 `page` / `page_size`（`limit` 只属于导出端点），审计行超过 5 条即失败    | 修正为 `page_size=5`（既有陈旧用例，与本功能无关但会被本功能的审计行触发）          |
| —                                                  | `SettingsPage.test.tsx` 依赖「空值 input 下标」选密码框，新增头像文件选择框后命中错误元素                         | 改为按 label 查询；补 `useMe` 新 hooks 的 mock 与 `QueryClientProvider`             |

另外把 `UploadBodyLimitMiddleware` 的上限选择从五层嵌套三元改为表驱动（本次新增一条分支后嵌套已不可读），行为逐字不变。另外 `e2e/screenshots/outputs/` 同样被 gitignore：截图登记表只在本地生成，不随截图提交。

### 14.1 本机验收环境的两处既有工具链限制

截图环境在本机绕开了两个与本次改动无关的环境问题，均用非侵入方式解决（不改系统安装、不改仓库默认配置）：

1. **本机 Homebrew ffmpeg 9.0.1 没有 WebP 编码器**（`ffmpeg -encoders` 无 `libwebp`），而截图 seed 的视频 poster 生成依赖它：报错 `Default encoder for format webp (codec webp) is probably disabled`。解决：`x env use ffmpeg` 取到带 `libwebp` 的 ffmpeg v6.0.0，仅在 seed 与 worker 进程的 `PATH` 前置，`ffprobe` 仍用本机版本。
2. **Docker Hub 不可达**（`python:3.11-slim` 拉取 EOF），`screenshot-ml-stub` 无法构建。解决：该 stub 只依赖 fastapi/uvicorn/pydantic，直接用 API venv 在宿主机 `127.0.0.1:9100` 运行 `docs-site/dev/examples/mock-v2-backend/main.py`，seed 传 `--ml-backend-mode stub --ml-backend-url http://127.0.0.1:9100`。

这两条值得补进截图环境排障文档（本机无 GPU + Docker Hub 受限时的替代路径）。

### 14.3 入口包预算触发的实现调整（CI 反馈）

首次 PR CI 的 `Frontend verification / Bundle size check` 失败：入口 chunk **703.2 KB / 700.0 KB**，而 `origin/main` 当时是 **699.8 KB / 700.0 KB**——预算只剩 0.2 KB 余量，任何新增入口代码都会超。

两处不损失行为的削减（共约 2.2 KB）：

1. `UserAvatar` 不再引入 Radix Avatar 原语（约 +3 KB），改为 `<img onError>` + 常驻首字母底层。代价：不再复用 `components/shadcn/ui/avatar.tsx`（该文件此前也是死代码），回退逻辑自持约 10 行。
2. 内置头像选择器改为 `React.lazy` + 按需挂载（仅 `pickerOpen` 时渲染），把选择器与目录请求移出入口 chunk（与 `TopBar` 懒加载 `NotificationsPopover` 同一写法）。

削减后仍需 **+1.6 KB**（15 处展示点接入 + 引用解析 + 展示组件），因此按仓库既有做法上调主包预算 `700 → 712 KB`（历史先例：`600→620`、`660→700`、dockview `300→400`，均随功能提交一起上调）。

### 14.2 浏览器验收结果（开发栈 API 8100 / Web 3100，admin/123456）

| 验收项            | 结果                                                                                                                                                            |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 上传竖图 600×1200 | 存为 **256×256 WebP**（1346 B）；取像素验证顶部仍为红、中部绿、底部蓝 → 居中方形裁剪、方向未丢、无拉伸                                                          |
| 头像读取（匿名）  | `GET /api/v1/avatars/{token}` 无 `Authorization` 返回 200 + `image/webp` + `Cache-Control: public, max-age=86400`；带 ETag 复取 304；非法/超长/非 hex token 404 |
| 内置头像          | `/avatars/pixel/pixel-07.svg` 由前端静态服务返回 `image/svg+xml`；选择后顶栏与预览立即更新                                                                      |
| 选择器            | 深色与浅色两套主题各 32 张全部渲染、无破图、选中项有明确描边；版权与「立即生效」说明可见                                                                        |
| 协作面            | 用户与权限列表出现带图片的头像行，同表其余用户仍为首字母（混排不错位）；项目负责人无头像时回退首字母                                                            |
| 恢复默认          | 无头像时按钮禁用；有头像动作后可用（组件测试覆盖清除路径）                                                                                                      |

### 14.4 文档截图：已捕获复核但**回退**（清单不变量）

个人资料页截图确实按 §2 的隔离环境重截并复核过，但**不能**随本 PR 落地，原因是截图清单的不变量：

- `apps/web/e2e/screenshots/outputs/manifest.json` 是**受版本控制**的（仅该目录内的临时产物被忽略；`git add <目录>` 会被拒绝，必须显式 `git add <文件>`——本次就是这么漏掉的）。
- `manifest-reporter` 只在**完整矩阵**（全部 matrix project + 全部 scene target）跑完后才重写清单；单场景 / 单 project 运行**故意不更新**，因此 `docs-site/scripts/check-image-manifest.mjs --strict` 会因「SHA-256 与磁盘文件不一致」失败。
- 用本机跑完整 `desktop-light` 矩阵验证可重复性：**65 张里 38 张字节不同、还有 3 个 scene 失败**（本机 fixture/stub 与既有基线机器不一致）。也就是说，想让清单被工具合法更新，就必须连带重写 38 张无关截图并逐一重新复核。

因此本 PR 回退该截图（连同 `media-reviews.json` 的复核记录），把「个人资料页截图加入头像行」列为后续工作：需要一次完整的截图矩阵运行 + 38 张图的复核，或由维护者明确同意后做单条目的清单修补。

### 14.5 PR review 反馈：头像依赖随 Node 基线一起收口

Codex review 指出 `@dicebear/core@10` 声明 `node>=22`，与仓库当时的 Node 20 基线矛盾。最初的处理是把生成链降到声明 `node>=18` 的 DiceBear v9 并重生成 32 张产物；随后独立的基础设施改动把仓库/CI 基线升到 **Node 22**（与生产镜像 `node:22-alpine` 一致，见 `docs/plans/1789454560_node-baseline-22.md`）。基线落地后，本 PR **回退到 DiceBear v10 + `@dicebear/styles`**，恢复原 32 张头像外观；`gen:avatars --check` 在 v10 下仍字节一致。

## Outcome

- Landed commits: `1d0fc8ee`（主体）、`e695f6db`（计划记录）、`8557ee5f`（工作树桶隔离）、`7db7f197`（头像组测试）、`fada5ffd`（合并 main 解冲突）、`8608f174`（入口包削减 + 预算上调）；`632686c3` / `7b52ecc0` 的截图重截与复核**已回退**（§14.4）
- Release milestone: Not yet determined
- User documentation: `docs-site/user-guide/reference/settings.md`（个人资料「头像」）
- Developer documentation: `docs-site/dev/reference/storage-buckets.md`（`avatars` 桶 + 可见性边界）、`docs-site/dev/reference/generated-artifacts.md`（像素头像生成物）、`docs-site/ops/deploy/{docker-compose,lan-production}.md、`docs-site/dev/how-to/worktree-environments.md`与`docs-site/dev/concepts/runtime-environments.md`（桶数）
- ADR: 无（分桶与免鉴权读取的取舍记入 `storage-buckets.md` 与 `apps/api/app/api/v1/avatars.py` 模块注释，未达到 ADR 门槛）
- CHANGELOG: Unreleased / Added 已加条目
- Remaining work:
  1. **个人资料页文档截图**加入头像行：需要一次完整截图矩阵运行 + 连带变化图片的复核，或维护者同意后做单条目清单修补（§14.4）。
  2. 评论 / Issue / 通知 / 审计日志的头像仍为文字或首字母（§12），需要为那 6+ 个 payload 单独扩展。
  3. Playwright e2e 未新增；选择器与头像组由 vitest 组件测试覆盖（含图片/首字母混排）。
  4. 本机 ffmpeg 缺 `libwebp`、Docker Hub 受限两条环境问题建议补进截图排障文档（§14.1）。
  5. CI 观察：`Frontend verification / vitest with coverage` 曾在 `ProjectDetailPanel.test.tsx` 的 OCR 用例上超时失败——即 `vitest.setup.ts` 注释里记录的已知 CI 满载抖动，与本改动无关；本地该文件通过。
