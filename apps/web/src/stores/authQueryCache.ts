import type { QueryClient } from "@tanstack/react-query";
import { useAuthStore } from "./authStore";

/** Query caches live in memory; durable offline records keep their own account owner. */
export function bindAuthQueryCache(client: QueryClient): () => void {
  return useAuthStore.subscribe((current, previous) => {
    if (
      current.user?.id !== previous.user?.id ||
      current.token !== previous.token ||
      current.sessionId !== previous.sessionId
    ) {
      // clear() cancels old queries, preventing their late results from being
      // installed after credentials change or the next account starts reading
      // these shared keys.
      client.clear();
    }
  });
}
