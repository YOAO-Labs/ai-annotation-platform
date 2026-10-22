---
audience: [annotator, project_admin, reviewer, super_admin]
type: reference
since: v0.1.0
status: stable
last_reviewed: 2026-10-12
---

# 常见问题 (FAQ)

汇总账号登录、标注作业、质检审核、数据导入导出及 AI 辅助中的高频疑问与实用技巧。点击问题标题即可展开查看详细解答。

## 登录与账号

<details>
<summary><strong>Q: 忘记密码如何找回？</strong></summary>

点击登录页「忘记密码」，系统会向注册邮箱发送重置链接。若当前部署环境未配置 SMTP 邮件服务，请联系超级管理员在控制台或数据库手动重置。

提交后平台先受理请求，再由后台发送邮件；临时邮件服务故障会自动重试，因此邮件可能延迟。成功提示不会透露该邮箱是否已注册。若等待后仍未收到，请检查垃圾邮件并联系管理员。

</details>

<details>
<summary><strong>Q: 连续输错密码导致账号被锁定？</strong></summary>

平台不会永久锁定账号。启用人机验证的部署会在同一 IP 连续登录失败达到管理员配置的阈值后，要求完成 Cloudflare Turnstile 验证。页面按服务端提示显示验证码；验证后若登录仍失败，需要重新验证，登录成功才清零失败计数。若因网络问题无法加载验证码，请联系管理员或切换网络环境。

</details>

<details>
<summary><strong>Q: 自助注册和邀请注册有什么区别？</strong></summary>

- **自助注册**：管理员开启「开放注册」后任意用户可注册，默认获得 `viewer`（观察者）角色；在生产环境默认要求邮箱验证。
- **邀请注册**：通过项目管理员发送的专属邀请链接注册，自动按预设项目角色加入对应项目，无需邮箱二次验证。

自助注册的邮箱验证邮件由后台异步发送，临时邮件服务故障会重试；邮件可能稍后到达。邀请注册流程保持原样。

使用独立身份服务时，登录页的注册开关与管理端的注册默认值都以该服务为准；数据库中的管理员覆盖值仍优先。重置开关会恢复该身份服务的部署默认值。如果身份服务不可用，系统会返回服务错误，不会用 API 的默认配置代替。

</details>

<details>
<summary><strong>Q: 在哪里修改密码？密码规则是什么？</strong></summary>

登录后点击左下角「**设置**」→「修改密码」。新密码需满足强度要求：至少 **8 位**，且必须同时包含大写字母、小写字母与数字。

</details>

## 标注作业

<details>
<summary><strong>Q: 任务提交后能否撤回？</strong></summary>

- 任务处于 `review`（待审核）且**审核员尚未领取**时，标注员可在工作台点击「撤回提交」，任务回退至 `in_progress` 继续编辑。
- 审核员一旦领取或任务已通过，标注员不可自行撤回；如需修改，请联系审核员执行退回，或由管理员「重开任务」。
</details>

<details>
<summary><strong>Q: 画布标注卡顿如何优化？</strong></summary>

- 单张图片标注对象超过 200 个时，可暂时关闭右侧「标注列表」面板，减少 DOM 渲染压力；
- 视频时序标注帧率较高或分辨率较大时，推荐使用最新版 Chrome 或 Edge 浏览器以获得最优硬件加速。
</details>

<details>
<summary><strong>Q: 标注草稿会丢失吗？断网怎么办？</strong></summary>

工作台所有操作即时持久化写入服务端，不依赖定时整页保存。当网络意外断开或后端服务异常时，操作会自动排入本地 **IndexedDB 离线队列**，并在网络恢复后自动顺序重试落库。

</details>

<details>
<summary><strong>Q: AI 候选框显示为「已被覆盖」半透明是什么意思？</strong></summary>

当某个 AI 预测框与已有的人工标注框重叠度（IoU）超过项目设置的阈值（默认 0.7）时，系统自动判定为潜在重复候选，在画面中半透高亮。此时点击「全部采纳」会自动跳过这些重合项，防止重复入库。

</details>

<details>
<summary><strong>Q: 多边形（Polygon）如何增删顶点？</strong></summary>

- **删除顶点**：选中多边形后，按住 `Shift + 单击顶点` 即可删除（顶点数 ≤ 3 时禁止删除）；
- **插入顶点**：选中多边形，悬停在任意边上会出现「+」标记，按住 `Alt + 单击` 即可插入新顶点。
</details>

