/**
 * 预标"配置区"共享展示组件 (纯渲染, 状态由 usePreannotateConfig 提供)。
 *
 * 渲染: 任务类型 / 模型任务 / 类别白名单 / doc 提示 / prompt+alias / backend 多选 /
 * params+variant / 命名预设 / 输出形态。批量页与工作台 AI 面板共用。
 * 批量专属的 predict_mode / 并发 / run 按钮不在此, 由调用方拼。
 */
import { CatalogModelSelect } from "./CatalogModelSelect";
import { Icon } from "@/components/ui/Icon";
import { TabRow } from "@/components/ui/TabRow";
import { VariantSelector } from "@/components/ml/VariantSelector";
import { type TextOutputMode } from "@/hooks/usePreannotation";
import { SchemaForm } from "@/pages/Workbench/components/SchemaForm";
import { ClassWhitelistRow } from "./ClassWhitelistRow";
import { PresetRow } from "./PresetRow";
import { type PreannotateConfig } from "./usePreannotateConfig";
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
  /** backend 多选 (批量页); 省略或 <=1 项时不渲染选择器 (工作台单 backend). */
  backends?: Array<{ id: string; name: string }>;
  selectedBackendId?: string | null;
  onSelectBackend?: (id: string | null) => void;
  projectMlBackendId?: string | null;
  backendSelectorLabel?: string;
  /**
   * 收起整图「模型任务」下拉 (默认 false)。下游阶段卡里另有「下游模型」选择器作真值, 此处的整图
   * model-first 下拉与之冗余 (尤其选 crop 下游如 rec 时, 整图 det/e2e 选择已无意义), 由调用方收起。
   * 变体/参数/backend 选择不受影响, 仍渲染。
   */
  hideModelTaskSelector?: boolean;
  /**
   * 收起类别白名单行 (默认 false)。该白名单属源整图几何模型, 下游阶段卡里它不参与下游 model 的
   * class_filter (下游按 parent_class_filter 父框类名筛), 留着是死控件且误导, 由下游卡收起。
   */
  hideClassWhitelist?: boolean;
}

