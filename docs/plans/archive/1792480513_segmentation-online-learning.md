# 图像实例分割在线学习链路计划草案

> Status: implemented / local acceptance complete
> 创建日期：2026-10-20
> 范围确认：图像实例分割优先，覆盖多边形、画笔及 SAM 修正后的实例掩码。
> 事实基线：当前 checkout HEAD `0a24819f4`；编写前工作区干净。
> 实施授权：用户于 2026-10-20 明确要求“开始实施”；按 S1–S7 顺序验证各独立结果，不预留发布版本。

建议复用现有在线学习设施，以 Ultralytics 已合格的 `yolo11n-seg` 为首个模型，补齐“冻结数据 → 训练 → 受控模型 → 试用与人工发布 → 实例掩码预标注 → 人工修正与审核 → 新数据版本 → 再训练”。在线学习采用有边界的批次迭代，每次冻结输入与模型身份；自动训练和主动选样在人工闭环成立后分别开放。

最小可用替代方案是继续下载当前简单多边形训练包与权重，人工管理部署。推荐方向增加平台受控回流和原生掩码训练，收益是让画笔／SAM 修正真正回到下一轮训练，并保留数据、权重、类别、评估及发布来源。

## 1. 完成标准与范围

完整链路须完成两轮可追溯迭代：第一轮从已审核实例标注训练并发布；第二轮对该模型的预测作人工修正、重新审核、冻结新版本并训练候选，再由负责人比较、试用和选择发布或保留旧版。训练成功与模型质量提升分别验收。

| 结果         | 完成定义                                                                                       |
| ------------ | ---------------------------------------------------------------------------------------------- |
| 人工完整闭环 | 多边形与原生 raster mask 均有明确训练入口；孔洞、多部件及重叠实例不被静默填充、拆分或合并      |
| 任务共存     | 同项目检测、分割的当前发布、历史代次、策略和基线独立，现有检测入口继续可用                     |
| 可信评估     | 训练报告区分 box／mask；逐样本结果使用 mask IoU；候选在同一冻结基准和评估协议下比较            |
| 可控自动化   | 策略可关闭、限频、阻断重复失败，默认不启用；产生候选，不自动发布                               |
| 可追溯与恢复 | 任一预测可定位权重、模型、部署、发布、训练尝试和数据版本；失败、取消、失联及回滚不覆盖人工真值 |
| 主动学习     | 建议样本经人工接纳、标注、审核后进入训练；有同预算随机对照和历史数据防污染                     |

本次规划包含图片实例分割的单项目训练、受控 Ultralytics 模型回流、原生掩码训练适配、样本评估、兼容权重初始化、自动重训和主动选样。复用现有 API、PostgreSQL、Celery、对象存储、训练执行器及 YOLO 推理服务。

语义分割、全景分割、视频时序训练、点云、跨项目合并、逐样本实时反向传播、伪标签自动转真值、自动替换线上模型和分布式训练不在本轮。SAM 继续作为辅助标注与修正工具，本轮训练目标是项目实例分割模型，不包含 SAM 基础模型微调。LibreYOLO 分割、更多模型规格、增强预览与参数搜索列为有明确启动门的独立扩展，不阻塞主链路。

## 2. 已有基础与真实缺口

[ADR 0085](../../adr/0085-qualified-polygon-instance-training.md) 和 [T14](1792036132_training-task-qualification.md) 已交付简单多边形训练，明确没有交付分割推理回流。当前代码优先于历史计划：新数据契约已经是 schema 3，`task=segment`、`media_type=image`、`format=ultralytics-yolo`；旧 schema 2 的 `image-instance-segmentation` 仍作为历史读取身份。

| 链路           | 当前事实与代码锚点                                                                                                                                                                                                                               | 本计划增加的结果                                                            |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------- |
| 冻结数据       | [dataset.py](../../../apps/_shared/training_contract/training_contract/dataset.py)、[training_datasets.py](../../../apps/api/app/services/training_datasets.py)；仅合格简单多边形                                                                      | 新增原生实例掩码桥接，保留已有契约和历史摘要                                |
| 划分与增量     | [training_dataset_bridge.py](../../../apps/api/app/services/training_dataset_bridge.py)；内容／场景连通分组、祖先分区、语义摘要已存在                                                                                                               | 掩码像素摘要和桥接身份进入增量语义，不把操作版本噪声算作新样本              |
| 训练与恢复     | [train.py](../../../apps/training-executor/training_executor/train.py)、[catalog.py](../../../apps/_shared/training_contract/training_contract/catalog.py)；原生 segment 训练、取消、完整检查点恢复、B/M 报告及下载                                    | raster 数据消费者、对应验证器及受控初始化资格                               |
| 模型注册与推理 | [model_return.py](../../../apps/_shared/training_contract/training_contract/model_return.py)、[trained_models.py](../../../apps/api/app/services/trained_models.py)、[managed_models.py](../../../apps/yolo-backend/managed_models.py) 多层限制 detection | 分割独立资格、region 类别绑定、严格安装收据与实例掩码输出                   |
| 普通分割输出   | [predictor.py](../../../apps/yolo-backend/predictor.py) 当前读取 `masks.xy` 形成 polygon                                                                                                                                                            | 受控路径读取原生逐实例二值 mask，保留孔与多部件，禁止缺类别／分数时回退为零 |
| 当前发布       | [trained_model.py](../../../apps/api/app/db/models/trained_model.py) 的 selection 主键与 generation 按项目                                                                                                                                          | 按项目与任务独立发布及回退                                                  |
| 自动策略       | [learning_policy.py](../../../apps/api/app/db/models/learning_policy.py) 每项目一条；[learning_policies.py](../../../apps/api/app/services/learning_policies.py) 仅 detect                                                                             | 独立 segment 策略和基线；保留项目级自动训练串行上限                         |
| 样本评估       | [sample_evaluations.py](../../../apps/api/app/services/sample_evaluations.py)、[sample_evaluation_engine.py](../../../apps/api/app/services/sample_evaluation_engine.py) 使用 bbox 真值及检测匹配                                                      | 掩码真值、像素证据、mask 匹配、错误回链和可比性检查                         |
| 主动选样       | [active_learning.py](../../../apps/api/app/services/active_learning.py)、[active_learning_engine.py](../../../apps/api/app/services/active_learning_engine.py) 使用检测启发式                                                                          | 分割独立策略与可解释评分，继续使用随机对照及人工接纳                        |
| 人工掩码真值   | [ai_mask_accept.py](../../../apps/api/app/services/ai_mask_accept.py)、[mask_mutation.py](../../../apps/api/app/services/mask_mutation.py)、[raster_mask_storage.py](../../../apps/api/app/services/raster_mask_storage.py) 已有原子变更和内容寻址 RLE    | 复用这些所有者处理预测接受、编辑版本、审核及冻结；不另建可写掩码真值        |

