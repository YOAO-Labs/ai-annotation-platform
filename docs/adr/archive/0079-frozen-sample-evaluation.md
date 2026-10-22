# 0079 — 冻结逐样本证据与后端请求所有权

- **Status:** Accepted
- **Date:** 2026-10-15
- **Deciders:** Maintainer-authorized implementation
- **Supersedes:** —

## Context

原生训练汇总不能解释某张图片的误检和漏检。直接重跑当前任务会混入可变图片、标注和模型选择，普通 HTTP 取消也不能证明 GPU 或加载线程已经退出。可恢复评估需要分开约束平台写入所有权、原生执行所有权和不可变报告身份。

## Decision

以独立 EvaluationRun、Attempt 和成员 checkpoint 冻结明确的验证成员、模型／部署／权重、类别绑定、用途、阈值、评估器、预处理与运行 profile。每次恢复使用新的 generation 和 claim，稳定成员 request UUID 不变；AsyncJob 是可空展示引用，源任务没有实时外键，其余持久来源保留 RESTRICT。

固定 1–32 个成员、单图 200／总计 2,000 个真值、100 个预测、16 MiB 原图和 1,600 万像素。预处理完整验证 SHA、EXIF 校正、RGB 转换及无元数据 PNG；原图和派生身份同时保存。评估器声明稳定置信度排序、同类最大 IoU 一对一匹配和明确平局规则，报告固定阈值 TP/FP/FN 与派生指标，不声称 AP/mAP。诊断只解释既有错误。

平台通过确切部署的 RoutedMLBackendClient 和既有准入执行，事务只覆盖领取与检查点提交，不持锁等待网络。后端持久 storage UUID、固定 flock 条带和 24 小时请求绑定，锁覆盖真实模型加载、预测及原子收据发布。完成收据可在没有新 GPU 准入时读取；封存持久墓碑并取得同一把锁，防止迟到 POST 在取消确认后启动。超时或失联本身不确认原生退出，不释放未知租约。profile 改变时拒绝未完成请求。

每个样本提交前复核当前 attempt、claim、generation、原创建者和 API Key 权限。发布完整报告前再验证全部 checkpoint，上传后读取验证 SHA、元数据和结构，并原子提交终态指针。每次读取重新校验存储并重算语义。报告候选清理必须先检查已提交指针，避免 COMMIT 确认丢失或并发取消删除另一方成功发布的对象。持久删除意图先于存储清理，后台继续回收失败删除及迟到上传。

当前源解析独立于历史真值：读取一致的当前引用与有界标注，条件读取对象、验证完整内容及规范标签，再复查授权和数据变化。前端明确提示变化，正常工作台编辑不改历史。单调会话代次、随机缓存 scope、视图修订及请求中止共同阻止账号、尝试、筛选和成员 ABA。

## Consequences

正向：

- 每张冻结图片的原生预测、匹配和来源可以独立核对，失败不会伪装为完整分数。
- 重复派发、响应丢失、取消与恢复共用稳定请求身份，既防止重复写入，也约束真实原生重叠。
- 当前标注修正与历史评估分离，同口径比较保留重复成员权重而排除候选权重身份。
- 复用 PostgreSQL、对象存储、Celery 与既有推理准入，无新增常驻服务或依赖。

负向：

- 新增三张表、后端持久收据和维护流程，需要迁移、重建推理实例与刷新 worker／beat。
- 执行限时和样本预算有意保持有限；超限、过期或 profile 改变的未完成评估需重新创建。
- 已保留评估会阻止来源数据、部署和项目删除；每次报告读验证增加有界存储与 CPU 开销。
- 当前只承诺受控 Ultralytics 图片矩形检测，不扩大到其他任务或训练器。

## Alternatives Considered

仅展示原生汇总产物无法解释成员误差。复用 Prediction／Annotation 表会使历史证据与当前候选、人工编辑耦合。只用 Celery 状态或数据库 claim 不能确认原生线程退出；只用进程内锁不能跨重启恢复。把可变签名源 URL 发给推理实例会留下验证后的内容替换窗口。新建独立评估服务会扩大部署范围，现有队列与受控实例足以承接有界请求。

## Notes

- 领域：`apps/api/app/services/sample_evaluations.py`、`sample_evaluation_engine.py`、`sample_evaluation_sources.py` 与 `workers/sample_evaluations.py`。
- 原生所有权：`apps/yolo-backend/sample_evaluations.py`、`main.py`、`predictor.py`。
- 迁移：`apps/api/alembic/versions/0183_sample_evaluations.py`。
- 计划：`docs/plans/archive/1792036036_training-sample-evaluation.md`。
