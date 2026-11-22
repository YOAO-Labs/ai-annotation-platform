---
title: 文档产物与真值边界
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-07-29
---

# 文档产物与真值边界

文档站包含手写正文、受版本控制的生成文件、仅在构建期出现的镜像，以及图片和示例源码。维护时先确认真值源，再决定编辑位置和是否提交产物；不要直接修改构建期镜像。

## 产物总表

| 类型            | 站点路径或产物                                                                                                                  | 真值源                                                                                           | 生成或同步方式                                                                                              | Git 跟踪           | Pages 部署触发                                                               |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- | ------------------ | ---------------------------------------------------------------------------- |
| 手写正文        | `docs-site/index.md`、`docs-site/user-guide/**`、`docs-site/dev/**`、`docs-site/api/**`、`docs-site/ops/**` 中非生成的 Markdown | 文件本身                                                                                         | 无，直接编辑                                                                                                | 是                 | `docs-site/**`                                                               |
| 手写公共资源    | `docs-site/public/api-reference.html`                                                                                           | 文件本身                                                                                         | 无，直接编辑                                                                                                | 是                 | `docs-site/**`                                                               |
| 受跟踪生成页    | `docs-site/user-guide/workbench/hotkeys.generated.md`                                                                           | `apps/web/src/pages/Workbench/state/hotkeys.ts`                                                  | `docs-site/scripts/generate-hotkeys.mjs`；`pnpm docs:gen` 或文档构建前自动刷新                              | 是，生成后需提交   | 提交生成页时由 `docs-site/**` 触发；源文件变化会在文档校验中执行一致性检查   |
| 受跟踪生成物    | `apps/web/public/avatars/pixel/**`（32 张 `pixel-NN.svg` + `manifest.json`）                                                    | `apps/web/scripts/gen-pixel-avatars.mjs` 的固定选项表                                            | `pnpm --filter web gen:avatars`；`--check` 只比对不写入                                                     | 是，生成后需提交   | 修改脚本或产物本身由 `apps/web/**` 触发常规 CI                               |
| 受跟踪生成页    | `docs-site/user-guide/workbench/settings.generated.md`                                                                          | 工作台的 `workbenchSettingsFields.ts`、`workbenchLocalDefaults.ts` 与 `apps/web/src/api/auth.ts` | `pnpm --filter docs-site settings`；文档构建前自动刷新，`check:codegen` 只读比对                            | 是，生成后需提交   | 源文件变化会触发文档一致性检查；提交生成页时由 `docs-site/**` 触发 Pages     |
| 受跟踪生成页    | `docs-site/api/guides/_routes.generated.md`                                                                                     | `apps/api/app/api/v1/**` 中的路由声明                                                            | `pnpm docs:gen`；pre-commit 与文档开发/构建前自动刷新，`check:codegen` 只读比对                             | 是，生成后需提交   | 路由源码会触发文档校验；提交生成页时由 `docs-site/**` 触发 Pages             |
| 受跟踪生成页    | `docs-site/dev/reference/env-vars.md`                                                                                           | `.env.example` 的变量与注释                                                                      | `pnpm docs:gen-env-vars`                                                                                    | 是，生成后需提交   | 提交生成页时由 `docs-site/**` 触发；仅修改 `.env.example` 不会单独触发 Pages |
| 受跟踪 API 契约 | `apps/api/openapi.snapshot.json`                                                                                                | FastAPI 路由与 Pydantic schema                                                                   | `pnpm openapi:export`                                                                                       | 是，唯一版本化契约 | `apps/api/openapi.snapshot.json`                                             |
| 构建期镜像      | `docs-site/dev/adr/**`                                                                                                          | `docs/adr/**`                                                                                    | `docs-site/scripts/mirror-adr.mjs`；文档开发或构建前重建                                                    | 否，已忽略         | `docs/adr/**`                                                                |
| 构建期镜像      | `docs-site/changelog/**`                                                                                                        | `CHANGELOG.md` 与 `docs/changelogs/**`                                                           | `docs-site/scripts/mirror-changelog.mjs`；文档开发或构建前重建                                              | 否，已忽略         | `CHANGELOG.md`、`docs/changelogs/**`                                         |
| 构建期镜像      | `docs-site/roadmap/**`                                                                                                          | `ROADMAP.md`、`ROADMAP/*.md` 与 `ROADMAP/archive/*.md`                                           | `docs-site/scripts/mirror-changelog.mjs`；文档开发或构建前重建，归档源仍保留 `/roadmap/archived-*` 公开 URL | 否，已忽略         | `ROADMAP.md`、`ROADMAP/**`                                                   |
| 构建期公共文件  | `docs-site/public/openapi.json`                                                                                                 | `apps/api/openapi.snapshot.json`                                                                 | `docs-site/scripts/sync-openapi.mjs`；文档开发或构建前复制                                                  | 否，已忽略         | `apps/api/openapi.snapshot.json`                                             |
| 构建期公共文件  | `docs-site/public/llms.txt`、`docs-site/public/llms-full.txt`                                                                   | 当前可发布 Markdown 正文                                                                         | `docs-site/scripts/generate-llms.mjs`；文档开发或构建前生成                                                 | 否，已忽略         | `docs-site/**`，以及 ADR、CHANGELOG、Roadmap 的真值源                        |
| 示例源码        | `docs-site/dev/examples/**`                                                                                                     | 示例目录中的源码和 README                                                                        | 无；作为可复制示例维护，不作为正文页面维护                                                                  | 是                 | `docs-site/**`                                                               |
| 用户手册配图    | `docs-site/user-guide/images/**`（含 GIF）                                                                                      | 已发布文件及其正文引用                                                                           | [按需更新指定素材](../how-to/update-screenshots)，不随构建生成                                              | 是，核对后提交     | `docs-site/**`                                                               |
| 用户手册视频    | `docs-site/public/media/**` 中的视频与 WebP 封面                                                                                | 已发布文件及 `<DocsVideo>` 引用                                                                  | 按需更新指定素材，保留封面及文字说明                                                                        | 是，核对后提交     | `docs-site/**`                                                               |
| 首页演示媒体    | `docs-site/public/home/**` 中的视频与封面；`docs-site/.vitepress/theme/assets/home/hero/**` 中的静态图                          | 已发布文件及主题组件引用                                                                         | 按需更新；静态卡片可独立从已核对的源图转换 WebP                                                             | 是，核对后提交     | `docs-site/**`                                                               |
| 宣传文章配图    | `docs/articles/media/**` 中的 GIF 和静态图                                                                                      | 已发布文件及文章引用                                                                             | 按需更新指定素材，与正文交付分开                                                                            | 是，核对后提交     | 不触发 Pages                                                                 |
| 可编辑文档图    | `docs-site/public/diagrams/**`                                                                                                  | 内嵌 Excalidraw scene 与 Virgil 字体的 SVG 文件本身                                              | 使用 Excalidraw 重新导入和导出；`pnpm --filter docs-site check:diagrams` 校验合同                           | 是                 | `docs-site/**`                                                               |
| 维护材料        | `docs-site/maintainers/image-checklist.md`                                                                                      | 历史编辑备注与素材索引                                                                           | 不作为采集清单或发布门禁                                                                                    | 是，但不发布为页面 | `docs-site/**` 会触发构建，文件本身被排除在发布内容之外                      |