现有多边形限制为每实例 512 顶点、每图 300 实例／8,192 顶点；有孔、多部件、raster mask、退化和原生 float32 重复实例均拒绝。训练已固定 `overlap_mask=False`、`mask_ratio=1`，受限空间／组合增强保持关闭；不能因上游框架支持更多参数而扩大平台资格。

开发概念文档仍有“分割采用 schema 2”的历史表述。实施首个相关节点时同步更新该说明；各阶段仅在完成原生与产品验收后，才将相应功能写为已支持。

## 3. 核心决策

### 3.1 一份实例真值，两种训练桥接

工作台继续使用现有 `region` 标注与 canonical COCO RLE，不引入新的 geometry。保留当前简单多边形训练桥接；新增逻辑身份 `geometry=instance-mask`、`format=aap-instance-rle`、`bridge=ultralytics-instance-rle-1` 的新数据契约。新契约使用独立 schema 修订，在转实施时按现有注册机制分配，不冒用旧 `ultralytics-yolo` 格式。

新增训练包包含冻结原图、逐实例 canonical RLE、类别顺序及 manifest。每个实例记录来源 annotation ID/version、源 geometry 类型、像素摘要、尺寸、面积、bbox 和源对象摘要。孔洞与断连部分保持同一实例；重叠实例分别保存，不压成互斥 class map。包内引用使用相对路径与摘要，不暴露私有存储路径，也不依赖训练时仍可访问当前项目对象。

简单多边形可在用户显式选择新桥接时，按冻结 rasterizer／坐标规则栅格化。保留原始顶点与转换身份，并记录分辨率引起的覆盖变化；向量到像素的转换不称为向量无损。旧多边形包、旧实验与摘要继续按原消费者运行。两种桥接之间不直接指定父版本；切换时须显式转换并导入已核验的历史分组／分区账本，拒绝跨分区合并，不重新随机划分祖先样本。

### 3.2 批次学习，人工真值，人工发布

预测、未审核编辑、评论、QC issue、主动选样建议都不是训练真值。进入下一轮必须满足已审核或现有“负责人逐项确认”规则；无目标必须显式确认。被模型漏检而完全未标的图片不能自动成为负样本。

每次训练固定全量合格训练集合与历史样本，先复用现有 20,000 张／20 GiB 上限，不默认仅用本轮增量微调。修正像素和类别会形成变化样本；只有 annotation version、编辑次数或评论变化不触发重训。类目增删或重排需要新类别空间、显式重建基线，不静默沿用旧权重头部或旧比较身份。

发布仍需负责人确认指定部署的试用结果。自动策略只产生候选；“指标较好”不替代发布操作。

### 3.3 发布与策略按任务隔离，资源仍受统一约束

`ProjectModelSelection` 改为 `(project_id, task)` 主键；`ModelRelease` 增加 task，generation 唯一范围改为 `(project_id, task, generation)`。回滚链和 CAS 检查均限定同任务；既有项目级 request_id 唯一约束保留，请求摘要包含明确的新增任务身份，防止同一请求跨任务复用。

`LearningPolicy` 改为 `(project_id, task)` 唯一；触发去重改以 `policy_id + policy_revision + candidate_digest` 为身份。策略、冷却、语义基线和失败阻断分别保存。项目级 advisory lock、每项目一个活跃自动训练、物理 GPU 租约和 lost owner 处理继续复用；两个任务排队串行，不争抢同一资源。

历史行回填为 detect 前必须核验模型／配置／父数据身份；未知或冲突记录阻断迁移，不能猜测。历史 JSON、请求摘要、release snapshot 和训练配置不重写。

### 3.4 预测保留原尺寸掩码，不以轮廓替代

受控 segment 推理使用固定的原生 mask 后处理、原图尺寸和阈值；保存原始实例索引、native class index、有限置信度、mask 摘要及相关 bbox。后端先校验数量和索引对齐，再编码，平台再次检查类别顺序、尺寸、二值前景及资格身份；缺失或错配整体拒绝，不使用 class 0／score 0 兜底。

优先使用协议已有的 mask 对象携带机制和内部 raster geometry，经现有存储服务物化为内容寻址 ref。推理服务不获取平台对象存储写凭证。若现有传输模型无法承载有界 RLE，在 S2 内完成加法扩展、生成契约及预算校验后才开放能力。

首批只开放整图 segment 源阶段，复用普通 AI 预标和工作台当前题入口。裁剪 segment 输出需要 ROI 内外像素、letterbox、整数偏移和掩码拼接的独立资格，未通过前拒绝该组合；不以原有 polygon ROI 变换直接替代。

### 3.5 同一评估身份下比较，质量提升用真实数据证明

训练最终报告继续保留原生 box／mask 指标，记录最佳权重与验证数据身份；原生 fitness 可能是多个指标之和，不显示成准确率。

逐样本评估另用版本化 mask evaluator：原图像素坐标、二值 mask IoU、同类一对一匹配、固定置信度／IoU 阈值和稳定排序；展示 TP／FP／FN、错类关联、matched IoU／Dice、按实例像素面积分组的错误。匹配使用 mask 而非 bbox；Dice 只统计已匹配实例，漏检另计，不能用高 Dice 掩盖 FN。该报告不冒充 COCO AP。

