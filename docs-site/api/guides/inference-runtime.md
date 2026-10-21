---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-21
---

# Inference 命名编排运行配置

本页说明保存的命名预标编排如何选择数据 owner，以及如何在本地启用可选的 Inference HTTP runtime。该进程只承载命名编排的私有读写协议，不执行模型推理或 Celery 预测任务。它描述 API 与 owner 之间的私有边界，不新增终端用户 API。

命名 `ProjectPipeline` 与 Project 内嵌的旧 `preannotate_pipeline` 是两个不同能力：前者由 Inference 承担；后者继续由 Workspace 管理。六个公开命名编排路由与 DTO 保持不变：

- `GET` / `POST /api/v1/project-pipelines`
- `GET` / `PUT` / `DELETE /api/v1/project-pipelines/{pipeline_id}`
- `POST /api/v1/projects/{project_id}/pipelines/apply`

## 选择一个数据 owner

API 默认借用当前请求的 SQLAlchemy Session 和事务。不设置下表中的 selector 即保持该模式；现有 API 启动不依赖独立 Inference 进程。

| 模式          | API 配置                                           | 事务归属                     |
| ------------- | -------------------------------------------------- | ---------------------------- |
| 借用 API 事务 | URL、DSN 和 DSN 文件 selector 留空                 | API 在成功命令后提交一次     |
| 专用 DSN      | 只设 `INFERENCE_PIPELINES_DATABASE_URL` 或 `_FILE` | Inference 专用数据库连接提交 |
| 私有 HTTP     | 设 `INFERENCE_PIPELINES_URL` 和控制 token          | Inference runtime 提交       |

URL 与 DSN 不能同时选择。直接值和它的 `_FILE` 形式也不能同时设置。显式选择的 owner 配置错误或不可用时 API 返回服务不可用，不回退到借用 Session 或另一种远程模式。

### 使用专用 DSN

API 进程直接连接当前 PostgreSQL，但使用专为命名编排创建的 `inference_pipelines_client` LOGIN。凭据应通过 secret file 提供：

```dotenv
INFERENCE_PIPELINES_URL=
INFERENCE_PIPELINES_DATABASE_URL=
INFERENCE_PIPELINES_DATABASE_URL_FILE=/absolute/path/to/inference-pipelines-client-dsn
INFERENCE_PIPELINES_TIMEOUT_SECONDS=30
```

该账号只能执行私有 `aap_inference_pipelines_v1` schema 中的 `pipeline_policy()` 与 `pipeline_step(jsonb,text,jsonb,text[],text[])`。先应用 migration 0271 和 `infra/docker/inference-pipelines-grants.sql`；不要给它 `project_pipelines`、回执表或其他 owner 的原表权限。若 API 运行于宿主机，`_FILE` 必须指向宿主机 API 进程可读的 secret 文件。

### 使用私有 HTTP runtime

可选 Compose 服务在容器内监听 8768，并将宿主机端口限制映射到 `127.0.0.1:8768`。以下例子假设 API 进程运行在宿主机上：

```dotenv
# API 进程的环境
INFERENCE_PIPELINES_URL=http://127.0.0.1:8768
INFERENCE_PIPELINES_TOKEN_FILE=/absolute/path/to/inference-control-token
INFERENCE_PIPELINES_DATABASE_URL=
INFERENCE_PIPELINES_DATABASE_URL_FILE=
INFERENCE_PIPELINES_TIMEOUT_SECONDS=30

# Compose 从宿主机读取这两个文件，并只读挂载给 inference 容器
INFERENCE_RUNTIME_PIPELINES_DATABASE_URL_FILE=/absolute/path/to/inference-pipelines-client-dsn
INFERENCE_RUNTIME_CONTROL_TOKEN_FILE=/absolute/path/to/inference-control-token
```

使用仓库现有 Compose 文件启动可选服务：

```sh
docker compose -f docker-compose.yml -f docker-compose.inference.yml up -d --build inference
```

Compose 将 DSN 和 bearer 分别挂载到容器内的 `/run/secrets/inference_pipelines_database_url` 与 `/run/secrets/inference_control_token`，再设置 `INFERENCE_PIPELINES_DATABASE_URL_FILE` 和 `INFERENCE_CONTROL_TOKEN_FILE`。不要把 secret 内容写进 `.env`、镜像或命令行参数。API 使用的 token 文件和 runtime 控制 token 文件必须提供相同的 bearer 值。