export function PreannotateConfigForm({
  cfg,
  backends,
  selectedBackendId,
  onSelectBackend,
  projectMlBackendId,
  backendSelectorLabel = "ML Backend",
  hideModelTaskSelector = false,
  hideClassWhitelist = false,
}: Props) {
  // v0.18.12 · 统一「模型任务」选择器: 几何 (yolo) 与文本 (gsam2/sam3) 共用一套 model-first 选择,
  //   只差 prompt vs 类别白名单。doc 走上方「任务类型」, 不在此。
  // v0.20.5 · 单一「模型任务」下拉(对齐所有 backend): 候选 = 该 backend 全部可批量预标模型
  //   (几何检测/分割 + OCR/版面)。不再为 OCR/版面单设「任务类型」tab; 选中 model 由 hook 派发。
  const taskModels = cfg.selectableModels;
  const taskModel =
    cfg.selectableModels?.find((m) => m.id === cfg.selectedModelId) ?? cfg.selectableModels?.[0];
  const setTaskModelId = cfg.selectTaskModel;
  // 选择器的**真值键是 model_id**; 展示**直接用模型市场卡片标题 (display_name)**, 与卡片视图一致 ——
  // 用户在配置面板选的就是市场里那张卡 (同名同 id)。缺 display_name 才回落 task 标签 / id。
  const taskModelLabel = (m: { id: string; task?: string; display_name?: string }) =>
    m.display_name || GEOMETRIC_TASK_LABELS[m.task ?? ""] || m.task || m.id;
  return (
    <>
      {/* v0.14.18 · 多 backend 选择 (批量页) — 置于面板最上方, 先选后端再配置其余字段. */}
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
                {b.id === projectMlBackendId ? "（项目主后端）" : ""}
              </option>
            ))}
          </select>
        </label>
      )}

      {!hideModelTaskSelector && <CatalogModelSelect cfg={cfg} />}

      {/* 未接入 ML 后端 (cfg.backendId 为空): 不渲染任何预标参数 (prompt / 后端参数 / 预设 /
          输出形态), 仅给引导提示。修"项目未接后端却暴露 SAM 风格预标配置"的展示 bug:
          这些区块的旧条件 (panelShape 安全兜底 / setupQ 禁用空态) 把"未接后端"误当成
          "已接后端但能力声明不全", 导致全部以空态渲染出来。 */}
      {!cfg.backendId ? (
        <div className={cx(styles.field, styles.docHint)}>
          <Icon name="info" size={12} />
          <span>
            {backends && backends.length > 1
              ? "请先在上方选择 ML 后端，再配置预标参数。"
              : "该项目尚未接入 ML 后端，请先在项目设置中接入模型后再配置预标参数。"}
          </span>
        </div>
      ) : (
        <>
          {/* v0.14.17 / v0.18.12 / v0.20.5 · 统一「模型任务」下拉: 几何 (YOLO 闭集)、文本 (gsam2/sam3 开集)、
          OCR/版面 共用同一个下拉 —— 不再为 OCR/版面单设「任务类型」tab。选项 = 该 backend 可批量预标的
          model, value 直接是 model_id, 文案 = 模型市场卡片标题 (display_name)。多于 1 个可选 model 出下拉。 */}
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
            // v0.18.13 · 单一可选 model 不出下拉, 仍只读展示「当前模型」(模型市场卡片标题),
            //   任何 backend 单阶段都能看到将跑哪个 model, 不再「什么都不显示」。
            <div className={styles.field}>
              <span className={styles.fieldLabel}>当前模型</span>
              <span className={styles.readonlyValue}>{taskModelLabel(taskModel)}</span>
            </div>
          ) : null}

          {/* v0.19.3 WS2 · 源模型自报 batchable=false (交互/有状态) → 不能批量预标 (端点会 422)。 */}
          {cfg.sourceBatchableWarning && (
            <div className={styles.stageWarn}>
              <Icon name="warning" size={12} />
              <span>{cfg.sourceBatchableWarning}，运行将被端点拒绝。</span>
            </div>
          )}

          {/* v0.14.17 · YOLO 类别白名单勾选 ([index]类名). 留空=全部.
          下游卡 (hideClassWhitelist) 不出此行: 该白名单属源整图模型, 下游 model 的 class_filter
          不取它, 下游按 parent_class_filter (父框类名) 筛, 留着是死控件且误导。 */}
          {cfg.isGeometricBackend && !hideClassWhitelist && (
            <ClassWhitelistRow
              classes={cfg.geometricModel?.classes}
              selected={cfg.selectedClassIdx}
              onChange={cfg.setSelectedClassIdx}
              onWarm={cfg.selectedCatalogId ? undefined : () => cfg.warmMut.mutate()}
              warming={cfg.warmMut.isPending}
            />
          )}

          {/* v0.14.9 · OCR / 版面识别静态提示. */}
          {cfg.isDocMode && (
            <div className={cx(styles.field, styles.docHint)}>
              <Icon name="info" size={12} />
              <span>
                {cfg.taskType === "ocr" ? "OCR 文字识别" : "文档版面"}
                ：识别文本将写入 annotation 属性；若项目未配置 text 属性，文本不会入库。
              </span>
            </div>
          )}

          {/* prompt 区: 开放词表文本任务 (gsam2) 显示; OCR/版面 与 YOLO 几何 backend 隐藏. */}
          {!cfg.isDocMode && !cfg.isGeometricBackend && (
            <label className={styles.field}>
              <span className={styles.fieldLabel}>
                Prompt（同一段文本应用到所有选中批次；逗号分隔）
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
                        title={`${isActive ? "移除" : "添加"} 类别「${a.name}」的 alias${a.count > 0 ? ` · 历史 ${a.count} 次` : ""}`}
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
                    title="一键重填: 按频率拼上所有 alias"
                  >
                    重填
                  </button>
                </div>
              )}
              <textarea
                rows={2}
                value={cfg.prompt}
                onChange={(e) => cfg.setPrompt(e.target.value)}
                placeholder="例：car, person, traffic light"
                className={styles.promptInput}
              />
            </label>
          )}

          {/* v0.10.38 · 按后端参数面板. */}
          <div className={styles.field}>
            <span className={styles.fieldLabel}>后端推理参数（按 backend 记忆，覆盖项目默认）</span>
            {!cfg.selectedCatalogId && !cfg.isDocMode && cfg.setupQ.isLoading ? (
              <div className={styles.mutedText}>加载参数…</div>
            ) : !cfg.selectedCatalogId && !cfg.isDocMode && cfg.setupQ.isError ? (
              <div className={styles.mutedText}>
                无法拉取 backend /setup，运行时回落项目级阈值。
              </div>
            ) : cfg.isDocMode && !cfg.hasAnyParams && (cfg.variantGroups?.length ?? 0) === 0 ? (
              // v0.20.5 · 仅当既无 params 又无变体轴时才算「无可调参数」; OCR 可调项全在
              //   supported_variants(version/size/lang), 此前被 !hasAnyParams 误判隐藏。
              <div className={styles.mutedText}>该任务无可调参数。</div>
            ) : (
              <div className={styles.backendParamsStack}>
                <VariantSelector
                  schema={cfg.paramsSchema}
                  supportedVariants={cfg.variantGroups}
                  variantCombinations={cfg.variantCombinations}
                  defaults={cfg.variantDefaults}
                  value={cfg.paramsValue}
                  onChange={cfg.onVariantOrParamsChange}
                />
                {/* v0.20.5 · 有变体轴但无 params 时(OCR)不渲染空 SchemaForm 占位,只留变体选择器;
                仅当既无非变体 params 又无变体轴时才显「无可配置参数」占位。 */}
                {(cfg.hasNonVariantParams ||
                  (!cfg.hasAnyParams && (cfg.variantGroups?.length ?? 0) === 0)) && (
                  <SchemaForm
                    schema={cfg.paramsSchema}
                    value={cfg.paramsValue}
                    onChange={cfg.onParamsChange}
                  />
                )}
              </div>
            )}
          </div>

          {/* v0.14.16 · 命名预设. */}
          <PresetRow
            presets={cfg.presets}
            onApply={(p) => cfg.applyPreset(p.values)}
            onSave={(name) => cfg.savePreset(name, cfg.paramsValue)}
            onRemove={cfg.removePreset}
          />

          {/* 输出形态. v0.18.12 · 文本路径 task-first 去重: 分割 model 出 {掩膜, 全部} 二选 (删「框」——
          「分割@框」与「检测」task 等价冗余); 检测 model 无此 (强制 box, 结构即隐藏 SAM)。
          几何/doc 仍按 model 几何输出三选 (panelShape)。 */}
          {cfg.isTextPath
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
              )}
        </>
      )}
    </>
  );
}
