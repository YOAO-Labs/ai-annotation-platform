import { useMemo, useState } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { mlBackendsApi, type MLModelCapability } from "@/api/ml-backends";
import type { OutputAttributeSchemaItem } from "@/api/mlCapabilities";
import type { AttributeField, AttributeFieldType } from "@/api/projects";

const FIELD_TYPE_LABEL: Record<string, string> = {
  text: "文本",
  number: "数字",
  boolean: "开关",
  select: "下拉单选",
  multiselect: "下拉多选",
  range: "区间滑杆",
};
const ALLOWED_TYPES: AttributeFieldType[] = [
  "text",
  "number",
  "boolean",
  "select",
  "multiselect",
  "range",
];

export function itemToField(item: OutputAttributeSchemaItem): AttributeField {
  const type = (ALLOWED_TYPES as string[]).includes(item.type)
    ? (item.type as AttributeFieldType)
    : "text";
  const field: AttributeField = {
    key: item.key,
    label: item.label || item.key,
    type,
    required: false,
  };
  if (item.options?.length) {
    field.options = item.options.map((o) => ({ value: o.value, label: o.label }));
  }
  return field;
}

interface ModelEntry {
  backendName: string;
  model: MLModelCapability;
  classes: string[];
  schema: OutputAttributeSchemaItem[];
}

export interface PrefillPicked {
  classes: string[];
  attributes: AttributeField[];
}

interface Props {
  open: boolean;
  onClose: () => void;
  projectId: string;
  onPrefill: (picked: PrefillPicked) => void;
  targetUnitLabel: string;
  existingClassNames?: string[];
  existingAttrKeys?: string[];
}

