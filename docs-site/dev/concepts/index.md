---
title: 架构地图
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-26
---

# 架构地图

这里是「概念（系统架构）」板块的导航页。每篇文档聚焦回答一个「**是什么**」与「**为什么这样设计**」，而非「具体怎么操作」。

> 想查具体开发步骤？去 [开发文档首页：我该改哪里](/dev/#我该改哪里)。想排查运行时异常？去 [故障排查](/dev/troubleshooting/)。

## 推荐阅读路径

```
新人路径：
  系统全景（overview）与仓库地图（repository-map）
    → 业务域模型（project / batch / task / annotation / review / training-datasets）
      → 工作流机制（状态机 / 派题 / 锁 / 权限 / 计数 / 审计）
        → 端到端流程（batch lifecycle / AI handoff / data-flow）
          → AI 子系统与平台底层实现
```

## 总览

- [架构地图](./)：
  概念章节入口与推荐阅读顺序
- [系统全景](./overview)：
  各服务与子系统是什么、如何协同工作
- [仓库地图（模块归属与调用链）](./repository-map)：
  前后端与共享包目录边界、核心业务调用链与改码定位速查

## 业务域模型

- [项目模块](./project-module)：
  Project 承载的标签 schema、工具单元、权限边界、模型选择与统计口径
- [批次模块](./batch-module)：
  Batch 状态机、分派、调度联动与批量操作行为
- [任务模块](./task-module)：
  Task 状态机、锁、审核流、跨帧传播与可见性规则
- [标注模块](./annotation-module)：
  Annotation 的几何联合模型、预测采纳、乐观锁版本控制与派生回写
- [数据集、目录来源与增量同步架构](./dataset-sources-and-sync)：
  内容身份模型、受控目录与 S3/SFTP 原地引用、可恢复扫描登记、增量同步状态机、数据集工作台与质量复核
- [在线学习与训练体系](./training-datasets)：
  训练数据版本流、独立执行器 GPU 独占排他锁、逐样本评估、模型谱系与自训权重回流
- [视频标注工作台](./video-annotation-workbench)：
  视频元数据、多几何标注模型、帧级画布与 WebCodecs 解码边界
- [Scene + frame_index 跨 task 帧序列地基](./scene-and-frame-index)：
  将 3D 点云逐帧、2D 抽帧序列与长视频切片统一到同一时序抽象
- [Scene Track 与轨迹生命周期](./scene-track-lifecycle)：
  3D Scene 对象身份、存在区间、时间角色、统一命令与可逆操作
- [审核模块](./review-module)：
  Task 审核、Batch 审核、贡献者证据与自审拦截、角色矩阵及通知审计联动

## 工作流与协作机制

- [状态机总览](./state-machines)：
  Project / Batch / Task 以及在线学习与训练多套状态机如何联动
- [Scheduler 与派题](./scheduler-and-task-dispatch)：
  `/tasks/next` 如何选题、按同场景连续优先过滤并原子上锁
- [Task Lock](./task-locking)：
  锁的 TTL、抢占接管、续期以及任务编辑与光栅掩码写入锁顺序
- [可见性与权限](./visibility-and-permissions)：
  平台身份与项目角色矩阵、`resolve_project_access` 真源与可见性过滤
- [计数与派生字段](./counters-and-derived-fields)：
  Task / Batch / Project 聚合字段如何回写、哪些视图依赖它们
- [项目成员绩效数据](./project-performance)：
  贡献归属、首次审核通过率、活跃计时采集与项目权限边界
- [审计与通知](./audit-and-notifications)：
  业务动作如何落库审计、如何经 Redis Pub/Sub 实时推送给在线用户
- [反馈收敛与双写对账](./feedback-convergence)：
  幂等键、并发冲突收敛与派生计数对账恢复机制

## 端到端业务流程

- [批次生命周期（端到端）](./batch-lifecycle-end-to-end)：
  从建批、AI 预标、人工标注、送审到归档与重置的完整链路
- [AI 预标注接管](./ai-preannotate-handoff)：
  `pre_annotated` 的进入、人工接管、清理与回滚边界
- [数据流](./data-flow)：
  标注主链、AI 预标注、异步导出、在线学习闭环与实时通知的完整调用序列
- [存储位置、连接器与用途绑定](./storage-connections)：
  外部存储连接器、受控存储位置注册、九用途保存目标隔离与位置访问维护协议
- [原件验证读取、缓存与保留回收架构](./original-access-and-retention)：
  统一 `OriginalSource` 严格读取、认证下载与视频分片舞台、按需原图缓存、条件创建与准确版本回收
- [对象存储迁移、站点 Agent 与容量观测架构](./storage-migration-and-ops)：
  配置源迁移清单与复制批次、维护窗口与原生源写封闭、已封存读取映射、九用途原子切换、只读站点 Agent 与容量账目
- [系统设置运行时覆盖](./system-settings)：
  数据库级系统设置如何安全覆盖默认配置并保持多进程缓存一致

## AI 与推理子系统

- [预标注流水线](./prediction-pipeline)：
  单阶段与多阶段 DAG 编排、Celery 任务链、服务池路由与 GPU 仲裁
- [视频 AI 追踪](./video-ai-tracking)：
  Tracker 能力路由、分窗身份保持、候选暂存与逐段审阅接受 / 丢弃边界
- [AI 模型集成](./ai-models)：
  5 类第一方 ML Backend、能力协商与受控自训权重安装
- [模型服务接入、路由与认证](./ml-backend-routing-and-auth)：
  服务实例与模型的区别、服务池接流、训练回流和 Token 凭据

## 平台实现架构

- [后端分层](./backend-layers)：
  FastAPI Router / Service 领域子包 / ORM 模型与异步 Worker 的职责边界
- [前端分层](./frontend-layers)：
  路由页面、全局 Store、工作台领域状态与 OpenAPI 类型客户端的协作关系
- [工作台 Shell 架构](./workbench-shell)：
  标注 / 审核模式、Dockview 面板布局、快捷键系统与 Image / Video / 3D Stage 边界
- [API Schema 边界](./api-schema-boundary)：
  前后端契约真源、Pydantic schema 与前端自动生成类型的同步机制
- [后端基础设施（容器）](./backend-infrastructure)：
  PostgreSQL、Redis、MinIO、API、7 类 Celery Worker、ML Backend 与训练执行器容器职责
- [部署拓扑](./deployment-topology)：
  容器网络隔离、Nginx 反代、LAN 生产形态与独立训练执行器部署拓扑
- [运行环境形态（dev/staging/prod）](./runtime-environments)：
  同一套代码在开发、预发、生产与多工作树隔离环境下的运行差异
- [性能 HUD](./perfhud)：
  开发期内置性能诊断面板的指标采集原理与使用方法
- [超大图金字塔派生资产](./image-pyramid-assets)：
  超大源图如何生成不可变 overview / tile、视口按需调度与安全交付
- [图片工作台栅格资源协调](./raster-resource-coordination)：
  任务级光栅掩码编解码、WebGPU 加速合成、回退熔断与内存生命周期协调