<details>
<summary><strong>Q: SAM 智能工具为什么没有响应或按钮置灰？</strong></summary>

- 检查项目是否已在「项目设置 → ML 模型」中绑定并启用了支持交互能力的 ML Backend；
- 按钮置灰通常说明当前模型不支持该类 Prompt（悬停在工具图标上可查看提示原因）；
- 若状态指示灯为红灯，说明 `/setup` 通信异常，可尝试刷新页面重新握手。
</details>

## 质检与审核

<details>
<summary><strong>Q: 普通审核员能否回退已经审核通过的任务？</strong></summary>

不能。已进入 `completed`（已完成）状态的任务，仅项目管理员和超级管理员有权限执行重开或回退操作，普通质检员不可逆向流转。

</details>

<details>
<summary><strong>Q: 退回任务时如何给出清晰的返工反馈？</strong></summary>

点击退回后，必须在 4 类结构化枚举中选择一项（漏标、多标、类别错误、位置/尺寸不准），并在补充说明中明确指出具体目标编号或视频帧号（例如 `trk_01 frame 128: 目标完全驶出画面，请标记消失`）。

</details>

## 数据导入与导出

<details>
<summary><strong>Q: 平台支持哪些数据导入方式与格式？</strong></summary>

- **图像 / 视频**：支持浏览器多文件直接上传、ZIP 压缩包导入，以及 S3 兼容对象存储（AWS S3 / 阿里云 OSS / MinIO）与 SFTP 存储连接器批量同步；
- **点云多模态**：支持 nuScenes 标准数据集转换脚本导入，或按原生目录结构（`lidar/`、`camera/`、`calib/`）组织导入。详见 [数据集导入](./datasets/)。
</details>

<details>
<summary><strong>Q: 平台支持导出哪些标注格式？</strong></summary>

支持导出为标准 **COCO**、**YOLO (det / obb / seg)** 以及包含全模态几何的原生 **AAP JSON** 格式。详见 [导出格式参考](./reference/export-formats)。

</details>

<details>
<summary><strong>Q: 如何导入外部算法生成的模型预测？</strong></summary>

在项目卡片菜单点击「导入预测」，选择对应的格式类型（YOLO / COCO / AAP JSON）并上传。导入完成后，预测将作为 AI 候选出现在工作台供标注员核验。详见 [预测导入与导出](./datasets/prediction-import-export)。

</details>

## 任务分派与批次

<details>
<summary><strong>Q: 批次（Batch）与任务（Task）的关系是什么？</strong></summary>

**Task** 是单条待标注的数据单元（1 张图或 1 段视频）；**Batch** 是项目内的任务切片包。管理员以批次为单位指派标注员与质检员，隔离进度与权限。详见 [批次与分配](./projects/batch)。

</details>

<details>
<summary><strong>Q: 任务如何分配给团队成员？</strong></summary>

管理员在批次中指定 1 名标注员和 1 名质检员；批次激活后，标注员进入工作台时由系统队列自动按序派题。多批次场景可通过「按项目分派批次」一键均匀分派给组内成员。

</details>

## 视频标注

<details>
<summary><strong>Q: 视频标注中单帧框（Frame Box）与时序轨迹（Track）的区别？</strong></summary>

- **单帧框 (Video Bbox)**：仅存在于当前视频帧中，不具备跨帧身份延续性；
- **时序轨迹 (Video Track)**：代表同一个物体在连续帧上的运动轨迹，由关键帧（Keyframe）和前端自动插值帧（Interpolated frame）构成。多个单帧框可在右键菜单中「聚合为轨迹」。
</details>

<details>
<summary><strong>Q: 如何在视频中快速建立长跨度轨迹？</strong></summary>

在目标出现的首帧拉框建轨迹后，可按快捷键 `Ctrl + B` 发起 AI 视频时序追踪，支持双向或向前追踪；算法运行完毕后在时间轴预览候选，确认无误后点击「采纳」即可自动生成整段轨迹。详见 [AI 视频传播](./workbench/video-propagate)。

</details>

---

## 还有其他疑问？

如果上述问题未能解答您的疑惑，可以通过工作台顶栏或右下角的 **「BUG 反馈」** 提交工单（系统会自动采集控制台日志与页面状态），或联系所在项目的管理员。
