---
audience: [super_admin]
type: how-to
since: v0.10.14
status: stable
last_reviewed: 2026-10-14
---

# 公共模板治理

> 适用角色：超级管理员（公共模板专属操作）；项目管理员（私有 / 组织模板）

平台支持「公共模板」（`scope=public`）—— 全平台可见可用、不受组织边界限制。
本页重点讲公共模板的治理要点；通用模板操作见下方「权限说明」。

## 模板创建权限说明

<!-- TODO(v0.14.18) IMAGE_CHECKLIST: images/superadmin/public-templates/scope-selector.png — 可见范围下拉「公共」选项 disabled（非超管视角） [manual] -->

### 通用（适用所有角色 ≥ project_admin）

- **project_admin 和 super_admin** 均可创建模板（`apps/api/app/api/v1/project_templates.py:109`）。
- `scope=private`：创建者本人可见，任何 project_admin / super_admin 均可建。
- `scope=organization`：同组织成员可见，任何 project_admin / super_admin 均可建（需指定 `organization_id`）。

### 公共模板专属限制（super_admin 专属）

- `scope=public` **仅超级管理员可创建或将已有模板升级到 public**。
- 非超管在模板编辑界面选 `scope=public` 会被禁用 / 后端返 403。

> 设计意图：公共模板是平台层面的"官方背书"，需要超管把关 schema 一致性、命名规范、合规性，避免出现"野生公共模板"污染列表。

## 推荐流程

![模板库「全部」视图：项目示例模板与公共模板的范围标签和实际使用次数](../images/superadmin/public-templates/templates-list.png)

截图中项目模板由种子项目导出，公共模板由超级管理员发布；使用次数来自真实 API 返回，未应用的示例模板显示 0。

1. 项目管理员先在自己组织内打磨模板（`scope=private` → 灰度试用 → 推到 `scope=organization`）。
2. 跑通后将模板 ID / 用例报给超管，超管 PATCH `scope=public`。
3. 命名建议：用 `[场景]-[版本号]` 风格，例如 `自动驾驶-车辆检测-v2`。

## 从项目导出模板

`/project-templates` 页面提供「从已有项目导出模板」入口（`CreateFromProjectDialog`）：

1. 打开模板库 → 点「从项目导出」按钮。
2. 选择源项目，系统自动 dump 项目的类别/属性/工具绑定、标注指引等可克隆字段。
3. 导出后可在模板库中编辑 scope / 名称，再按需升级为 organization / public。

后端：`POST /project-templates`（携带 `source_project_id`）。

## 公共模板的可编辑性

- 任何超级管理员都可以编辑 / 删除任意公共模板。
- 非超管只能"克隆"公共模板为私有副本，再自由修改。

## 删除公共模板

- 弹确认但不阻拦。
- 已基于该模板创建的项目不受影响（字段已 deepcopy）。
- 删除前如有重大使用量（如 `usage_count > 50`），建议先在群里告知一声，避免破坏团队习惯。

## 与 ML backend / model-market 的关系

公共模板可以预填 `ai_enabled` 标志，但**不绑定具体 ml_backend_id 也没有 `ai_model` 字段**（`project_templates` 表中无此列，`apps/api/app/db/models/project_template.py` 可验证）。应用模板创建项目时，新项目需自行从全局注册表启用 ml_backend，详见 [启用 ML 后端](../projects/ml-backends)。