发布比较必须核对数据／类别顺序、冻结权重、图像预处理、mask decoder、阈值、evaluator 与尝试身份。不同身份并排展示并说明差异，不排名。用于训练选择的验证集与独立留出测试集分开；留出集从主动池和训练候选中排除，记录访问与比较次数，避免反复调参污染。小型流程夹具仅证明功能，不证明业务精度提升。

## 4. 端到端数据流与身份

```text
Reviewed polygon / raster annotations + explicit negatives
  -> frozen dataset: source versions + class order + split ledger + mask bytes
  -> existing executor: pinned configuration + GPU owner + attempt
  -> verified best weights + final box/mask report
  -> task-aware model registration + exact deployment receipt
  -> fixed-image trial -> manual release for project + segment
  -> frozen preannotation pipeline -> exact backend + model weights
  -> staged instance-mask predictions
  -> human accept / correct -> new annotation version -> review
  -> next frozen dataset -> manual or policy-triggered training

Frozen evaluation set -> mask sample evaluation -> candidate decision
Unlabelled pool -> active selection + random control -> human assignment/review
```

训练输入只从已冻结版本读取；预测不能直接连到训练真值。事件和定期检查只触发幂等协调，不能在标注保存事务内执行训练。晚到结果写入原尝试的证据，不能覆盖新 owner、当前发布或更新后的人工标注。

| 对象       | 必须冻结的身份                                                                                      |
| ---------- | --------------------------------------------------------------------------------------------------- |
| 数据版本   | canonical task／media／bridge、类别及顺序、原图摘要、实例像素／标签摘要、来源版本、分区和历史账本   |
| 实验／尝试 | 数据版本、完整参数定义与有效配置、基础／初始化权重、镜像及适配器、资源 owner                        |
| 模型／部署 | 权重 SHA-256、Segment 架构与规格、类别绑定、输入输出契约、原生后处理、部署 check_id                 |
| 发布／预测 | project + task + generation、模型／部署／release ID、冻结管线及实例索引；直接候选试用显式标记未发布 |
| 评估／选样 | 固定样本／候选池、模型引用、预处理、evaluator／strategy、阈值、预算、seed 和结果尝试                |

## 5. 可独立交付的七个阶段

整体预计涉及超过八个文件、多处数据库约束以及三类以上运行组件；不新增常驻服务。以下是依赖顺序和独立结果，工作量为单名熟悉仓库工程师的粗估，包含对应验证、不承诺排期。每个阶段单独转实施、审阅和验收。

```text
Existing polygon training
  -> S1 task-scoped release ownership
  -> S2 managed segmentation return
  -> S3 native raster training ----> manual two-round closed loop
       -> S4 mask sample evaluation
            -> S5 segmentation warm start
                 -> S6 task-scoped automatic retraining
            -> S7 segmentation active selection

S7 does not require S6; S6 can remain disabled permanently.
```

### S1：发布身份按任务隔离（约 2–3 人日）

改造 selection、release、schema、路由、目录当前发布查询和前端任务筛选；历史 detect 回填和约束变更使用可核验迁移。当前模型注册能力不变，segment 发布位可为空。

旧 `project-release` 明确保留检测语义；新增 `project-release:segment` 动态别名，仅在请求接受时解析，再冻结为精确模型／部署／release。旧管线、已有请求和历史快照继续原样读取；不在 worker 执行时重新寻找“当前最佳模型”。

验收：检测发布、取消选择和回退保持行为；任务间 generation、并发 CAS、跨任务 rollback、request_id 重用被正确隔离。独立收益是同项目多任务发布所有权清晰，尚未合格的分割注册继续关闭。

回退：无 segment 写入前可按迁移约束回退；有新任务记录后采用应用前滚修复或兼容读取版本，不删除分割记录来迁就旧二进制。

### S2：Ultralytics 分割模型受控回流（约 4–6 人日）

以 `yolo11n-seg` 和当前固定依赖为唯一首批资格，补共享 eligibility、安装 schema、checkpoint 检查、pool 隔离、receipt、region 类别绑定、wire context、严格 raster 输出和预测写入。资格按运行时和输出契约声明，前后端复用同一判定，不成批删除 detection 守卫。

在现有“模型与发布”页完成注册、加载检查、原冻结验证图试用、人工发布和回退；在模型目录、普通预标与工作台当前题调用同一精确部署。保留草稿批次／工作台两种作用域检查、编辑锁和 worker 二次校验。

验收：非方图、重叠、同 bbox 不同形、孔洞／断连、空输出、错类索引、尺寸与数量错配、损坏权重、失效 token／部署、重复安装与任务晚到；RLE 写入和读回逐像素等于后处理结果。接受或编辑必须产生带来源的新 annotation，人工真值不被后续预测覆盖。

独立收益：现有多边形训练权重可回流产生可编辑掩码。此阶段尚不能宣称画笔修正可再训练，UI 明确当前训练桥接限制。回退关闭新注册／投递资格，保留已有模型、预测和人工标注；旧发布通过新代次恢复。

### S3：原生实例掩码训练与人工闭环（约 6–10 人日）

新增 mask 数据桥接及原生消费者，复用训练 executor 的 Segment trainer、loss、owner 和产物发布。实现受控 dataset／collate／validator 适配，直接将逐实例 mask 提供给原生训练与验证接口；不经过 `masks_to_polygons`，不对上游包全局 monkey-patch。

原图与 mask 共用冻结的 resize／letterbox／水平翻转参数，mask 使用最近邻；颜色变换只作用于图像。bbox 从变换后的 mask 推导。保留 overlap_mask=false、mask_ratio=1 和现有禁用增强；记录缩放后消失的实例及计数，在正式启动前报告，不静默移除正样本。验证器同样读取 mask 真值；标签缓存、batch 索引、顺序、空图、实例筛选和 loss 输入全部纳入资格。

