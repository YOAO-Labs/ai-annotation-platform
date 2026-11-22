import { useSyncExternalStore } from "react";
import { originalOwnerKey } from "@/api/originals";
import { useAuthStore } from "@/stores/authStore";

export function useOriginalAssetOwner(): string {
  return useSyncExternalStore(useAuthStore.subscribe, originalOwnerKey, originalOwnerKey);
}
