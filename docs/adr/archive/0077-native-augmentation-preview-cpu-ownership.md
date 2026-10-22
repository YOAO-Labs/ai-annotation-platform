# 0077 — 原生增强预览使用独立且有界的 CPU 所有权

- **Status:** Accepted
- **Date:** 2026-10-15
- **Deciders:** Maintainer-authorized implementation
- **Supersedes:** —

## Context

训练配置需要展示真正经过原生数据变换后的图片、框和来源。浏览器复刻 Mosaic、几何变换和过滤会产生另一套语义；复用活跃训练进程会污染其 RNG、缓存或状态。已有执行器按固定镜像拉取工作，GPU 租约和执行尝试不能代表 CPU 辅助任务。

## Decision

在现有执行器中增加独立 CPU 监督线程和子进程，以单独的领域记录、资源租约、journal、客户端、取消标志和继承 flock 管理。GPU 训练监督器继续持有自己的资源；CPU 预览不创建训练执行、不加载 Docker socket、不获取数据库或存储管理凭据。

首个资格范围为固定 Ultralytics YOLO11 图片检测。使用相同原生数据集、增强和批预处理流水线，单独进程隐藏 CUDA，限制单核／单线程、虚拟地址 16 GiB、CPU 30／35 秒和墙钟 45 秒。服务器 60 秒请求停止。最多 16 个有界冻结来源、4 张输出，按字节、像素和框数同时限制。LibreYOLO 保持明确不可用，直到独立资格验证完成。

请求冻结完整训练配置／定义／镜像、数据快照／包和标签、原生参数、来源选择策略、独立预览种子和轮次。观察包装器不改变原生数组和 RNG，仅记录来源取用及裁剪／过滤操作。原生输出携带完整框与源码指纹；训练草稿、摘要和随机流不接收预览控制项。

持久化 claim 和 finish 收据支持精确重试。失联、撤权、取消和超时不能释放活跃或未知 CPU 进程租约。内存保留 `Popen`，包括 PID 写盘失败的窗口；同 boot 重试发布前须确认退出。新 boot 必须重新取得子进程继承的 flock。旧 PID 不作为跨 boot 信号目标。

入队、领取、成功发布和查看均复核当前项目与 scope 权限；失去权限后仍允许有效 fence 确认停止。产物清单和短期上传授权绑定所有者、完整输入、长度和 SHA，每次读取重新校验对象。共享预览行锁串行读取与删除。24 小时过期，最后一次 60 秒 PUT 授权关闭后才能清理。清理存储期间不持 CPU 租约锁。

## Consequences

正向：

- 预览与训练共享原生变换语义，来源和完整框可追踪，确定性测试比较观察前后的真实字节与坐标。
- 独立 CPU 所有权允许预览与 GPU 训练并发；错误与撤权不会污染训练取消状态或错误释放资源。
- 冻结输入与已验证产物支持明确的幂等、过期、损坏和权限语义。

负向：

- CPU 和主机内存仍与训练共享，存在有限资源竞争；单个预览只反映指定种子的随机批样本。
- 多一组表、内部协议和持久 journal；执行器需重建，新增任务注册需刷新 worker 与 beat。
- 未确认退出的租约可能需要运维协调，不能用简单超时回收；预览清理前数据版本仍受引用保护。

## Alternatives Considered

浏览器模拟变换不能保证原生过滤、几何和随机语义一致。把预览伪装成训练会错误使用 GPU 租约并混淆产物历史。新增容器调度服务扩大部署和凭据范围，现有受控镜像及拉取协议已经能够提供独立 CPU 子进程。资格失败时保留冻结原图查看与参数解释，不生成近似增强结果。

## Notes

- 实现：`apps/_shared/training_contract/training_contract/preview.py`、`apps/api/app/services/training_previews.py`、`apps/training-executor/training_executor/preview.py`、`preview_worker.py`。
- 迁移：`apps/api/alembic/versions/0181_training_previews.py`。
- 计划：`docs/plans/archive/1792036036_training-augmentation-preview.md`。