冻结阶段将源 RLE 纳入 GC 保活根，创建快照与取得内容锁在同一事务提交；完整训练包校验并提交 ready 后，保活才转移到独立训练对象。仍可按同一快照重试的 failed／cancelled 数据版本继续保活未物化的源 RLE，包括派发失败场景，不随任务结束或历史版本 TTL 到期释放；只有完成独立物化，或版本显式删除并进入不可重试清理流程后才释放源引用。复用现有 mask 历史版本与存储 GC 所有者，并扩展 [cleanup.py](../../../apps/api/app/workers/cleanup.py) 的训练快照引用根；验证失败、取消、重试和删除并发。前台预检不读取无界 RLE，不从已经变化的原标注补数据。

首批候选预算：单图最多 16M 像素／300 实例、单样本编码 RLE 总量 16 MiB；单样本所有实例的原尺寸累计解码量最多 128M 像素元素，按实例数乘图像面积预检；batch 的实例 mask 训练平面合计最多 64M 元素。逐实例解码、变换并释放原尺寸 mask，不同时展开全部实例；首批原生数据加载固定 workers=0，不预取、不保留原尺寸 mask 缓存。资格测量计入 RGB 原图、uint8／float32 tensor、collate、loss 临时副本与 GPU 峰值，不能只按压缩 RLE 大小估计。推理和评估也须在实例 mask 重建前约束原尺寸累计输出及峰值。

以上是新桥接提案值，须经原生内存资格实测后冻结为能力参数；超限前置拒绝并说明原因，不靠捕获 OOM 后随机重试。整包继续受现有样本／素材总量上限约束，另冻结 mask 对象及压缩包预算。后续增加 workers／prefetch 需按并发倍数重新验收，不能沿用单进程证据。

验收：源 RLE、包内 RLE、实际消费者输入在原尺寸逐像素一致；变换后的 tensor 与独立参考结果一致。额外观测原生 loss 内部插值方式、mask／prototype 目标平面及实例索引，报告其中消失的细小实例；mask_ratio=1 不代表 loss 内部没有降采样。明确区分包内真值保真与训练目标分辨率限制。孔洞、断连属于同实例，重叠保持独立。

原生新 validator 使用独立 evaluator descriptor，记录实际 mask_processing、源码及选项；同步调整 training_evaluations.py 对新资格的显式校验，旧 descriptor 继续按原实现读取，不伪装成旧评估身份。真实原生短训练、验证、取消／同实验续训、最佳权重重载和下一轮注册均通过；再完成一次“预测→画笔或 SAM 修正→审核→新 mask 数据→第二轮训练”。

独立收益：人工完整闭环成立，可长期手工选择训练和发布。回退停新 mask 数据／实验领取，保留已有包、模型和报告；历史数据仍用其原始消费者。S2 的模型资格必须接受已核验的新 bridge 来源，不能只允许旧 polygon 数据身份。

### S4：掩码样本评估与候选决策（约 3–5 人日）

扩展现有 sample evaluation 服务，加入任务适配器、mask 真值、冻结预测证据、独立匹配器、结果与工作台回链；保留既有模型血缘和追加决策账本。原检测 evaluator、阈值及历史结果完全使用原读取身份。评估证据优先存于现有 evaluation 所有者管理的独立前缀；若引用公共 raster 内容，则将未删除的冻结评估规格／证据纳入对应 GC 保活根，不仅依赖原标注仍存在。

首批复用现有最多 32 张的逐样本分析窗口，不将其包装成完整业务评测集。原生最终验证负责全验证集 B/M 报告；若发布采用更大固定集合，需有界分页执行及汇总，不能将未执行样本计为零。掩码证据预算独立于检测当前 256 KiB 响应上限；按 S2/S3 的像素与编码预算建立流式读取、对象持久化和保留期。

验收覆盖 bbox 相同但 mask 不同、错类、完全漏检、空图误检、孔洞、重叠、阈值边界和稳定排序。旧基线过期或类别／decoder 不一致明确报错。负责人可查看两模型同图叠加、错误计数、掩码差异和来源；选择模型仍是人工决策。

独立收益：能定位分割候选的错误并形成可审计比较，手动学习继续可用。回退关闭新评估创建，保留历史结果和原消费者。

### S5：分割权重初始化（约 2–3 人日）

扩展 shared initialization、API 来源校验和原生加载资格，仅接受本项目同任务、同架构、同类别顺序、已核验权重。新实验只继承权重，重置 optimizer／scheduler／epoch／随机状态并固定新数据与配置；resume 仍恢复原实验全部状态。

默认使用累计合格训练集合，避免仅学习新增样本而遗忘历史。类别变更时提示改用目录权重创建新实验；不自动替换 head。初始化来源显式固定，后来发布新模型不会改写已保存策略或实验。

验收以实际原生 state、起始 epoch、optimizer、新数据身份和加载权重摘要证明 fresh training／resume 的区别；比较冷启动与 warm start 在同一基准下的结果，不假设后者必然更优。

独立收益：人工迭代可从上一轮合格权重起步。回退仅关闭新来源选择，已有初始化实验和来源引用保留。

### S6：按任务隔离的自动重训（约 3–5 人日）

将 LearningPolicy／LearningTrigger 迁移为第 3.3 节的任务所有权；扩展模板、父数据、自动 snapshot、语义分区、claim 和 UI。旧接口缺 task 时明确按 detect 读取；新增 segment 策略必须显式携带 task 且与父数据、参数、初始化身份一致。

继续使用现有 min_changed_samples、check_interval_minutes、cooldown_minutes 和训练预算，不添加第二套定时器。数据仅修正验证集时不触发训练；类别／bridge 切换进入需负责人重新确认的基线状态。成功后推进该任务基线；失败对同一 train digest 阻断，新增合格变化或明确人工重试才再触发。

