---
audience: [ops, super_admin]
type: how-to
status: stable
last_reviewed: 2026-11-19
---

# 本机 ML 服务维护

系统设置中的「维护镜像与容器」面向当前部署用 Compose 启用的 Grounded SAM 2、SAM 3、YOLO、ONNXTools 和 RapidOCR。五者都从部署指定的 manifest digest 拉取镜像，不从平台源码构建，也不安装其他插件。操作会短暂停止目标服务；已接受的推理请求和后台模型构建先完成，再退出。空闲视频追踪会话及内存缓存会清空，用户可能需要重新开始追踪。权重卷、模型目录、数据库中的标注与 GPU fence 高水位保留。

认证、服务池接流和 GPU 许可分别检查不同条件，基本原理见[模型服务接入、路由与认证](../../dev/concepts/ml-backend-routing-and-auth)。

### 控制协议切换

切换前列出每个注册实例、服务池成员、插件镜像摘要和实际运行进程，包括后台模型构建、视频会话和流式响应。先按旧协议停止新准入并取得每个旧实例的物理关闭或排空回执；断线、请求取消、令牌过期和数据库无活动行都不能代替 GPU 或 I/O 的完成证明。

API、Worker、协议 wheel、Plugin-SDK、全部受影响的镜像与 Compose 注入必须成组更新。数据库迁移 `bo0469` 拒绝尚未结清的路由范围、尝试及控制操作；它只改变新回执的 admission header，不改旧回执、签名密钥、audience 或代际。以匹配制品实际完成准入、请求、排空和旧头拒绝之后才重开接流。没有相应设备或实例证明的候选不能标记为部署已采用。

## 首次接入与日常同步

旧部署首次接入需要升级 API 和启用的 Backend 镜像，并应用 Compose 的凭据挂载。API 启动会先创建或复用 `ML_MANAGED_AUTH_DIR` 下各服务的私有文件，再尝试同步环境注册项；服务暂未升级导致握手失败时，本地文件会保留，API 仍可启动。不能把旧镜像仅重启一次就当作已接入认证。

API 对凭据父目录可写，每个服务只读挂载自己的子目录并用 `BACKEND_AUTH_DIR` 指向该挂载，worker 无需读取这些文件。旧名 `AAP_BACKEND_AUTH_DIR` 不再注入；读取旧名的 backend 制品必须与 Compose 同代更新。非 root API 与凭据目录的归属应一致；相对目录按当前 checkout 解析，各部署和工作树分别使用自己的目录。目录与 Token 不要烘焙进镜像或复制到 Git。

完成镜像和挂载升级后，超管在「设置 → 系统设置 → ML 服务认证与维护」初始化并同步认证，再执行实例健康检查。已有手动 Token 保留；YOLO 的 `YOLO_MANAGED_MODELS_TOKEN` 优先于通用手动值与文件默认值，保留手动模式时平台也应填写同一凭据。项目启用和候选模型加载仍需分别完成。

同步按实例提交，每个成功实例的认证变更与审计记录在同一事务保存。后续实例失败不会撤销已成功的同步，可核对审计记录后重试；某实例的审计写入失败时，该实例的认证变更一并回滚。本地凭据文件会保留，再次初始化可直接复用。

后续补齐默认文件或同步实例无需重建镜像。修改 `.env` 中的手动 Token 或挂载配置，则需重建目标容器使启动配置生效。再次初始化不会轮换已有凭据，镜像或容器重建也不会替换持久目录中的 Token。

## 部署宿主机维护程序

此功能需要额外安装宿主机维护程序。API 只保存维护请求，没有 Docker socket，也不执行 Docker 命令。未安装、心跳过期或未识别到安全的服务容器时，页面显示原因并禁用操作。不是任意远程 ML Backend 的部署工具。

部署管理员先在宿主机准备本项目的 Python 虚拟环境和 Docker Compose，然后以前台方式验证下列命令。替换为实际部署的绝对目录及固定 Compose project 名；不要把生产参数指向其他工作树。

```bash
cd /srv/ai-annotation-platform
apps/api/.venv/bin/python scripts/ml-backend-runner.py \
  --root /srv/ai-annotation-platform \
  --project ai-annotation-platform \
  --compose-file /srv/ai-annotation-platform/docker-compose.yml \
  --compose-file /srv/ai-annotation-platform/docker-compose.ml.yml \
  --operations-dir /srv/ai-annotation-platform/data/backend-manager
```

