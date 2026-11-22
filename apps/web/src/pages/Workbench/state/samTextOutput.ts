import type { TextOutputMode } from "./useInteractiveAI";

export const SAM_OUTPUT_STORAGE_PREFIX = "wb:sam:textOutput:";

export function samOutputStorageKey(projectId: string): string {
  return `${SAM_OUTPUT_STORAGE_PREFIX}${projectId}`;
}

export function samOutputUserStorageKey(userId: string, projectId: string): string {
  return `workbench.${userId}.sam.outputMode:${projectId}`;
}

export function defaultOutputMode(typeKey: string | undefined | null): TextOutputMode {
  if (typeKey === "image-det") return "box";
  return "mask";
}

const VALID: ReadonlySet<TextOutputMode> = new Set(["box", "mask", "both"]);

function readMode(storage: Storage, key: string): TextOutputMode | null {
  const raw = storage.getItem(key);
  return raw && VALID.has(raw as TextOutputMode) ? (raw as TextOutputMode) : null;
}

export function readStoredOutputMode(
  projectId: string,
  userId?: string | null,
): TextOutputMode | null {
  if (typeof window === "undefined") return null;
  try {
    const sessionMode = readMode(window.sessionStorage, samOutputStorageKey(projectId));
    if (sessionMode) return sessionMode;
  } catch {
    // sessionStorage 不可用 (隐私模式 / SSR) 静默回退
  }
  if (userId) {
    try {
      return readMode(window.localStorage, samOutputUserStorageKey(userId, projectId));
    } catch {
      // localStorage 同样 best-effort
    }
  }
  return null;
}

export function writeStoredOutputMode(
  projectId: string,
  mode: TextOutputMode,
  userId?: string | null,
): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(samOutputStorageKey(projectId), mode);
  } catch {
    // 同上
  }
  if (userId) {
    try {
      window.localStorage.setItem(samOutputUserStorageKey(userId, projectId), mode);
    } catch {
      // localStorage 记忆失败不影响本会话
    }
  }
}

export function resolveInitialOutputMode(
  projectId: string | undefined,
  typeKey: string | undefined | null,
  userId?: string | null,
): TextOutputMode {
  if (projectId) {
    const stored = readStoredOutputMode(projectId, userId);
    if (stored) return stored;
  }
  return defaultOutputMode(typeKey);
}
