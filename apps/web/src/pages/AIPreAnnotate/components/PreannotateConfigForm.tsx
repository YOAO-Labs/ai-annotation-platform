import { useId } from "react";
import { CatalogModelSelect } from "./CatalogModelSelect";
import { Icon } from "@/components/ui/Icon";
import { TabRow } from "@/components/ui/TabRow";
import { VariantSelector } from "@/components/ml/VariantSelector";
import { type TextOutputMode } from "@/hooks/usePreannotation";
import { SchemaForm } from "@/pages/Workbench/components/SchemaForm";
import { ClassWhitelistRow } from "./ClassWhitelistRow";
import { PresetRow } from "./PresetRow";
import type { PreannotateConfig, useCropRecognitionConfig } from "./usePreannotateConfig";
import styles from "./ProjectDetailPanel.module.css";

const OUTPUT_MODE_TABS = ["□ 框", "○ 掩膜", "⊕ 全部"];
const OUTPUT_MODE_LABELS: Record<TextOutputMode, string> = {
  box: "□ 框",
  mask: "○ 掩膜",
  both: "⊕ 全部",
};
const OUTPUT_MODE_BY_LABEL: Record<string, TextOutputMode> = {
  "□ 框": "box",
  "○ 掩膜": "mask",
  "⊕ 全部": "both",
};

const GEOMETRIC_TASK_LABELS: Record<string, string> = {
  detect: "检测（框）",
  segment: "分割（掩膜）",
  pose: "关键点",
  obb: "朝向框",
};

function cx(...names: Array<string | false | null | undefined>) {
  return names.filter(Boolean).join(" ");
}

interface Props {
  cfg: PreannotateConfig;
  backends?: Array<{ id: string; name: string }>;
  selectedBackendId?: string | null;
  onSelectBackend?: (id: string | null) => void;
  projectMlBackendId?: string | null;
  backendSelectorLabel?: string;
  hideModelTaskSelector?: boolean;
  hideClassWhitelist?: boolean;
  stageParameters?: ReturnType<typeof useCropRecognitionConfig>;
}