生产部署需再明确传入 `--compose-file /srv/ai-annotation-platform/docker-compose.prod.yml`。按实际启用的服务，将 `docker-compose.ml-gsam2.yml`、`docker-compose.ml-sam3.yml`、`docker-compose.ml-yolo.yml`、`docker-compose.ml-onnxtools.yml`、`docker-compose.ml-rapidocr.yml` 中相应的文件逐个作为 `--compose-file` 传入；文件列表必须与容器标签完全一致。程序固定读取部署目录中的 `.env.production`；开发配置读取 `.env`。不支持临时命令、任意 Compose 文件或自定义 entrypoint。

API 的 `ML_BACKEND_OPERATIONS_DIR` 和 runner 的 `--operations-dir` 必须指向同一个私有目录。API 在容器内时，配置容器路径并通过读写 bind 映射该宿主机目录；worker 无须读取队列。目录权限为 `0700`，文件为 `0600`。非 root API 与 runner 应使用同一 UID；root API 创建的文件保留目录所有者。不要将目录复制到公开共享存储。

长期运行时由部署管理员将命令安装为 systemd 服务，以固定部署用户运行，设置 `WorkingDirectory` 为部署目录，`UMask=0077`。建议 `KillMode=process`，避免停止 runner 时 systemd 连带杀死仍等待优雅退出的 Docker 客户端。维护过程中不要停止或重启 runner；只有一个实例可以持有维护锁。

## 操作与安全检查

只有超级管理员可以提交和查看维护任务；提交请求先持久化审计记录，再交给宿主机执行。任务全局串行，不重复提交。任务详情仅显示固定阶段消息，不向浏览器暴露宿主机命令输出、环境变量、路径或认证密钥。

程序核对容器的 Compose project、工作目录、配置文件、service 和持久挂载，同时校验 PID 1 是单 worker Uvicorn、关闭钩子和受审的 `gpu_lifecycle.py` 源码摘要。运行中的镜像必须绑定真实 RepoDigest；Compose 为对应服务配置 `GSAM2_LIFECYCLE_SHA256`、`SAM3_LIFECYCLE_SHA256`、`YOLO_LIFECYCLE_SHA256`、`ONNXTOOLS_LIFECYCLE_SHA256` 或 `RAPIDOCR_LIFECYCLE_SHA256`。值来自对该镜像安装的生命周期源码审查。升级时同步更新目标镜像及源码摘要；旧容器标签保留原摘要供停机前校验。未知旧镜像、修改过进程模型、缺少关闭钩子或尚未完成启动的容器均拒绝自动维护，需部署管理员先人工升级。

对所有五种服务，程序先拉取指定 manifest digest，核对实际 RepoDigest，再以无网络、无 GPU 的短期容器检查安装包中的源码和关闭钩子。任一准备或校验失败都不停止旧容器。准备成功后重新核对原容器 ID，再执行 `docker stop --signal SIGTERM --timeout -1`，停止接受新请求并无限等待其正常退出。Docker 官方明确 `--timeout -1` 不会在超时后发送 SIGKILL：[停止容器](https://docs.docker.com/reference/cli/docker/container/stop/)。runner 的阶段跟踪默认最多 1800 秒，可由部署管理员用 `--phase-timeout` 调整；跟踪超时不会杀死容器或仍在等待的停止命令。

仅确认容器正常退出后，程序才以固定目标运行 `up -d --no-deps --no-build --force-recreate`，不启动依赖、不删除模型卷、不重启其他服务。完成仅表示目标容器已健康，认证、GPU 新 boot_id 的证明和实际推理仍由平台重新探活确认；维护程序不清空或重置 fence、epoch、GPU 账本。

## 中断与诊断

完整的镜像拉取与维护命令输出仅保存在私有队列目录的 `<任务 UUID>.log`，供宿主机部署管理员检查。浏览器不返回原始日志。请按部署日志保留策略清理已经结束的日志和任务文件，保留需要审计的记录。

runner 重启后不会自动重放旧任务；未开始的请求安全取消。停止阶段之后的错误、阶段跟踪超时或正在执行时 runner 中断，会阻止继续提交。此时先在宿主机核实原容器是否仍在优雅退出、目标容器是否已经建立，以及持久模型挂载是否正确。不要为了恢复按钮而强制结束现有推理。

确认状态安全后，停止 runner，再使用原固定命令附加 `--acknowledge-interrupted <任务 UUID>` 重启。此参数只解除队列阻塞，不会自动执行或恢复容器操作；超级管理员可随后重新提交维护请求。程序没有强制终止、自动回滚镜像或删除数据选项。
