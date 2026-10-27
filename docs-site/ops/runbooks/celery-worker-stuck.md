---
title: Runbook：Celery Worker 卡死
audience: [ops]
type: how-to
status: stable
last_reviewed: 2026-10-26
---

# Runbook：Celery Worker 卡死 {#runbook-celery-worker-卡死}

当任务长时间没有进展时，先确认它所在的队列和实际消费该队列的 worker，再选择恢复分支。下面的“只读诊断”不会改变服务状态；“恢复操作”中的命令会启动或重启服务，执行前需确认对应的服务范围。

## 症状 {#症状}

- AI 预标注 Job 长时间停在 `running` 状态（超过 15 分钟）
- `docker compose ps` 中某个 celery worker 服务状态为 `Exited` 或 `Restarting`
- 超管失败预测页面无新进展

## 识别受影响的服务与队列

以下命令在目标部署的 Compose 项目目录执行；使用自定义 Compose 文件或项目名时，沿用该部署的 `-f` / `-p` 参数。先按任务类型找到队列，再按队列找到服务。`docker-compose.yml` 中的 `-Q` 参数是消费关系的权威来源。

| 队列                                   | 消费服务                      | 典型任务                                 |
| -------------------------------------- | ----------------------------- | ---------------------------------------- |
| `default`, `media`, `cleanup`, `audit` | `celery-worker`               | 通用任务、媒体处理、清理、审计           |
| `maintenance`                          | `celery-worker-maintenance`   | 分区维护、统计刷新                       |
| `gpu.control`                          | `celery-worker-gpu-control`   | GPU repair、tombstone GC、collector 账本 |
| `ml`, `gpu`                            | `celery-worker-gpu`           | GPU 预标注、视频追踪                     |
| `ml.cpu`                               | `celery-worker-cpu`           | CPU 预标注                               |
| `export`                               | `celery-worker-export`        | 导出                                     |
| `image-pyramid`                        | `celery-worker-image-pyramid` | 图像金字塔生成                           |

同一症状可能涉及多个队列。先记下受影响的 Job 或任务类型，再只处理对应服务；定时任务没有触发时，同时检查 `celery-beat` 是否运行。

## 只读诊断 {#快速诊断}

::: tip 只读检查

以下命令只读取服务状态、日志、队列长度和 worker 任务列表，不会启动、重启或删除任务。

```bash
# 1. 查看实际服务状态
docker compose ps --all celery-worker celery-worker-maintenance celery-worker-gpu-control celery-worker-gpu celery-worker-cpu celery-worker-export celery-worker-image-pyramid celery-beat

# 2. 查看目标服务最近日志（把 <worker-service> 替换为上表中的服务）
docker compose logs --tail=100 <worker-service>

# 3. 查看目标队列积压（把 <queue> 替换为实际队列）
docker compose exec redis redis-cli llen <queue>

# 4. 查看 worker 当前任务和待处理任务
docker compose exec <worker-service> celery -A app.workers.celery_app inspect active
docker compose exec <worker-service> celery -A app.workers.celery_app inspect reserved
```

如果服务状态正常但队列持续增长，比较该队列长度和 `inspect active/reserved` 的结果：没有消费记录通常指向错误的 `-Q`、worker 未连接 Redis 或 worker 没有运行；有活动任务但没有完成记录则进入“容器运行但无进展”分支。

:::

## 存储任务的无进展提示

扫描、预检／登记、容量盘点、迁移清单及复制任务的详情区分“已提交进展”和通用任务更新时间。只有实际接受的目录、页、条目或新结果才记录推进；心跳、续租和GET查询不会刷新。运行中有效记录超过900秒没有新推进时提示核实，首次观测前或历史未知不推断卡死。

先查看阶段、单位和最后推进时间，再按上文核对准确队列的active／reserved及所属节点日志。长时间单页读取、资源等待和尚无中间结果的prepare都可能暂时没有新记录；平台管理员核实基础设施，数据集管理员结合来源和轮次核实处理范围。角色提示不是个人认领，任务发起者也不自动成为处置人。

