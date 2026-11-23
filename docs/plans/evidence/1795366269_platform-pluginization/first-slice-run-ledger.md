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