如 API 也运行在 Docker 网络中，应让 API 与该服务加入同一网络，并把 URL 改为 `http://inference:8768`；同时将控制 token 文件单独挂载给 API。上面的 Compose override 只为 `inference` 服务挂载这两个 secret。

`apps/inference/Dockerfile` 以 UID/GID 10008 运行。Compose 服务使用只读根文件系统、32 MiB `/tmp`、丢弃 Linux capabilities、`no-new-privileges`、64 PID 上限、1 CPU 和 256 MiB 内存限制。容器内绑定 `0.0.0.0:8768` 仅供 Compose 网络访问，宿主机发布仍限制在 loopback。runtime 默认 active-request 上限为 8，可由 `INFERENCE_MAX_CONCURRENCY` 调整；超过上限的请求立即收到 busy/unavailable 响应，不进入等待队列。请求与响应 body 上限各为 16 MiB。

`GET /health` 会验证 runtime 配置、专用数据库连接、函数权限与 Native policy；它不是公开业务端点。私有 bearer 保护 `POST /v1/pipelines/execute`、`/query` 和 `/status`。终端用户仍只访问上表列出的 `/api/v1` 路由。runtime 直接启动时默认绑定 `INFERENCE_HOST=127.0.0.1`、`INFERENCE_PORT=8768`；Compose 显式将容器绑定改为 `0.0.0.0`，再通过宿主机 loopback 发布。

## 权限、事务与恢复

API 先用现有 Identity 认证构造 actor ID 与严格字符串 role 快照；合法平台 role 包括 `super_admin`、`project_admin`、`employee` 与 `viewer`。普通 list/CRUD 保留该初始快照，不在 Inference 层增加账号活跃状态检查。Project 可见性依旧在其原有读取阶段调用 Workspace Project Query。

Apply 的公开 `require_project_owner` 检查仍在读取源编排之前执行。之后 Inference 按原顺序读取来源、校验 stages、查询 backend registry/pool/project enablement，再通过 Workspace 的固定能力按账号、项目、必要时成员的顺序重新读取当前权威并持锁到事务结束。Native 的每个阶段仍是单独语句，保留 READ COMMITTED 快照；Workspace facts 与最终 pipeline 写入共享一个 PostgreSQL 事务。

借用模式中，manager 返回 `caller_pending`；API 随即提交一次，再按完整原命令回执读取新的流水线行投影，并通过 Identity UserBrief 取创建者显示名。专用 DSN 和 HTTP owner 只有在自己的提交完成后返回 `committed`，API 不重复提交。apply 响应仍返回 `created_by_name: null`。回执绑定投影本身不要求外层事务已经提交；如果在调用方外层事务或 savepoint 中读取，它只证明当前事务可见的结果，不代表数据库已提交。

HTTP 执行收到含糊确认时，`PipelineHttpClient` 最多发送一次同命令 status 查询，不会重新发送写命令。status 只把其他已提交事务的回执报告为 `committed`；当前创建事务中的未提交回执为 `unknown`，这不等于确认回滚。未知结果返回 unavailable。公开重试会生成新操作 UUID，本 API 没有新增公开幂等键。

Pipeline 行 codec 会保留 `stages` 的 JSONB `null` 与标量，不提前改写历史值。非空 scalar 在 apply 的既有 `validate_saved_pipeline` 阶段失败；JSONB `null` 会通过该函数的旧空值快捷路径，随后在 backend 阶段迭代失败。list/detail 的 `ProjectPipelineOut.stages` 仍在 public response 阶段要求对象数组；mutation 的新行投影与 Identity 名称补充发生在提交后，所以 response validation failure 仍可能出现在提交之后。此处不把坏值提前清洗，也不声称预处理与旧失败时点等价。

## 当前边界

本页描述的代码与 opt-in 配置已经存在。借用事务、专用 DSN、HTTP 的冻结 API 回放、真实权限锁冲突、丢响应恢复、重启、非空备份恢复、独立安装包、受限容器和完整 migration 均已通过限定本地验收。Compose 的 CPU/内存限制是配置值，不代表生产容量或 SLO 验收。生产配置没有在本页范围内变更。该切片只覆盖保存的命名编排，不表示完整 Inference 模块已经可以任意更换语言；已有 Go 月报实验仍是独立尝试。

架构决策见 [ADR-0146](/dev/adr/0146-inference-named-pipeline-owner)。
