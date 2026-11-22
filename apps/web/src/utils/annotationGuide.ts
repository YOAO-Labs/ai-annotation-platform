export function annotationGuideVersion(content: string | null | undefined): string {
  const value = content ?? "";
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `guide-v1-${(hash >>> 0).toString(16).padStart(8, "0")}`;
}

function guideStorageScope(userId: string | null | undefined, projectId: string, version: string) {
  return `${userId || "anonymous"}:${projectId}:${version}`;
}

function safeLocalStorage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function guideSeenStorageKey(
  userId: string | null | undefined,
  projectId: string,
  version: string,
) {
  return `wb:guide-read:${guideStorageScope(userId, projectId, version)}`;
}

export function guideCollapsedStorageKey(
  userId: string | null | undefined,
  projectId: string,
  version: string,
) {
  return `wb:guide-collapsed:${guideStorageScope(userId, projectId, version)}`;
}

export function isGuideSeen(
  userId: string | null | undefined,
  projectId: string,
  version: string,
  storage: Pick<Storage, "getItem"> | null = safeLocalStorage(),
) {
  if (!storage) return false;
  try {
    return storage.getItem(guideSeenStorageKey(userId, projectId, version)) !== null;
  } catch {
    return false;
  }
}

export function markGuideSeen(
  userId: string | null | undefined,
  projectId: string,
  version: string,
  storage: Pick<Storage, "setItem"> | null = safeLocalStorage(),
) {
  if (!storage) return;
  try {
    storage.setItem(guideSeenStorageKey(userId, projectId, version), String(Date.now()));
  } catch {
    // Private browsing and storage quota failures must not block the guide.
  }
}

export function isGuideCollapsed(
  userId: string | null | undefined,
  projectId: string,
  version: string,
  storage: Pick<Storage, "getItem"> | null = safeLocalStorage(),
) {
  if (!storage) return false;
  try {
    return storage.getItem(guideCollapsedStorageKey(userId, projectId, version)) === "1";
  } catch {
    return false;
  }
}

export function markGuideCollapsed(
  userId: string | null | undefined,
  projectId: string,
  version: string,
  collapsed: boolean,
  storage: Pick<Storage, "setItem"> | null = safeLocalStorage(),
) {
  if (!storage) return;
  try {
    storage.setItem(guideCollapsedStorageKey(userId, projectId, version), collapsed ? "1" : "0");
  } catch {
    // Private browsing and storage quota failures must not block the guide.
  }
}
