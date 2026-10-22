import { useRef, useState } from "react";
import { Search } from "lucide-react";
import type { TrainingParameterSchema } from "@/api/generated/types.gen";
import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/utils";
import { TrainingParameterField, trainingInputClass } from "./TrainingParameterField";
import {
  commonParameterKeys,
  valueToDraft,
  type ParameterDraft,
  type ParameterDrafts,
} from "./trainingConfiguration";

export function TrainingConfigurationEditor({
  schema,
  fields,
  disabled,
  onChange,
  onReset,
}: {
  schema: TrainingParameterSchema;
  fields: ParameterDrafts;
  disabled: boolean;
  onChange: (key: string, value: ParameterDraft) => void;
  onReset: (keys: string[]) => void;
}) {
  const [search, setSearch] = useState("");
  const [all, setAll] = useState(false);
  const [modifiedOnly, setModifiedOnly] = useState(false);
  const [activeGroup, setActiveGroup] = useState("basic");
  const groups = useRef(new Map<string, HTMLDetailsElement>());
  const needle = search.trim().toLocaleLowerCase();
  const editable = schema.fields.filter((field) => field.status === "editable");
  const matching = editable.filter(
    (field) =>
      (!modifiedOnly || field.key in fields) &&
      (all || modifiedOnly || !!needle || commonParameterKeys.has(field.key)) &&
      (!needle ||
        `${field.label} ${field.key} ${field.aliases.join(" ")}`
          .toLocaleLowerCase()
          .includes(needle)),
  );
  const visibleGroups = Object.entries(schema.groups).filter(([key]) =>
    (modifiedOnly ? editable : matching).some((field) => field.group === key),
  );
  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <label className="relative min-w-40 flex-1">
          <span className="sr-only">搜索训练参数</span>
          <Search className="pointer-events-none absolute left-3 top-2.5 size-4 text-muted-foreground" />
          <input
            aria-label="搜索训练参数"
            className={`${trainingInputClass} pl-9`}
            placeholder="中文名称、原生键或别名"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        <div className="flex w-full min-w-0 items-center gap-3 sm:w-auto">
          <select
            aria-label="参数范围"
            className={cn(trainingInputClass, "w-auto flex-1 sm:flex-none")}
            value={all ? "all" : "common"}
            onChange={(event) => setAll(event.target.value === "all")}
          >
            <option value="common">常用参数</option>
            <option value="all">全部可编辑参数（{editable.length}）</option>
          </select>
          <label className="flex shrink-0 items-center gap-2 whitespace-nowrap text-xs">
            <input
              type="checkbox"
              className="size-4 accent-primary"
              checked={modifiedOnly}
              onChange={(event) => setModifiedOnly(event.target.checked)}
            />
            仅看已修改
          </label>
        </div>
      </div>
      <p className="mb-4 text-xs leading-relaxed text-muted-foreground">
        未修改的参数使用受控默认值。预热、优化器与增强依赖将在检查时核验，最终生效值会在开始前逐项列出。
      </p>
      <div className="grid min-w-0 gap-5 lg:grid-cols-[160px_minmax(0,1fr)]">
        <nav aria-label="训练参数分组" className="hidden self-start lg:sticky lg:top-20 lg:block">
          {visibleGroups.map(([key, label]) => (
            <button
              type="button"
              key={key}
              aria-current={activeGroup === key ? "true" : undefined}
              className={`mb-1 flex w-full items-center justify-between rounded-md px-3 py-2 text-left text-xs focus-visible:ring-2 focus-visible:ring-ring ${activeGroup === key ? "bg-muted font-medium text-foreground" : "text-muted-foreground hover:bg-muted"}`}
              onClick={() => {
                setActiveGroup(key);
                const node = groups.current.get(key);
                if (node) {
                  node.open = true;
                  requestAnimationFrame(() => {
                    if (node.isConnected) node.scrollIntoView({ block: "start" });
                  });
                }
              }}
            >
              {label}
              <span className="tabular-nums text-muted-foreground">
                {matching.filter((field) => field.group === key).length}
              </span>
            </button>
          ))}
        </nav>
        <div className="min-w-0 space-y-3">
          <p role="status" className="text-xs text-muted-foreground">
            显示 {matching.length} / {editable.length} 个可编辑参数 · {Object.keys(fields).length}{" "}
            项已修改
          </p>
          {!visibleGroups.length && (
            <p className="rounded-md bg-muted p-5 text-sm text-muted-foreground">
              没有匹配的参数。可切换参数范围或清除筛选。
            </p>
          )}
          {visibleGroups.map(([key, label]) => (
            <div key={key} className="relative">
              <details
                open={
                  matching.some((field) => field.group === key) &&
                  (activeGroup === key || !!needle || modifiedOnly)
                }
                onToggle={(event) => {
                  if (event.currentTarget.open && !needle && !modifiedOnly) setActiveGroup(key);
                }}
                ref={(node) => {
                  if (node) groups.current.set(key, node);
                  else groups.current.delete(key);
                }}
                className="scroll-mt-20 rounded-lg border border-border bg-card"
              >
                <summary className="cursor-pointer py-3 pl-4 pr-24 text-xs font-medium">
                  {label}
                  <span className="ml-2 text-xs font-normal text-muted-foreground">
                    {matching.filter((field) => field.group === key).length} 项
                  </span>
                </summary>
                <div className="border-t border-border px-4 pb-2">
                  {modifiedOnly && !matching.some((field) => field.group === key) && (
                    <p className="py-3 text-xs text-muted-foreground">本组没有已修改的参数。</p>
                  )}
                  {matching
                    .filter((field) => field.group === key)
                    .map((field) => (
                      <TrainingParameterField
                        key={field.key}
                        field={field}
                        value={fields[field.key] ?? valueToDraft(field.default)}
                        changed={field.key in fields}
                        disabled={disabled}
                        onChange={(value) => onChange(field.key, value)}
                        onReset={() => onReset([field.key])}
                      />
                    ))}
                </div>
              </details>
              <Button
                type="button"
                size="xs"
                variant="ghost"
                className="absolute right-2 top-2 disabled:pointer-events-auto"
                disabled={
                  disabled || !editable.some((field) => field.group === key && field.key in fields)
                }
                onClick={() =>
                  onReset(editable.filter((field) => field.group === key).map((field) => field.key))
                }
              >
                重置本组
              </Button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