export function PrefillFromBackendDialog({
  open,
  onClose,
  projectId,
  onPrefill,
  targetUnitLabel,
  existingClassNames = [],
  existingAttrKeys = [],
}: Props) {
  const backendsQ = useQuery({
    queryKey: ["prefill-project-backends", projectId],
    queryFn: () => mlBackendsApi.list(projectId),
    enabled: open,
    staleTime: 30_000,
  });
  const onlineBackends = useMemo(
    () => (backendsQ.data ?? []).filter((b) => b.state === "connected" || b.state === "predicting"),
    [backendsQ.data],
  );
  const capResults = useQueries({
    queries: onlineBackends.map((b) => ({
      queryKey: ["prefill-project-backend-caps", projectId, b.id],
      queryFn: () => mlBackendsApi.capabilities(projectId, b.id),
      enabled: open,
      staleTime: 60_000,
      retry: false,
    })),
  });

  const capsLoading = backendsQ.isLoading || capResults.some((r) => r.isLoading);
  const capsError = backendsQ.isError;
  const capSig = capResults.map((r) => r.dataUpdatedAt).join(",");
  const entries = useMemo<ModelEntry[]>(() => {
    const out: ModelEntry[] = [];
    onlineBackends.forEach((b, i) => {
      const caps = capResults[i]?.data;
      for (const model of caps?.models ?? []) {
        if (model.composition === "composite") continue;
        const classes = (model.classes ?? []).map((c) => c.name);
        const schema = model.output_attribute_schema ?? [];
        if (classes.length > 0 || schema.length > 0) {
          out.push({ backendName: b.name, model, classes, schema });
        }
      }
    });
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onlineBackends, capSig]);

  const existingClasses = useMemo(() => new Set(existingClassNames), [existingClassNames]);
  const existingKeys = useMemo(() => new Set(existingAttrKeys), [existingAttrKeys]);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [checkedClasses, setCheckedClasses] = useState<Set<string>>(new Set());
  const [checkedKeys, setCheckedKeys] = useState<Set<string>>(new Set());

  const selected = useMemo(
    () => entries.find((e) => `${e.backendName}::${e.model.id}` === selectedId) ?? null,
    [entries, selectedId],
  );

  const selectEntry = (entry: ModelEntry) => {
    setSelectedId(`${entry.backendName}::${entry.model.id}`);
    setCheckedClasses(new Set(entry.classes.filter((c) => !existingClasses.has(c))));
    setCheckedKeys(new Set(entry.schema.map((s) => s.key).filter((k) => !existingKeys.has(k))));
  };

  const toggle = (set: Set<string>, key: string): Set<string> => {
    const next = new Set(set);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    return next;
  };

  const handlePrefill = () => {
    if (!selected) return;
    const classes = selected.classes.filter((c) => checkedClasses.has(c));
    const attributes = selected.schema.filter((s) => checkedKeys.has(s.key)).map(itemToField);
    if (classes.length === 0 && attributes.length === 0) return;
    onPrefill({ classes, attributes });
    onClose();
  };

  const pickedCount = checkedClasses.size + checkedKeys.size;

  return (
    <Modal open={open} onClose={onClose} title="从模型服务预填配置" width={640}>
      <div className="flex flex-col gap-3">
        <p className="m-0 text-xs leading-normal text-muted-foreground">
          模型服务会报告支持的类别（如 YOLO 的 COCO 类）与属性字段（如车型 / 颜色，含下拉选项）。
          选择一个模型，勾选要预填的类别 / 属性，即可合并进「{targetUnitLabel}
          」工具单位 （类别同名跳过、属性同 key 覆盖）。
        </p>

        {capsLoading && (
          <div className="rounded-md border border-dashed border-border p-6 text-center text-xs text-muted-foreground">
            正在加载本项目模型服务能力…
          </div>
        )}
        {capsError && (
          <div className="rounded-md border border-dashed border-border p-6 text-center text-xs text-status-danger">
            加载模型服务能力失败，请稍后重试。
          </div>
        )}
        {!capsLoading && !capsError && entries.length === 0 && (
          <div className="rounded-md border border-dashed border-border p-6 text-center text-xs text-muted-foreground">
            已接入的模型服务均未声明类别或输出属性。需要接入提供类别（如 YOLO 检测）或属性（如
            ONNXTools 车辆属性）的模型服务后才能预填。
          </div>
        )}

        {entries.length > 0 && (
          <div className="flex flex-col gap-2">
            <span className="text-xs font-semibold text-muted-foreground">选择模型</span>
            <div className="flex flex-col gap-1.5">
              {entries.map((entry) => {
                const id = `${entry.backendName}::${entry.model.id}`;
                const active = id === selectedId;
                const parts: string[] = [];
                if (entry.classes.length) parts.push(`${entry.classes.length} 个类别`);
                if (entry.schema.length) parts.push(`${entry.schema.length} 个属性`);
                return (
                  <button
                    key={id}
                    type="button"
                    onClick={() => selectEntry(entry)}
                    className={`flex items-center justify-between gap-2 rounded-md border px-3 py-2 text-left transition-colors ${
                      active
                        ? "border-primary bg-primary/10"
                        : "border-border bg-card hover:bg-muted"
                    }`}
                  >
                    <span className="min-w-0">
                      <span className="block truncate text-sm font-medium text-foreground">
                        {entry.model.display_name || entry.model.id}
                      </span>
                      <span className="block truncate text-xs text-muted-foreground">
                        {entry.backendName} · {parts.join(" · ")}
                      </span>
                    </span>
                    {active && <Icon name="check" size={14} className="text-brand" />}
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {selected && selected.classes.length > 0 && (
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-muted-foreground">
                类别（勾选要预填的）
              </span>
              <div className="flex gap-2">
                <button
                  type="button"
                  className="text-2xs text-brand hover:underline"
                  onClick={() => setCheckedClasses(new Set(selected.classes))}
                >
                  全选
                </button>
                <button
                  type="button"
                  className="text-2xs text-muted-foreground hover:underline"
                  onClick={() => setCheckedClasses(new Set())}
                >
                  清空
                </button>
              </div>
            </div>
            <div className="flex max-h-44 flex-wrap content-start gap-1.5 overflow-y-auto rounded-md border border-border bg-card p-2">
              {selected.classes.map((name) => {
                const checked = checkedClasses.has(name);
                const exists = existingClasses.has(name);
                return (
                  <label
                    key={name}
                    className={`inline-flex cursor-pointer items-center gap-1.5 rounded-sm border px-2 py-1 text-xs ${
                      checked ? "border-primary bg-primary/10" : "border-border bg-muted"
                    }`}
                    title={exists ? "该工具单位已有同名类别，预填时会跳过" : undefined}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => setCheckedClasses((p) => toggle(p, name))}
                    />
                    <span className="text-foreground">{name}</span>
                    {exists && <span className="text-2xs text-muted-foreground">已存在</span>}
                  </label>
                );
              })}
            </div>
          </div>
        )}

        {selected && selected.schema.length > 0 && (
          <div className="flex flex-col gap-2">
            <span className="text-xs font-semibold text-muted-foreground">
              属性（勾选要预填的）
            </span>
            <div className="flex flex-col gap-1.5">
              {selected.schema.map((item) => {
                const checked = checkedKeys.has(item.key);
                const exists = existingKeys.has(item.key);
                return (
                  <label
                    key={item.key}
                    className="flex cursor-pointer items-start gap-2.5 rounded-md border border-border bg-card p-2.5"
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => setCheckedKeys((p) => toggle(p, item.key))}
                      className="mt-0.5"
                    />
                    <span className="min-w-0 flex-1">
                      <span className="flex flex-wrap items-center gap-1.5">
                        <span className="text-sm font-medium text-foreground">
                          {item.label || item.key}
                        </span>
                        <code className="rounded-sm bg-muted px-1 py-0.5 text-2xs text-muted-foreground">
                          {item.key}
                        </code>
                        <span className="text-2xs text-muted-foreground">
                          {FIELD_TYPE_LABEL[item.type] ?? item.type}
                        </span>
                        {exists && (
                          <span className="text-2xs text-status-caution">已存在 · 将覆盖</span>
                        )}
                      </span>
                      {item.options?.length ? (
                        <span className="mt-1 block text-xs text-muted-foreground">
                          {item.options.length} 个选项：
                          {item.options
                            .slice(0, 8)
                            .map((o) => o.label)
                            .join(" / ")}
                          {item.options.length > 8 ? " …" : ""}
                        </span>
                      ) : null}
                    </span>
                  </label>
                );
              })}
            </div>
          </div>
        )}

        <div className="flex items-center justify-end gap-2 border-t border-border pt-3">
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
          <Button
            variant="primary"
            disabled={!selected || pickedCount === 0}
            onClick={handlePrefill}
          >
            预填选中项
          </Button>
        </div>
      </div>
    </Modal>
  );
}
