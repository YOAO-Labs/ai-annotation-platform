# 架构地图

这里是「概念（架构）」板块的导航页。每篇文档回答一个「**为什么**」或「**是什么**」，而非「怎么做」。

> 想做具体操作？去 [How-to](/dev/#我该改哪里)。想排查问题？去 [故障排查](/dev/troubleshooting/)。

## 先看哪里

```
新人路径：
  系统全景（overview）
    → 业务域模型（project / batch / task / annotation / review）
      → 工作流机制（状态机 / 派题 / 锁 / 权限）
        → 端到端流程（batch lifecycle / AI handoff）
          → AI 子系统 / 平台实现
```

## 总览

- [架构地图](./)：
  概念章节入口与阅读顺序
- [系统全景](./overview)：
  各模块是什么、怎么拼在一起

## 业务域模型

- [项目模块](./project-module)：
  Project 承载哪些配置、权限和统计边界
- [批次模块](./batch-module)：
  Batch 状态机、分派、调度联动和 bulk 行为
- [任务模块](./task-module)：
  Task 状态机、锁、审核流和可见性如何工作
- [标注模块](./annotation-module)：
  Annotation 的数据模型、采纳、版本控制与 task / batch 回写
- [视频标注工作台](./video-annotation-workbench)：
  视频元数据、多几何 annotation schema、Konva Stage 与编辑边界
- [Scene + frame_index 跨 task 帧序列地基](./scene-and-frame-index)：
  把 3D 点云逐帧 / 2D 抽帧序列 / 多段 mp4 拼接长录像统一到同一抽象
- [Scene Track 与轨迹生命周期](./scene-track-lifecycle)：
  3D Scene 对象身份、存在区间、时间角色、统一命令和可逆操作如何协作
- [审核模块](./review-module)：
  Task review、batch review、角色矩阵与通知审计联动

## 工作流与协作机制

- [状态机总览](./state-machines)：
  Project / Batch / Task 三套状态机如何联动
- [Scheduler 与派题](./scheduler-and-task-dispatch)：
  `/tasks/next` 如何选题、过滤和上锁
- [Task Lock](./task-locking)：
  锁的 TTL、接管、续期和并发保护
- [可见性与权限](./visibility-and-permissions)：
  用户为什么能看到某个 project / batch / task
- [计数与派生字段](./counters-and-derived-fields)：
  聚合字段如何回写、哪些页面依赖它们
- [项目成员绩效数据](./project-performance)：
  贡献归属、首次审核事实、计时覆盖与项目权限
- [审计与通知](./audit-and-notifications)：
  业务动作如何落审计、怎样 fan-out 到在线用户

## 端到端业务流程

- [批次生命周期（端到端）](./batch-lifecycle-end-to-end)：
  从建批、预标、送审到归档 / 重置的完整链路
- [AI 预标注接管](./ai-preannotate-handoff)：
  `pre_annotated` 的进入、人工接管、清理与回滚
- [数据流](./data-flow)：
  一条标注从上传到导出走哪些节点

## AI 与推理子系统

- [预标注流水线](./prediction-pipeline)：
  Job 生命周期、Celery 任务链、错误处理
- [视频 AI 追踪](./video-ai-tracking)：
  tracker 能力路由、分窗身份、候选暂存与接受 / 丢弃边界
- [AI 模型集成](./ai-models)：
  GroundingDINO / SAM 怎么接入，预测怎么存

## 平台实现架构

- [后端分层](./backend-layers)：
  FastAPI / Service / Repository 各层边界
- [前端分层](./frontend-layers)：
  页面 / 组件 / Store / API Client 的关系
- [工作台 Shell 架构](./workbench-shell)：
  标注 / 审核模式与 image / video / 3D Stage 边界
- [API Schema 边界](./api-schema-boundary)：
  前后端契约在哪里定义、如何保证一致
- [后端基础设施（容器）](./backend-infrastructure)：
  哪些容器，各自职责，容器间网络
- [部署拓扑](./deployment-topology)：
  生产环境的网络拓扑与服务边界
- [运行环境形态（dev/staging/prod）](./runtime-environments)：
  同一套代码在三种环境下的运行差异（谁进容器 / profile / ENVIRONMENT 断言）
- [性能 HUD](./perfhud)：
  开发期内置性能面板的原理与使用
- [超大图金字塔派生资产](./image-pyramid-assets)：
  超大源图如何生成不可变 overview/tile、怎样安全交付，以及现实开发夹具如何复现

## 尚未覆盖（待补充）

- WebSocket 实时通知架构（详见 [ws-protocol](/dev/reference/ws-protocol)，ADR 层面的 rationale 待写）
- 多租户 / 权限边界（见 [安全模型](/ops/security/)）
