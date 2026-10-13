# 仓库优化 P10 验证通道：渲染器资格验收（点云 WebGPU / WebCodecs 精确帧）

> 完成日期：2026-09-20 · 隶属计划：`docs/plans/archive/1789880018_repository-optimization-plan.md`（P10 渲染验证通道，非 canonical P10 完成）
> 分支：`worktree-agent-opt-final-renderer`；验收根 `e41e4973c`（含 P7 命名空间化 pointcloud 消费者修复；渲染器产品输入与前述已测状态一致）
> 证据图例：**[V]** 实际执行；**[FIXED-EXTERNAL]** 曾因外部回归受阻、外部修复后已复跑通过
> 边界：零产品源码变更（仅新增本文档）；不 push；不做 P8/P9/台账编辑；未触碰 main 8000 / feedback 8100 / Grafana 3001；未做全局 pkill；未使用全局管理员或 reset 绕过隔离。

## 1. 点云 WebGPU（严格门控，已通过）**[V]**

命令（自有 e2e 模式 `aap_wt_de57dbd75d11794b_e2e`，launcher 保留独占端口；本机 Web 端口 3100）：

```bash
pnpm dev:worktree -- init --mode e2e
pnpm dev:worktree -- exec --mode e2e -- bash -lc \
  'set -o pipefail; cd apps/web && PLAYWRIGHT_POINTCLOUD_WEBGPU=1 \
   PLAYWRIGHT_JSON_OUTPUT_NAME=/tmp/aap-final-renderer-e2e/pointcloud.json \
   pnpm playwright test e2e/tests/workbench-pointcloud-edit.spec.ts \
   e2e/tests/workbench-pointcloud-playback.spec.ts --project=pointcloud --reporter=line,json'
```

- **退出码 0；JSON：expected 21 / unexpected 0 / skipped 0 / flaky 0；duration 209.3s** [V]。
- 两个 spec 属专用 `pointcloud` project（`chromium` project 会报 No tests found）；`PLAYWRIGHT_POINTCLOUD_WEBGPU=1` 触发严格断言：`pointcloud-renderer-backend` 的 `data-backend` 必须为 `webgpu`，并校验有界 Worker 与不回读 Canvas 行为。全部 21 例通过。

### 1.1 浏览器与适配器证据（同一资格环境，浏览器自报）**[V]**

以与资格项目相同的启动参数（`--enable-unsafe-webgpu --enable-features=Vulkan --use-angle=vulkan --disable-vulkan-surface --ignore-gpu-blocklist`）在 `pointcloud` project 内导航到自有隔离源后读取：

| 项                            | 值                                                                                                                                                                                                                      |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| location                      | `http://127.0.0.1:3100/login`（自有隔离端口）                                                                                                                                                                           |
| isSecureContext               | `true`（`about:blank` 为不透明源、无 `navigator.gpu`，不可作为证据）                                                                                                                                                    |
| 浏览器                        | Chromium `147.0.7727.15`（repository bundled：`~/.cache/ms-playwright/chromium-1217/chrome-linux64/chrome`，无 channel 覆盖）                                                                                           |
| adapter.vendor / architecture | `nvidia` / `ampere`（本次观测中 device/description 为空；不对 Chrome 版本做通用脱敏政策断言）                                                                                                                           |
| isFallbackAdapter             | `null`（字段未暴露）                                                                                                                                                                                                    |
| 能力                          | texture-compression-bc、timestamp-query、subgroups、maxTextureDimension2D 16384 等 21 项特性                                                                                                                            |
| 判定                          | 证据为「浏览器自报 adapter.vendor=nvidia / architecture=ampere」与「严格断言 data-backend=webgpu」两项；BC/timestamp/subgroups 特性**单独不能**证明不存在软件回退。主机虽有 2× RTX 3090，此记录来源为浏览器，非主机推断 |

实际 JSON 关键字段（临时文件已按清理要求删除，以下为提交留存的精简记录）：

```
pointcloud.json stats: {"expected":21,"unexpected":0,"skipped":0,"flaky":0,"duration":209321.618}
adapter (same qualification project, secure own origin): {
  "location":"http://127.0.0.1:3100/login","isSecureContext":true,"hasGpu":true,
  "browserVersion":"147.0.7727.15",
  "adapter":{"vendor":"nvidia","architecture":"ampere","device":"","description":""},
  "isFallbackAdapter":null,"maxTextureDimension2D":16384,
  "featureCount":21
}
```

> 备注 [FIXED-EXTERNAL]：先前在同一命令下首错为 `seed/login failed: 404`（两个 spec 仍注入硬编码全局 `admin@e2e.test`，而 owned seed 创建命名空间化账号）。该 P7 消费者迁移回归由 root 修复（`e41e4973c`，六处 pointcloud spec 改用 `data.admin_email`）后，本条即为修复后复跑结果；首错原文：`Error: seed/login failed: 404`（`e2e/fixtures/seed.ts:889 injectToken`，调用点 edit.spec:428 / playback:11）；修复后已不再复现，临时证据文件按清理要求删除。

## 2. WebCodecs 精确帧（已通过，本通道保留）**[V]**

命令：`PLAYWRIGHT_REQUIRE_WEBCODECS=1 pnpm playwright test e2e/tests/video-webcodecs-precise-frame.spec.ts --project=chromium --reporter=line`

- **9 passed，退出码 0**（1.2m），webcodecs 输入未变，未复跑。
- 严格门控生效：断言走 WebCodecs 精确解码并校验实际解码像素与标注几何对齐，而非 UI 时间戳。**边界：严格开关禁用的是 spec 的应用级回退分支，不是 WebCodecs 内部可能存在的软件解码；硬件解码仍未取证。**
- 事实备注：本次通过来自 **repository bundled Chromium**（未使用系统 Chrome / channel 覆盖）；不得据此推断硬件解码加速——严格像素断言只证明精确解码路径，硬件解码厂商/引擎未单独取证。

## 3. 输入指纹与复用

- 验收根 `e41e4973c`；点云/WebCodecs 相关源与 `playwright.config.ts` 在本次运行中即为该根内容；`git status` 清洁、无测试/工具改动残留（临时适配器 probe spec 仅作一次性取证，运行后删除、未提交）。
- 后续 CI/docs-only rebase 可复用：§2 的 WebCodecs 9 例（输入未变）与 §1 的点云 21 例（渲染器输入未变；若 pointcloud 源或 config 变化需按 §1 命令复跑）。

## 4. 清理

自有 e2e 模式以精确确认值销毁（`--confirm de57dbd75d11794b:e2e`）；销毁后 doctor：独立数据库/Redis/buckets 均未创建（已复核，不再重复销毁）。任务产生的临时 probe spec、test-results、node 脚本，以及 `/tmp/aap-final-renderer-e2e`、`/tmp/aap-opt-final-renderer-evidence` 等临时证据目录均已删除；关键 JSON 字段已精简嵌入本文档，长期留存仅本文档与英文交接报告 `/tmp/aap-opt-final-renderer-report.md`。
