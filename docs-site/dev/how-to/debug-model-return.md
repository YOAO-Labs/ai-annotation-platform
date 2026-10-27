---
title: 排查模型回流与路由
audience: [dev, ops]
type: how-to
status: stable
last_reviewed: 2026-10-19
---

# 排查模型回流与路由

模型已训练成功但无法加载、发布或生成预标注时，先确定失败发生在哪个边界。训练执行器、平台 API、推理 Backend 和预标注 Worker 使用不同的接口与凭据，健康检查成功不能证明整条链路可用。

用户操作见[模型回流与默认预标](/user-guide/online-learning/models)，实例、服务池和 Token 的原理见[模型服务接入、路由与认证](../concepts/ml-backend-routing-and-auth)。本页用于定位当前实现中的请求与状态，不要求先修改运行配置。

## 先固定一组排查对象

从页面复制项目、来源实验、成功执行尝试、候选模型和目标实例的完整 ID，记录请求时间、HTTP 状态与错误信息。加载成功后再记录部署 ID，发布后记录发布 ID 和代次。不要仅凭同名模型或服务判断身份。

在浏览器网络面板中，先核对下表的平台 API 请求。它们以 `/api/v1` 为前缀，需要当前用户权限；服务端随后调用 Backend，浏览器不直接发送 Backend Token。日志与问题报告不要包含 Token、签名下载 URL 或完整认证请求头。

| 操作       | 平台 API                                                         | 应观察的结果                                                |
| ---------- | ---------------------------------------------------------------- | ----------------------------------------------------------- |
| 添加候选   | `POST /projects/{project_id}/trained-models`                     | 固定 `run_id`、`attempt_id`、权重和全部类别绑定             |
| 加载并试用 | `POST /projects/{project_id}/trained-models/{version_id}/deploy` | 请求体指定 `registry_id`，响应部署为 `ready` 或保留失败原因 |
| 查询模型   | `GET /projects/{project_id}/trained-models/{version_id}`         | 返回候选与部署记录，固定样本图链接按需签发                  |
| 发布或回退 | `POST /projects/{project_id}/model-releases`                     | 校验模型与部署组合、样本确认和 `expected_generation`        |
| 查看发布   | `GET /projects/{project_id}/model-releases`                      | 确认项目当前选择和历史，不以候选标记代替发布状态            |

## 按失败阶段定位

### 1. 候选注册被拒绝

检查所选成功尝试是否属于本项目实验，最佳权重与原生报告是否已验证，类别绑定是否覆盖全部原生索引。注册检查的是所选 `TrainingAttempt`，不能用实验最新状态替代它。

回流资格由 `training_contract.model_return.registration_eligibility()` 判断。当前检测范围内，Ultralytics 可回流，LibreYOLO 仅 YOLO9 t/s/m/c 的已核验原生权重可回流。分割训练、YOLO9 e、普通 Backend 目录中的其他模型能力不能据此获得回流资格。

代码入口：`apps/api/app/services/trained_models.py` 的 `register()`，以及 `apps/_shared/training_contract/training_contract/model_return.py`。

### 2. 服务不可选或认证不通过

先核对实例是否在项目中启用、认证方式是否为 `managed:yolo` 或匹配的手动 `token`，以及健康状态是否为 `connected`。这些是界面允许加载的前置条件，提交时后端还会再次检查项目、服务池与能力。

系统托管认证有三个不同结果：本地私有文件有效、目标服务接受凭据、实例健康与能力就绪。超管可从系统设置初始化并同步凭据，或编辑目标实例保存认证，再执行健康检查。普通 `/health` 和 `/metrics` 公开；受保护的 `/managed-auth` 才会验证凭据并返回 provider 与认证来源。

固定 provider 只向部署指定的 HTTP origin 发送自己的凭据，禁用环境代理与重定向。不能把任意外部 URL 标成系统托管以取得默认 Token。已有手动配置优先，初始化不覆盖、不轮换该配置。

