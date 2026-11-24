# 文档站迁移至独立仓库 YOAO-Labs/YOAO-Docs 计划

## 变更概述

本计划将主仓库（`YOAO-Labs/YOAO`，当前远端 `ai-annotation-platform`）中的 `docs-site/` 迁移到组织下的独立仓库 `YOAO-Labs/YOAO-Docs`，并将公开文档站发布到 `https://yoao-labs.github.io/YOAO-Docs/`。

核心决策如下。

1. 目标仓库为 `YOAO-Labs/YOAO-Docs`，GitHub Pages 地址为 `https://yoao-labs.github.io/YOAO-Docs/`。主仓库保留静态重定向页，将旧地址 `https://yoao-labs.github.io/ai-annotation-platform/<path>` 重定向到新地址。
2. 新仓库采用全新初始化单次提交导入，不携带主仓库 `docs-site/` 的 1961 条历史提交，控制仓库体积。
3. 派生文档（OpenAPI、CHANGELOG、快捷键、工作台设置、API 路由索引、环境变量、训练参数）通过主仓库 `repository_dispatch` 触发 `YOAO-Docs` CI 拉取主仓库源码自动同步。本地开发通过环境变量 `YOAO_REPO_PATH` 指向主仓库路径。
4. 计划文件（`docs/plans/`）、ADR（`docs/adr/`）与路线图（`ROADMAP.md`、`ROADMAP/`）仅保留在主仓库，不再镜像或发布到文档站。
5. `docs-site/dev/examples/` 下的 `echo-ml-backend` 与 `mock-v2-backend` 留在主仓库并迁出至 `examples/ml-backend/`，主仓库 CI 继续执行合同测试，文档站引用其 GitHub 源码路径。

## 当前状态

- `PR-1` 已在工作树分支 `docs/doc-site-remove` 完成实现与本地 Unit、Live、Perf 验证，待提交/合并后推进 `PR-2`。
- 尚未创建远端 `YOAO-Labs/YOAO-Docs` 仓库，尚未配置 `DOCS_SYNC_TOKEN`。
- 执行证据记录于 [migration-run-ledger.md](evidence/1795511333_docs-site-separate-repo-migration/migration-run-ledger.md)。

## PR 落地顺序

1. `PR-1`（`YOAO-Labs/YOAO`）：解耦主仓库对 `docs-site/` 的运行时夹具、README 图片、示例后端与仓库级治理脚本依赖。
2. `PR-2`（`YOAO-Labs/YOAO-Docs`）：初始化独立文档仓库，单次提交导入文档内容，并改造跨仓库脚本支持 `YOAO_REPO_PATH`。
3. `PR-3`（`YOAO-Labs/YOAO-Docs`，需人工验收后合并）：接入校验、自动同步与 Pages 发布工作流，上线 `https://yoao-labs.github.io/YOAO-Docs/`。
4. `PR-4`（`YOAO-Labs/YOAO`，需人工验收后合并）：新增跨仓库 `repository_dispatch` 触发器，并将主仓库 Pages 工作流切换为旧地址重定向页。
5. `PR-5`（`YOAO-Labs/YOAO`）：删除 `docs-site/` 目录，清理工作区、锁文件、CI、pre-commit、工具脚本与 SDK 文档测试耦合。
6. `PR-6`（`YOAO-Labs/YOAO`）：更新仓库说明、Agent 技能、README、DEV.md、文章与 PR 模板中的文档链接和指引。

## 依赖关系与文件所有权

```mermaid
flowchart LR
  PR1["PR-1 (YOAO): 解耦夹具、示例与治理脚本"] --> PR2["PR-2 (YOAO-Docs): 初始化仓库与脚本参数化"]
  PR2 --> PR3["PR-3 (YOAO-Docs): CI 同步与 Pages 上线 (人工验收)"]
  PR3 --> PR4["PR-4 (YOAO): 跨仓库触发器与旧地址重定向 (人工验收)"]
  PR4 --> PR5["PR-5 (YOAO): 删除 docs-site 与工具链清理"]
  PR5 --> PR6["PR-6 (YOAO): 链接、指令与技能文档更新"]
```

