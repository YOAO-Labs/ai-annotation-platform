---
title: aap tui 终端运维台
audience: [dev]
type: reference
status: beta
last_reviewed: 2026-10-19
---

# aap tui 终端运维台

`aap tui` 是面向服务器和 SSH 环境的监控与轻运维入口。它提供 Projects、Datasets、Jobs、Training、
ML Backends、看板和绩效七个主视图；写操作只包括项目导出、Job 软取消、
失败项重试和导出包下载。

## 安装与启动

```bash
pip install 'ai-annotation-sdk[tui]'
aap tui
```

凭据解析与 CLI 相同：显式环境变量 `AAP_BASE_URL` / `AAP_API_KEY` 优先，否则读取 `aap login`
写入的配置。TUI 支持 Textual 8.x；SDK release 对 Textual major 设置了上界，避免未经验证的布局和
worker 行为变化直接进入现有环境。

## 加载与刷新

启动时只请求 Projects、Jobs 和当前主体 `me()`：

- Projects 是默认视图，立即加载。
- Jobs 每 3 秒轮询，即使当前停留在其他 tab，也会继续报告完成状态。
- Datasets、Training、ML Backends、看板和绩效第一次打开时才加载；再次打开直接展示缓存，按 `r` 明确刷新。
- ML Backends 主表不自动做 N+1 轮询。Backend 详情仍订阅 1 秒 WebSocket 实时流。

每个视图独立保存最近成功时间和错误。刷新失败时保留上一轮数据，并在表格标题与状态栏显示 HTTP
状态和 detail；后台 Jobs 刷新成功不会清除当前 Datasets 或 ML 视图的错误。空结果显示 `0 条`，与请求
失败明确区分。

## 主视图查询与分页

| 视图        | 筛选                                       | 分页     |
| ----------- | ------------------------------------------ | -------- |
| Projects    | name / display_id，服务端查询              | 不分页   |
| Datasets    | search + data_type，服务端查询             | 50 条/页 |
| Jobs        | status + kind，服务端查询                  | 50 条/页 |
| Training    | 项目 UUID + 状态，服务端查询               | 20 条/页 |
| ML Backends | name + project + state，已聚合缓存本地过滤 | 不分页   |

文本搜索使用 300ms 防抖；新筛选会把 offset 重置为 0，已取消的旧请求结果不会覆盖最新条件。分页状态显示
`第 start–end 条 / total`，可使用按钮或 `p` / `n`。表格按 row key 恢复光标；原行不在新结果时回到
第一页行。服务端没有统一排序参数，因此 TUI 不提供只排序当前页的列头操作。

## 下钻视图

- **ProjectDetailScreen**：Overview、Batches、Members、Jobs、Backends 和 Pools 六个子 tab。每个 pane
  独立请求和报错。项目 Jobs 使用 `project_id` 在服务端过滤；Pools 展示逻辑 pool ID、项目启用态、
  member 数、routing generation 与 default variants。
- **DatasetDetailScreen**：Overview、Items 和 Linked Projects。Items 按 50 条分页，只读展示文件元数据。
- **BatchDetailScreen**：显示状态、进度、审核/退回数、优先级、deadline、标注员和审核员；`r` 通过
  `batches.get()` 刷新，不在 TUI 复制状态流转表单。
- **Job 详情**：运行中 Job 可软取消，failed Job 可确认后调用 `jobs.retry_failed()`，完成态导出 Job 可
  输入本地路径下载。可重试 kind 由服务端判定，TUI 不维护另一份清单。
- **Backend 详情**：保留最近 REST 快照，并订阅 `/ws/ml-backend-stats` 实时展示 GPU、显存、缓存、
  pool 和预热状态。鉴权失败不重连；瞬时连接错误指数退避。

项目级 Pools 在 TUI 中只读。Backend enablement、member drain/resume 和 capability drift accept 在 CLI
完成，避免为低频高风险运维再维护一套表单。

## 训练监控

Training 首次打开时读取全局训练队列，可选择项目和状态并分页。`r` 手动刷新，`p` / `n` 翻页，`/` 聚焦项目筛选，Esc 清空条件；不在后台自动轮询训练队列。慢请求期间合并重复刷新，失败时保留最近成功数据并显示该视图的错误。全局队列要求平台管理角色，权限不足时在打开该视图后显示服务端错误，不影响其他视图。

选中实验按 `o` / Enter 打开 Overview、Telemetry、Logs 详情，查看当前 attempt、指标、模型回流资格及恢复原因。`r` 重新核对当前 attempt 并刷新；新 attempt 不沿用旧日志游标。遥测与日志按有界序列页读取，空数据和断网错误分别显示，Esc 返回队列。

训练 `succeeded`、`cancelling`、`lost` 等状态直接来自 run/attempt。它们与 Jobs 的完成状态不同，停止中或失联不代表已释放 GPU。训练创建、恢复、部署、发布、自动策略、参数搜索和人工主动学习决策使用 [学习 CLI](./cli#aap-learning) 或 Web 页面；TUI 不提供这些写操作。

## 看板与绩效

看板展示可见项目的总量、完成量、AI 标注率、待审量及 12 周趋势。

绩效视图对所有角色先加载 `dashboard.me_performance()` 的本人摘要；super-admin 额外加载全员排行。
project-admin 的跨成员排行需要明确项目上下文，TUI 不猜测项目，可使用：

```bash
aap dashboard people --project <project-id>
```

## 快捷键

| 按键         | 动作                                        |
| ------------ | ------------------------------------------- |
| `r`          | 刷新当前视图                                |
| `/`          | 聚焦当前筛选栏                              |
| `esc`        | 清空主视图筛选；在详情或弹窗中返回          |
| `p` / `n`    | 上一页 / 下一页                             |
| `o` / `回车` | 打开选中行                                  |
| `e`          | 在 Projects 发起导出                        |
| `c`          | 在 Jobs 对 pending / running Job 发起软取消 |
| `q`          | 退出主界面；详情屏中返回                    |
| `ctrl+p`     | Textual 命令面板                            |

所有确认框同时支持键盘和按钮。破坏性操作默认聚焦“取消”；成功或服务端返回的 403/409/422 会显示在状态栏，
不会输出 traceback。

## 窄终端与实时监控

TUI 在 100 列以下进入 compact 布局：隐藏动作栏中可由 `r` / `o` 替代的次要按钮，保留业务动作和 Footer
快捷键。折线图宽度在 24–64 列之间自适应，modal 使用 90% 宽度且最大 72 列；DataTable 固定第一列并
保留原生横向滚动，所以 80×24 与宽终端使用同一套列定义。

实时 Backend 屏只在进入时建立 WebSocket，离开即取消。WebSocket 不可用时静态 REST 快照仍保留，
状态栏说明降级原因。