资格失效、项目停用或权限撤销时禁止新领取；关闭策略按现有状态机取消未启动工作，运行中任务通过现有停止流程处理，lost 不释放 GPU。两个任务共享“每项目一个活跃自动实验”的上限，调度按到期时间稳定排序，不能因检测策略反复检查让分割永久饥饿。

验收：检测与分割各有策略及冷却；同内容重复编辑不触发，像素／类别修正被识别；重复 beat、并发检查、请求重试只产生一次有效触发；关闭、取消、失败阻断、无权限及存储故障都有可读原因。候选完成不改变线上发布。

独立收益：无需手动定期创建同类实验，仍可手工训练与发布。回退先关闭 segment 策略和待领取任务，保留检测兼容路径与所有历史记录；多任务数据存在后不执行破坏性 schema downgrade。

### S7：分割主动选样与人工收益反馈（约 3–5 人日）

在现有轮次、分组去重、随机对照、人工接纳和反馈中加入 segment 适配器。首批策略使用“最大 1-confidence”和“跨类别最大 mask IoU”两个可解释分量，分别保留原始值，以两个分量的最大值排序；该分数不称为校准不确定性、像素熵或模型质量。没有概率图时不构造虚假的边界熵。

继承 25% 随机探索和独立等预算随机对照，冻结 seed、策略和图像来源；零实例输出视为缺少信号，通过随机探索获得覆盖，不能排到永远不会审核的位置。排除所有训练／验证／留出集历史成员及相连场景组。

人工接纳建议只复用分派／批次规则，进入工作台仍需完成标注审核；计算修正实例数、耗时和后续固定评估变化时分别报告样本量及缺失证据。真实收益比较以同预算随机对照、固定留出集、相同训练协议为准，不能把接纳率等同于精度提升。

验收：评分用 mask 交叠、场景去重和历史排除有效；已分派／编辑锁变化后接纳冲突；两组预算、随机性和重试幂等可重现。回退停止新轮次；已人工接纳的任务继续正常标注，历史建议不回收人工工作。

## 6. API、数据迁移及用户流程

| 接口／数据面                        | 计划变更及兼容要求                                                                                      |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------- |
| 训练数据创建／预检                  | 复用现有路径；增加显式桥接选择及能力查询，返回不支持实例、尺寸、退化和对象预算原因                      |
| 模型注册／部署                      | 路径保持；任务从实验与权重一致性核验，segment 使用 region 类别；receipt 加任务／输出契约身份            |
| 发布列表／创建／回退                | 增加 task 字段／筛选，旧请求缺失按 detect；新任务参与 CAS 和摘要；历史摘要按原 schema 验证              |
| 动态模型引用                        | 保留 project-release；新增 project-release:segment，接受请求时解析；直接 trained:ID 从冻结模型核验 task |
| learning-policy 与 triggers         | 复用路由，通过显式 task 选择独立策略；旧客户端只访问 detect，不返回任意一条策略                         |
| sample-evaluations／active-learning | 增加 task-aware capability 和版本化 mask evaluator／strategy；不让 detection parser 猜测 raster 数据    |
| 生成契约                            | 各节点同步 OpenAPI、web 类型、SDK snapshot／coverage；不在客户端手工复制公共能力表                      |

发布隔离在 S1 迁移，策略隔离在 S6 迁移，避免一次引入所有数据库变更。每次迁移先做只读身份预检，再作加法列／回填／约束切换；标注真值、历史权重、数据包及摘要不重写。兼容部署窗口内旧写入只能落 detect；多任务写入开启前，确认所有运行中的旧代码已退出或只读。

产品继续复用“训练实验／训练数据／模型与发布／自动训练”四个标签页，不增加永久侧栏入口。任务切换保持已有 URL、分页和异步隔离模式；模型与策略的任务选择须明显可见。关闭能力应说明具体资格缺口，不能显示可点入口后再返回笼统错误。hash、内部 ID 继续沿用现有复制交互。

## 7. 失败恢复、预算与可观测性

| 失败或压力             | 处理与验证                                                                                            |
| ---------------------- | ----------------------------------------------------------------------------------------------------- |
| 预检后原图／mask 变化  | 冻结版本和摘要冲突失败，保留原请求重试语义；不临场读取当前 annotation 替补                            |
| RLE GC 与冻结并发      | 冻结引用进入保活根，完整包 ready 后才转移保活；可重试失败／取消版本保留源引用，验证重试和删除清理账本 |
| 对象存储／部署不可用   | 使用既有有界重试与状态；不切换权重、不把缺失 mask 当空预测或负样本                                    |
| GPU 忙／失联           | 原所有者和租约继续有效，新训练排队；不能为缩短等待回收仍可能运行的进程                                |
| 数据增至十倍           | 预检分页、RLE 有界流式处理、全量训练包硬上限；样本评估与主动池按既有窗口，超限说明而非无界扩容        |
| 上线后质量回退         | 对该任务创建回滚发布代次；已投递工作继续使用冻结旧引用，下一次新请求使用回退版                        |
| 代码／协议回退         | 停新资格和新领取，保留能读取新记录的兼容版本；已有多任务记录后不宣称可直接运行旧二进制                |
| 新消费者不满足掩码保真 | 停在 S2，继续训练已合格多边形、下载与人工部署；不以近似转换宣布完整闭环完成                           |

复用现有训练／评估／异步任务观测，增加 task、bridge、mask decoder 和像素／对象预算诊断，避免日志写 mask 正文。区分新审核独立图片数、像素／类别修正样本数、选样接纳数、数据排除数、训练等待／执行耗时、推理延迟及 mask 错误；标签数量和原生 fitness 不当作业务提升指标。

最脆弱假设是：固定 Ultralytics 原生 loss 与 validator 能通过受控适配消费精确逐实例 bitmap，且训练、验证、恢复和产物来源仍一致。S3 的消费者、真实原生执行和逐像素测试是启动资格，不是默认事实。若不成立，完整掩码闭环不得开放；另立框架适配决策，不扩大 polygon 转换损失来绕过。S1/S2 仍独立可用。

