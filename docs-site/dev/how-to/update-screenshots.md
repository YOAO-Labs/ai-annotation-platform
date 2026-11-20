---
title: 按需截图与录屏
description: 在真实界面验证指定流程，按需采集证据或更新指定的文档素材
audience: [dev]
type: how-to
status: stable
last_reviewed: 2026-10-22
---

# 按需截图与录屏

需要核对界面、提供演示或更新某份文档素材时，由 AI 在真实浏览器中操作本次指定的流程。仓库不维护截图或录屏场景、基线或定期采集任务。功能和渲染测试照常运行，可保留失败诊断。

## 按需操作真实界面

1. **明确目标。** 确认要展示的页面、账号角色、起始状态、关键操作和预期结果；只有请求需要时才指定主题、视口或交付格式。
2. **核对环境。** 在获授权的隔离环境中核对实际版本和 Web 地址。会写数据时，核对 API、数据库、存储及所需的 worker 和队列都属于该环境；可按[多工作树环境](./worktree-environments)准备，使用启动器实际打印的地址。只用角色匹配的测试账号及可丢弃数据；拟公开的样本还须确认许可和传播范围。不要借用生产数据、日常开发资源或他人的浏览器页签。
3. **操作并核对。** 先读 `agent-browser` 技能和 CLI 的 `core` 指南，每次任务新建一个命名会话。通过 `snapshot -i` 定位实际控件，页面变化后重新取快照，等待具体可见状态或真实请求完成。画布操作应按实际渲染内容定位；涉及保存时还要检查响应并在必要时刷新读回。演示推理时使用真实服务。不能通过改写界面状态、替换业务响应或补录别的画面把失败变成成功。
4. **只采集需要的证据。** 登录和含敏感信息的准备步骤结束后，按需截图，或在同一会话录制指定动作和结果。临时文件放在仓库之外，不录凭据、签名链接、真实账号、通知或客户数据。

以下命令仅示意如何在同一会话观察、截图和录屏，不是固定流程。先设置核对过的 `AUTHORIZED_WEB_URL`；录屏需 PATH 中有 `ffmpeg`。

```bash
agent-browser skills get core
capture_dir="$(mktemp -d /tmp/yoao-ui-media.XXXXXX)"
export AGENT_BROWSER_SESSION="$(agent-browser session id --scope worktree --prefix "ui-media-${capture_dir##*.}")"
agent-browser open "$AUTHORIZED_WEB_URL"
agent-browser snapshot -i
# 操作至目标起始状态并确认隐私后，按需截图：
agent-browser screenshot "$capture_dir/state.png"
# 或用同一会话录屏：
agent-browser record start "$capture_dir/flow.mp4"
# 用真实控件完成所需操作，观察结果后停止：
agent-browser record stop
agent-browser close
```

将文件回放检查与界面验收分开：查看截图实物或回放成片的开头、关键交互及结尾，核对画面可读、操作与真实结果吻合、没有黑帧或私密信息；如有封面也单独检查。文件生成成功不代表内容正确或保存成功。汇报实际版本、角色、观察结果和阻碍，按归属收尾，临时文件仅按授权交付或清理。

## 更新指定的公开素材

临时证据不是发布素材。仅在任务明确要求更新时，经内容和隐私复核后替换指定文件及其引用；其余已发布素材保持原样。沿用[现有存放路径](../reference/generated-artifacts#产物总表)，在相关操作旁引用。图片写准确的 `alt`；文档视频沿用 `<DocsVideo>`，保留贴切的封面、图注和必要的文字步骤或字幕／文字稿，不能只靠画面传达关键信息。短 GIF 也须有文字说明。不要按矩阵补拍或批量刷新。

```bash
node docs-site/scripts/check-orphan-images.mjs --strict
pnpm docs:build
```

文件和引用检查不能代替本地预览；验证实际页面的媒体位置、可读性和播放。若明确要求更新首页 Hero **静态卡片**，可用 `pnpm --filter docs-site media:home-hero --asset <id>` 将核对过的源图转换为 WebP；该命令不操作浏览器。
