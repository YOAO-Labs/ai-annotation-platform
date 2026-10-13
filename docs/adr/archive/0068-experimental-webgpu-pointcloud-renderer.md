# 0068 — 3D 点云采用默认关闭的 WebGPU 实验渲染路径

- **Status:** Accepted
- **Date:** 2026-08-24
- **Deciders:** core team
- **Supersedes:** —

## Context

真实 nuScenes mini 六相机任务暴露出两类相互独立的瓶颈：PCD 在主线程解析，且时间轴预取只保留原始
响应；相机 RGB 上色又在切帧后解码六张图，经 Canvas 回读完整 RGBA，再由短生命周期 Worker 生成并
上传逐点颜色。相邻帧即使命中 HTTP 缓存，RGB 可见仍约需 1.2 秒。选中框后展开三正交视图还会增加
点云提交压力，因此不能只替换相机上色的一小段 shader 来判断 WebGPU 是否有价值。

Three.js 的 WebGPU renderer 会异步初始化，并可能实际使用 WebGL2 fallback。其 WebGPU point
primitive 固定为 1 pixel，直接搬用 `PointsMaterial` 会丢失现有可调点大小。与此同时，WebGPU 在
不同浏览器、操作系统与驱动上的收益并不稳定；`navigator.gpu` 存在也不能证明能取得 adapter。

## Decision

1. 增加工作台本地实验设置 `experiment.pointCloudWebGpuRenderer`，默认关闭，并只在重新打开或刷新
   3D 工作台时读取。页面显示初始化后的真实 backend：WebGPU、WebGL2 fallback 或 Legacy WebGL2。
2. 实验路径同时覆盖主视图和单 renderer 三正交视图。可见点云使用 counted Sprite / instanced point
   quad，保留点大小、正交裁剪、selection mask、邻帧点云与稳定源点索引；框、gizmo、网格、坐标轴和
   标签继续使用 WebGPU renderer 支持的内置材质。点拾取使用不进入场景的共享 geometry 代理。
3. renderer 初始化失败或 device lost 时在页面生命周期内重建 Legacy 场景；renderer 不持有标注、
   undo/redo 或保存状态，回退不会改变 annotation 数据。
4. 点云计算改为工作台生命周期内的持久 Worker。相邻帧预取直接缓存按 URL、轴向与抽稀阈值键控的
   parsed typed arrays，并缓存解码后的 `ImageBitmap`；缓存失败项可重试，未引用 bitmap 淘汰时显式
   `close()`。
5. 实验 RGB 路径不调用 Canvas `getImageData()`，也不生成逐点 RGB attribute。TSL 在点材质中完成
   标定投影、相机选择和 texture 采样，亮度、对比度与 Gamma 只更新 uniform。低分辨率遮挡深度图暂由
   同一持久 Worker 生成并上传为 float texture；后续只有 GPU depth pass 在真实 A/B 中证明额外收益，
   才替换该已验证的约 90 ms 阶段。
6. 切帧立即把实例数归零并隐藏旧点，保留场景级 geometry、实例缓冲、`PointsNodeMaterial` 和固定六路相机采样 TSL 拓扑；新帧只更新点属性、纹理和标定 uniform，容量不足时才分级扩容。新 geometry 到位后先显示高度色，相机纹理就绪后至多切换一次 RGB。
   两条 renderer 路径都不再为等待 RGB 隐藏点云。
7. 本 ADR 只批准实验 Pilot，不批准默认开启或删除 Legacy。转正常功能必须另立决策，并通过真实
   nuScenes A/B、至少两个 OS/GPU 组合、功能一致性、device-lost 回退与资源 plateau 门。

## Consequences

正向：

- renderer 迁移和去除 Canvas/逐点 RGB 的收益可以分别测量，实际落到 WebGL2 fallback 的样本不会被
  误算成 WebGPU 收益。
- PCD 解析、相机解码与颜色准备不再阻塞主线程，邻帧可复用 parsed frame 与 decoded bitmap。
- 实验失败有完整 Legacy 对照和页面级回退，不改变保存合同，也不要求数据库、API 或部署环境变化。

负向：

- 维护两套 renderer 与颜色路径，直到实验通过推广门或被撤销。
- 主视图与三视图各有 renderer owner，同一相机 texture 会分别上传；缓存与 dispose 必须持续通过长会话
  plateau 检查。
- Worker depth raster 仍有 CPU 计算成本；它不是当前约 1 秒图像回读瓶颈，但后续数据必须决定是否值得
  增加 GPU depth render target 的复杂度。
- WebGPU fallback 和 Node material 仍受 Three.js 实验 API 变化影响，需要在依赖升级时重新验证。

## Alternatives Considered

**只改成持久 Worker**：可以省去 Worker 创建成本，但保留六图 Canvas 回读、整图复制和逐点 RGB 上传，
无法解决已测得的主要等待，拒绝作为完整方案。

**只给相机上色增加一个 WebGPU shader**：无法测量选框和三视图的 renderer 压力，也会留下混合后端，
拒绝。

**立即全量替换 Legacy**：缺少跨设备真实收益与回退证据，且 Three.js WebGPU renderer 仍为实验状态，
拒绝。

## Notes

- 实施与性能门：`docs/plans/archive/2026-08-24-v0.24.5-3d-webgpu-renderer-pilot.md`
- 外部证据：`docs/research/22-supervisely-cvat-workbench.md`
