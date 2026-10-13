---
audience: [dev]
type: how-to
since: v0.1.0
status: stable
last_reviewed: 2026-09-30
---

# CI 服务依赖踩坑

GitHub Actions e2e job 跑挂的几个非典型问题，集中归档。

## 症状 1：MinIO 镜像 manifest not found / unauthorized

```
Error response from daemon: manifest for bitnami/minio:latest not found
```

### 根因

Bitnami 撤了该镜像的 manifest；Quay 的社区镜像也已停止分发，原先固定的 digest 会返回 `unauthorized`。这类错误发生在容器启动前，不能靠延长 FastAPI 或 Playwright 超时修复。

### 修复

使用 [IBM 公告提供的镜像](https://www.ibm.com/support/pages/node/7289585)，保留原 digest。`ci.yml`、`e2e-run.yml` 与 `docker-compose.yml` 使用相同镜像；更新时先验证 manifest digest 与运行平台，再同步三个入口，不能改为浮动 `latest`。

GitHub Actions 的 `services:` 块**不接受 image 的 args**（没法给 MinIO 镜像传 `server` 子命令）。直接在 step 里 `docker run` 最干净：

```yaml
- name: Start MinIO
  run: |
    docker run -d --name minio \
      -p 9000:9000 \
      -e MINIO_ROOT_USER=minioadmin \
      -e MINIO_ROOT_PASSWORD=minioadmin \
      icr.io/fusion-open/minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e server /data

    # 轮询 health 端点直到就绪
    for i in $(seq 1 30); do
      if curl -fsS http://127.0.0.1:9000/minio/health/live; then exit 0; fi
      sleep 1
    done
    docker logs minio
    exit 1
```

桶 `annotations` 由 FastAPI lifespan `ensure_bucket` 第一次 HeadBucket 失败时自建，无需预建。

## 症状 2：FastAPI lifespan 卡死 30s 后 `/health` 不通

```
ECONNREFUSED 127.0.0.1:8000
```

后续所有 Playwright case 全连不上。

### 根因

E2E job 之前没声明 `minio` service，FastAPI lifespan 里 `boto3.HeadBucket(annotations)` 连不上 `localhost:9000`，botocore 反复重试拉满 30s health-check 窗口，curl 打不通 `/health`。

### 修复

显式定义 minio service（早期沿用 `bitnami/minio` 时）或用上文 `docker run` 方案。FastAPI 必须能在 lifespan 里成功 `HeadBucket` 才会暴露 `/health`。

## 症状 3：E2E IPv6 解析失败

某些 GitHub runner 上 `localhost` 解析到 `::1` 但服务只监听 `0.0.0.0`，导致 connect 失败。

### 修复

显式用 `127.0.0.1`：

```yaml
env:
  API_BASE_URL: http://127.0.0.1:8000
```

## 症状 4：layout-stress 视频用例 reload 后画布 30s 超时

```
Error: expect(locator).toBeVisible() failed
Locator: getByTestId('video-konva-stage').locator('.konvajs-content > canvas').first()
Timeout: 30000ms
```

夜间 / PR 的 `layout-stress` 套件间歇红，API 日志里同时出现大量 `S3 ListObjectsV2` 重试和 `ml-backends` 502，容易被误判成存储抖动。

### 根因

对时 trace 网络流（`0-trace.network`）可见：`page.reload()` 的文档请求毫秒级返回，第一波启动 API 全部秒回；随后页面**约 28 秒零网络活动、零 screencast 帧**，`manifest-v2` 直到 30s 预算最后 2 秒才发出。即渲染主线程被布局恢复 + Konva 多层 + SwiftShader 软渲染冻结，`S3 重试` 来自测试间隙的 seed 清理、502 是 CI 无 ML backend 的预期探测，都是伴生噪音。API 全程响应正常。

### 判别方法

下载失败 run 的 `playwright-report-layout-stress` 工件，对齐 reload 文档请求与 `manifest-v2` 请求的时间差：差值接近超时预算、且期间页面无任何请求即为此症；API 侧请求一旦发出就快速返回可排除后端。

### 修复

- 图片 / 视频布局矩阵迁出 pointcloud 项目，不再使用 SwiftShader；
- `rememberCanvas` 先等 stage 宿主（挂载即代表 manifest 就绪）再等画布，`@stress` 场景预算放宽到 90s；
- 压力套件允许 1 次重试（见 `apps/web/playwright.stress.config.ts`）。

同一次取证还确认了 `video-issue-context` 的 stale seek 竞态：同一冻结窗口内，时间轴点击的异步精确取帧迟到后会把键盘步进已达的帧号拽回点击时刻的旧帧（G2-1 实测 F120→F105，两次重试稳定复现）。测试侧先以 `expectPaintSettled` 串行化规避（PR #113）；应用层随后在播放控制器的帧号写入汇聚点加了单调栅栏——暂停态只接受最新导航目标帧或其回声，迟到旧帧回报直接丢弃（Issue #114）。`expectPaintSettled` 因此是冗余防护而非唯一防线，可按需保留。

## 教训

- **GitHub Actions `services:` 块不能传 args**，需要传命令的服务必须用 `docker run`。
- **lifespan 阻塞会拖垮 health check**。如果某依赖在 CI 里不强制必须，给它加超时 + 降级。
- **`localhost` 在不同环境不等价**，CI 里始终写 `127.0.0.1`。
- **API 日志噪音会误导归因**：断言画布 / 就绪类超时先对时 trace 网络流，再决定是否怀疑后端；不需要 WebGL 的用例别放进 SwiftShader 项目。

## 相关

- commit: `b63b192` fix(ci): MinIO 改用 docker run
- commit: `51a84b1` fix(ci): e2e 增加 minio service
- commit: `45da7db` fix(ci): IPv6 解析失败