- 独立于主干可立即开分支的 PR 只有 `PR-1`。
- `PR-2` 依赖 `PR-1` 合并（或以 `PR-1` 提交为导入基线），确保导入内容不含已迁出的 `dev/examples/` 与主仓库治理脚本。
- `PR-3` 依赖 `PR-2` 合并。
- `PR-4` 依赖 `PR-3` 合并且新站 Pages 可公开访问，同时依赖操作者预先在 `YOAO-Labs/YOAO` 配置 `DOCS_SYNC_TOKEN`。
- `PR-5` 依赖 `PR-4` 合并并确认重定向与跨仓库触发正常。
- `PR-6` 依赖 `PR-5` 合并（可与 `PR-5` 末期并行准备，但在 `PR-5` 落地后合并以保证死链检查覆盖最终状态）。

各 PR 允许触碰的文件边界如下。

| PR     | 仓库        | 允许修改范围                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ------ | ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PR-1` | `YOAO`      | `apps/api/fixture-media/**`, `infra/docker/Dockerfile.api`, `.dockerignore`, `apps/api/app/api/v1/_test_seed*.py`, `apps/api/tests/test_media_seed_workspace_*.py`, `apps/api/tests/test_worktree_filtering_identity_native.py`, `docs/assets/readme/**`, `README.md`, `examples/ml-backend/**`, `docs-site/dev/examples/**`, `docs-site/dev/tutorials/ml-backend-starter.md`, `docs-site/scripts/check-{adr,plans-freshness,changelog-version}.mjs`, `scripts/check-{adr,plans-freshness,changelog-version}.mjs`, `scripts/test-suites.json`, `scripts/test-suite-inventory.mjs`, `scripts/plan-e2e-suites.mjs`, `.github/workflows/{ci,docs-validate,docs-acceptance}.yml`, `docs/plans/README.md` |
| `PR-2` | `YOAO-Docs` | 新仓库全部初始文件（根目录文档、`.vitepress/**`, `scripts/**`, `package.json`, `pnpm-lock.yaml`, `.gitignore`, `.prettierignore`, `README.md`）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `PR-3` | `YOAO-Docs` | `.github/workflows/{docs-validate,docs-sync,docs-deploy}.yml`, `README.md`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `PR-4` | `YOAO`      | `.github/workflows/docs-sync-dispatch.yml`, `.github/workflows/docs-deploy.yml`, `scripts/build-docs-redirect.mjs`（或工作流内联构建目录）                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `PR-5` | `YOAO`      | 删除 `docs-site/**`, 修改 `pnpm-workspace.yaml`, `package.json`, `pnpm-lock.yaml`, `.github/workflows/{ci,docs-validate,docs-acceptance,claude-docs-impact}.yml`, `.github/docs-impact-map.json`, `.pre-commit-config.yaml`, `.gitignore`, `.prettierignore`, `.codex/environments/environment.toml`, `scripts/{orca-worktree-setup.sh,test-orca-worktree-setup.py,check-dead-links.mjs,check-doc-impact.mjs,check-doc-version-prefix.mjs,check-product-naming.mjs,plan-e2e-suites.mjs,plan-e2e-suites.test.mjs,export_openapi.py,image-reference-utils*.mjs}`, `packages/python-sdk/tests/test_docs_snippets.py`                                                                                    |
| `PR-6` | `YOAO`      | `CLAUDE.md`, `README.md`, `DEV.md`, `CHANGELOG.md`, `.github/PULL_REQUEST_TEMPLATE.md`, `docs/plans/README.md`, `docs/adr/README.md`, `docs/articles/*.md`, `.agents/skills/**`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |

---

## PR-1 解耦运行时夹具、README 资源、示例后端与治理脚本

- [x] **依赖**：无，直接基于主仓库默认分支开出独立工作树。
- [x] **修改文件**：
  - 媒体夹具迁移：`docs-site/public/home/sam-tools/smart-point.webm` 复制至 `apps/api/fixture-media/smart-point.webm`，更新 `infra/docker/Dockerfile.api`、`.dockerignore`、`apps/api/app/api/v1/_test_seed.py`、`apps/api/app/api/v1/_test_seed_filters.py`、`apps/api/tests/test_media_seed_workspace_native.py`、`apps/api/tests/test_media_seed_workspace_live.py`、`apps/api/tests/test_worktree_filtering_identity_native.py`。
  - README 静态图迁移：将 `docs-site/public/home/ai-assisted-annotation-poster.webp` 与 `docs-site/.vitepress/theme/assets/home/hero/{video-track,pointcloud,data-manager}.webp` 复制至 `docs/assets/readme/`，更新 `README.md` 图片路径。
  - 示例后端迁移：将 `docs-site/dev/examples/echo-ml-backend` 与 `docs-site/dev/examples/mock-v2-backend` 移至 `examples/ml-backend/`，更新 `.github/workflows/ci.yml`、`scripts/test-suites.json`、`scripts/test-suite-inventory.mjs`、`scripts/plan-e2e-suites.mjs`、`docs-site/dev/tutorials/ml-backend-starter.md` 中的 `<!-- snippet:... -->` 路径。
  - 主仓库治理脚本迁移：将 `docs-site/scripts/check-adr.mjs`、`check-plans-freshness.mjs`、`check-changelog-version.mjs` 移至 `scripts/`，修正相对路径，更新 `.github/workflows/docs-validate.yml`、`.github/workflows/docs-acceptance.yml` 与 `docs/plans/README.md`。
- [x] **本 PR 构建的单一变更**：切断主仓库代码、Docker 镜像、后端测试、CI 示例测试、README 渲染与仓库治理脚本对 `docs-site/` 目录的硬编码依赖。
- [x] **可见结果**：`rg "docs-site/(public|dev/examples)" apps/ infra/ README.md scripts/ .github/workflows/ci.yml` 返回为空，API 镜像与示例后端测试均脱离 `docs-site/` 独立运行。
- [x] **验证（Unit）**：
  - 在工作树运行 `node --test scripts/*.test.mjs`、`node scripts/check-adr.mjs`、`node scripts/check-plans-freshness.mjs`、`node scripts/check-changelog-version.mjs`。
  - 运行 `examples/ml-backend/echo-ml-backend` 与 `examples/ml-backend/mock-v2-backend` 的 pytest 套件全部通过。
  - 运行引用 `smart-point.webm` 的后端单测或路径断言测试全部通过。
- [x] **验证（Live）**：
  - 在独立工作树执行 `docker build -f infra/docker/Dockerfile.api --target runtime .`（或等效构建阶段），检查容器内 `/app/fixture-media/smart-point.webm` 存在且大小与源文件一致（462097 字节）。
  - 执行 `pnpm --filter docs-site check:snippets` 与 `pnpm docs:build`，确认片段校验对 `examples/ml-backend/echo-ml-backend/main.py` 生效且当前文档站仍可完整构建。
- [x] **验证（Perf）**：
  - 指标：`infra/docker/Dockerfile.api` 构建上下文传输体积与 `pnpm docs:build` 耗时。
  - 探针：先在主干记录基线（`docker build` 上下文大小与 `time pnpm docs:build`），再在本 PR 工作树执行同一探针。
  - 失败阈值：Docker 构建上下文体积增加超过 1 MB，或 `pnpm docs:build` 耗时退化超过基线 15%。

---

## PR-2 初始化 YOAO-Labs/YOAO-Docs 并完成脚本跨仓库参数化

- [ ] **依赖**：`PR-1` 已合并；已在 `YOAO-Labs` 组织下创建公开仓库 `YOAO-Labs/YOAO-Docs`。
- [ ] **修改文件**：
  - 将 `PR-1` 之后 `docs-site/` 的内容提升到 `YOAO-Labs/YOAO-Docs` 根目录，排除 `node_modules/`、`.vitepress/cache/`、`.vitepress/dist/`、已生成的 `public/openapi.json`、`public/llms*.txt`、`dev/adr/`、`changelog/`、`roadmap/`。
  - 将 `scripts/image-reference-utils.mjs`、`scripts/image-reference-utils.test.mjs` 与 `scripts/check-dead-links.mjs` 迁入 `YOAO-Docs/scripts/` 并适配根目录路径。
  - 改造跨仓库脚本（`scripts/sync-openapi.mjs`、`mirror-changelog.mjs` 仅保留 CHANGELOG 镜像、`generate-hotkeys.mjs`、`generate-settings.mjs`、`generate-settings.test.mjs`、`generate-api-index.mjs`、`generate-env-vars.mjs`、`generate-training-reference.py`、`check-doc-snippets.mjs`），统一从 `process.env.YOAO_REPO_PATH ?? resolve(DOCS_ROOT, "../YOAO")` 解析主仓库根目录；主仓库路径缺失时报错并打印设置方法。移除 `scripts/mirror-adr.mjs` 及 `mirror-changelog.mjs` 中的 ROADMAP 镜像逻辑。
  - 更新 `.vitepress/config.ts` 与 `.vitepress/navigation/`：`sitemap.hostname` 改为 `https://yoao-labs.github.io/YOAO-Docs/`；移除 ADR 与 ROADMAP 侧边栏、导航入口及 `editLink` 映射；`editLink.pattern` 仅将 `changelog/` 指向 `YOAO-Labs/YOAO`，其余页面指向 `YOAO-Labs/YOAO-Docs`。
  - 更新 `scripts/generate-llms.mjs` 中的 `SITE` 为 `https://yoao-labs.github.io/YOAO-Docs`，更新 `dev/index.md` 与 `dev/tutorials/ml-backend-starter.md` 链接。
  - 新增独立 `pnpm-lock.yaml`、`.gitignore`、`.prettierignore` 与 `README.md`（说明本地预览与 `YOAO_REPO_PATH` 用法）。
- [ ] **本 PR 构建的单一变更**：建立不依赖主仓库 pnpm workspace 的独立文档仓库，并让所有同步与生成脚本支持通过 `YOAO_REPO_PATH` 跨仓库读取源码。
- [ ] **可见结果**：在独立克隆目录中执行 `YOAO_REPO_PATH=<主仓库绝对路径> pnpm build` 可零报错产出完整站点，且 `git rev-list --count HEAD` 为初始提交数（不携带主仓库历史）。
- [ ] **验证（Unit）**：
  - 在 `YOAO-Docs` 工作树中设置 `YOAO_REPO_PATH` 运行 `pnpm check:snippets`、`pnpm check:frontmatter`、`pnpm check:navigation -- --strict`、`pnpm check:readability`、`pnpm check:orphan-images`、`pnpm test:references`、`pnpm check:diagrams`、`pnpm check:codegen` 与 `node scripts/check-dead-links.mjs .`，全部通过。
  - 取消设置且不存在 `../YOAO` 时运行 `node scripts/sync-openapi.mjs`，验证退出码非 0 且错误消息明确提示设置 `YOAO_REPO_PATH`。
- [ ] **验证（Live）**：
  - 在 `YOAO-Docs` 工作树执行 `DOCS_BASE=/YOAO-Docs/ YOAO_REPO_PATH=<主仓库路径> pnpm build && pnpm preview`，通过 `agent-browser` 打开预览地址。
  - 验收断言：首页渲染正常且无控制台报错；搜索框能检出工作台文档；`/YOAO-Docs/api/` 的 Scalar 视图成功加载 `openapi.json`；`/YOAO-Docs/changelog/` 镜像页可正常访问；导航与侧边栏不再包含 ADR、ROADMAP 与计划页；普通页面的"在 GitHub 编辑此页"指向 `YOAO-Labs/YOAO-Docs`，CHANGELOG 页面指向 `YOAO-Labs/YOAO`。
- [ ] **验证（Perf）**：
  - 指标：`YOAO-Docs` 仓库 `.git` 目录体积与冷启动 `pnpm install && pnpm build` 总耗时。
  - 探针：先在主仓库记录 `pnpm --filter docs-site build` 耗时作为基线，再在新仓库记录 `du -sh .git` 与 `time pnpm build`。
  - 失败阈值：新仓库初始 `.git` 体积超过 100 MB，或 `pnpm build` 耗时超过主仓库基线 20%。

---

## PR-3 接入 YOAO-Docs CI 校验、自动同步与 Pages 发布（人工验收门禁）

- [ ] **依赖**：`PR-2` 已合并；仓库 Settings -> Pages 已启用 GitHub Actions 部署源。
- [ ] **修改文件**：
  - 新增 `.github/workflows/docs-validate.yml`：在 PR 与 push 时检出 `YOAO-Docs` 及 `YOAO-Labs/YOAO@main` 到 `_yoao-source/`，设置 `YOAO_REPO_PATH=${{ github.workspace }}/_yoao-source`，执行全部检查脚本、死链检查与 VitePress 构建。
  - 新增 `.github/workflows/docs-sync.yml`：监听 `repository_dispatch`（类型 `yoao-docs-sync`）与 `workflow_dispatch`，按事件载荷 `ref`（默认 `main`）检出 `YOAO-Labs/YOAO`，运行代码派生文档生成器（`hotkeys`、`settings`、`api-index`、`env-vars`、`training-reference`）。若受跟踪的生成文件有 diff，自动创建或更新同步 PR（分支 `chore/docs-sync`）；若无受跟踪文件改动（例如仅 CHANGELOG/OpenAPI 变动），直接触发或串联 Pages 重新部署。
  - 新增 `.github/workflows/docs-deploy.yml`：监听 `push` 到 `main`、`workflow_dispatch` 与 `docs-sync` 完成信号，检出 `YOAO-Labs/YOAO@main` 完成构建期镜像（`sync-openapi`、`mirror-changelog`），设置 `DOCS_BASE=/YOAO-Docs/` 构建并发布至 GitHub Pages。
  - 按主仓库工作流命名规范校验所有新工作流（顶级与 job 名称唯一、无斜杠）。
- [ ] **本 PR 构建的单一变更**：为 `YOAO-Labs/YOAO-Docs` 建立独立的校验、跨仓库同步和 GitHub Pages 自动发布流水线。
- [ ] **可见结果**：`https://yoao-labs.github.io/YOAO-Docs/` 公开可访问，`workflow_dispatch` 手动触发 `docs-sync.yml` 可完整跑通。
- [ ] **验证（Unit）**：
  - PR 内 `docs-validate.yml` 全部步骤在 GitHub Actions 跑绿。
  - 手动触发 `docs-sync.yml`（指向 `YOAO-Labs/YOAO` 当前 `main`），确认无报错且在无变更时不产生空 PR。
- [ ] **验证（Live）**：
  - 使用 `agent-browser` 访问公网线上地址 `https://yoao-labs.github.io/YOAO-Docs/`。
  - 验收断言：首页、`/user-guide/`、`/dev/`、`/ops/`、`/api/`、`/changelog/` 均返回 HTTP 200 且静态资源（图片、图标、`openapi.json`、`llms.txt`）无 404；移动端与暗色模式切换正常。
  - **人工验收门禁**：由操作者确认线上新站页面、导航与同步 PR 行为符合预期后再推进 `PR-4`。
- [ ] **验证（Perf）**：
  - 指标：`docs-deploy.yml` 从触发到 Pages 生效的端到端 CI 耗时。
  - 探针：先记录主仓库最近 3 次 `docs-deploy.yml` 成功运行耗时的中位数作为基线，再记录 `YOAO-Docs` 首次与二次（命中 pnpm cache）部署耗时。
  - 失败阈值：命中缓存后部署耗时超过基线 30% 或超过 5 分钟。

---

## PR-4 主仓库接入跨仓库派发与旧地址 Pages 重定向（人工验收门禁）

- [ ] **依赖**：`PR-3` 已合并且通过人工验收；操作者已在 `YOAO-Labs/YOAO` 仓库 Secrets 配置 `DOCS_SYNC_TOKEN`（对 `YOAO-Labs/YOAO-Docs` 具有 `contents: write` 权限的 Fine-grained PAT 或 GitHub App Token）。
- [ ] **修改文件**：
  - 新增 `.github/workflows/docs-sync-dispatch.yml`：监听 `push` 到 `main`（限定路径：`apps/api/openapi.snapshot.json`、`apps/api/app/api/v1/**`、`apps/web/src/pages/Workbench/state/{hotkeys,workbenchSettingsFields,workbenchLocalDefaults}.ts`、`apps/web/src/api/auth.ts`、`apps/_shared/{training,task}_contract/**`、`.env.example`、`CHANGELOG.md`、`docs/changelogs/**`、`examples/ml-backend/**`）以及 `release` 发布事件，向 `YOAO-Labs/YOAO-Docs` 发送 `repository_dispatch`（`event_type: yoao-docs-sync`，载荷携带 `ref` 与 `sha`）。
  - 替换 `.github/workflows/docs-deploy.yml`：不再构建 `docs-site/`，改为生成极简静态重定向包（`index.html` 与 `404.html`，同时包含 `<meta http-equiv="refresh">` 与保留子路径、query、hash 的 JS 重定向，指向 `https://yoao-labs.github.io/YOAO-Docs/<subpath>`），并部署到主仓库 GitHub Pages。
- [ ] **本 PR 构建的单一变更**：将主仓库文档发布链路切换为向新仓库派发同步事件，并为旧 Pages 域名提供无损路径重定向。
- [ ] **可见结果**：访问旧站任意深链自动跳转到新站对应页面，主仓库源码变更推送 `main` 后自动触发 `YOAO-Docs` 的 `docs-sync.yml`。
- [ ] **验证（Unit）**：
  - 运行 `node scripts/check-workflow-names.mjs --strict` 全部通过。
  - 本地运行重定向页面生成逻辑，校验生成的 `index.html` 与 `404.html` 在不同子路径（含 query 与 hash）下输出目标 URL 正确。
- [ ] **验证（Live）**：
  - 合并前在本地 HTTP 服务挂载生成的重定向静态目录，使用 `agent-browser` 访问 `/ai-annotation-platform/` 与 `/ai-annotation-platform/dev/reference/ml-backend-protocol?tab=v2#schema`，断言跳转到 `https://yoao-labs.github.io/YOAO-Docs/` 对应路径且保留 query 与 hash。
  - 合并后用 `agent-browser` 访问公网旧地址 `https://yoao-labs.github.io/ai-annotation-platform/user-guide/`，断言最终落点为 `https://yoao-labs.github.io/YOAO-Docs/user-guide/` 且状态 200。
  - 手动触发一次 `docs-sync-dispatch.yml`，在 `YOAO-Labs/YOAO-Docs` Actions 面板确认收到 `yoao-docs-sync` 事件并运行成功。
  - **人工验收门禁**：由操作者确认线上重定向与跨仓库 dispatch 均正常后，再执行 `PR-5` 的删除操作。
- [ ] **验证（Perf）**：
  - 指标：重定向页面体积与主仓库 `docs-deploy.yml` 耗时。
  - 探针：记录旧 `docs-deploy.yml` 耗时基线，对比切换为静态重定向包后的构建体积与 CI 耗时。
  - 失败阈值：重定向包体积超过 20 KB，或主仓库 `docs-deploy.yml` 总耗时超过 60 秒。

---

## PR-5 主仓库删除 docs-site 并清理工作区与工具链

- [ ] **依赖**：`PR-4` 已合并且通过人工验收。
- [ ] **修改文件**：
  - 删除 `docs-site/` 整个目录。
  - 更新 `pnpm-workspace.yaml` 移除 `docs-site`，更新根 `package.json` 移除 `docs:dev`、`docs:build`、`docs:preview`、`docs:hotkeys`、`docs:settings`、`docs:gen`、`docs:gen-env-vars`，运行 `pnpm install` 刷新 `pnpm-lock.yaml`。
  - 清理 CI 与 Hooks：
    - `.github/workflows/ci.yml` 移除 `docs-site/scripts/*.test.mjs` 与 `pnpm docs:build`。
    - 删除 `.github/workflows/docs-acceptance.yml`。
    - 精简 `.github/workflows/docs-validate.yml`，仅保留主仓库文档治理检查（`scripts/check-adr.mjs`、`scripts/check-plans-freshness.mjs`、`scripts/check-changelog-version.mjs`、`scripts/check-dead-links.mjs` 扫描 `README.md`、`DEV.md`、`docs/`）。
    - 清理 `.pre-commit-config.yaml` 中对 `docs-site/` 的自动生成 hook 与排除正则。
  - 清理配置文件与脚本：`.gitignore`、`.prettierignore`、`.codex/environments/environment.toml`、`scripts/orca-worktree-setup.sh`、`scripts/test-orca-worktree-setup.py`、`scripts/check-dead-links.mjs`（移除 `docs-site/.vitepress/content.mjs` 导入与 `docs-site/` 特判）、`scripts/check-doc-version-prefix.mjs`、`scripts/check-product-naming.mjs`、`scripts/plan-e2e-suites.mjs`、`scripts/plan-e2e-suites.test.mjs`、`scripts/export_openapi.py` 注释、删除不再被主仓库使用的 `scripts/image-reference-utils*.mjs`。
  - 调整 `packages/python-sdk/tests/test_docs_snippets.py`：改为校验 `packages/python-sdk/README.md` 中的 Python 代码块（原 `docs-site/dev/sdk/*.md` 片段校验已归属 `YOAO-Docs`）。
  - 更新 `.github/workflows/claude-docs-impact.yml`、`.github/docs-impact-map.json` 与 `scripts/check-doc-impact.mjs`：保留影响提示能力，将提示路径前缀改为 `YOAO-Labs/YOAO-Docs:<path>`（避免把跨仓库路径误判为主仓库本地文件）。
- [ ] **本 PR 构建的单一变更**：彻底移除主仓库 `docs-site/` 及其全部构建、锁文件、hook 与脚本残留。
- [ ] **可见结果**：`test ! -d docs-site` 成立，`pnpm install --frozen-lockfile`、`pnpm lint`、`pnpm typecheck`、`pnpm test` 与 `git diff --check` 全部通过。
- [ ] **验证（Unit）**：
  - 在工作树运行 `node --test scripts/*.test.mjs`、`uv run pytest scripts/test-orca-worktree-setup.py`、`uv run pytest packages/python-sdk/tests/test_docs_snippets.py`。
  - 运行 `node scripts/check-workflow-names.mjs --strict`、`node scripts/check-adr.mjs`、`node scripts/check-plans-freshness.mjs`、`node scripts/check-changelog-version.mjs`、`node scripts/check-product-naming.mjs`。
  - 运行 `pnpm lint` 与 `pnpm typecheck`。
- [ ] **验证（Live）**：
  - 在全新独立工作树执行 `bash scripts/orca-worktree-setup.sh`，确认不再尝试链接 `docs-site/node_modules`，`pnpm --filter web build` 正常产出。
  - 使用 `pre-commit run --all-files`（或对改动文件执行）确认所有本地 hook 无缺失脚本报错。
- [ ] **验证（Perf）**：
  - 指标：主仓库工作区体积、`pnpm install --frozen-lockfile` 耗时与 `ci.yml` 前端/脚本 job 耗时。
  - 探针：对比 `PR-5` 前后 `du -sh .`（排除 `.git` 与 `node_modules`）和 `time pnpm install --frozen-lockfile --ignore-scripts`。
  - 失败阈值：主仓库源码体积减少低于 70 MB（`docs-site` 当前为 85 MB），或 `pnpm install` 耗时不降反升超过 10%。

---

## PR-6 更新主仓库文档、指令与技能链接

- [ ] **依赖**：`PR-5` 已合并。
- [ ] **修改文件**：
  - `CLAUDE.md`（`AGENTS.md` 软链指向此文件）：更新文档变更对照表、设计系统链接、测试指南链接、截图指引链接与预览命令说明，指向 `YOAO-Labs/YOAO-Docs` 与 `https://yoao-labs.github.io/YOAO-Docs/`。
  - `README.md` 与 `DEV.md`：将所有 `./docs-site/...` 与旧 `https://yoao-labs.github.io/ai-annotation-platform/...` 链接替换为 `https://yoao-labs.github.io/YOAO-Docs/...`。
  - `.github/PULL_REQUEST_TEMPLATE.md`、`docs/plans/README.md`、`docs/adr/README.md`、`docs/articles/01-*.md` 至 `06-*.md`：更新在线文档链接与跨仓库协作说明。
  - `.agents/skills/` 下引用 `docs-site/` 的技能文档（`yoao-release`、`yoao-doc-media`、`yoao-runtime`、`yoao-backend-boundaries`、`yoao-dataset-storage`、`yoao-online-learning`、`yoao-prediction-pipeline`、`shadcn`、`design-taste-frontend`、`redesign-existing-projects`）：替换本地相对路径为新文档站 URL 或 `YOAO-Labs/YOAO-Docs` 路径说明。
  - `CHANGELOG.md`：在顶部 `Unreleased` 节记录文档站迁移至独立仓库与新访问地址。
- [ ] **本 PR 构建的单一变更**：统一主仓库所有面向人、Agent 与贡献者的文档指引和外链到新文档站，消除指向已删除 `docs-site/` 的断链。
- [ ] **可见结果**：`rg "docs-site/" README.md DEV.md CLAUDE.md .github/ .agents/skills/ docs/plans/README.md docs/adr/README.md docs/articles/` 返回为空（历史归档目录 `docs/plans/archive/`、`docs/adr/archive/`、`docs/changelogs/`、`ROADMAP/archive/` 保持原样不批量改写）。
- [ ] **验证（Unit）**：
  - 运行 `node scripts/check-dead-links.mjs README.md DEV.md CLAUDE.md docs/plans/README.md docs/adr/README.md docs/articles/` 零断链。
  - 运行 `node scripts/check-doc-version-prefix.mjs --staged` 与 `git diff --check` 无错误。
- [ ] **验证（Live）**：
  - 抽检 `README.md` 与 `DEV.md` 更新后的全部 `https://yoao-labs.github.io/YOAO-Docs/...` 链接（通过 `curl -fsI` 或 `lychee` 针对改动文件执行），断言全部返回 HTTP 200。
  - 在 `agent-browser` 打开渲染后的 README 图片与关键跳转页，确认 4 张 hero 图与跳转目标均正常加载。
- [ ] **验证（Perf）**：
  - 指标：`scripts/check-dead-links.mjs` 全量扫描耗时。
  - 探针：对比 `PR-5` 前（含 `docs-site/`）与 `PR-6` 后运行 `time node scripts/check-dead-links.mjs` 的耗时。
  - 失败阈值：移除 `docs-site/` 后扫描耗时未下降或超过 5 秒。

---

## 尚未验证的外部前提

以下项目无法在当前规划阶段通过本地命令证实，需在执行对应 PR 前由操作者完成或确认。

1. `YOAO-Labs/YOAO-Docs` 仓库创建权限与 GitHub Pages 组织级策略是否允许公开站点部署。
2. `DOCS_SYNC_TOKEN`（用于主仓库 `repository_dispatch` 调用 `YOAO-Labs/YOAO-Docs`）需由组织管理员签发并写入 `YOAO-Labs/YOAO` 的 Actions Secrets。
3. 主仓库 GitHub 实际名称已为 `YOAO-Labs/YOAO`，而旧 Pages 路径配置为 `/ai-annotation-platform/`；执行 `PR-4` 前需在仓库 Settings -> Pages 确认当前真实发布的 base path，避免重定向路径前缀失配。
