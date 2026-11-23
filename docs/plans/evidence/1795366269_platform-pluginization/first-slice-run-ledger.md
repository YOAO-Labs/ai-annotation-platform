# 平台插件化首切片运行账本

## 2026-11-23：M1a 直连与 M2a 盘点

**基线与范围。** 平台代码 `ef8d3b2df2185a09491bc6f10f8ab2c576675fdb`。作者在仓库外的 CPython 3.12.11 虚拟环境仅安装 wheel，无平台源码依赖、数据库、对象存储密钥或平台管理凭据。运维端独立使用当前 API、真实 Inference HTTP owner、受限 PostgreSQL 登录和临时 Redis／MinIO。测试数据是一张有两个已知色块的 PNG；未预置 Registry、路由成功状态或预测。所有写入限本次可销毁资源，不使用维护者日常环境。

### 制品与输入

- 初始作者 SDK 候选 `35ed27ed`，manifest SHA256 `8950ef9dffd4447d4be3235c762b1d59daa74ae47bb89b379e8800744e480f29`；初始便携契约候选 `64f1cef9`，manifest SHA256 `3f971e84b5b808352131981a9759102dc363bea9e31e0be0d5ad47064e85a684`。安装前核对两份 manifest 的 8 个第一方 wheel；不是从 PyPI 安装或发布。初始 `yoao-protocol-v2` wheel 的 SHA256 为 `b06ca5eacad3165a4dfe32f492f951b914eb6fa0226079586b78659e41b7e0e2`。
- `yoao-plugin init` 生成 CPU 颜色检测器；构建、非 editable 安装，使用 `yoao-plugin dev` 检查多检测与空图。给生成的服务添加教程中的精确图片主机许可后，重建并重装作者 wheel，SHA256 `bffaffb40a754e45b75ea0cde0496de98b6608ec96bf228dcf5e4add4611105d`。不是 echo、预训练模型或 GPU 推理。
- 强制路由使用发行端 `scripts/build-plugin-contracts.py --revision ef8d3b2df2185a09491bc6f10f8ab2c576675fdb` 构建的当前五个契约 wheel，经 manifest `52a5feefa3d4e254a3a7fb14bca5bb41cfbaeed0ebc9fc8079598f8e13983ca5` 核验后，在同一作者环境离线替换。此代协议 wheel SHA256 `26c2884dc0e03f2ff568e11895afeb2b266ebf5403e842b0e9be5f265d29240a`；**两代发行名和包版本相同，不能代替摘要**。构建端使用已提交的平台源码；作者没有安装源码、sdist 或 API SDK。
- 原图 `multi.png`，100 × 100，SHA256 `722170eab51edcd9de65a5aabaf49f394d1d21c30cca000c490ab61a8a2f8ccb`。独立预期：`red-region [10,10,20,30]`、`green-region [50,40,30,50]`（百分比），分数均为 1。

### HTTP 与持久回执

1. `yoao-plugin dev` 实际只监听 `127.0.0.1`，本地 `yoao-plugin check` 八项通过。以回环地址调用全局注册 API 返回 **422**；仅把客户端地址改成网关不能改变监听接口。
2. 停止 `dev`，改用已安装的 `my-color-region-plugin serve` 监听专属私网接口。同网容器访问 `/health` 为 **200**，无服务 token 的 `/setup` 为 **403**。超管真实登录，以私网 URL 和手动 token 注册为 **201**；Registry `extra_params` 没有插件安装身份，未声明 GPU。实例健康检查返回 `ok`，作者端收到认证 `/setup`。
3. 单成员池有活动成员与能力指纹，但全局池最初停用。项目启用被 **409 `ml_backend_pool_not_enabled`** 拒绝；超管启用全局池后，项目显式启用返回 **200**。未修改表数据绕过启用。
4. 默认路由 `off`：项目管理凭据调用实际 `predict-test`，模型取回平台签名的原图，物理 `POST /predict` 和项目响应均 **200**；任务 ID、两框标签、坐标及分数与独立预期一致。路由账本为 `off / finished / response / 200`。
5. 强制路由负例：未绑定的首次请求被 **503** 拒绝，owner `begin` 的原因为 `continuity_unverified`，scope 已取消，没有物理 attempt。初始协议 wheel 只处理 `/v1/aap-routing/control`，对当前 `/v1/yoao-routing/control` 返回 **503**；原 `reconcile` 查得 `unknown`，没有把它记成成功。该时刻 owner 读数为零 open scope、零 uncertain attempt。
6. 强制路由正例：现场保留原控制 ID，停止自有旧进程，同一实例、同一作者 wheel 换上核验过的当前契约；以 `AAP_ROUTE_REGISTRY_ID` 与 `ROUTE_CONTROL_TOKEN_FILE` 启动。用同一操作恢复，真实 drain／bind 两次 HTTP **200**；命令与原状态查询均为 `committed / completed`，`continuity=ready`。重新观察健康后，项目预测返回同一任务和两框精确结果，账本为 `enforce / finished / response / 200`。未新建安装身份，未关闭强制策略，未向作者交付 routing owner bearer。

**证据索引：** Registry `33c34e58-1e19-453c-923f-afb6e7099ae9`，服务池 `df074a2f-583c-4c6e-9d68-78657dcc2816`，任务 `8014985f-6bde-4124-8980-9715335f0f4d`，路由控制 `1cabadd8-63c7-40dc-bf7e-62667eabdede`。三条 scope 均关闭，唯一控制完成。作者进程已停止，隔离栈按所有权正常退役。以上验证项目 HTTP 试预测，不验证浏览器操作、批量候选落库、托管安装退役或生产容量；没有发布 PyPI／OCI。

