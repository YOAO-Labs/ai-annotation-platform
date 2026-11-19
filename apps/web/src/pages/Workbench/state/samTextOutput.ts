/**
 * SAM text 输出模式工具.
 *
 * 智能默认按项目 type_key:
 *   image-det → "box"  (DINO 直出, 跳过 SAM mask, 标注员要的就是 bbox)
 *     其它      → "mask" (适用于 image-seg / mm / video / lidar)
 *
 * "both" 不作智能默认, 仅作用户 opt-in (Tab 切活跃几何, 复杂度高).
 *
 * 用户切换写 sessionStorage `wb:sam:textOutput:{projectId}` 跨切题保留;
 * 同时写用户级 localStorage `workbench.{userId}.sam.outputMode:{projectId}` 跨会话记忆。
 */

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

/**
 * 计算初始 outputMode: sessionStorage (本会话显式选择) > 用户级 localStorage 记忆 >
 * type_key 智能默认。(项目级 text_output_default 已退役: 用户在交互工具栏改一次即被
 * localStorage 偏好永久覆盖, 批量预标按模型能力派生不读它。)
 */
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
