---
title: 设计系统（Design System）
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-19
---

# 设计系统（Design System）

本页涵盖两套相互独立但职责互补的设计系统：**Web 标注应用**（`apps/web`，基于 Tailwind CSS + shadcn/ui，占本页主要篇幅）与**文档站**（`docs-site`，基于 VitePress，见文末[文档站设计令牌](#documentation-site-tokens-vitepress)）。二者不共享样式表。

Web 应用采用 Tailwind CSS 搭配本地维护的 shadcn/ui 原语。运行时主题变量统一定义在 `apps/web/src/styles/shadcn.css`，组件样式必须消费 Tailwind 语义工具类或 `--sc-*` CSS 变量。

共享筛选控件、URL 状态同步与查询/操作作用域规范详见[筛选状态与结果作用域](./filtering.md)。

<span id="theme-tokens" aria-hidden="true"></span>

## 主题令牌（Theme Tokens）

`shadcn.css` 是中性表面、文本颜色、边框、圆角、焦点环以及画布专用主题变量的唯一事实源（Single Source of Truth）。

| 用途           | 变量令牌 / Tailwind 类名                                                |
| -------------- | ----------------------------------------------------------------------- |
| 页面背景       | `--sc-background` / `bg-background`                                     |
| 卡片表面       | `--sc-card` / `bg-card`                                                 |
| 浮层表面       | `--sc-popover` / `bg-popover`                                           |
| 主文本         | `--sc-foreground` / `text-foreground`                                   |
| 次级文本       | `--sc-muted-foreground` / `text-muted-foreground`                       |
| 弱化背景表面   | `--sc-muted` / `bg-muted`                                               |
| 细分隔边框     | `--sc-border` / `border-border`                                         |
| 主操作按钮     | `--sc-primary` / `bg-primary text-primary-foreground`                   |
| 品牌强调色     | `--sc-brand` / `text-brand bg-brand`                                    |
| 危险/错误状态  | `--sc-status-danger` / `text-status-danger bg-status-danger-soft`       |
| 警告/进行中    | `--sc-status-caution` / `text-status-caution bg-status-caution-soft`    |
| 成功/完成状态  | `--sc-status-positive` / `text-status-positive bg-status-positive-soft` |
| 信息/AI 状态   | `--sc-status-info` / `text-status-info bg-status-info-soft`             |
| 辅助信息/计数  | `--sc-status-info-alt` / `text-status-info-alt bg-status-info-alt-soft` |
| 搜索命中高亮   | `--sc-mark` / `bg-mark text-mark-foreground`                            |
| 工作台：图像   | `--sc-stage-image` / `text-stage-image bg-stage-image-soft`             |
| 工作台：视频   | `--sc-stage-video` / `text-stage-video bg-stage-video-soft`             |
| 工作台：3D点云 | `--sc-stage-threed` / `text-stage-threed bg-stage-threed-soft`          |
| 遮罩背景模糊   | `--sc-overlay-blur` / `backdrop-blur-overlay`                           |

Web 应用的暗色模式由 `<html data-theme="dark">` 驱动。`shadcn.css` 已将 Tailwind 的 `dark:` 变体定向到该属性，因此组件应直接使用 `dark:` 工具类，严禁在 `apps/web` 中引入 `.dark` 类选择器。

<span id="semantic-color" aria-hidden="true"></span>

## 语义色彩（Semantic Color）

中性界面元素应保持中性克制，仅在表达明确业务语义时使用彩色令牌：

| 业务语义        | 基础色相  | 语义工具类组合                                 |
| --------------- | --------- | ---------------------------------------------- |
| 失败 / 破坏性   | `rose`    | `bg-status-danger-soft text-status-danger`     |
| 进行中 / 警告   | `amber`   | `bg-status-caution-soft text-status-caution`   |
| 成功 / 已完成   | `emerald` | `bg-status-positive-soft text-status-positive` |
| AI / 模型推理   | `violet`  | `bg-status-info-soft text-status-info`         |
| 一般信息 / 计数 | `sky`     | `bg-status-info-alt-soft text-status-info-alt` |

- **状态徽标（Status Chips）**：统一使用语义浅色背景（`*-soft`）搭配同名状态文字色，需要快速扫视提示时可加一个小号 `bg-current` 圆点。严禁手写成对的裸色相类（如 `text-rose-600 dark:text-rose-400`），应直接消费读取 `shadcn.css` 的状态工具类。
- **搜索高亮与模态标识**：列表面板中的搜索命中文本使用 `bg-mark text-mark-foreground`（暖色实心底配深色字），保证亮暗主题下均清晰可读。模态标识色（`image` / `video` / `threed`）独立于状态色，仅在混合多模态视图（如快捷键面板的“全部类型”筛选）中区分命令所属的工作台，切勿将其复用于任务状态。
- **遮罩模糊与 3D 画布例外**：居中弹窗统一使用 `backdrop-blur-overlay` 工具类使浮动面板与模态框保持一致的背景模糊度。唯一例外是 **3D/点云工作台**：对正在进行软件光栅化或高频渲染的画布施加 `backdrop-filter` 会阻塞浏览器合成器回收，因此 3D 工作台浮层仅保留半透明压暗（dim）而不启用背景模糊。

<span id="type-scale" aria-hidden="true"></span>

## 字号阶梯（Type Scale）

应用采用紧凑型字号阶梯。请优先使用命名语义字号类，禁止在组件中直接编写任意像素字号类（如 `text-[13px]`）：

| 类名        | 字号 | 适用场景                           |
| ----------- | ---: | ---------------------------------- |
| `text-2xs`  | 10px | 高密度徽标、紧凑型辅助元数据       |
| `text-xs`   | 11px | 小号标签、表格表头、次级元数据     |
| `text-sm`   | 13px | 应用默认正文、表单控件、面板小标题 |
| `text-md`   | 15px | 紧凑型章节标题                     |
| `text-stat` | 22px | 仪表盘核心统计指标数字             |

少量历史迁移保留的精确尺寸类（`text-3xs`、`text-micro`、`text-ui`、`text-control-xl`）仅用于特定控件细节。若确需新增字号，请在 `shadcn.css` 中定义命名令牌后再使用。

<span id="z-index-scale" aria-hidden="true"></span>

## 层级阶梯（Z-Index Scale）

请使用语义化 `z-*` 工具类，禁止硬编码裸数字层级（如 `z-50` 或 `z-[999]`）：

| 类名                                         |        数值 | 适用场景                                    |
| -------------------------------------------- | ----------: | ------------------------------------------- |
| `z-local-1` ... `z-local-6`                  |     1 ... 6 | 画布或 3D 视口内部的局部紧凑叠放            |
| `z-base`                                     |          10 | 页面内局部抬升元素                          |
| `z-dock` / `z-dock-control`                  |     14 / 15 | 工作台停靠区（Dock）与停靠控制条            |
| `z-local-overlay`                            |          20 | 局部菜单与拖拽缩放手柄                      |
| `z-popover` / `z-popover-elevated`           |     30 / 55 | 需浮于局部面板之上的气泡卡片                |
| `z-dropdown`                                 |          50 | 下拉菜单与 Select 弹层内容                  |
| `z-modal`                                    |          50 | 基于 Radix 叠放上下文的模态框遮罩与内容     |
| `z-floating`                                 |          50 | 工作台浮动面板（Floating Panels）           |
| `z-tooltip`                                  |          50 | Tooltip 提示内容与箭头                      |
| `z-drawer-backdrop` / `z-drawer`             |     60 / 61 | 自定义抽屉（Drawer）遮罩与内容              |
| `z-notification-backdrop` / `z-notification` |   200 / 201 | 通知中心弹层与角标                          |
| `z-workbench-modal`                          |        1000 | 工作台内部阻塞式对话框                      |
| `z-app-drawer-backdrop` / `z-app-drawer`     | 1099 / 1100 | 应用外壳级抽屉（如快捷 Bug 反馈）遮罩与内容 |

<span id="rules" aria-hidden="true"></span>

## 样式编码铁律（Rules）

- **禁止裸色值**：组件 `className` 中不得出现 `#hex`、`rgb(...)`、`oklch(...)` 或 Tailwind 任意颜色工具类（画布/数据可视化着色与阴影 rgba 属于极窄例外）。
- **禁止遗留变量**：CSS Modules 中严禁读取已废弃的 `var(--color-*)`，一律使用 `--sc-*`。
- **禁止一次性状态色**：页面内不得自行拼凑状态色彩，必须复用上文定义的语义状态工具类。
- **禁止任意像素字号与裸层级**：统一使用紧凑字号阶梯与语义 `z-*` 工具类。
- **优先使用本地原语**：底层交互行为优先复用 `apps/web/src/components/shadcn/ui/` 中的原语；`@/components/ui/*` 仅作为保持现有应用 API 兼容的薄封装层。

<span id="copy-actions-for-errors-and-logs" aria-hidden="true"></span>

## 错误与日志复制控件

- **`@/components/ui/CopyableError`**：用于展示 API 报错、Worker 失败详情与运行时诊断信息。向 `message` 传入完整的原始字符串（`preview` 与 `truncate` 仅影响视觉截断）。在已有摘要或可交互卡片旁使用 `iconOnly` 模式，并确保复制按钮位于链接或其他按钮外部。单文件错误需保留上下文标识符；普通表单校验或常规状态文案不要替换为诊断复制控件。
- **`@/components/ui/CopyButton`**：用于日志面板或堆栈工具栏的专用复制按钮。文案需准确描述复制范围（例如训练日志复制的是“当前已加载的日志块”而非全部历史归档），并保持日志内容本身可选中、可滚动。两个组件共享剪贴板回退逻辑并自动阻止行点击、表单提交与键盘事件冒泡；Toast 适配器同样为错误通知提供带完整标题与详情的复制操作。

<span id="copy-actions-for-technical-identities" aria-hidden="true"></span>

## 技术标识与哈希复制控件

- **`@/components/ui/CopyValueButton`**：用于复制不透明哈希（Hash）与内部 UUID。传入完整原始 `value`、ID 类型设置 `kind="id"`（默认为哈希），并提供包含字段语义的无障碍 `label`（如 `复制权重哈希`）。视觉上呈现为紧凑复制图标，悬停与键盘聚焦时 Tooltip 显示 `复制哈希` 或 `复制 ID`，触控设备自动获得更大的点击热区。
- **焦点与上下文保持**：剪贴板回退控件挂载在当前聚焦的 `dialog` 或 `alertdialog` 内部以维持焦点陷阱（Focus Scope）。证据对比视图会复制当前对比项持久化的原始标识（含已不在实时下拉列表中的历史候选）。
- **排版与同名消歧**：复制按钮始终与字段标签或对象名称保持同行（含窄屏布局），作为链接、下载按钮和复选框标签的兄弟节点，切勿嵌套在其它可交互控件或原生 `<option>` 内。当对象名称可能重复时，需在名称旁保留可读的区分标识并带入弹窗标题与无障碍名称中。

<span id="decision-dialogs" aria-hidden="true"></span>

## 阻塞式决策对话框（Decision Dialogs）

确认操作、破坏性操作、多选分支与必填原因输入统一使用 `@/components/ui/decisionDialog`（`confirmDialog` / `choiceDialog` / `inputDialog` / `alertDialog`），由全局唯一的 `<DecisionDialogHost />` 渲染。该宿主同时挂载在 `App.tsx` 的两个 `ToastRack` 旁，确保脱离 `AppShell` 的全屏工作台路由也能正常弹出。内容型弹窗（表单、预览、多区块面板）继续使用 `components/ui/Modal`；浏览器原生 `beforeunload` 因必须同步阻塞而保留原生实现。

- **单一视觉语言与 ESLint 门禁**：`apps/web/src` 已通过 ESLint `no-restricted-properties` 规则严禁直接调用 `window.confirm`、`window.alert` 与 `window.prompt`（测试文件除外）。
- **文案与焦点规范**：采用“动词 + 对象 + 后果”三段式表达——标题写明动作与对象（如 `删除数据集`），描述说明影响范围与不可逆后果，确认按钮使用明确动词。危险操作采用克制的语义色（`text-status-danger bg-status-danger-soft` 图标容器），默认聚焦“取消”按钮；普通确认默认聚焦“确认”按钮，`Esc` 与点击遮罩均视为取消。

<span id="filter-presentation" aria-hidden="true"></span>

## 筛选器视觉与交互规范

- **统一入口与图标**：筛选入口统一使用 `Icon name="filter"` 漏斗图标（而非加号或齿轮），展开式筛选使用 `components/filters/FilterTrigger`，行内范围切换使用 `FilterGroup`。为 SVG 显式指定尺寸类（常规控件 `size-4`，紧凑面板 `size-3`）。
- **响应式容器（`FilterPanel`）**：桌面端为附着在触发器旁的非模态受控 Popover（8px 触发间距、12px 视口安全边距，仅内容区滚动），窄屏自动切换为底部抽屉（Sheet），跨断点切换时由父级保留草稿态。
- **原子组件复用**：优先复用 `FilterToggle`、`FilterSelect`、`FilterSection` 与 `ActiveFilterChip`。已生效筛选标签的移除按钮必须作为兄弟节点而非嵌套按钮；高频筛选项保持行内展示，复杂的 Data Manager 组合表达式置于工具栏下方。状态重置、历史记录与分页规则详见[筛选状态与结果作用域](./filtering.md)。

<span id="icon-semantics" aria-hidden="true"></span>

## 业务列表

表格与列表行复用 `components/ui/listStyles`：语义卡片背景、统一边框与分隔线、弱化表头、紧凑行间距和悬停状态。数值列可按业务需要右对齐，宽表格在容器内横向滚动，不撑宽整页。列表末尾使用 `ListPagination`，默认 20 行，提供 10／20／50／100 行和上一页／下一页；空结果保留控件，读取中禁用操作。分页、计数及筛选依赖见[筛选状态与结果范围](./filtering.md#数量与操作范围)。

## 下拉菜单的提示与选项

原生 `select` 中的「请选择」「加载中」「暂无可用项」使用 `<option disabled hidden>`，保留当前字段的提示显示，但不进入可选列表。共享主题根据被选中的 `disabled hidden` 选项自动应用 `--sc-muted-foreground`；真实选项恢复 `--sc-foreground`，禁用选项保持弱化。此规则同样适用于保留非空执行 ID 的临时提示，不依赖提示文案或空字符串判断。

「全部」「不绑定」「首次划分」等具有业务含义的空值仍为正常选项。需要撤销选择时，提供明确的取消／清除按钮或「退出当前分段」动作，不能让隐藏提示兼任清除入口。清除比较基线时同步重置其类别、页码和变化筛选；保留当前数据版本。异步加载、分页或权限变化时，继续保留已有精确 ID，不自动改选列表中的第一项。

选项只放辨识条目所需的名称、状态及来源；用途、操作指导等说明放在控件下方，以弱化文字展示，必要时通过 `aria-describedby` 关联。空列表按业务状态禁用控件。Radix Select 使用 `SelectValue` 的 `placeholder`，提示不应创建为 `SelectItem`，Trigger 已内置占位文字样式。

浏览器回归使用 `pnpm --filter @anno/web test:e2e:layout --grep 'native prompts|real empty filter|Radix placeholders'`，覆盖明暗主题、窄屏、键盘选择、普通空值筛选和类别绑定清除，夹具不依赖 API 或数据库。

## 图标语义规范（Icon Semantics）

应用以 Lucide 作为主图标库，`components/ui/Icon.tsx` 统一渲染 Lucide 图标与 `annotationIcons.ts` 中的少量标注专用图标（共用 24px 网格、`currentColor`、描边宽度与无障碍装饰属性）：

- **跨页语义一致**：相同动作（搜索、保存、删除）在不同页面复用同一图标，不追求全局图标唯一；但在同一导航栏或工具组内，不同选项必须具备清晰的轮廓区分度，不能仅靠颜色或角标区分。
- **消费既有注册表**：侧边栏导航描述符、画布工具（经由 `TOOL_REGISTRY` / `ALL_TOOLS` 的 `ToolMeta.icon`）与项目配置工具组（`TOOL_UNIT_GROUPS`）是图标的唯一真源，消费方不得另建平行映射表。
- **标注工具图形约定**：图像与视频单帧工具共享基础几何图形；多边形/折线轨迹增加尾帧轮廓，Mask 轨迹结合区域轮廓；旋转框使用带手柄的倾斜矩形，关键点使用连接节点；Mask 涂抹使用画笔，智能涂鸦使用带标记笔触，画布评论保留铅笔，剪刀用于裁剪与点云框选。

## 滚动区域与布局稳定性

原生纵向滚动区域使用 `scrollbar-gutter: stable`，在短内容和长内容之间保持相同的内容宽度。应用的共享策略位于 `apps/web/src/styles/scrollbars.css`，由 `shadcn.css` 加载：

- Tailwind 的 `overflow-auto`、`overflow-y-auto`、`overflow-scroll`、`overflow-y-scroll` 已统一扩展，响应式和状态变体也会保留这项行为。
- CSS Modules 中自行声明的 `overflow: auto/scroll` 或 `overflow-y: auto/scroll` 必须同时声明 `scrollbar-gutter: stable`。无高度约束的代码块、宽表格和横向标签栏使用 `overflow-x-auto` / `overflow-x: auto`。
- 独立页面的根节点和原生 `textarea` 预留滚动条空间；主应用壳层和全屏工作台关闭根节点预留，由内部滚动区域承担。不要给所有元素统一设置 gutter，`overflow: hidden` 的画布和裁剪容器也会因此损失宽度。
- Radix 弹窗打开时保留根节点 gutter，并在支持该属性的浏览器中关闭 `react-remove-scroll-bar` 额外的 body 右外边距补偿，避免重复空白；滚动锁和焦点管理仍由 Radix 负责。
- Radix ScrollArea、隐藏原生滚动条的工具栏及 Dockview 自绘滚动条不需要额外预留；Dockview 原生纵向标签溢出菜单单独覆盖。编辑器的纵向预留由受高度约束的外层负责，内部横向代码区保持原有空间。

文档站独立在 `docs-theme.css` 中覆盖根节点、侧栏、移动导航、菜单、目录下拉和搜索结果；保留横向表格、代码块及隐藏滚动条目录的行为。隔离在 iframe 内的 API Reference 由 `public/api-reference.html` 为其根节点及 Scalar 原生滚动容器单独设置 gutter。

运行 `pnpm --filter @anno/web test:e2e:layout` 可在无 API、无数据库的浏览器夹具中验证长短内容切换、弹窗滚动锁、工作台画布及自绘／横向滚动。该测试使用真实共享样式和本地组件，覆盖桌面明暗主题与窄屏，并显式保留原生滚动条。

<span id="validation" aria-hidden="true"></span>

## 样式门禁校验（Validation）

修改前端样式后，必须运行 Web 令牌门禁与 Lint 检查：

```bash
pnpm --filter @anno/web lint:css-tokens
pnpm --filter @anno/web lint
```

`lint:css-tokens` 会扫描 Tailwind 类名中的裸色值与暗色配对写法，对未使用语义工具类的状态色、任意像素字号或裸数字 `z-index` 发出告警，并在 CSS 读取遗留 `--color-*` 变量时直接报错拦截。

<span id="documentation-site-tokens-vitepress" aria-hidden="true"></span>

## 文档站设计令牌（VitePress）

文档站（`docs-site`）运行在 VitePress 默认主题之上，**不引入** Web 应用的 `shadcn.css`。文档站采用独立的双层令牌体系，使品牌着陆页与正文阅读页在共享同一品牌色源的同时，拥有各自克制的色彩浓度、排版与动效节奏：

| 层级         | 样式文件                          | 作用范围                                     | 视觉特征                                                                                      |
| ------------ | --------------------------------- | -------------------------------------------- | --------------------------------------------------------------------------------------------- |
| 阅读层（L1） | `.vitepress/theme/docs-theme.css` | 全部正文页面（user-guide / dev / ops / api） | 雾白/墨黑中性表面、皇家蓝链接、中文舒适阅读行高，将 `--docs-*` 映射至 VitePress `--vp-*` 变量 |
| 品牌展示层   | `.vitepress/theme/docs-home.css`  | 仅限首页品牌组件                             | 高饱和皇家蓝 / 纸白 / 墨黑 / 酸性绿 + 衬线展示字体（`--home-*`），绝不覆盖正文阅读层令牌      |

- **共享品牌色源**：两层均派生自皇家蓝 `#172cff`。首页将其用于大面积品牌背景，正文页则收敛为 `--vp-c-brand-1`（链接与激活态）。酸性绿 `--home-acid` 仅限首页 AI/标注节点装饰使用，严禁进入正文阅读页。
- **暗色模式选择器**：VitePress 通过切换 `html.dark`（而非 Web 应用的 `data-theme`）控制暗色模式，因此 `docs-theme.css` 中的暗色覆盖统一使用 `.dark` 选择器，并将皇家蓝提亮至 `#93a0ff` 以保证墨黑背景下的链接对比度。
- **纯本地字体栈**：`--docs-font-sans`、`--docs-font-mono` 与 `--docs-font-serif` 均为纯系统/本地字体栈，文档站严禁注入 Google Fonts 等运行时外部网络字体。

<span id="documentation-reading-layout" aria-hidden="true"></span>

### 文档阅读版式（Reading Layout）

`DocsLayout.vue` 通过 VitePress 默认主题的公开插槽扩展阅读体验，在正文标题上方注入所属文档域链接与文档类型标签：

| 版式要素   | 阅读层设计规格                                                                                 |
| ---------- | ---------------------------------------------------------------------------------------------- |
| 左侧导航栏 | 桌面端宽 `256px`；按任务分组、原生当前分支展开，采用蓝色高亮文本与柔和激活背景                 |
| 正文容器   | `--docs-reading-width: 760px`；单栏连续阅读流，宽表格与代码块在局部容器内横向滚动              |
| 右侧大纲栏 | `--docs-outline-width: 200px`；展示 `H2`/`H3` 层级、支持长标题换行与原生当前章节指示条         |
| 栏目导览页 | `pageClass: docs-hub-page` 且 `aside: false`；宽 `1040px`，任务入口卡片置于参考索引之前        |
| 标题与正文 | 本地衬线 `H1`（桌面 `40px` / 移动 `32px`）；无衬线 `H2`/`H3`（`26px`/`20px`）；正文 `16px/1.8` |
| 表面与圆角 | 扁平阅读画布；选中态使用 `--docs-radius-sm: 4px`，卡片/媒体/代码块使用 `--docs-radius: 8px`    |

顶部菜单与桌面侧边栏在 `960px` 断点启用，右侧目录大纲在 `1280px` 断点启用。修改阅读层 CSS 后，请使用 `docs-site/scripts/check-reading-layout.mjs` 捕获并校验历史锚点、正文计算宽度及亮/暗主题表现（详见[编写与维护文档](../how-to/write-documentation)）。
