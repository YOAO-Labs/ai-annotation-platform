import type { PoolEvictRecord } from "@/api/adminMlIntegrations";
import type { MLModelCapability } from "@/api/ml-backends";
import { infraLabel, taskLabel, taskSuffix } from "./labels";
import type { CatalogGroupBy, FlatModel, ListRow } from "./types";

export function effectiveInfra(m: MLModelCapability, backendInfra?: string): string | undefined {
  return m.infra ?? backendInfra;
}

export function effectiveModalities(m: MLModelCapability, backendModalities?: string[]): string[] {
  if (m.modality) return [m.modality];
  return backendModalities ?? [];
}

export function groupModels(items: FlatModel[], groupBy: CatalogGroupBy) {
  if (groupBy === "none") return [{ key: "all", label: "全部模型", items }];
  const map = new Map<string, { key: string; label: string; items: FlatModel[] }>();
  for (const item of items) {
    let key = "unknown";
    let label = "未知";
    if (groupBy === "backend") {
      key = item.backendId;
      label = item.backendName;
    } else if (groupBy === "task") {
      key = item.model.task ?? "unknown";
      label = item.model.task ? taskLabel(item.model.task) : "未知任务";
    } else if (groupBy === "infra") {
      key = effectiveInfra(item.model, item.backendInfra) ?? "unknown";
      label = infraLabel(key);
    }
    if (!map.has(key)) map.set(key, { key, label, items: [] });
    map.get(key)!.items.push(item);
  }
  return [...map.values()].sort((a, b) => a.label.localeCompare(b.label));
}

export function toggle(set: Set<string>, value: string): Set<string> {
  const next = new Set(set);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  return next;
}

export function uniq<T>(arr: T[]): T[] {
  const seen = new Set<T>();
  const out: T[] = [];
  for (const x of arr)
    if (!seen.has(x)) {
      seen.add(x);
      out.push(x);
    }
  return out;
}

export function pickVariantOption<T extends { value: string; recommended?: boolean }>(
  options: T[],
  preferred: string | undefined,
): T | null {
  return (
    options.find((option) => option.value === preferred) ??
    options.find((option) => option.recommended) ??
    options[0] ??
    null
  );
}

export function legalAxisOptions(
  axis0Value: string,
  axis1: NonNullable<MLModelCapability["supported_variants"]>[number],
  combos: string[][],
) {
  if (combos.length === 0) return axis1.variants ?? [];
  const legal = combos
    .filter((combo) => combo[0] === axis0Value && combo.length >= 2)
    .map((combo) => combo[1]!);
  return (axis1.variants ?? []).filter((option) => legal.includes(option.value));
}

export function pickDefaultVariants(m: MLModelCapability): Record<string, string> {
  const out: Record<string, string> = { ...(m.default_variants ?? {}) };
  for (const group of m.supported_variants ?? []) {
    if (out[group.key]) continue;
    const picked = pickVariantOption(group.variants ?? [], undefined);
    if (picked?.value) out[group.key] = picked.value;
  }
  return out;
}

export function runtimeKeyFor(
  task: string | undefined,
  variants: Record<string, string>,
): string | undefined {
  if (task && variants.series && variants.size)
    return `${variants.series}/${variants.size}/${task}`;
  if (variants.sam_variant && variants.dino_variant) {
    return `sam=${variants.sam_variant}/dino=${variants.dino_variant}`;
  }
  if (variants.model_variant) return variants.model_variant;
  return undefined;
}

export function loadedKeySet(item: FlatModel): Set<string> {
  const keys = new Set<string>();
  for (const loaded of item.healthMeta?.pool?.loaded_keys ?? []) keys.add(loaded.key);
  for (const legacy of item.healthMeta?.pool?.loaded_variants ?? []) {
    if (legacy.sam_variant && legacy.dino_variant) {
      keys.add(`sam=${legacy.sam_variant}/dino=${legacy.dino_variant}`);
    }
  }
  return keys;
}

