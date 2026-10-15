import { useAuthStore } from "./authStore";

let revision = 0;
const listeners = new Set<() => void>();
useAuthStore.subscribe((current, previous) => {
  if (current.user?.id === previous.user?.id && current.token === previous.token) return;
  revision += 1;
  listeners.forEach((listener) => listener());
});

export const getAuthSessionRevision = () => revision;
export function subscribeAuthSession(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