## 维护规则

1. 手写正文直接修改 canonical 页面；同一主题不要在多个目录维护重复说明。
2. 受跟踪生成文件必须从真值源重新生成，并与真值源放在同一提交中。生成文件中的手工修改会在下次生成时丢失；`check:codegen` 会只读比对快捷键、工作台设置和 API 路由索引。
3. `docs-site/dev/adr/`、`docs-site/changelog/` 和 `docs-site/roadmap/` 是构建期镜像。只编辑对应的仓库源文件，不提交镜像目录。
4. `docs-site/public/openapi.json`、`llms.txt` 和 `llms-full.txt` 是构建输出，不要加入版本控制。
5. 示例 README、维护清单和其他内部材料不是站点正文；它们保留在 `docs-site/` 只是为了和相关资产就近维护。
6. 已发布媒体以实际文件和引用为准，不随产品测试、文档构建或发布检查重新采集。只复核本次更新的媒体。
7. Excalidraw 图表不属于用户手册截图。使用 `<ExcalidrawDiagram>` 引用，并以内嵌 scene 的单个 SVG 作为唯一真值。

## 常用同步命令

```bash
pnpm docs:gen
pnpm docs:gen-env-vars
pnpm openapi:export
pnpm --filter docs-site check:diagrams
node docs-site/scripts/check-orphan-images.mjs --strict
pnpm docs:build
```

`pnpm docs:build` 会物化所有构建期镜像和公共文件，也会刷新构建前挂载的受跟踪生成页。提交前只暂存应当受 Git 跟踪的文件。
