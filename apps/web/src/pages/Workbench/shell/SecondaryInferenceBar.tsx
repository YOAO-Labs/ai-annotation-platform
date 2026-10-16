// Secondary inference keeps prompt, configuration and request ownership outside the toolbar.
// The compact primary area runs the selected capability; full settings disclose inline parameters.
import { useEffect, useMemo, useRef, useState } from "react";
import { X } from "lucide-react";

import { ContextToolbar } from "./ContextToolbar";
import { Button } from "@/components/ui/Button";
import { Button as IconButton } from "@/components/shadcn/ui/button";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import { VariantSelector } from "@/components/ml/VariantSelector";
import type { AttributeField } from "@/api/projects";
import type { AnnotationResponse } from "@/types";
import { displayClassName } from "../stage/colors";
import {
  SchemaForm,
  deriveDefaults,
  type JsonSchemaField,
  type JsonSchemaObject,
} from "../components/SchemaForm";
import { TOOLBAR_FIELD_LABEL_CLASS as FIELD_LABEL_CLASS } from "./workbenchToolbarChrome";
import {
  buildSecondaryInferencePayload,
  hasConfigurableParams,
  missingAttributeFields,
  needsTextPrompt,
  useRunSecondaryInference,
  useSecondaryCapabilities,
  type SecondaryCapability,
} from "../state/useSecondaryInference";
import { useSecondaryParamPrefs } from "../state/useSecondaryParamPrefs";

interface Props {
  projectId: string | undefined;
  taskId: string;
  annotation: AnnotationResponse;
  readOnly?: boolean;
  /** v0.20.12 · 项目已有属性键: 判定 attributes-型能力的输出键是否有承接位 (无则产物看不见)。 */
  existingAttributeKeys?: Set<string>;
  /** v0.20.12 · 一次把缺失属性字段补进项目 (复用工作台属性补全)。 */
  presentationHidden?: boolean;
  onEnsureAttributeFields?: (fields: AttributeField[]) => void;
}

// Match the interactive toolbar's field geometry and semantic theme tokens.
const SELECT_CLASS =
  "h-8 min-w-0 w-full rounded-lg border border-border bg-muted/40 px-2 text-xs text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring";
// Older models may omit the platform role; only recognized confidence keys fall back.
const CONFIDENCE_KEYS = new Set([
  "confidence",
  "conf",
  "conf_threshold",
  "score_threshold",
  "box_threshold",
]);

const TASK_LABELS: Record<string, string> = {
  detect: "检测",
  obb: "旋转框检测",
  segment: "分割",
  instance_segmentation: "分割",
  pose: "关键点",
  classify: "分类",
  cls: "分类",
  ocr: "文字识别",
  doc_layout: "版面分析",
};
const taskLabel = (t: string | null | undefined) => (t && TASK_LABELS[t.toLowerCase()]) || "其他";

const capKey = (c: SecondaryCapability) => `${c.backendId}:${c.model.id}`;

const TARGET_HINT: Record<SecondaryCapability["writeTarget"], string> = {
  attributes: "属性写回原框",
  geometry: "检出建子框",
};

/** 按 task 把能力分桶, 保持原始顺序; 返回 [task, caps[]] 供 <optgroup> 渲染。 */
function groupByTask(caps: SecondaryCapability[]): Array<[string, SecondaryCapability[]]> {
  const order: string[] = [];
  const buckets = new Map<string, SecondaryCapability[]>();
  for (const c of caps) {
    const t = c.model.task ?? "";
    if (!buckets.has(t)) {
      buckets.set(t, []);
      order.push(t);
    }
    buckets.get(t)!.push(c);
  }
  return order.map((t) => [t, buckets.get(t)!]);
}