export function PreannotateConfigForm({
  cfg,
  backends,
  selectedBackendId,
  onSelectBackend,
  projectMlBackendId,
  backendSelectorLabel = "模型服务",
  hideModelTaskSelector = false,
  hideClassWhitelist = false,
  stageParameters,
}: Props) {
  const promptId = useId();
  const paramsHintId = useId();
  const parameters = stageParameters ?? cfg;
  const docMode = !!stageParameters || cfg.isDocMode;
  const geometric = !stageParameters && cfg.isGeometricBackend;
  const taskModels = cfg.selectableModels;
  const taskModel =
    cfg.selectableModels?.find((m) => m.id === cfg.selectedModelId) ?? cfg.selectableModels?.[0];
  const setTaskModelId = cfg.selectTaskModel;
  const taskModelLabel = (m: { id: string; task?: string; display_name?: string }) =>
    m.display_name || GEOMETRIC_TASK_LABELS[m.task ?? ""] || m.task || m.id;
  return (
    <>
      {backends && backends.length > 1 && onSelectBackend && (
        <label className={styles.field}>
          <span className={styles.fieldLabel}>{backendSelectorLabel}</span>
          <select
            value={selectedBackendId ?? ""}
            onChange={(e) => {
              cfg.setCatalogModel?.("");
              onSelectBackend(e.target.value || null);
            }}
            className={styles.promptInput}
          >
            {backends.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
                {b.id === projectMlBackendId ? "（主服务）" : ""}
              </option>
            ))}
          </select>
        </label>
      )}

      {!hideModelTaskSelector && <CatalogModelSelect cfg={cfg} />}

      {!cfg.backendId ? (
        <div className={cx(styles.field, styles.docHint)}>
          <Icon name="info" size={12} />
          <span>
            {backends && backends.length > 1
              ? "请先在上方选择模型服务，再配置预标参数。"
              : "该项目尚未接入模型服务，请先在项目设置中接入模型服务后再配置预标参数。"}
          </span>
        </div>
      ) : (
        <>
          {hideModelTaskSelector || cfg.selectedCatalogId ? null : taskModels.length > 1 ? (
            <label className={styles.field}>
              <span className={styles.fieldLabel}>模型任务</span>
              <select
                value={taskModel?.id ?? ""}
                onChange={(e) => setTaskModelId(e.target.value)}
                className={styles.promptInput}
              >
                {taskModels.map((m) => (
                  <option key={m.id} value={m.id}>
                    {taskModelLabel(m)}
                  </option>
                ))}
              </select>
            </label>
          ) : taskModel ? (
            <div className={styles.field}>
              <span className={styles.fieldLabel}>当前模型</span>
              <span className={styles.readonlyValue}>{taskModelLabel(taskModel)}</span>
            </div>
          ) : null}

          {!stageParameters && cfg.sourceBatchableWarning && (
            <div className={styles.stageWarn}>
              <Icon name="warning" size={12} />
              <span>{cfg.sourceBatchableWarning}，运行将被端点拒绝。</span>
            </div>
          )}

          {geometric && !hideClassWhitelist && (
            <ClassWhitelistRow
              classes={cfg.geometricModel?.classes}
              selected={cfg.selectedClassIdx}
              onChange={cfg.setSelectedClassIdx}
              onWarm={cfg.selectedCatalogId ? undefined : () => cfg.warmMut.mutate()}
              warming={cfg.warmMut.isPending}
            />
          )}

          {docMode && !stageParameters && (
            <div className={cx(styles.field, styles.docHint)}>
              <Icon name="info" size={12} />
              <span>
                {cfg.taskType === "ocr" ? "OCR 文字识别" : "文档版面"}
                ：识别文本将写入 annotation 属性；若项目未配置 text 属性，文本不会入库。
              </span>
            </div>
          )}

          {!docMode && !geometric && (
            <label className={styles.field} htmlFor={promptId}>
              <span id={`${promptId}-label`} className={styles.fieldLabel}>
                提示词
              </span>
              {cfg.aliases.length > 0 && (
                <div className={styles.aliasList}>
                  {cfg.aliases.map((a) => {
                    const isActive = cfg.promptTokenSet.has(a.alias.toLowerCase());
                    return (
                      <button
                        key={a.name}
                        type="button"
                        onClick={() => cfg.toggleAlias(a.alias)}
                        className={cx(styles.aliasChip, isActive && styles.aliasChipActive)}
                        title={`${isActive ? "移除" : "添加"} 类别「${a.name}」的别名${a.count > 0 ? ` · 历史 ${a.count} 次` : ""}`}
                      >
                        <span>
                          {isActive ? "✓ " : ""}
                          {a.alias}
                        </span>
                        <span className={styles.aliasName}>({a.name})</span>
                        {a.count > 0 && <span className={styles.aliasCount}>×{a.count}</span>}
                      </button>
                    );
                  })}
                  <button
                    type="button"
                    onClick={() => cfg.setPrompt(cfg.aliases.map((x) => x.alias).join(", "))}
                    className={styles.refillButton}
                    title="按频率重新填入所有别名"
                  >
                    重填
                  </button>
                </div>
              )}
              <textarea
                id={promptId}
                aria-labelledby={`${promptId}-label`}
                aria-describedby={`${promptId}-hint`}
                rows={2}
                value={cfg.prompt}
                onChange={(e) => cfg.setPrompt(e.target.value)}
                placeholder="例：car, person, traffic light"
                className={styles.promptInput}
              />
              <span id={`${promptId}-hint`} className={styles.mutedText}>
                同一段文本应用到所有选中批次，以逗号分隔。
              </span>
            </label>
          )}

          <div
            className={styles.field}
            role="group"
            aria-label="推理参数"
            aria-describedby={paramsHintId}
          >
            <span className={styles.fieldLabel}>推理参数</span>
            {!cfg.selectedCatalogId && !docMode && cfg.setupQ.isLoading ? (
              <div className={styles.mutedText}>加载参数…</div>
            ) : !cfg.selectedCatalogId && !docMode && cfg.setupQ.isError ? (
              <div className={styles.mutedText}>无法获取模型服务参数，运行时使用项目级阈值。</div>
            ) : docMode && !parameters.hasAnyParams && !parameters.variantGroups?.length ? (
              <div className={styles.mutedText}>该任务无可调参数。</div>
            ) : (
              <div className={styles.backendParamsStack}>
                <VariantSelector
                  schema={parameters.paramsSchema}
                  supportedVariants={parameters.variantGroups}
                  variantCombinations={parameters.variantCombinations}
                  defaults={parameters.variantDefaults}
                  value={parameters.paramsValue}
                  onChange={parameters.onVariantOrParamsChange}
                />
                {(parameters.hasNonVariantParams ||
                  (!parameters.hasAnyParams && !parameters.variantGroups?.length)) && (
                  <SchemaForm
                    schema={parameters.paramsSchema}
                    value={parameters.paramsValue}
                    onChange={parameters.onParamsChange}
                  />
                )}
              </div>
            )}
            <span id={paramsHintId} className={styles.mutedText}>
              {stageParameters ? "仅用于当前识别阶段。" : "按模型服务保存，覆盖项目默认值。"}
            </span>
          </div>

          <PresetRow
            presets={parameters.presets}
            onApply={(p) => parameters.applyPreset(p.values)}
            onSave={(name) => parameters.savePreset(name, parameters.paramsValue)}
            onRemove={parameters.removePreset}
          />

          {!stageParameters &&
            (cfg.isTextPath
              ? cfg.textOutputOptions.length > 1 && (
                  <div className={styles.field}>
                    <span className={styles.fieldLabel}>输出形态</span>
                    <TabRow
                      tabs={cfg.textOutputOptions.map((o) => OUTPUT_MODE_LABELS[o])}
                      active={OUTPUT_MODE_LABELS[cfg.outputMode]}
                      onChange={(label) => {
                        const m = OUTPUT_MODE_BY_LABEL[label];
                        if (m) cfg.setOutputMode(m);
                      }}
                    />
                  </div>
                )
              : cfg.panelShape.showOutputMode && (
                  <div className={styles.field}>
                    <span className={styles.fieldLabel}>输出形态</span>
                    <TabRow
                      tabs={OUTPUT_MODE_TABS}
                      active={OUTPUT_MODE_LABELS[cfg.outputMode]}
                      onChange={(label) => {
                        const m = OUTPUT_MODE_BY_LABEL[label];
                        if (m) cfg.setOutputMode(m);
                      }}
                    />
                  </div>
                ))}
        </>
      )}
    </>
  );
}
