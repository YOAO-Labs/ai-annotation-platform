# 提案 · 截图自动化 v2（释放 Playwright 全部能力）

> 状态：**已归档**。前置：v0.8.7 已落地 14 场景脚手架（`apps/web/e2e/screenshots/scenes.ts` + `screenshots.spec.ts` + `playwright.screenshots.config.ts`）。本提案是它的下一代。
>
> 目标：从「截 14 张静态 PNG」升级为「跑一次 → 文档站 95% 视觉素材自动产出 + 可视化回归 + 多视口 / 多主题 / 多语言矩阵 + 流程 GIF / MP4」。

---

## 0. TL;DR

- 现状只用了 Playwright `page.screenshot()` 的最基础能力；trace / video / 多 project / mask / clip / locator-screenshot / visual diff / accessibility tree / network mock / device emulation / aria snapshot 全部没用上。
- IMAGE_CHECKLIST 有 14 张静态图 + 2 张 GIF（e2e.gif），实际 user-guide 里 `<!-- TODO IMAGE_CHECKLIST -->` 占位还有更多，且 v0.10.x SAM 3 / exemplar 工作台、ops/runbooks、dev/tutorials 全要新图——现阶段一张张人手补已经撑不住。
- 拆 4 层能力上：**M1 矩阵化**（多视口 + 主题 + 角色 + locale）→ **M2 局部 / 高亮 / 注释**（locator screenshot + 自动红框 + 数据脱敏 mask）→ **M3 动效与流程**（video → GIF/MP4 + trace 自助调试）→ **M4 视觉回归 + 文档侧渲染**（toMatchSnapshot + 自动 image manifest 注入 markdown）。
- 估时 ~3 周；M1 / M2 单独都能立刻见效，无强耦合。

---

## 1. 现状盘点

### 1.1 已有资产（v0.8.7 F4）

| 资产 | 说明 |
|---|---|
| `apps/web/playwright.screenshots.config.ts` | 独立 config，**不进 CI**，手动跑 |
| `apps/web/e2e/screenshots/scenes.ts` | 14 个场景（角色 / 路由 / prepare 钩子 / target 路径） |
| `apps/web/e2e/screenshots/screenshots.spec.ts` | 单文件 driver；`seed.peek()` 不破坏 dev 数据 |
| `docs-site/user-guide/IMAGE_CHECKLIST.md` | 人工对账清单 |
| 输出 | `docs-site/user-guide/images/<page>/<name>.png` |

### 1.2 没用上的 Playwright 能力（关键 gap）

| 能力 | 现状 | 价值 |
|---|---|---|
| `projects` 多 project 矩阵 | 只跑一个浏览器 | 一次性产出 desktop / tablet / mobile / dark |
| `locator.screenshot()` | 全部用 `page.screenshot({ fullPage: true })` | 单组件 / 工具栏 / dialog 局部图，文档命中率高 |
| `mask` 参数 | 没用 | 时间戳 / 头像 / token 自动打码，数据脱敏 |
| `clip` 参数 | 没用 | 精准框出特定区域（如「IoU 重叠双框」） |
| `boundingBox` + 红框装饰 | 没做 | 当前红框靠人在 Figma 后处理；可在截图后用 sharp 自动绘 |
| `video: 'on'` | 没用 | 直接产出 mp4 → ffmpeg 转 GIF / WebM，e2e.gif 自动产出 |
| `trace: 'on'` | 没用 | 截图失败时一键 trace.zip 排错 |
| `aria-snapshot` / `page.accessibility` | 没用 | 给 a11y 文档自动产 ARIA 树 |
| 多 storageState | 单一 super_admin 兜底 | 同一场景跑 4 个角色 → 4 张图，体现权限差异 |
| `emulate({ colorScheme, locale, reducedMotion })` | 没用 | 暗色 / 浅色 / zh-CN / en-US 矩阵 |
| `request.route()` mock | 没用 | 给「失败状态」「空态」「极端数据」做截图（线上没这些场景） |
| `expect(page).toHaveScreenshot()` | 没用 | 视觉回归（catch UI 漏改） |
| Component testing (`@playwright/experimental-ct-react`) | 没用 | 给 design system / 组件库单独出图，不依赖整页 |
| `test.step()` 章节化 | 没用 | trace / report 里能看到「点击工具栏 → 截图」每步成本 |
| Codegen / pwdebug | 用过 | 维护者写新场景靠手撸 prepare 钩子，新场景 onboarding 慢 |

### 1.3 痛点

