/**
 * v0.9.13 · 项目级 batch 变更 WS 订阅.
 *
 * 后端在 BatchService.transition / check_auto_transitions 转态成功后,
 * `publish_batch_status_change()` 推 `batch.status_changed`; issue #124 起
 * 标注员 / 质检员改派成功后 `publish_batch_assignment_change()` 推
 * `batch.assignment_changed` (改派不改 status, 但会改变「未分派 draft 可预标」的
 * 资格)。两者都发到频道 `project:{project_id}:batch`。本 hook 解析后 invalidate
 * ["batches", projectId] + ["projects"] (后者因为 project 卡片 / 总览也聚合 batch 计数).
 *
 * 在以下 3 个 useBatches 消费方挂载:
 *   - AIPreAnnotate/ProjectDetailPanel
 *   - Projects/sections/BatchesSection (项目设置)
 *   - Workbench/shell/WorkbenchShell
 *
 * 修复 B-15 第二症状: 标注员触发 task in_progress 后 batch 自动从 active/pre_annotated
 * → annotating, 但前端原本无 invalidate 路径 (useBatches 无 refetchInterval, 只
 * 依赖 useNotificationSocket 的 ["notifications"] 通知触发, 而 batch.auto_transition
 * 不写 notification 表). 本 hook 闭环此感知.
 */

import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { useReconnectingWebSocket } from "@/hooks/useReconnectingWebSocket";
import { useAuthStore } from "@/stores/authStore";
import { buildWsUrl } from "@/lib/wsHost";

/** 会让 ["batches", projectId] 缓存失效的 batch 事件类型。 */
const BATCH_INVALIDATING_EVENTS = new Set(["batch.status_changed", "batch.assignment_changed"]);

export function useBatchEventsSocket(projectId: string | undefined): void {
  const qc = useQueryClient();
  const token = useAuthStore((s) => s.token);
  // Root WS URL carries the current account token.  Wait for auth hydration and
  // never fall back to an unauthenticated connection; a token/account switch
  // changes the URL and the reconnecting hook tears the old socket down.
  const url = projectId && token ? buildWsUrl(`/ws/batches/project/${projectId}`, { token }) : null;

  const onMessage = useCallback(
    (e: MessageEvent) => {
      let parsed: { type?: string } | null = null;
      try {
        parsed = JSON.parse(e.data);
      } catch {
        return;
      }
      if (!parsed) return;
      // 心跳 ping 帧不触发 invalidate
      if (parsed.type === "ping") return;
      if (!parsed.type || !BATCH_INVALIDATING_EVENTS.has(parsed.type)) return;
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
    [qc, projectId],
  );

  useReconnectingWebSocket(url, { onMessage, enabled: !!url });
}
