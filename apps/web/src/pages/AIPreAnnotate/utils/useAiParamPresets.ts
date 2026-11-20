/**
 * AI 预标推理参数的命名预设 (variant + params 快照).
 *
 * 现有 useAiToolParamPrefs 只记"上次用的一份"值 (按 backend 分桶, 存后端 User.preferences,
 * 其 schema 为 extra:forbid 无法挂新字段). 本 hook 在其上提供"具名多套"能力: 用户把当前
 * (variant + params) 存成命名预设、一键套用、删除。不修改后端 schema，
 * 预设存 localStorage, 按 (backendId, taskType) 分桶隔离 — 切到别的 backend/task 不串台.
 *
 * 已知限制: localStorage 不跨设备/浏览器同步 (换设备预设丢失). 后续如需共享再评估 DB 化.
 */
import { useCallback, useEffect, useState } from "react";

export interface AiParamPreset {
  id: string;
  name: string;
  /** (variant + params) 合并快照 — 即面板 paramsValue 的整体值. */
  values: Record<string, unknown>;
  createdAt: number;
}

function storageKey(backendId: string, taskType: string): string {
  return `ai-param-presets:${backendId}:${taskType}`;
}

function readPresets(key: string): AiParamPreset[] {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as AiParamPreset[]) : [];
  } catch {
    return [];
  }
}

function genId(): string {
  // 轻量唯一 id (非密码学用途): 时间戳 + 随机后缀.
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

export function useAiParamPresets(backendId: string | null, taskType: string) {
  const key = backendId ? storageKey(backendId, taskType) : null;
  const [presets, setPresets] = useState<AiParamPreset[]>([]);

  useEffect(() => {
    setPresets(key ? readPresets(key) : []);
  }, [key]);

  const persist = useCallback(
    (next: AiParamPreset[]) => {
      setPresets(next);
      if (key) {
        try {
          localStorage.setItem(key, JSON.stringify(next));
        } catch {
          /* 配额满 / 隐私模式: 静默, 不阻塞预标 */
        }
      }
    },
    [key],
  );

  /** 存当前值为命名预设; 同名则覆盖. 返回新建/更新的预设. */
  const save = useCallback(
    (name: string, values: Record<string, unknown>): AiParamPreset | null => {
      const trimmed = name.trim();
      if (!key || !trimmed) return null;
      const snapshot = { ...values };
      const existing = presets.find((p) => p.name === trimmed);
      const preset: AiParamPreset = existing
        ? { ...existing, values: snapshot, createdAt: Date.now() }
        : { id: genId(), name: trimmed, values: snapshot, createdAt: Date.now() };
      const next = existing
        ? presets.map((p) => (p.id === existing.id ? preset : p))
        : [...presets, preset];
      persist(next);
      return preset;
    },
    [key, presets, persist],
  );

  const remove = useCallback(
    (id: string) => {
      persist(presets.filter((p) => p.id !== id));
    },
    [presets, persist],
  );

  return { presets, save, remove };
}
