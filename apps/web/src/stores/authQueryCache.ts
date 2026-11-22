import type { QueryClient } from "@tanstack/react-query";
import { useAuthStore } from "./authStore";

export function bindAuthQueryCache(client: QueryClient): () => void {
  return useAuthStore.subscribe((current, previous) => {
    if (
      current.user?.id !== previous.user?.id ||
      current.token !== previous.token ||
      current.sessionId !== previous.sessionId
    ) {
      client.clear();
    }
  });
}
