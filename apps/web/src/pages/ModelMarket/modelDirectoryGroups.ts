import type { CatalogEntry } from "@/api/modelCatalog";

export type ModelDirectoryGroup = {
  id: string;
  name: string;
  series: string;
  entries: CatalogEntry[];
};

const sizeOrder = [
  "n",
  "t",
  "tiny",
  "s",
  "small",
  "m",
  "medium",
  "b",
  "base",
  "base_plus",
  "c",
  "l",
  "large",
  "x",
  "e",
];
const collator = new Intl.Collator("en", { numeric: true });

export function variantLabel(entry: CatalogEntry): string {
  const variants = Object.entries(entry.model_variants ?? {});
  const dimensions = variants.filter(([key]) => key !== "series" && key !== "version");
  return (
    (dimensions.length ? dimensions : variants).map(([, value]) => value).join(" / ") || "默认"
  );
}

/** Display groups preserve the concrete selection and the service-pool boundary. */
export function groupModelDirectory(entries: CatalogEntry[]): ModelDirectoryGroup[] {
  const groups = new Map<string, ModelDirectoryGroup>();
  for (const entry of entries) {
    const series = entry.model_variants?.series ?? entry.model_variants?.version ?? "";
    const scopes = [
      ...new Set((entry.placements ?? []).map((p) => p.pool_id ?? p.registry_id)),
    ].sort();
    const id =
      entry.source === "project_trained" || !scopes.length
        ? entry.id
        : JSON.stringify([scopes, entry.model_id, entry.task, series]);
    const group = groups.get(id) ?? { id, name: entry.name, series, entries: [] };
    group.entries.push(entry);
    groups.set(id, group);
  }
  return [...groups.values()]
    .map((group) => ({
      ...group,
      entries: group.entries.sort((a, b) => {
        const ai = sizeOrder.indexOf(a.model_variants?.size ?? a.model_variants?.sam_variant ?? "");
        const bi = sizeOrder.indexOf(b.model_variants?.size ?? b.model_variants?.sam_variant ?? "");
        return (
          (ai < 0 ? sizeOrder.length : ai) - (bi < 0 ? sizeOrder.length : bi) ||
          collator.compare(variantLabel(a), variantLabel(b))
        );
      }),
    }))
    .sort(
      (a, b) =>
        collator.compare(a.name, b.name) ||
        collator.compare(a.series, b.series) ||
        collator.compare(a.id, b.id),
    );
}