export function SecondaryInferenceBar({
  projectId,
  taskId,
  annotation,
  readOnly,
  existingAttributeKeys,
  onEnsureAttributeFields,
  presentationHidden = false,
}: Props) {
  const pushToast = useToastStore((s) => s.push);
  const { capabilities } = useSecondaryCapabilities(projectId);
  const run = useRunSecondaryInference();
  const ownerKey = JSON.stringify([projectId, taskId, annotation.id]);
  const owner = useMemo(() => ({ key: ownerKey }), [ownerKey]);
  const latestOwner = useRef<typeof owner | null>(owner);
  latestOwner.current = owner;
  useEffect(() => {
    latestOwner.current = owner;
    return () => {
      latestOwner.current = null;
    };
  }, [owner]);
  const pendingRef = useRef(new Set<string>());
  const [pending, setPending] = useState<ReadonlySet<string>>(() => new Set());
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  // v0.20.13 · 每能力的推理参数 (阈值等); paramsOpen = 当前是否展开选中能力的参数面板。
  const [paramsByKey, setParamsByKey] = useState<Record<string, Record<string, unknown>>>({});
  // v0.20.17 · 每能力已选的模型变体档位 (series/size 等); 与 default_variants 合并成实际下发档位。
  const [variantByKey, setVariantByKey] = useState<Record<string, Record<string, unknown>>>({});
  // v0.20.18 · 开集文本模型的查询文本 (按能力); 每次运行的临时查询, 不持久化。
  const [promptByKey, setPromptByKey] = useState<Record<string, string>>({});
  const [paramsOpen, setParamsOpen] = useState(false);
  // v0.20.17 · 用户级偏好 (按 backendId:modelId): 参数 + 变体跨框/跨设备记住。
  const { byModel, loaded: prefsLoaded, save: savePref } = useSecondaryParamPrefs();

  // 偏好载入后, 把存过的 params/variants 灌进组件 state (仅未触碰的键, 不覆盖当前编辑)。
  useEffect(() => {
    if (!prefsLoaded) return;
    setParamsByKey((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const [k, entry] of Object.entries(byModel)) {
        if (!(k in next) && entry.params) {
          next[k] = entry.params;
          changed = true;
        }
      }
      return changed ? next : prev;
    });
    setVariantByKey((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const [k, entry] of Object.entries(byModel)) {
        if (!(k in next) && entry.variants) {
          next[k] = entry.variants;
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [prefsLoaded, byModel]);

  if (readOnly || capabilities.length === 0) return null;

  const existing = existingAttributeKeys ?? new Set<string>();
  // 选中能力: 用户选过的; 未选/失效则回落首个。
  const selected = capabilities.find((c) => capKey(c) === selectedKey) ?? capabilities[0];
  const selKey = capKey(selected);
  const busy = pending.has(ownerKey);
  const missing = missingAttributeFields(selected, existing);
  const canParams = hasConfigurableParams(selected.model);
  // v0.20.18 · 开集文本模型: 需用户输入检测/分割目标文本; 空文本禁运行 (跑了也检不出)。
  const wantsText = needsTextPrompt(selected);
  const promptMissing = wantsText && !(promptByKey[selKey] ?? "").trim();

  const openParamsFor = (cap: SecondaryCapability) => {
    setParamsOpen((v) => !v);
    setParamsByKey((prev) =>
      prev[capKey(cap)]
        ? prev
        : {
            ...prev,
            [capKey(cap)]: deriveDefaults(cap.model.params as JsonSchemaObject),
          },
    );
  };

  const onRun = async (cap: SecondaryCapability) => {
    const key = capKey(cap);
    if (readOnly || promptMissing || pendingRef.current.has(ownerKey)) return;
    pendingRef.current.add(ownerKey);
    setPending(new Set(pendingRef.current));
    try {
      const resp = await run.mutateAsync({
        taskId,
        annotationId: annotation.id,
        body: buildSecondaryInferencePayload(
          cap,
          paramsByKey[key],
          variantByKey[key],
          promptByKey[key],
        ),
      });
      if (latestOwner.current !== owner) return;
      const childCount = resp.created_children.length;
      const attrKeys = Object.keys(resp.annotation.attributes_meta ?? {});
      // 写了但项目缺承接字段 → 属性面板看不见, 提示去补全 (避免"跑了没反应")。
      const invisible = attrKeys.filter((k) => !existing.has(k));
      const sub =
        cap.writeTarget === "geometry"
          ? childCount > 0
            ? `新增 ${childCount} 个子框`
            : "未检出子物"
          : attrKeys.length > 0
            ? invisible.length > 0
              ? `写回 ${attrKeys.length} 项, 其中 ${invisible.length} 项项目缺字段 (面板不显示, 请补全)`
              : `写回 ${attrKeys.length} 项属性`
            : "无属性产出";
      const produced = cap.writeTarget === "geometry" ? childCount > 0 : attrKeys.length > 0;
      pushToast({
        msg: `${cap.label} 已完成`,
        sub,
        kind:
          cap.writeTarget === "attributes" && invisible.length > 0
            ? "warning"
            : produced
              ? "success"
              : "",
      });
    } catch (err) {
      if (latestOwner.current !== owner) return;
      pushToast({
        msg: `${cap.label} 二次推理失败`,
        sub: String((err as Error)?.message ?? err),
        kind: "error",
      });
    } finally {
      pendingRef.current.delete(ownerKey);
      if (latestOwner.current) setPending(new Set(pendingRef.current));
    }
  };

  const confidenceProperties = Object.fromEntries(
    Object.entries(
      (selected.model.params as JsonSchemaObject | undefined)?.properties ?? {},
    ).filter(([key, raw]) => {
      if (!raw || typeof raw !== "object") return false;
      const field = raw as JsonSchemaField;
      return (
        !field.readOnly &&
        (field.type === "number" || field.type === "integer") &&
        (field["x-platform-role"] === "confidence" ||
          (!field["x-platform-role"] && CONFIDENCE_KEYS.has(key)))
      );
    }),
  );
  const updateParams = (next: Record<string, unknown>) => {
    setParamsByKey((prev) => ({ ...prev, [selKey]: next }));
    savePref(selKey, { params: next });
  };
  // Both surfaces edit the existing per-model owner; preserve other defaults and edited fields.
  const paramValues = {
    ...deriveDefaults(selected.model.params as JsonSchemaObject),
    ...paramsByKey[selKey],
  };
  const renderPrimary = (compact: boolean) => (
    <div className="flex w-full min-w-0 flex-col gap-3">
      {wantsText && (
        <label className="flex min-w-0 flex-col gap-1.5">
          <span className={FIELD_LABEL_CLASS}>目标文本</span>
          <input
            type="text"
            value={promptByKey[selKey] ?? ""}
            disabled={busy}
            onChange={(e) => setPromptByKey((prev) => ({ ...prev, [selKey]: e.target.value }))}
            placeholder="如 car . person"
            title="检测或分割目标，多个用 . 分隔"
            aria-label="二次推理目标文本"
            data-testid="secondary-prompt"
            className={SELECT_CLASS}
          />
        </label>
      )}
      {compact && Object.keys(confidenceProperties).length > 0 && (
        <div data-testid="secondary-quick-confidence" className="min-w-0 [&_input]:accent-brand">
          <SchemaForm
            schema={{ type: "object", properties: confidenceProperties }}
            value={paramValues}
            onChange={updateParams}
            disabled={busy}
          />
        </div>
      )}
      <Button
        size={compact ? "xs" : "sm"}
        variant="ai"
        className="w-full"
        disabled={busy || promptMissing}
        onClick={() => onRun(selected)}
        title={
          promptMissing
            ? "请先输入检测/分割目标文本"
            : `${selected.backendName} · ${TARGET_HINT[selected.writeTarget]}`
        }
        data-testid="secondary-run"
      >
        <Icon name={busy ? "loader2" : "sparkles"} size={13} />
        {busy ? "运行中…" : "运行"}
      </Button>
    </div>
  );
  if (presentationHidden) return null;
  return (
    <ContextToolbar
      key={ownerKey}
      id="secondary"
      label="二次推理"
      summaryLabel="二次推理常用工具"
      summaryTitle={`${displayClassName(annotation.class_name)} · ${selected.label} · ${TARGET_HINT[selected.writeTarget]}`}
      panelSize="compact"
      summary={
        <>
          <Icon name={busy ? "loader2" : "sparkles"} size={14} />
          <span className="max-w-24 truncate" data-testid="secondary-summary-capability">
            {selected.label}
          </span>
          {wantsText && (promptByKey[selKey] ?? "").trim() && (
            <span
              className="max-w-20 truncate text-muted-foreground"
              data-testid="secondary-summary-prompt"
            >
              {promptByKey[selKey].trim()}
            </span>
          )}
        </>
      }
      quickActions={[]}
      primaryContent={renderPrimary(true)}
    >
      {(close) => (
        <div data-testid="secondary-inference-bar" className="flex min-w-0 flex-col gap-3">
          <div className="flex items-start justify-between gap-3">
            <div className="flex min-w-0 items-start gap-2.5">
              <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-muted text-foreground">
                <Icon name="sparkles" size={16} />
              </span>
              <div className="min-w-0">
                <h2 className="m-0 text-sm font-semibold">二次推理</h2>
                <p className="mb-0 mt-1 text-2xs leading-relaxed text-muted-foreground">
                  {selected.writeTarget === "attributes"
                    ? "识别选中对象，补全属性"
                    : "在选中区域内识别并创建子对象"}
                </p>
              </div>
            </div>
            <IconButton
              size="icon-xs"
              variant="ghost"
              className="shrink-0 rounded-full text-muted-foreground"
              aria-label="收起二次推理设置"
              title="收起设置，继续标注"
              onClick={close}
            >
              <X />
            </IconButton>
          </div>
          <div className="grid min-w-0 grid-cols-2 gap-3">
            <label className="flex min-w-0 flex-col gap-1.5">
              <span className={FIELD_LABEL_CLASS}>识别能力</span>
              <select
                data-testid="secondary-cap-select"
                aria-label="二次推理能力"
                value={selKey}
                disabled={busy}
                onChange={(e) => {
                  setSelectedKey(e.target.value);
                  setParamsOpen(false);
                }}
                className={SELECT_CLASS}
                title="选择在当前对象区域运行的模型"
              >
                {groupByTask(capabilities).map(([task, group]) => (
                  <optgroup key={task} label={taskLabel(task)}>
                    {group.map((c) => (
                      <option key={capKey(c)} value={capKey(c)}>
                        {c.label}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
            </label>
            <div className="flex min-w-0 flex-col gap-1.5">
              <span className={FIELD_LABEL_CLASS}>当前对象</span>
              <span className="flex h-8 min-w-0 items-center gap-2 text-xs">
                <Icon name="scan" size={14} />
                <span className="min-w-0 truncate" title={displayClassName(annotation.class_name)}>
                  {displayClassName(annotation.class_name)}
                </span>
              </span>
            </div>
            {(selected.model.supported_variants?.length ?? 0) > 0 && (
              <div className="col-span-2 flex flex-wrap gap-2 [&_select]:h-8 [&_select]:rounded-lg">
                <VariantSelector
                  compact
                  supportedVariants={selected.model.supported_variants}
                  variantCombinations={selected.model.variant_combinations}
                  defaults={selected.model.default_variants ?? {}}
                  value={variantByKey[selKey] ?? {}}
                  disabled={busy}
                  onChange={(next) => {
                    setVariantByKey((prev) => ({ ...prev, [selKey]: next }));
                    savePref(selKey, { variants: next });
                  }}
                />
              </div>
            )}
          </div>
          {missing.length > 0 && onEnsureAttributeFields && (
            <div className="flex items-center justify-between gap-2 rounded-lg bg-status-caution-soft p-2 text-xs text-status-caution">
              <span className="flex min-w-0 items-center gap-1.5">
                <Icon name="warning" size={13} />
                缺少 {missing.length} 个属性字段
              </span>
              <Button
                type="button"
                size="xs"
                variant="ghost"
                onClick={() => onEnsureAttributeFields(missing)}
                disabled={busy}
                title={`补全 ${missing.map((f) => f.key).join(", ")}，以显示识别结果`}
                data-testid="secondary-fill"
              >
                补全字段
              </Button>
            </div>
          )}
          {renderPrimary(false)}
          {canParams && (
            <>
              <button
                type="button"
                onClick={() => openParamsFor(selected)}
                disabled={busy}
                title="推理参数 (阈值等)"
                data-testid="secondary-params-toggle"
                aria-expanded={paramsOpen}
                aria-controls="secondary-params-panel"
                className="flex w-full items-center gap-2 border-t border-border/60 pt-3 text-left text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-40"
              >
                <Icon name="settings" size={13} />
                <span className="font-medium">推理参数</span>
                <span className="ml-auto max-w-36 truncate text-2xs text-muted-foreground">
                  {selected.backendName}
                </span>
                <Icon name={paramsOpen ? "chevUp" : "chevDown"} size={12} />
              </button>
              {paramsOpen && (
                <section
                  id="secondary-params-panel"
                  className="min-w-0 [&_input]:accent-brand [&_select]:h-8 [&_select]:rounded-lg"
                  data-testid="secondary-params-panel"
                  aria-label="二次推理参数"
                >
                  <SchemaForm
                    schema={selected.model.params as JsonSchemaObject}
                    value={paramValues}
                    onChange={updateParams}
                    disabled={busy}
                  />
                </section>
              )}
            </>
          )}
        </div>
      )}
    </ContextToolbar>
  );
}