export function isLoadedRuntimeKey(
  item: FlatModel,
  variants: Record<string, string>,
  runtimeKey?: string,
): boolean {
  const keys = loadedKeySet(item);
  if (runtimeKey && keys.has(runtimeKey)) return true;
  for (const key of keys) {
    if (variants.sam_variant && key.includes(`sam=${variants.sam_variant}`)) return true;
    if (variants.dino_variant && key.includes(`dino=${variants.dino_variant}`)) return true;
    if (variants.model_variant && key === variants.model_variant) return true;
    if (variants.series && key.startsWith(`${variants.series}/`)) return true;
  }
  return false;
}

export function currentPoolSize(item: FlatModel): string {
  const pool = item.healthMeta?.pool;
  if (!pool) return "—";
  const size = pool.current_size ?? pool.loaded_keys?.length ?? pool.loaded_variants?.length ?? 0;
  return pool.cap != null ? `${size}/${pool.cap}` : String(size);
}

export function lastEvict(item: FlatModel): PoolEvictRecord | null {
  return item.healthMeta?.pool?.last_evict ?? item.healthMeta?.video_pool?.last_evict ?? null;
}

export function formatEvict(evict: PoolEvictRecord): string {
  const seconds = Math.max(0, (Date.now() - Date.parse(evict.at)) / 1000);
  const ago =
    seconds >= 3600
      ? `${Math.floor(seconds / 3600)} 小时前`
      : seconds >= 60
        ? `${Math.floor(seconds / 60)} 分钟前`
        : `${Math.floor(seconds)} 秒前`;
  return `${ago} 淘汰 ${evict.key}，原因 ${evict.reason}`;
}

export function buildModelRowsLegacy(item: FlatModel): ListRow[] {
  const m = item.model;
  const axes = (m.supported_variants ?? []).filter(
    (g) => Array.isArray(g.variants) && g.variants!.length > 0,
  );
  const baseName = m.display_name ?? m.id;
  const geom = m.supported_geometric_outputs ?? [];
  if (axes.length === 0) {
    return [
      {
        parent: item,
        rowKey: `${item.backendId}:${m.id}`,
        primaryLabel: baseName,
        primaryId: m.id,
        tasks: m.task ? [m.task] : [],
        geometries: geom,
        secondaryLabel: "—",
        warmVariants: pickDefaultVariants(m),
        runtimeKey: runtimeKeyFor(m.task, pickDefaultVariants(m)),
      },
    ];
  }
  const axis0 = axes[0]!;
  const axis1 = axes[1];
  const combos = m.variant_combinations ?? [];
  const suffix = taskSuffix(m.task);
  return (axis0.variants ?? []).map((v0) => {
    const v0Label = v0.label ?? v0.value;
    let secondaryLabel = "—";
    let secondaryTitle: string | undefined;
    if (axis1) {
      let legal: string[];
      if (combos.length > 0) {
        legal = combos.filter((c) => c[0] === v0.value && c.length >= 2).map((c) => c[1]!);
      } else {
        legal = (axis1.variants ?? []).map((v) => v.value);
      }
      const opts = (axis1.variants ?? []).filter((v) => legal.includes(v.value));
      if (opts.length > 0) {
        secondaryLabel = opts.map((v) => v.label ?? v.value).join(" / ");
        secondaryTitle = opts
          .map((v) => {
            const bits = [
              v.label ?? v.value,
              v.vram_gb != null ? `${v.vram_gb}GB` : null,
              v.tier ? tierLabel(v.tier) : null,
            ].filter(Boolean);
            return bits.join(" · ");
          })
          .join("\n");
      }
    } else {
      const bits = [
        v0.vram_gb != null ? `${v0.vram_gb}GB` : null,
        v0.tier ? tierLabel(v0.tier) : null,
      ].filter(Boolean);
      secondaryLabel = bits.length > 0 ? bits.join(" · ") : "—";
    }
    const pickedAxis1 = axis1
      ? pickVariantOption(
          legalAxisOptions(v0.value, axis1, combos),
          m.default_variants?.[axis1.key],
        )
      : null;
    const warmVariants: Record<string, string> = { [axis0.key]: v0.value };
    if (axis1 && pickedAxis1?.value) warmVariants[axis1.key] = pickedAxis1.value;
    return {
      parent: item,
      rowKey: `${item.backendId}:${m.id}:${v0.value}`,
      primaryLabel: suffix ? `${v0Label}-${suffix}` : v0Label,
      primaryId: `${m.id} / ${v0.value}`,
      tasks: m.task ? [m.task] : [],
      geometries: geom,
      secondaryLabel,
      secondaryTitle,
      warmVariants,
      runtimeKey: runtimeKeyFor(m.task, warmVariants),
    };
  });
}

