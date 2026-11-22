import { useCallback, useEffect, useState } from "react";

export interface AiParamPreset {
  id: string;
  name: string;
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