| # | 痛点 | 后果 |
|---|---|---|
| Q1 | 一个场景只产一张图 | desktop / tablet 翻倍场景靠手撸 |
| Q2 | 全页截图占比高 | UI 改一点位置截图全变，diff 噪声大 |
| Q3 | 数据脱敏靠 prepare 钩子手动改 DOM | 容易漏；时间戳 / 真实邮箱偶有泄漏 |
| Q4 | 红框 / 标注靠人后处理 | 文档迭代时素材失同步 |
| Q5 | GIF 没自动化 | `e2e.gif` 占位至今未补 |
| Q6 | 没视觉回归 | UI 改坏没人发现，文档图自动陈旧 |
| Q7 | 失败截图无辅助证据 | trace / video 没开，复现靠重跑 |
| Q8 | 文档站不知道哪些图是自动的 | maintainer 容易手动覆盖被下次自动跑反盖 |

---

## 2. 设计原则

1. **声明式场景**：场景描述「我要什么」（路由 / 角色 / 视口 / 主题 / mask / 注释），driver 负责「怎么截」。
2. **矩阵 by default**：viewport × theme × locale × role 自动展开；scene 只声明它关心哪些维度。
3. **失败可调试**：所有失败 retain trace + video，dev 直接 `npx playwright show-trace` 看现场。
4. **图能再生**：不依赖人工后处理；红框 / mask / 拼接全程序化。
5. **回归可控**：视觉回归与「素材产出」分离 —— 视觉回归用 fixed-data，素材产出用 peek-data。
6. **不进 CI 默认**：完整矩阵跑全本地 / 周期任务；CI 只跑视觉回归子集。

---

## 3. 架构设计

### 3.1 目录与运行

```
apps/web/e2e/screenshots/
├── README.md
├── playwright.screenshots.config.ts         # 多 project：desktop-light / desktop-dark / tablet / mobile / a11y
├── scenes/
│   ├── _types.ts                            # ScreenshotScene 扩展类型
│   ├── _helpers/
│   │   ├── annotate.ts                      # 截图后自动加红框 / 标注（用 sharp + svg overlay）
│   │   ├── mask.ts                          # mask 选择器集合（时间 / 头像 / token / B-编号）
│   │   ├── recorder.ts                      # video → GIF / WebM 的封装
│   │   └── matrix.ts                        # 矩阵展开器
│   ├── auth.ts                              # 登录 / 忘密
│   ├── workbench-bbox.ts                    # bbox 全套
│   ├── workbench-polygon.ts
│   ├── workbench-sam.ts                     # 含 v0.10.x exemplar 入口
│   ├── projects.ts
│   ├── review.ts
│   ├── export.ts
│   ├── superadmin.ts
│   ├── ops-runbooks.ts                      # 失败预测 / Celery 卡死 等
│   └── flows/                               # 流程 GIF（产 video）
│       ├── e2e-quickstart.ts                # 30s 录屏：登录 → 标 → 提交
│       ├── ai-preannotate.ts
│       └── review-reject.ts
├── fixtures/
│   ├── seed-readonly.ts                     # peek 模式（同 v0.8.7）
│   └── seed-fixed.ts                        # 视觉回归用：fixture 数据库快照
└── outputs/
    ├── images/                              # 落到 docs-site/user-guide/images/ 的最终素材
    ├── flows/                               # GIF / WebM / MP4
    ├── manifest.json                        # 自动产出的图清单（后述）
    └── visual-regression/                   # toMatchSnapshot 基线
```

### 3.2 ScreenshotScene 扩展

```ts
export interface ScreenshotScene {
  name: string;
  role: Role | Role[];                     // 多角色矩阵：写 ['annotator', 'reviewer']
  route: (d: SeedData) => string;
  prepare?: (page: Page, d: SeedData) => Promise<void>;

  // ── v2 新增 ────────────────────────────
  /** 不写=全页；写 selector=locator.screenshot；写 'clip'=区域 */
  capture?:
    | { kind: 'fullPage' }
    | { kind: 'locator'; selector: string; padding?: number }
    | { kind: 'clip'; rect: { x: number; y: number; width: number; height: number } }
    | { kind: 'composite'; layers: CompositeLayer[] };  // 多 locator 拼图

  /** 自动加红框 / 标号（svg overlay 后合成） */
  annotate?: Array<{
    selector: string;
    style?: 'rect-red' | 'rect-blue' | 'arrow' | 'numbered';
    label?: string;
  }>;

  /** 数据脱敏 mask（用 page.screenshot mask） */
  mask?: string[];                          // selector 列表

  /** 矩阵维度：不写=只跑 desktop-light-zh-cn-default-role */
  matrix?: {
    viewports?: Array<'desktop' | 'tablet' | 'mobile'>;
    themes?: Array<'light' | 'dark'>;
    locales?: Array<'zh-CN' | 'en-US'>;
    reducedMotion?: boolean;                // a11y 出一份
  };

  /** 模拟网络 / 数据状态：失败 / 空态 / 极端 */
  mockState?: 'happy' | 'empty' | 'error' | 'loading' | 'rate-limited';

  /** 视觉回归：true=参与 toMatchSnapshot；默认 false */
  regression?: boolean;

  /** 流程录制：开 → 产 GIF/MP4 */
  record?: {
    enabled: true;
    steps: (page: Page, d: SeedData) => Promise<void>;
    output: { gif?: string; mp4?: string };
    fps?: number;
    duration?: number;
  };

  target: string | ((axis: MatrixAxis) => string); // 矩阵展开时按轴生成路径
}
```

