import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { useReconnectingWebSocket } from "@/hooks/useReconnectingWebSocket";
import { useAuthStore } from "@/stores/authStore";
import { buildWsUrl } from "@/lib/wsHost";

const BATCH_INVALIDATING_EVENTS = new Set(["batch.status_changed", "batch.assignment_changed"]);

export function useBatchEventsSocket(projectId: string | undefined): void {
  const qc = useQueryClient();
  const token = useAuthStore((s) => s.token);
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
      if (parsed.type === "ping") return;
      if (!parsed.type || !BATCH_INVALIDATING_EVENTS.has(parsed.type)) return;
      qc.invalidateQueries({ queryKey: ["batches", projectId] });
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
    [qc, projectId],
  );

  useReconnectingWebSocket(url, { onMessage, enabled: !!url });
}