## 8. 验证矩阵及实施入口

本节记录最初的验证要求；实际执行结果与剩余边界见文末 Outcome。T14 的历史训练／续训记录不替代本计划新增验收。

| 验证层     | 必须覆盖的行为                                                                                                     |
| ---------- | ------------------------------------------------------------------------------------------------------------------ |
| 共享契约   | 新旧任务身份、未知 bridge 拒绝、历史检测序列化不变、mask 摘要、类序与资格矩阵                                      |
| 数据与存储 | 孔／多部件／重叠、显式负样本、损坏与超限 RLE、源版本漂移、GC 根、分区继承和类目变更                                |
| 原生消费者 | 非方图、EXIF 对齐、实际 resize／letterbox／flip、像素一致、tiny instance 消失报告、空 batch、loss／val 同真值      |
| 模型回流   | 实际 Segment 头、权重摘要、native index 对齐、安装收据、认证、精确实例路由及丢失部署副本恢复                       |
| 发布与策略 | task 级 CAS／回退、旧客户端 detect 兼容、迁移冲突、重复 beat／检查、权限变化与取消／lost                           |
| 评估和选样 | mask 匹配、错类／漏检／空图、可比性、历史排除、随机对照、预算、迟到尝试和接纳竞态                                  |
| 浏览器     | 两轮完整流程；桌面／390px、明暗主题、键盘、刷新／返回、延迟响应／切任务、权限和空／错误态；查看实际 API 与 console |

定位现有回归入口，按每阶段改动挑选测试，不在写草案时执行整套应用测试：

- shared：`apps/_shared/training_contract/tests/test_dataset.py`、`test_model_return.py`、`test_initialization.py`。
- executor：`apps/training-executor/tests/test_adapters.py`、`test_executor.py`、`test_initialization.py`；S3 新增 raster consumer／validator fixture。
- backend：`apps/yolo-backend/tests/test_managed_models.py`、`test_predict_contract.py`、`test_sample_evaluations.py`。
- API：`apps/api/tests/test_training_dataset_bridge.py`、`test_training_datasets.py`、`test_trained_models.py`、`test_trained_model_routing.py`、`test_pipeline_validation.py`、`test_sample_evaluations.py`、`test_learning_policies.py`、`test_active_learning.py`、`test_training_initialization.py`。
- web：复用 Projects／Training 下对应 API、组件与行为测试，新增任务发布切换、mask 评估和策略互不覆盖回归。

转实施后先核对 runtime doctor 和可丢弃 test 模式，再使用当前 checkout 虚拟环境。例如：

```sh
pnpm dev:worktree -- doctor --mode dev
pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && .venv/bin/python -m pytest tests/test_training_dataset_bridge.py tests/test_trained_models.py tests/test_pipeline_validation.py -q -p no:cacheprovider'
pnpm lint:python
pnpm --filter @anno/web typecheck
pnpm --filter @anno/web lint
pnpm --filter @anno/web lint:css-tokens
pnpm openapi:export
pnpm openapi:check
pnpm codegen
pnpm docs:build
git diff --check
```

上述是已有命令与首批定位入口；各阶段转实施时冻结精确测试列表、SDK 契约检查和受控原生镜像命令。数据库集成仅运行于已验证可丢弃实例，不能直接对日常数据库执行 pytest。原生执行在固定镜像／独立虚拟环境验证，不借 API venv 安装另一套 Torch。刷新 worker 前核对真实挂载；共享 Python 契约／依赖变化时重建受影响镜像，并确认新签名／能力已在实际 worker 内生效。

每次测试后清理该次产生的临时图片、RLE、截图、报告、缓存、训练／评估对象和可丢弃数据；保留用户数据和既有验收环境。两轮原生端到端验收须保存必要摘要证据，再清理所属项目、任务、对象和临时容器；不把验收权重默认为用户长期模型。

## 9. 扩展触发门、文档与转实施条件

| 独立扩展              | 可核验启动条件                                                                                               |
| --------------------- | ------------------------------------------------------------------------------------------------------------ |
| 裁剪区域分割回流      | 整图 S2 通过；非方 ROI、边界裁剪、letterbox 与全图掩码重建逐像素通过，保留 root／stage 几何血缘              |
| 更多 Ultralytics 规格 | 该规格的 checkpoint／架构、mask 输出、原生训练恢复及预算独立通过；不自动继承 n 的资格                        |
| 分割增强预览          | S3 的真实变换消费者稳定；预览使用相同 mask 路径和身份，不用绘制模拟替代                                      |
| 分割参数搜索          | S4 比较身份与预算可靠；串行预算、取消、来源与排名资格独立通过                                                |
| LibreYOLO 分割        | 官方实现及本地受控依赖实际具备 segment train／val／predict／checkpoint，逐实例 mask 保真与运行时兼容全部通过 |
| 语义／全景／视频      | 至少有明确用户需求和独立任务／真值／评估决策；不复用实例 RLE 伪装 class map 或时序身份                       |

新增常驻服务、环境变量、CLI 和外部账号均为零。继续使用项目已有数据库／对象存储认证、训练 executor 凭证和模型市场服务认证；外部权重与镜像首次获取沿用现有受控来源和摘要流程，可预先准备以脱离外网运行。编写时只读确认 pnpm、uv、docker 命令和 API venv 存在、官方资料可访问；未确认 Docker daemon、当前 GPU 可用量、镜像与线上认证，均在转实施的 runtime 预检核验，不视为已就绪。

实施相应节点时更新[在线学习用户指南](../../../docs-site/user-guide/online-learning/index.md)、[模型回流指南](../../../docs-site/user-guide/online-learning/models.md)、[训练 API 指南](../../../docs-site/api/guides/training-datasets.md)、[训练架构说明](../../../docs-site/dev/concepts/training-datasets.md)、[预标注架构](../../../docs-site/dev/concepts/prediction-pipeline.md)及 CHANGELOG Unreleased；任务发布隔离和原生 mask bridge 各自记录 ADR。API 改动同步 README、生成工件和 SDK 契约。只在真实新增环境配置时修改 .env.example／环境变量文档／DEV，不为预留功能新增开关。