### 3.3 多 project 矩阵（playwright.config）

```ts
projects: [
  { name: 'desktop-light', use: { viewport: { width: 1440, height: 900 }, colorScheme: 'light' } },
  { name: 'desktop-dark',  use: { viewport: { width: 1440, height: 900 }, colorScheme: 'dark'  } },
  { name: 'tablet',        use: { ...devices['iPad Pro 11'] } },
  { name: 'mobile',        use: { ...devices['iPhone 14'] } },
  { name: 'a11y',          use: { reducedMotion: 'reduce', forcedColors: 'active' } },
  { name: 'i18n-en',       use: { locale: 'en-US' } },
],
```

scene 默认只跑 `desktop-light`；声明 `matrix.themes:['light','dark']` 自动跑两个 project。

### 3.4 自动注释（红框 / 编号）

- `prepare` 后 `page.evaluate` 注入临时 SVG overlay（不污染 DOM 持久态）；
- `page.screenshot` 拿到原始图后，用 [sharp](https://sharp.pixelplumbing.com/) + 已记录的 boundingBox 在文件层叠加红框；
- 优势：UI 改名 / 移动后红框自动跟随选择器，文档不掉队。
- 数据脱敏 `mask:[...]` 直接用 Playwright 自带 mask（覆盖紫色块或自定义颜色）。

### 3.5 流程录制 → GIF

- Playwright `video: { mode: 'on', size }` 产 `.webm`；
- 用 ffmpeg（[已存在 docker / npm 路径](https://www.npmjs.com/package/@ffmpeg-installer/ffmpeg)）转 GIF：调色板优化 + duration 限定；
- 输出 `e2e-quickstart.gif`（1280×720，30s，<5MB）；
- `record.steps` 是脚本化交互（点登录 → 点标注 → 提交）；脚本失败可回放 trace。

### 3.6 视觉回归

- 独立 `pnpm screenshots:regression`，用 `seed-fixed.ts`（固定 fixtures，不用 peek）；
- `expect(page).toHaveScreenshot('xxx.png', { maxDiffPixelRatio: 0.01 })`；
- 阈值高一点 + 频率低（每次 release 之前跑一次，不进 PR CI）；
- 失败时 PR 评论附 diff 三联图（base / current / diff）。

### 3.7 文档站集成

- `outputs/manifest.json` 例：
  ```json
  {
    "bbox/toolbar.png": { "auto": true, "scene": "workbench-bbox.toolbar", "lastRun": "2026-05-09T12:00:00Z" },
    "bbox/iou.png":     { "auto": false, "note": "需手动覆盖：现实 IoU 数据" }
  }
  ```
- VitePress 自定义组件 `<AutoImage src="bbox/toolbar.png" />`：
  - `auto:true` 时显示 badge「自动产出 · 上次更新 yyyy-mm-dd」
  - 链接到 scene 源码
- maintainer 手动改图时把 `auto: true` 改成 `false` + `note` 写原因，下次 driver 跳过该 target 不覆盖。

### 3.8 可调试性

- 所有失败默认 retain trace + video + screenshot；
- `pnpm screenshots --debug=<scene-name>` 进 codegen 模式辅助写 prepare；
- `pnpm screenshots --only-changed`：基于 git diff 只跑变更过的 scene（按 scene 文件 mtime / scene name 跟 docs MD 引用图反查）。

---

## 4. 里程碑切片

### M1 — 矩阵化 + locator 截图（约 5 个工作日）

**目标**：scene 类型扩展；多 project 上线；80% 现有场景改成 locator capture。

- [ ] `playwright.screenshots.config.ts` 重写为多 project
- [ ] `ScreenshotScene` 接口扩展 `capture` / `mask` / `matrix`
- [ ] 改写 driver `screenshots.spec.ts`：按 project + scene.matrix 展开 test
- [ ] 14 个现有场景按需切 `locator.screenshot()`（toolbar / dialog / 属性面板 → locator；全页保留 export 等）
- [ ] mask 默认集合：`[data-screenshot-mask]` + 头像 + 时间戳（统一 data attr 约定）
- [ ] dark / mobile 矩阵打开 3-5 个核心 scene 试水
- [ ] outputs 路径按 `<page>/<name>.<theme>.<viewport>.png` 命名；matrix 单维度时省略后缀

**验收**：跑一次 `pnpm screenshots`，desktop-light 全 14 张；desktop-dark + mobile 各 3-5 张；diff 噪声明显下降。

### M2 — 注释 + 数据脱敏 + 状态 mock（约 4 个工作日）

**目标**：红框 / 编号 / 脱敏全部程序化；空态 / 失败态可截。

- [ ] `_helpers/annotate.ts`：sharp + svg overlay 实现 rect-red / arrow / numbered
- [ ] DOM 约定：`[data-screenshot-mask]` + tailwind plugin 调试时显紫
- [ ] 5 个 scene 接 `annotate`（bbox/toolbar 高亮矩形按钮；export/format-select 标号 4 个格式）
- [ ] `mockState` driver：empty / error / loading 各一个示例（projects 列表空态 / ml-backend down 错误 / 大批次 loading）
- [ ] `pnpm screenshots:lint`：检查所有 user-guide 引用图是否在 manifest

**验收**：`bbox/toolbar.png` 自动带红框；`projects/empty-state.png` 自动产出；任一场景跑两次 diff 像素一致。

### M3 — 流程录制（GIF/MP4）+ trace 一键调试（约 4 个工作日）

**目标**：补齐 IMAGE_CHECKLIST 的 e2e.gif；新增 ai-preannotate / review-reject GIF；失败可一键复现。

- [ ] `_helpers/recorder.ts`：video → ffmpeg → GIF/WebM 流水线
- [ ] 三个流程脚本 `flows/e2e-quickstart.ts` / `ai-preannotate.ts` / `review-reject.ts`
- [ ] 输出体积优化：720p / 调色板抽样 / 帧抽样（`--vf fps=10`）
- [ ] trace / video on-failure 全开；CI / 本地都 retain
- [ ] `pnpm screenshots --debug=<scene>` 集成 pwdebug
- [ ] 把 `docs-site/user-guide/images/getting-started/e2e.gif` 真正补上

**验收**：e2e.gif 自动产出 < 5MB；任一 scene 失败后 `playwright show-trace outputs/.../trace.zip` 直接复现。

### M4 — 视觉回归 + 文档侧渲染（约 4 个工作日）

**目标**：UI 改坏自动告警；文档站知道哪些图自动 / 哪些手动；不再"被反盖"。

- [ ] `seed-fixed.ts`：基于固定 alembic 快照恢复（脚本化）
- [ ] `pnpm screenshots:regression`：`toHaveScreenshot` 子集（先 10 张核心页）
- [ ] `outputs/manifest.json` driver 写入；`docs-site/scripts/check-image-manifest.mjs` 校验所有 markdown 引用都在 manifest
- [ ] VitePress 主题 `<AutoImage>` 组件：badge + 来源链接
- [ ] CI 加 release-gate job：cron 每周一跑 regression，失败开 issue
- [ ] 文档：`docs-site/dev/how-to/update-screenshots.md`（新）说明新增 scene 流程

**验收**：故意改一处 UI → regression CI red；manifest 检查能列出"被引用但没自动产出"的图。

---

## 5. 与既有 Roadmap 的关联

- 与 [v0.10.x](./0.10.x.md) 协同：sam3 / exemplar 工作台需要新场景，M1 完成后 v0.10.1 可直接加 scene 文件。
- 与「文档 IA 重构」（[2026-05-09-docs-ia-redesign.md](./2026-05-09-docs-ia-redesign.md)）协同：IA M2 物理迁移会改图片路径，本提案 M4 的 manifest 校验能在迁移时给出精准 dead-image 列表。
- 替代 `IMAGE_CHECKLIST.md` 的人工对账：M4 上线后 manifest.json 即真相，CHECKLIST 文件可移到 archived。

---

## 6. 风险与对策

| 风险 | 概率 | 缓解 |
|---|---|---|
| 矩阵爆炸（4 viewport × 2 theme × 2 locale = 16 倍） | 高 | 默认只跑 desktop-light；矩阵显式 opt-in；CI 不跑全矩阵 |
| sharp / ffmpeg 平台依赖 | 中 | 用 npm 自带 wasm fallback；Docker compose 提供 screenshot service profile |
| dev 数据漂移破坏视觉回归 | 中 | 视觉回归走独立 `seed-fixed`；素材产出走 peek 不互相干扰 |
| video 体积大 | 中 | 流程录制只对 `record.enabled` 的 scene 开；retain on-failure 只保留最近 5 个 |
| maintainer 手改的图被覆盖 | 中 | manifest `auto:false` 跳过；driver 输出前做 hash 比对 |
| 红框 overlay 与 reducedMotion / 暗色冲突 | 低 | annotate 后置在文件层（不进 DOM），不受运行时影响 |

---

## 7. 不做的事

- ❌ 不接 Storybook / Chromatic（成本和收益已被 Playwright 多 project 覆盖；除非未来真做组件库）
- ❌ 不做 cross-browser 矩阵（Firefox / Safari）—— 文档素材一致性靠 Chromium 即可
- ❌ 不做"自动配文案"（GPT 描述截图）—— 准确度不够
- ❌ 不接 Percy / Applitools 商业 visual regression —— 自带 `toHaveScreenshot` 已够，且不依赖外部服务

---

## 8. 开放问题

1. fixed-data 的 alembic 快照怎么落？容器内 dump.sql + restore，还是单独 fixture seed？
   - 倾向 fixture seed：跨机器可复现，不依赖 pg dump 二进制兼容
2. video / GIF 要不要进 git？（5MB × N 个流程）
   - 倾向 LFS 或 ignore；放 [Cloudflare R2](https://developers.cloudflare.com/r2/) / 仓库 release asset
3. 矩阵命名规则：`bbox/toolbar.dark.mobile.png` vs 子目录 `dark/mobile/bbox-toolbar.png`？
   - 倾向后者：sidebar / manifest 体感更清晰
4. CI 频率：每周一 cron 还是每个 release tag 触发？
   - 倾向二选一 + 手动 dispatch
5. Playwright Component Testing 要不要并入？
   - 不在本提案范围；等 design system 独立后单独立项

---

## 9. 文档与 ADR 产出

- [ ] **ADR-00YY**：截图自动化采用「多 project + scene 声明式 + 视觉回归独立通道」的取舍
- [ ] `docs-site/dev/how-to/update-screenshots.md`（新）
- [ ] `docs-site/dev/reference/screenshot-data-attrs.md`（新）：约定 `[data-screenshot-mask]` / 高亮目标的 data 属性
- [ ] CHANGELOG —— M1~M4 各加一段
- [ ] `IMAGE_CHECKLIST.md` 在 M4 后归档（用 manifest 替代）

---

## 10. 时间预估

| 切片 | 估计工时 |
|---|---|
| M1 矩阵化 + locator 截图 | ~5 工作日 |
| M2 注释 + 脱敏 + 状态 mock | ~4 工作日 |
| M3 流程 GIF + trace 调试 | ~4 工作日 |
| M4 视觉回归 + 文档渲染 | ~4 工作日 |
| **合计** | **~3.5 周** |

---

## Sources

- Playwright 多 project：https://playwright.dev/docs/test-projects
- `locator.screenshot()`：https://playwright.dev/docs/api/class-locator#locator-screenshot
- `mask` 参数：https://playwright.dev/docs/api/class-page#page-screenshot-option-mask
- `toHaveScreenshot` 视觉回归：https://playwright.dev/docs/test-snapshots
- Playwright trace viewer：https://playwright.dev/docs/trace-viewer
- sharp：https://sharp.pixelplumbing.com/
- ffmpeg GIF best practices：https://trac.ffmpeg.org/wiki/Create+a+thumbnail+image+every+X+seconds+of+the+video
- 退役前的源码：[截图场景目录](https://github.com/YOAO-Labs/ai-annotation-platform/tree/474b1ce171388f2e475024836d1a54c2f248fcb5/apps/web/e2e/screenshots/scenes)、[Playwright 截图配置](https://github.com/YOAO-Labs/ai-annotation-platform/blob/474b1ce171388f2e475024836d1a54c2f248fcb5/apps/web/playwright.screenshots.config.ts)。当前流程见[按需截图和录屏](../../docs-site/dev/how-to/update-screenshots.md)及[截图维护清单](../../docs-site/maintainers/image-checklist.md)。
