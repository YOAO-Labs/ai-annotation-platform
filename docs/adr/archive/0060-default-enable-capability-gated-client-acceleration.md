# 0060 — 默认启用按客户端能力安全回退的 WebCodecs 与 Raster Mask WebGPU

- **Status:** Accepted
- **Date:** 2026-07-30
- **Deciders:** core team
- **Supersedes:** ADR-0056、ADR-0057、ADR-0058 与 ADR-0059 中 Raster Mask WebGPU 默认关闭的 rollout 决策

## Context

WebCodecs 精确帧与 Raster Mask WebGPU 已具备能力探测、资源预算、稳定诊断和精确 CPU / 原生视频
回退。Linux GPU 证据证明实现可用，但 macOS、Wayland、Windows 和跨浏览器硬解矩阵尚未完成。继续默认
关闭会让真实客户端覆盖率与故障分布长期不可见，也让已完成的安全回退合同无法在正常流量中接受验证。

## Decision

1. WebCodecs 精确帧缺省开启；URL query 优先于 localStorage，`0` / `false` 显式关闭。浏览器不支持、
   codec 或 chunk 不可用、预算不足及解码失败均沿既有原生视频路径回退。
2. Raster Mask WebGPU build-time gate 缺省为 `true`，但只在大 ROI `square dilate` 操作中惰性探测
   adapter；准入阈值、单 owner、字节预算、device lost 和 CPU Worker fallback 合同不变。
3. 生产镜像必须保留 `VITE_EXPERIMENTAL_RASTER_MASK_WEBGPU=false` 的整体回滚构建参数；WebCodecs
   保留本机用户关闭入口。
4. 默认开启不等于跨平台资格已经完成。ROADMAP 继续跟踪 macOS / Wayland / Windows correctness、长会话、
   p95、fallback rate，以及 macOS VideoToolbox 和跨浏览器 1080p/4K 硬解证据。
5. WebGPU 仍只使用访问网页的客户端 GPU，不扩展为视频解码、通用 Konva 渲染或服务端 GPU 计算。

## Consequences

正向：

- 受支持客户端无需手工开关即可获得精确帧或大 ROI Mask 加速。
- 不支持客户端继续使用已验证的原生视频或 CPU Worker 路径，功能正确性不依赖 GPU 可用性。
- 两条路径都有显式回滚入口，后验矩阵可以基于真实默认配置执行。

负向：

- 首次符合条件的 Mask 操作可能承担 adapter / pipeline 冷启动；当次操作仍允许走 CPU。
- 尚未完成资格的平台可能产生更高 fallback rate，必须通过诊断和 BUG 报告持续复核。
- 默认 bundle 会包含懒加载 WebGPU provider chunk；显式关闭构建仍须验证 provider 和 adapter 请求为零。

## Alternatives Considered

**继续默认关闭直到跨平台矩阵完成**：风险最低，但无法按当前产品决策开放已完成能力，拒绝。

**移除回退并强制 GPU / WebCodecs**：会把客户端能力差异变成功能故障，拒绝。

**按 User-Agent 白名单启用**：UA 不能证明 codec、adapter、驱动和预算，且维护成本高于运行时能力探测，拒绝。

## Notes

- WebCodecs：`apps/web/src/pages/Workbench/stage/useVideoChunkDecoder.ts`
- Raster Mask WebGPU gate：`apps/web/src/pages/Workbench/stage/shared/rasterMask.worker.ts`
- 生产 build arg：`infra/docker/Dockerfile.web`、`docker-compose.prod.yml`
- 后验验证：[ROADMAP.md../../../ROADMAP.md)