### 部署盘点

根目录 44 份 Compose 文件按 `services` 一级键去重，基础 15、基础与两个生产变体合计 20、全量 59。差集的 39 项及两个非 Compose 边界见[部署收敛决定表](../../../../docs-site/dev/concepts/module-deployment.md#deployment-convergence)。依据是对应入口、授权 SQL、运行选择器和文档；没有删除服务、运行全套 split 冷验收或改变日常拓扑。维护者确认第一批服务后才进入 M2b。

## M2b Workspace

2026-11-23，维护者确认的首批仅含成员管理、负责人移交、项目配置、模板查询、项目查询、成员目录查询。Workspace 继续拥有 SQL 能力和业务规则；API 不接收六项操作的专用数据库连接。Identity 资料与 Inference 凭据仍分别归各自的 owner。此项不授权迁移已拆分的日常实例。

| 根目录 Compose 指标 | M2a | 首批合入后 |
| ------------------- | --: | ---------: |
| 定义文件            |  44 |         35 |
| 去重服务键          |  59 |         53 |
| 基础档服务键        |  15 |         15 |
| 基础及生产变体      |  20 |         20 |

移除六个按操作拆分的服务定义，以及仅用于日常组合、跨模块改名、独立 Workspace 接入探测的三个叠加文件。`docker-compose.inference-probe.yml` 保留。

- **启动保护**：7 项通过。进程环境与 `.env` 中仍生效的旧 URL、DSN 或 DSN 文件会阻止 API 启动；错误只报变量名，不含值。独立 Inference 探测配置仍可用。
- **进程无关契约**：Workspace 操作、改名及访问协议与 manager 样例 202 项通过，共享读取和投影 21 项通过；Workspace Node peer 自检 19 项通过。
- **冻结 SQL 对照**：在本批代码的父版本上，3 项实库检查通过。普通登录不能直读私表、列、收据、辅助函数或序列；Node 使用同一权限。负责人移交被后续操作取代后仍能按原收据重放，无操作保持稳定。

**冷启动和数据库恢复。** 在同一无 `.env` 的新 checkout（`2218cdc2eb469476911a55b6b0707f5da15748cd`），为两档凭据分别创建隔离环境，以 `identity_api_consumer` 启动真实 API、以专用探测账号启动 Inference。两档都经 HTTP 验证六项操作：超管移交返回 200，原负责人试图转回返回 403；合法与非法 pipeline 分别返回 200 和 422，后者不改项目。另验质量晋升审计失败整体回滚、解绑和删除。两次均在 `pg_dump`／`pg_restore` 后重启进程，从副本读回新负责人、改名和模板；无效 bearer 返回 401。

- **默认 LAN**（`lan-restore.json`，备份 SHA256 `4a265e084ca6fcc39a9b98a51886a3b7fd9742ec34099d50cfc34c0763e3ddd0`）：真实上传、按字节读回并解绑指南均成功。另一个新库以相同登录复制已发布指南；删除源项目不影响仍有引用的副本，最后一次解绑才退役（`lan-guide-clone.json`）。
- **严格 32 函数档**（`strict-restore.json`，备份 SHA256 `ed559f2ab5be6938bf756d3e0c1a0f1397cdb6ec38c782ece6a8711d34473afe`）：该档不授予发布指南的 `guide_assets_step`，直接调用为 SQLSTATE `42501`，公开上传为 503。项目引用和成功收据不变；对象存储确实已有内容，由唯一的 `pending` 记录持有，公开读取为 404。数据库恢复后仍保持这一边界，未补授权。

**事务和独立探测。** 两个独立会话确认成员写入与负责人移交能随调用方一起回滚。改名 `Car → Truck` 同时更新 Workspace 工具绑定与 Labeling 标注；无操作保持锁至投影结束，注入写入失败后另一会话读到原值。提交后投影出错不撤销已经成功的改名。保留的 `docker-compose.inference-probe.yml` 在隔离网桥实际启动镜像 `sha256:8fa79d7f11d08c0d45efb8d5888b7bedabef266d1c5338b14dc1e1f4115d78e7`；后端收到一次带预期认证的 `/setup`，创建项目返回 200。停掉探测容器后下一次创建返回 503；独立会话只看到先前的一个项目和一条配置收据，没有备用读取或写入。

成员公开 HTTP 重放比对冻结的 20 组响应和 4 条业务审计，包括预览令牌、旧版本、无权接手、实际交接和重复添加。首次运行的测试数据在插入后修改用户主键，被 Identity 存在性外键拒绝；改为首次插入即给定主键后，同一冻结对照通过（`member-http.junit.xml`）。未重录预期，也未改变 Identity 外键。

模板、项目与成员目录按冻结 HTTP 样本重放。旧探针尝试用普通登录直接 `INSERT`／`SELECT` Workspace 私表，数据库按预期拒绝。修复后的探针通过公开能力写入，在调用方临时表上观测 ORM 自动刷新，独立会话核对提交前不可见、回滚无残留。五项针对性复验通过（含开启和禁用自动刷新两种情形），原样本与授权未改；见 `queries.junit.xml`、`query-replay.junit.xml`。

**范围。** 本地 JSON／JUnit 位于 `.artifacts/test-results/workspace-m2b-20261123/`；首次失败的报告与窄项复验分开保留。此批只验证 Workspace 六项及它们的调用边界，不代表整个 M2 的导入、标注、审核、通知、插件预标注链路已联验，也不是对象存储灾备、GPU 或容量验收。M2c 未执行。
