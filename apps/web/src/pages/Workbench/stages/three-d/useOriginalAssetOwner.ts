import { useSyncExternalStore } from "react";
import { originalOwnerKey } from "@/api/originals";
import { useAuthStore } from "@/stores/authStore";

/** Token refresh keeps the same cache namespace; a new login does not. */
export function useOriginalAssetOwner(): string {
  return useSyncExternalStore(useAuthStore.subscribe, originalOwnerKey, originalOwnerKey);
}