平台入口为 `admin_ml_integrations.py` 的 `/managed-auth`、`/managed-auth/initialize` 和 `/registry/{registry_id}/health`，完整前缀是 `/api/v1/admin/ml-integrations`。认证实现为 `services/managed_inference_auth.py`，Backend 边界为共享运行时的 `backend_auth.py`。

### 3. 安装或固定样本试用失败

`install_and_trial()` 先调用 Backend `POST /managed-models`，核对收据，再向标准 `POST /predict` 发送一张原冻结验证图片。检查下载 URL 从 Backend 所在网络是否可达、权重摘要与原生类别顺序是否一致、训练和推理框架版本是否兼容。

预测使用 `trained:{version_id}` 及最小 `frozen_model_ref`，Backend 回传 `meta.trained_model` 和 `native_class_index`。平台核对身份、矩形框坐标和置信度后才映射项目类别。只看到安装收据不能将部署认定为 `ready`；重新加载可修复损坏副本，但不能改写候选权重。

代码入口为 `services/trained_models.py` 的 `install_and_trial()`、`wire_context()` 与 `validate_and_map_results()`，Backend 对应 `apps/yolo-backend/managed_models.py` 和 `main.py`。字段与状态码见[受控模型部署协议](../reference/ml-backend-protocol#_12-受控自训模型部署协议-managed-models)。

### 4. 试用成功但无法发布

确认提交的是同一模型与 `ready` 部署，`sample_confirmed` 为真，且 `expected_generation` 来自最新项目选择。并发发布冲突需要刷新、核对并重新确认，不能盲目覆盖代次。候选的待评估、归档等人工标记与发布记录分别保存。

发布前还会重读目标实例的安装收据。试用成功后服务丢失权重或认证变化，仍可能阻止发布，应修复该实例并重新核验。

### 5. 发布后任务没有进入预期实例

先比较任务冻结引用中的候选、部署、实例和发布代次，再查看任务创建时间。发布或回退只影响后续新建任务，旧任务继续使用入队时固定的版本。

普通服务池路由与受控模型固定实例需要分别判断。`off` 走 legacy 实例，`observe` 仅计算影子决策，`enforce` 才使用路由租约与接流状态；受控模型始终要求指定实例满足条件，不回退到其他未知权重。GPU 仲裁使用另一套逐资源 effective mode，访问 Token 不替代 GPU 准入或控制签名。

核对 `services/trained_models.py` 的 `resolve_reference()` 与 `assert_routable_backend()`，以及 `services/ml_routing/`。若输出存在但工作台看不到候选，再检查工作台显示阈值与审阅状态，不将显示过滤误判为服务没有返回结果。

## 修改后的验证范围

| 改动边界                         | 聚焦检查位置                                                                                    |
| -------------------------------- | ----------------------------------------------------------------------------------------------- |
| 注册、部署与发布                 | `apps/api/tests/test_trained_models.py`                                                         |
| 系统托管、并发认证变更与观测凭据 | `test_managed_inference_auth.py`、`test_managed_auth_routes.py`、`test_managed_observe_auth.py` |
| Backend 安装与请求恢复           | `apps/yolo-backend/tests/test_managed_models.py`、`test_sample_evaluations.py`                  |
| 回流资格                         | `apps/_shared/training_contract/tests/test_model_return.py`                                     |
| 界面入口、类别绑定和不可用原因   | `apps/web/src/pages/Projects/ProjectModelsPanel.test.tsx`                                       |

API 集成测试使用[独立测试数据库](../testing)，不能指向日常开发库。模拟响应测试只验证协议与状态处理；真实模型安装、GPU、网络及容器维护需在目标部署单独验收。执行器配置见[在线训练执行器部署](/ops/deploy/docker-compose#_8-5-受控训练执行器-training-executor)，服务升级见[本机 ML 服务维护](/ops/deploy/ml-backend-maintenance)。