转实施负责人须按以下顺序核对：最新 HEAD／工作区与已交付节点 → 当前 task／bridge／能力 → 数据迁移和旧客户端兼容 → 精确文件清单与生成工件 → 可丢弃数据库与运行镜像 → 本阶段验收／回退 → 将该阶段计划置于 docs/plans 根目录并取得实施授权。旧 A–E／T01–T14 的历史授权不自动延伸到本草案。

建议先推进 S1–S3，获得可手工操作的完整掩码学习闭环；S4/S5 增加可信比较与初始化，S6/S7 分别降低操作成本和审核成本。粗估主线合计 23–37 人日，原生消费者是主要不确定项；业务评估集制作与模型收敛时间不包含在此估算内，不据此指定发布日或版本。

## 10. 外部依据与取舍

- [Ultralytics 实例分割官方说明](https://docs.ultralytics.com/tasks/segment/)区分 box 与 mask 指标，并提供逐实例 bitmap 输出。本计划沿用原生训练／预测能力，具体资格以仓库固定依赖及实测为准，不跟随官网最新示例切换模型。
- [官方分割数据格式](https://docs.ultralytics.com/datasets/segment/)规定一实例一行归一化多边形。因此当前 YOLO 文本标签不能直接表示完整实例 RLE；新增桥接不能只改导出文件名。
- [固定版本 SegmentationPredictor 实现](https://raw.githubusercontent.com/ultralytics/ultralytics/v8.4.177/ultralytics/models/yolo/segment/predict.py)提供原图 mask 后处理路径；借用其后处理机制并冻结参数，不将 contour 当作 bitmap 真值。
- [固定版本 Format 实现](https://raw.githubusercontent.com/ultralytics/ultralytics/v8.4.177/ultralytics/data/augment.py)从 segments 格式化 instance masks。由此推断 raster 适配须覆盖实际 dataset／format／collate／validator，不能只向普通 YOLO 目录写 RLE。
- [CVAT 实际 YOLO segmentation 导出器](https://raw.githubusercontent.com/cvat-ai/cvat/develop/cvat/apps/dataset_manager/formats/yolo.py)显式执行 masks_to_polygons。本计划借鉴其格式适配边界，但不采用此转换作为训练保真方案。

没有选择“只解开分割 if 守卫”：安装、类别、geometry、评估、策略及项目发布所有权都存在独立约束。没有选择“另起训练服务”：现有执行器已具备训练与恢复所有权，新增服务会复制资源租约、凭据、发布和运维流程。原生 raster 适配若无法满足验收，须重新评估框架路线，不能降低真值合同。

## Implementation S1

- Fresh baseline: HEAD `0a24819f4`; only this plan and its backlog index were uncommitted. The primary API/Web are systemd user services; primary Celery and inference services are Compose-owned. Production containers are out of scope.
- Exact schema scope: `apps/api/alembic/versions/0192_task_model_releases.py`, `app/db/models/trained_model.py`, `app/schemas/trained_model.py`, and `app/schemas/model_catalog.py`.
- Exact behavior scope: API `api/v1/trained_models.py`, `api/v1/projects.py`, services `trained_models.py`, `model_catalog.py`, `model_lineage.py`, and the controlled-reference callers found by repository search.
- Web scope: `src/api/trainedModels.ts`, project model/release views, their task URL owners and related behavior tests. Generated OpenAPI, web and SDK contract artifacts are refreshed centrally.
- Migration: validate historical model, source-run, snapshot and selection identities before assigning detect; preserve immutable payloads; add task-scoped selection PK and release generation uniqueness. Downgrade rejects retained segment records.
- Verification: task-scoped release/CAS/rollback/idempotency tests, model catalog and lineage, frozen prediction routing, migration drift, UI/API adapter tests, typecheck/lint, CSS tokens if styling changes, SDK contract, OpenAPI and documentation checks. Database tests use verified disposable worktree test mode only.
- Runtime: inspect the development restart wrapper and migration target before refreshing API/Celery; browser acceptance uses an owned isolated session and removes its fixtures and artifacts.

## Outcome

- 已实现 S1–S7：任务独立发布与策略、受控原生实例掩码回流、schema 4 RLE 训练、mask 逐样本评估、同项目分割权重初始化、周期重训和带随机对照的主动选样。旧检测及简单多边形身份保持兼容。
- 发布安排：尚未确定；本次没有升级版本或部署生产环境。
- 用户文档：`docs-site/user-guide/online-learning/index.md`、`docs-site/user-guide/online-learning/models.md`。
- 开发与接口文档：`docs-site/dev/concepts/instance-segmentation-learning.md`、`docs-site/dev/concepts/training-datasets.md`、`docs-site/dev/reference/ml-backend-protocol.md`、`docs-site/dev/reference/yolo-import.md`、`docs-site/api/guides/training-datasets.md`、`docs-site/api/guides/projects.md`；README、DEV、OpenAPI、Web 类型与 SDK 契约同步。
- ADR：`docs/adr/archive/0089-task-scoped-native-instance-learning.md`；CHANGELOG 已增加 Unreleased 条目。

### 本地真实链路证据

2026-10-20，在独立 e2e 数据库、九个独立存储桶、独立 Redis 和私有验收容器中执行。训练沿用平台物理 GPU 租约及同设备文件锁，设备为 RTX 3090。固定镜像 manifest 为 `sha256:e3969cbab1289329609bda1f9a296106124e0802a285b77510e0776a3a933d7e`。

1. 四张 128×96 夹具覆盖孔洞、断连、重叠实例和显式负图；实际人工提交、独立复核、冻结及原生训练包构建成功。冻结 bundle SHA-256：`d436270c5dd9ea2c0abc07315cadd05864932d28feafad53196b70d5088563d5`。
2. 第一轮三 epoch 训练 `ba8f8bee-f716-438d-8e06-5cab23d57be5` 成功，best SHA-256 为 `47a0149eb4b11f87b23887632270421969f82e9a2ad17fa5c2884f0fa65717f8`；真实注册、GPU 安装、固定图片试用返回 19 个原生掩码，并人工发布为 segment generation 1。
3. 使用第一轮真实预测，人工接受并修正像素，再由另一用户复核；新数据版本 `35261639-7386-4940-b91a-c0cc1e5c0cd6` 继承划分。第二轮训练 `28fbccf4-a934-45ba-9d70-6638d5826242` 成功，best SHA-256 为 `9370b98f75e901e3c8e459474751340133ab159796139cc08c5dcd721d5be192`。
4. 第二轮原生初始化收据核验：561 个张量、183 个掩码头张量实际转移且摘要相等；epoch 0、optimizer state 0、scheduler -1、EMA updates 0、空 scaler、无历史 best。RLE 消费者记录实际 loss、validator 源码摘要和像素预算。
5. 两个候选均在同一冻结验证集完成 schema 2 mask 报告，均为 FN 2、无匹配；matched IoU／Dice 为 null，未将漏检伪造为高分。第二轮完成安装与试用，保留第一轮发布，不因训练成功自动替换。
6. segment 自动策略真实构建并训练第三个候选 `3adf188e-b005-4654-9918-9b268390a6ca`，成功后推进基线，提示仍需人工注册和发布。该策略只存在于可丢弃验收环境。
7. 主动选样对四张独立未标图片执行四次真实 GPU 推理；建议／随机对照各接纳一张，创建普通人工批次，未直接生成训练真值。
8. 实际浏览器验证冻结掩码的孔与断连、原生试用绘制、框／掩码分类别指标、主动选样证据及 390px 深色布局；所检页面 console 无错误。

### COCO8 夹具与受控分割生命周期补验

2026-10-20，当前 dev 的 `P-COCO8-TRAIN` 已增补官方 COCO8-seg 的 30 个原图实例 RLE，保留 8 张原图、30 个检测框和原 4/4 划分。新增分割数据版本 `c8c261eb-abab-4e68-a256-59f95b2bdac3` 已就绪，旧检测版本、执行、候选及发布整行摘要保持不变；再次播种没有新增版本或改写历史。浏览器实际读取了训练与验证冻结原图及逐实例掩膜，无 console 错误。640 输入、160 验证掩膜平面均没有实例完全消失。

原生命周期脚本已增加真实受控安装、收据回读、冻结引用及收据冲突拒绝、两轮 `trained:<版本>` 原图 RLE 冷加载、共享池完整卸载和受控预测故障矩阵。普通四任务模式保持兼容，但其报告不能替代受控分割资格。

- 实测固定 YOLO 镜像 `sha256:0bf2a78df66ac04043264f7f2e4632ebf6a3b3efefb9dffa3d979a64d3534f61`，对应后端源码提交 `38b56306e5cf61de726c61dc058107ab38512da0`，池容量 2。
- 物理卡为 RTX 3090 `GPU-ac5e7f45-7a36-c3f8-1f65-a54f28bfcd31`，驱动 `595.99.02`、CUDA `13.2`。隔离容器唯一可见该卡，验收期间没有其他 compute 进程。
- 候选来自本次真实 COCO8 原生 RLE CPU 训练，权重 SHA-256 为 `c43ec80645126e86697dad3c723f5b7cb35184ab1ec52b64285c1832cf4fabf6`；离线验收 UUID 只用于 Backend 安装，不声称已在平台注册或发布。
- 14 项检查全部通过，两轮各输出 11 个 640×426 原图实例 RLE。加载显存分别为 559／511 MiB，卸载均为 359 MiB，工作集回收率 100%，五个稳定样本波动为零；最终所有池、活动请求、builder／borrower 和 PyTorch allocated／reserved 均清零。
- 空闲开发 SAM3 仅在独占验收时短暂停止，原容器及镜像已恢复，健康为空池。未开启日常开发实例的生命周期验收开关，未变更生产服务或数据。
- 夹具 26 项及固定 YOLO 镜像 64 项测试通过，含显式原生 CPU 非方图资格。脱敏 JSON 证据保留在仓库外 `~/.local/state/ai-annotation-platform/acceptance/segmentation-20261020/`；验收容器、临时权重、测试数据库／桶／Redis 和浏览器已清理。

人工两轮回流、训练 GPU 故障矩阵、受控生命周期命令和跨 Backend 仲裁入口见[实例分割在线学习验收](../../../docs-site/dev/how-to/validate-segmentation-online-learning.md)。本机 GPU1 结果仅覆盖上述镜像、权重与卡；其他目标配置仍需独立验收。

### 回归与边界

- API 的数据库、存储、回流、发布、训练初始化、策略、评估、主动学习及迁移回归通过；包括跨层两轮闭环与仅像素变化计入增量的检查。新旧共享契约、固定原生 CPU 镜像训练／中断恢复与权重初始化检查通过。
- 前端集成 22 文件、285 项测试通过；分类别框／掩码修复另有 48 项定向回归通过。Web 类型、lint、颜色 token、OpenAPI 一致性、SDK 契约 4 项、文档构建与文档规范检查通过；现存 lint 警告不在本次范围内。
- 小型合成夹具仅证明功能与可追溯性，业务独立测试集、业务质量提升、各目标部署的受管 GPU 生命周期资格仍需项目方另行验收。本次未执行训练执行器 GPU 故障注入／断点恢复矩阵；原生恢复已在固定 CPU 镜像验证。并发与迟到所有权通过自动化回归，未将浏览器检查宣称为完整浏览器矩阵。
- 无生产数据或生产服务变更；验收数据库、桶、Redis、浏览器会话、临时容器、权重、报告与测试缓存在完成证据记录后清理，保留开发运行环境。