export function buildSharedRows(items: FlatModel[]): ListRow[] {
  type Acc = {
    parent: FlatModel;
    label: string;
    vram_gb?: number;
    tier?: string;
    tasks: string[];
    geometries: string[];
    modelIds: string[];
  };
  const byAxis = new Map<string, { title: string; values: Map<string, Acc> }>();

  for (const item of items) {
    const m = item.model;
    const axes = (m.supported_variants ?? []).filter(
      (g) => Array.isArray(g.variants) && g.variants!.length > 0,
    );
    const geom = m.supported_geometric_outputs ?? [];
    for (const axis of axes) {
      const axisKey = axis.key;
      if (!byAxis.has(axisKey)) {
        byAxis.set(axisKey, { title: axis.title ?? axisKey, values: new Map() });
      }
      const bucket = byAxis.get(axisKey)!;
      for (const v of axis.variants ?? []) {
        const acc = bucket.values.get(v.value);
        if (acc) {
          if (m.task) acc.tasks.push(m.task);
          for (const g of geom) acc.geometries.push(g);
          acc.modelIds.push(m.id);
        } else {
          bucket.values.set(v.value, {
            parent: item,
            label: v.label ?? v.value,
            vram_gb: v.vram_gb,
            tier: v.tier,
            tasks: m.task ? [m.task] : [],
            geometries: [...geom],
            modelIds: [m.id],
          });
        }
      }
    }
  }

  const rows: ListRow[] = [];
  for (const [axisKey, bucket] of byAxis) {
    for (const [value, acc] of bucket.values) {
      const bits = [
        acc.vram_gb != null ? `${acc.vram_gb}GB` : null,
        acc.tier ? tierLabel(acc.tier) : null,
      ].filter(Boolean);
      rows.push({
        parent: acc.parent,
        rowKey: `${acc.parent.backendId}:${axisKey}:${value}`,
        primaryLabel: acc.label,
        primaryId: `${bucket.title} / ${value}`,
        tasks: uniq(acc.tasks),
        geometries: uniq(acc.geometries),
        secondaryLabel: bits.length > 0 ? bits.join(" · ") : "—",
        warmVariants: { [axisKey]: value },
        runtimeKey: runtimeKeyFor(acc.parent.model.task, { [axisKey]: value }),
      });
    }
  }
  return rows;
}

export function buildListRows(items: FlatModel[]): ListRow[] {
  const byBackend = new Map<string, FlatModel[]>();
  for (const it of items) {
    if (!byBackend.has(it.backendId)) byBackend.set(it.backendId, []);
    byBackend.get(it.backendId)!.push(it);
  }
  const out: ListRow[] = [];
  for (const [, group] of byBackend) {
    const shared = group.filter((it) => it.model.variants_shared_across_tasks);
    const legacy = group.filter((it) => !it.model.variants_shared_across_tasks);
    if (shared.length > 0) out.push(...buildSharedRows(shared));
    for (const it of legacy) out.push(...buildModelRowsLegacy(it));
  }
  return out;
}

export function tierLabel(tier: string) {
  if (tier === "fast") return "快速";
  if (tier === "balanced") return "均衡";
  if (tier === "accurate") return "精度";
  return tier;
}