不要仅凭提示强杀任务或删除未收尾记录。迁移清单／复制仍须等待实际线程、Body、client与访问许可收尾，重派、恢复或关闭维护窗口沿各自既有协议执行。终态不再提示；它不证明源端已经停写或可以回收对象。

应用迁移0259后刷新实际执行这些任务的API与Worker，再核对新代码和任务注册。迁移只增加nullable观测列、不回填历史；挂载源码无需重建镜像。受控应用回退保留schema和观测事实，不通过删除列清空证据。

## 恢复操作 {#处理步骤}

先完成上面的只读诊断，再使用与队列匹配的服务名。不要为了一个队列重启整组 worker。

### 情况 A：容器已退出 {#情况-a-容器已退出}

::: warning 恢复操作

启动实际消费该队列的 worker：

```bash
docker compose up -d <worker-service>
```

例如，GPU 预标注和视频追踪使用 `celery-worker-gpu`，CPU 预标注使用 `celery-worker-cpu`。重启后对应 worker 会自动认领 pending 任务。

:::

### 情况 B：容器运行但无进展（Worker 卡死） {#情况-b-容器运行但无进展-worker-卡死}

::: warning 恢复操作

重启消费该队列的服务，并观察它重新连接 broker：

```bash
docker compose restart <worker-service>
docker compose logs -f <worker-service>
```

看到 `ready.` 后，再按“验证”确认目标任务有新的 received / succeeded / failed 记录。

:::

### 情况 C：代码变更后 Worker 运行旧版本 {#情况-c-代码变更后-worker-运行旧版本}

Celery worker 无热重载。改动后必须重启加载该模块的容器；改预标或 tracker 代码时通常需要重启 GPU worker。

::: warning 恢复操作

```bash
docker compose restart <worker-service>
```

:::

::: tip 只读验证

用实际服务名检查容器当前文件中的 callable 签名：

```bash
docker compose exec <worker-service> \
  python -c "import inspect, app.workers.tasks as t; print(inspect.signature(t.batch_predict))"
```

:::

该命令启动一个新的 Python 进程，只能确认容器文件中的签名；还需结合 worker 重启后的启动日志和实际任务记录，确认长期运行的 worker 已加载新代码。

### 情况 D：Redis 连接失败 {#情况-d-redis-连接失败}

::: tip 只读检查

```bash
docker compose ps redis
docker compose logs --tail=50 redis
```

:::

::: warning 恢复操作

重启 Redis 会中断 broker 连接；未持久化的消息可能丢失，不能把重启当作安全的清队列方式。先核对当前部署的持久化和恢复配置，并确认受影响任务的处理方式，再执行：

```bash
docker compose restart redis
docker compose restart <worker-service>
```

:::

## 验证

恢复后只验证受影响的 worker、队列和任务：

1. `docker compose ps <worker-service>` 显示服务运行中。
2. `docker compose logs --tail=100 <worker-service>` 出现 `ready.`，并且目标任务出现新的 received / succeeded / failed 记录。
3. `docker compose exec redis redis-cli llen <queue>` 的积压开始下降；没有积压时应保持为 0。
4. 在 Job 或失败预测页面确认原任务继续推进；如果仍无进展，保存新的日志和队列信息，进入升级路径，不要重复重启 Redis。

## 预防措施 {#预防措施}

- 生产环境建议为 Celery Worker 配置进程守护（systemd / supervisord）
- 设置 `CELERY_TASK_SOFT_TIME_LIMIT` 防止单任务永久阻塞
- 监控 `celery_queue_length` 指标（见 [监控与告警](/ops/observability/)）

## 相关文档 {#相关文档}

- [AI 预标注流水线](/user-guide/workflows/ai-preannotate-pipeline)
- [失败预测恢复流程](/user-guide/workflows/failed-prediction-recovery)
- [调试 Celery](/dev/how-to/debug-celery)
