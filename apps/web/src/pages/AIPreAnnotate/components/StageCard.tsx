/**
 * v0.18.2 · 多阶段预标注「下游阶段卡」(路径 B M2).
 *
 * 每张卡自持一份 usePreannotateConfig + PreannotateConfigForm 实例 (复用共享组件, 不改其本身,
 * 红线), 再补 M2 的阶段级字段: 父框类别过滤 (parent_class_filter) / ROI 扩展 (roi.pad) /
 * 写回属性键 (write.keys)。卡片把派生出的 stage payload 通过 onChange 上抛给容器, 由容器在运行时
 * 组装成 pipeline_stages 的并行兄弟。多张卡共享同一源阶段 = 单层并行扇出。
 *
 * v0.18.5 · 配置硬化: parent_class_filter / write.keys 由自由文本框升级为选择器 (类别取项目类别,
 * 属性键取下游 backend 自报 output_attribute_schema, 回落项目 attribute_schema), 消灭拼写误配;
 * 下游 backend 不产属性时给 ⚠ 警示; 键冲突 chip 由容器传 conflictKeys 标红。
 */

import { useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { hasInput, INPUT_BBOX_PROMPT_ID, INPUT_CROP_ID } from "@/api/capabilityInputs";
import { usePreannotateConfig, type PreannotateArgs } from "./usePreannotateConfig";
import { CatalogModelSelect } from "./CatalogModelSelect";
import { PreannotateConfigForm } from "./PreannotateConfigForm";
import { ChipMultiSelect } from "./ChipMultiSelect";
import type { PipelineStagePayload, PipelineStageStat } from "@/hooks/usePreannotation";
import { classifyDownstream, type StageCaps } from "../utils/pipelineGraph";
import styles from "./ProjectDetailPanel.module.css";

// v0.18.8 · 运行态 → Badge 原语 (语义色 + 暗色配对走设计系统, 不裸色)。
const RUN_STATE_BADGE: Record<
  string,
  { variant: "outline" | "ai" | "success" | "danger"; label: string; dot?: boolean }
> = {
  pending: { variant: "outline", label: "待运行" },
  running: { variant: "ai", label: "运行中", dot: true },
  done: { variant: "success", label: "已完成" },
  failed: { variant: "danger", label: "失败" },
};

function cx(...names: Array<string | false | null | undefined>) {
  return names.filter(Boolean).join(" ");
}

interface Props {
  id: string;
  /** 1-based 展示序号 (源阶段是 1, 第一张下游卡是 2…)。 */
  displayIndex: number;
  projectId: string;
  backends: Array<{ id: string; name: string }>;
  projectMlBackendId?: string | null;
  /** 源阶段 backend, 用于默认选一个不同的下游 backend。 */
  sourceBackendId: string | null;
  /** v0.18.5 · 项目类别 (类名)，父框类别过滤多选的回落选项。 */
  projectClasses: string[];
  /** v0.20.x · 上游(源/父)阶段筛完的有效类别名 —— 父框类别选项优先取此 (下游只会见到这些框);
   *  空/缺 → 回落 projectClasses。父框类别另支持自由文本输入, 不限于这些选项。 */
  parentClassOptions?: string[];
  /** v0.18.5 · 项目 attribute_schema 字段键，写回属性键多选的回落选项 (backend 未自报 schema 时)。 */
  projectAttributeKeys: string[];
  /** v0.18.5 · 本卡 write.keys 中与其它并行阶段冲突的键 (容器算好下发), 命中 chip 标红。 */
  conflictKeys?: Set<string>;
  /** v0.18.6 · 本阶段运行态统计 (容器从实时/终态下发); 无=未跑。 */
  stat?: PipelineStageStat;
  /** v0.18.6 · 本阶段运行态 (徽标用): pending/running/done。 */
  runState?: "pending" | "running" | "done";
  /** v0.18.16 · 检查器里非选中卡 CSS 隐藏 (不卸载, 保住自持配置状态)。 */
  hidden?: boolean;
  /** 派生出的 stage payload (未就绪=null) 上抛给容器; 容器据此组装 pipeline_stages。 */
  onChange: (id: string, payload: PipelineStagePayload | null) => void;
  /** v0.18.16 §13 · 上抛能力旗标, 供画布作可达性 / 产属性警示 (标红不硬拦)。 */
  onCaps?: (id: string, caps: StageCaps | null) => void;
  onRemove: (id: string) => void;
}

export function StageCard({
  id,
  displayIndex,
  projectId,
  backends,
  projectMlBackendId,
  sourceBackendId,
  projectClasses,
  parentClassOptions,
  projectAttributeKeys,
  conflictKeys,
  stat,
  runState = "pending",
  hidden = false,
  onChange,
  onCaps,
  onRemove,
}: Props) {
  const [backendId, setBackendId] = useState<string | null>(() => {
    const other = backends.find((b) => b.id !== sourceBackendId);
    return (other ?? backends[0])?.id ?? null;
  });
  const cfg = usePreannotateConfig({ projectId, backendId, inputMode: "crop" });

  const [classFilter, setClassFilter] = useState<Set<string>>(new Set());
  const [pad, setPad] = useState(0.05);
  const [writeKeys, setWriteKeys] = useState<Set<string>>(new Set());
  // v0.18.15 · 子物体命名 (写回属性键前缀, 如 hat → hat_color); 仅 attributes (分类) 阶段有意义。
  const [label, setLabel] = useState("");

  // v0.18.5 · 写回属性键选项: 优先下游 backend 自报 output_attribute_schema 的 key (最准),
  // 回落项目 attribute_schema 字段 key。
  const backendAttrOptions = useMemo(() => {
    const models = cfg.capabilitiesQ.data?.models ?? [];
    const seen = new Map<string, string>();
    for (const m of models) {
      for (const a of m.output_attribute_schema ?? []) {
        if (a?.key && !seen.has(a.key)) seen.set(a.key, a.label || a.key);
      }
    }
    return Array.from(seen, ([value, label]) => ({ value, label }));
  }, [cfg.capabilitiesQ.data]);

  const attrKeyOptions =
    backendAttrOptions.length > 0
      ? backendAttrOptions
      : projectAttributeKeys.map((k) => ({ value: k, label: k }));

  // 下游 backend 是否会产属性 (自报 schema / types 任一非空)。capabilities 就绪后才判定。
  const capabilitiesReady = !cfg.capabilitiesQ.isLoading && cfg.capabilitiesQ.data != null;
  const producesAttributes = useMemo(() => {
    const models = cfg.capabilitiesQ.data?.models ?? [];
    return models.some(
      (m) =>
        (m.output_attribute_schema?.length ?? 0) > 0 || (m.output_attribute_types?.length ?? 0) > 0,
    );
  }, [cfg.capabilitiesQ.data]);
  const showNoAttrWarning = backendId != null && capabilitiesReady && !producesAttributes;

  const stageArgs: PreannotateArgs | null = cfg.configReady
    ? cfg.buildArgs("skip_predicted")
    : null;

  // v0.18.2 / v0.18.13 / v0.18.15 / v0.20.x · 下游可选 model: 非交互、原子 (composition!=composite) 的批量单元 ——
  //   classification (裁 ROI 跑分类, crop 投递, 产属性) / box-seg (segmentation + bbox prompt,
  //   消费上游框出 mask, geometry 投递, 产几何) / detection (普通检测器在父 crop 上检子物体,
  //   crop 投递 + 坐标回映, 产几何, v0.18.15) / ocr 识别 (rec 原子在父 crop 上认字, crop 投递,
  //   产 text/orientation/language 属性 —— 跨 backend「上游 det → 下游 rec」编排的下游, v0.20.x)。
  //   编排只组合 atom: 一锅端 composite (如 ocr-e2e) / 交互式不作下游。
  const downstreamModels = useMemo(() => {
    const models = cfg.capabilitiesQ.data?.models ?? [];
    return models.filter(
      (m) =>
        m.composition !== "composite" &&
        !m.is_interactive &&
        (m.task === "classify" ||
          m.task === "detect" ||
          m.task === "ocr" ||
          (m.task === "segment" && (m.supported_prompts ?? []).includes("bbox"))),
    );
  }, [cfg.capabilitiesQ.data]);

  // 默认优先分类 (保持既有行为: 上游已裁 ROI, 分类比重复检测更准); 无分类则取第一个 (常是 box-seg)。
  const [selectedModelId, setSelectedModelId] = useState<string | null>(null);
  useEffect(() => {
    setSelectedModelId((prev) => {
      if (prev && downstreamModels.some((m) => m.id === prev)) return prev;
      const classify = downstreamModels.find((m) => m.task === "classify");
      return classify?.id ?? downstreamModels[0]?.id ?? null;
    });
  }, [backendId, downstreamModels]);
  const selectedModel = cfg.selectedCatalogId
    ? (cfg.geometricModel ?? null)
    : (downstreamModels.find((m) => m.id === selectedModelId) ?? null);

  // 下游 model 归类 (判据抽到 pipelineGraph.classifyDownstream, 与全局侧 GlobalStageInspector 共用):
  //   box-seg geometry (消费上游框出 polygon, geometry 投递) / crop-detect geometry (父 crop 检子物体,
  //   crop 投递 + 坐标回映, v0.18.15) / 文本识别 (rec 原子, task=ocr, crop 投递, 产 text/orientation/
  //   language 属性, v0.20.x)。产几何的两类共用: 隐藏属性字段、允许作父阶段。
  const { isBoxSegGeometry, isCropDetectGeometry, isGeometryDownstream, isOcrRecognize } =
    classifyDownstream(selectedModel);

  // 选中 rec 下游时, 把本卡继承的配置表单切到其整图 OCR 同胞 (e2e): ① 让 cfg.configReady=true (否则
  //   纯 rec 既非 doc/几何/prompt, stageArgs 为 null), ② 让继承的变体选择器暴露 version/size/lang
  //   轴 (rec 与 e2e 共用同套轴), payload 的 model_variants 据此对齐 rec。各卡 cfg 独立, 不影响源阶段。
  const ocrSiblingId = useMemo(
    () => cfg.selectableModels?.find((m) => m.task === "ocr")?.id ?? null,
    [cfg.selectableModels],
  );
  useEffect(() => {
    if (isOcrRecognize && ocrSiblingId && cfg.selectedModelId !== ocrSiblingId) {
      cfg.selectTaskModel(ocrSiblingId);
    }
    // selectTaskModel 每渲染新引用; 由 selectedModelId!==ocrSiblingId 守卫收敛, 不入依赖避免每帧重跑。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOcrRecognize, ocrSiblingId, cfg.selectedModelId]);

  // 派生 stage payload (不含 stage 序号 / parent_stage, 由容器补)。
  // stageArgs / currentVariantSlice 每次渲染新对象, 用 JSON 串作 useMemo 稳定依赖 (抽出供静态检查)。
  const stageArgsKey = JSON.stringify(stageArgs);
  const paramsKey = JSON.stringify(cfg.paramsValue);
  const variantSliceKey = JSON.stringify(cfg.currentVariantSlice);
  const payload = useMemo<Omit<PipelineStagePayload, "stage" | "parent_stage"> | null>(() => {
    const classArr = Array.from(classFilter);
    // v0.18.12 · box-seg geometry 下游: 直接由 backend+model 构造, 不依赖 prompt/configReady。
    // 变体取选中 model 自己声明的轴 (box-seg 只有 sam_variant), 从共享 variant 选择器值过滤。
    if (cfg.selectedCatalogId && !stageArgs) return null;
    if (isGeometryDownstream && cfg.backendId && selectedModel) {
      const axisKeys = new Set((selectedModel.supported_variants ?? []).map((g) => g.key));
      const variantSlice: Record<string, string> = {};
      for (const [k, v] of Object.entries(cfg.currentVariantSlice)) {
        if (axisKeys.has(k)) variantSlice[k] = v;
      }
      return {
        ml_backend_id: cfg.backendId,
        model_id:
          stageArgs?.model_id && cfg.selectedCatalogId ? stageArgs.model_id : selectedModel.id,
        task: selectedModel.task,
        model_variants: cfg.selectedCatalogId ? stageArgs?.model_variants : variantSlice,
        params: cfg.selectedCatalogId
          ? (stageArgs?.params ?? {})
          : Object.fromEntries(
              Object.entries(cfg.paramsValue).filter(
                ([key]) => key in (selectedModel.params?.properties ?? {}),
              ),
            ),
        ...(isCropDetectGeometry && cfg.selectedClassIdx.size
          ? { class_filter: [...cfg.selectedClassIdx].sort((a, b) => a - b) }
          : {}),
        parent_class_filter: classArr.length > 0 ? classArr : undefined,
        // box-seg → geometry 投递 (整图+父框列表); crop-detect → crop 投递 (裁父框 + 坐标回映)。
        ...(isCropDetectGeometry
          ? {
              roi: { mode: "crop" as const, pad },
              input: { mode: "crop" as const },
              write: { target: "geometry" as const },
            }
          : { roi: { mode: "geometry" as const, pad }, write: { target: "geometry" as const } }),
      };
    }
    // classify / ocr-rec 下游: 走 buildArgs 取 backend/params, 但**变体按选中下游 model 自己的轴**
    //   过滤 (与几何下游一致), 不再透传「模型任务」整图模型的轴; 且**不透传源模型的 class_filter**
    //   —— 其 index 空间属上游整图检测模型, 与下游分类/识别 model 的类别空间无关, 灌进去后端会误用/忽略。
    if (!stageArgs) return null;
    const keyArr = Array.from(writeKeys);
    const labelTrim = label.trim();
    const downstreamAxisKeys = new Set((selectedModel?.supported_variants ?? []).map((g) => g.key));
    const downstreamVariants: Record<string, string> = {};
    for (const [k, v] of Object.entries(cfg.currentVariantSlice)) {
      if (downstreamAxisKeys.has(k)) downstreamVariants[k] = v;
    }
    return {
      ml_backend_id: stageArgs.ml_backend_id,
      model_id: selectedModel?.id ?? stageArgs.model_id,
      task: selectedModel?.task ?? stageArgs.task,
      model_variants: downstreamVariants,
      params: stageArgs.params,
      parent_class_filter: classArr.length > 0 ? classArr : undefined,
      roi: { mode: "crop", pad },
      ...(labelTrim ? { label: labelTrim } : {}),
      write: {
        target: "attributes",
        ...(keyArr.length > 0 ? { keys: keyArr } : {}),
      },
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    stageArgsKey,
    isGeometryDownstream,
    isCropDetectGeometry,
    backendId,
    selectedModel?.id,
    variantSliceKey,
    cfg.selectedCatalogId,
    cfg.backendId,
    cfg.selectedClassIdx,
    paramsKey,
    classFilter,
    pad,
    writeKeys,
    label,
  ]);

  // 用 ref 固定 onChange, 避免容器每次渲染触发本 effect。
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  useEffect(() => {
    onChangeRef.current(id, payload as PipelineStagePayload | null);
  }, [id, payload]);
  // 卸载时清掉本卡的贡献。
  useEffect(() => {
    return () => onChangeRef.current(id, null);
  }, [id]);

  // v0.19.2 WS1 · 选中分类下游 model 自报了属性类型却不含 class → 跑完属性恒空 (与端点 422 对齐)。
  // undefined = 未自报 (不判); false = 自报了但缺 class。
  const selectedModelTypes = selectedModel?.output_attribute_types ?? [];
  const producesClass =
    selectedModelTypes.length > 0 ? selectedModelTypes.includes("class") : undefined;
  // isOcrRecognize 排除: rec 产 text/orientation/language (本就不含 class), 该警告对识别阶段是误报。
  const showNoClassWarning =
    !isGeometryDownstream && !isOcrRecognize && capabilitiesReady && producesClass === false;
  // v0.19.2 WS1 · write.keys 与 model 自报 output_attribute_schema 对账 (config-time 警告, 不硬挡):
  // 选的键不在 model schema 字段集 → 该 model 可能不产出。schema 缺失时跳过 (向后兼容)。
  const schemaKeySet = new Set(
    (selectedModel?.output_attribute_schema ?? [])
      .map((a) => a?.key)
      .filter((k): k is string => !!k),
  );
  const unknownWriteKeys =
    schemaKeySet.size > 0 ? Array.from(writeKeys).filter((k) => !schemaKeySet.has(k)) : [];

  // v0.19.3 WS2 · 选中下游 model 自报 batchable (resource_profile.batchable) → false 即交互/有状态,
  // 不能进批量预标 (与端点 _assert_capabilities 对齐)。非 boolean = 未自报 (不判)。
  const rpBatchable = selectedModel?.resource_profile?.batchable;
  const batchable = typeof rpBatchable === "boolean" ? rpBatchable : undefined;
  const showNotBatchableWarning = capabilitiesReady && batchable === false;

  // v0.18.16 §13 · 能力旗标上抛 (供画布可达性 / 产属性警示)。
  const supportedInputs = selectedModel?.supported_inputs ?? [];
  const supportedInputsKey = supportedInputs.join(",");
  const outputClassesKey = JSON.stringify(
    (selectedModel?.classes ?? [])
      .filter((c) => !cfg.selectedClassIdx.size || cfg.selectedClassIdx.has(c.index))
      .map((c) => cfg.selectedCatalogEntry?.output_classes?.[c.index] ?? c.name),
  );
  const caps = useMemo<StageCaps>(
    () => ({
      hasCapabilities: capabilitiesReady,
      knownInputs: supportedInputs.length > 0,
      acceptsCrop: hasInput(supportedInputs, INPUT_CROP_ID),
      acceptsBboxPrompt: hasInput(supportedInputs, INPUT_BBOX_PROMPT_ID),
      producesAttributes,
      producesClass,
      batchable,
      outputClasses: JSON.parse(outputClassesKey) as string[],
    }),
    // supportedInputsKey 串化 supportedInputs 作稳定依赖。
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [
      supportedInputsKey,
      capabilitiesReady,
      producesAttributes,
      producesClass,
      batchable,
      outputClassesKey,
    ],
  );
  const onCapsRef = useRef(onCaps);
  onCapsRef.current = onCaps;
  useEffect(() => {
    onCapsRef.current?.(id, caps);
  }, [id, caps]);
  useEffect(() => {
    return () => onCapsRef.current?.(id, null);
  }, [id]);

  const badge = RUN_STATE_BADGE[runState] ?? RUN_STATE_BADGE.pending;
  const targeted = stat?.targeted ?? 0;
  const okPct = targeted > 0 ? ((stat?.ok ?? 0) / targeted) * 100 : 0;

  return (
    <Card className={cx(styles.stageCard, hidden && styles.stageHidden)}>
      <div className={styles.stageCardHeader}>
        <span className={styles.stageRole}>
          <Icon name="brain" size={13} />
          <Badge variant="accent">
            {isCropDetectGeometry
              ? "检测"
              : isBoxSegGeometry
                ? "分割"
                : isOcrRecognize
                  ? "识别"
                  : "分类"}
          </Badge>
          <strong className={styles.sectionTitle}>阶段 {displayIndex}</strong>
        </span>
        <span className={styles.stageHeaderRight}>
          <Badge variant={badge.variant} dot={badge.dot}>
            {badge.label}
          </Badge>
          <Button size="sm" variant="ghost" onClick={() => onRemove(id)} title="移除该阶段">
            <Icon name="trash" size={11} />
          </Button>
        </span>
      </div>

      {/* v0.18.8 · 运行态: 进度条 (成功/目标) + 计数块 (StatCard 风格), 替换纯文本行。 */}
      {stat && (
        <div className={styles.stageRun}>
          <ProgressBar value={okPct} />
          <div className={styles.stageCounts}>
            <span className={styles.stageCount}>
              <span className={styles.stageCountLabel}>目标</span>
              <span className={styles.stageCountValue}>{targeted}</span>
            </span>
            <span className={styles.stageCount}>
              <span className={styles.stageCountLabel}>成功</span>
              <span className="text-status-positive">{stat.ok ?? 0}</span>
            </span>
            {(stat.failed ?? 0) > 0 && (
              <span className={styles.stageCount}>
                <span className={styles.stageCountLabel}>失败</span>
                <span className="text-status-caution">{stat.failed}</span>
              </span>
            )}
            {(stat.skipped_geometry ?? 0) > 0 && (
              <span className={cx(styles.stageCount, styles.mutedText)}>
                <span className={styles.stageCountLabel}>几何跳过</span>
                <span>{stat.skipped_geometry}</span>
              </span>
            )}
          </div>
        </div>
      )}

      {/* ML Backend 选择 (下游卡自持, 置于「下游模型」之上): 先选后端 → 再选下游模型 → 父框类别,
          模型版本/参数面板 (PreannotateConfigForm) 移到卡片最底部。原由 PreannotateConfigForm 渲染的
          后端下拉就此上移, 故下方不再向其传 backends/onSelectBackend (避免重复渲染)。 */}
      {backends.length > 1 && (
        <label className={styles.field}>
          <span className={styles.fieldLabel}>ML Backend</span>
          <select
            value={backendId ?? ""}
            onChange={(e) => {
              cfg.setCatalogModel?.("");
              setBackendId(e.target.value || null);
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

      {/* v0.18.12 · 下游模型选择: 同一 backend 暴露多个可作下游的批量原子时显式选 (分类 / 框→分割)。 */}
      <CatalogModelSelect cfg={cfg} />
      {!cfg.selectedCatalogId && downstreamModels.length > 1 && (
        <label className={styles.field}>
          <span className={styles.fieldLabel}>下游模型</span>
          <select
            className={styles.promptInput}
            value={selectedModelId ?? ""}
            onChange={(e) => setSelectedModelId(e.target.value || null)}
          >
            {downstreamModels.map((m) => (
              <option key={m.id} value={m.id}>
                {(m.display_name || m.id) +
                  (m.task === "segment"
                    ? "（框→分割）"
                    : m.task === "ocr"
                      ? "（识别）"
                      : m.task === "detect"
                        ? "（检测）"
                        : "（分类）")}
              </option>
            ))}
          </select>
        </label>
      )}

      {isCropDetectGeometry ? (
        <span className={styles.mutedText}>
          下游模型：{selectedModel?.display_name || selectedModel?.id}（在父框 crop 上检测子物体，
          检出几何回映回原图坐标并追加为新框；可作下游阶段的父）
        </span>
      ) : isBoxSegGeometry ? (
        <span className={styles.mutedText}>
          下游模型：{selectedModel?.display_name || selectedModel?.id}（框→分割：消费上游检测框出
          mask，无需 prompt；上方 prompt/输出形态对本阶段不生效，仅 SAM 变体有效）
        </span>
      ) : isOcrRecognize ? (
        <span className={styles.mutedText}>
          下游模型：{selectedModel?.display_name || selectedModel?.id}（在父框 crop 上识别文本，
          写回 text/orientation/language 属性；上方仅版本/尺寸/语言变体对本阶段有效）
        </span>
      ) : (
        selectedModel && (
          <span className={styles.mutedText}>
            下游模型：{selectedModel.display_name || selectedModel.id}（纯分类，跳过检测）
          </span>
        )
      )}

      {!isGeometryDownstream && showNoAttrWarning && (
        <div className={styles.stageWarn}>
          <Icon name="warning" size={12} />
          <span>
            该后端未自报输出属性，作下游分类只会重新检测、属性恒空。请改选会产属性的后端。
          </span>
        </div>
      )}

      {/* v0.19.3 WS2 · 选中 model 自报 batchable=false (交互/有状态) → 不能批量预标 (端点会 422)。 */}
      {showNotBatchableWarning && (
        <div className={styles.stageWarn}>
          <Icon name="warning" size={12} />
          <span>
            该模型为交互/有状态模型（batchable=false），不能用于批量预标流水线，运行将被端点拒绝。
          </span>
        </div>
      )}

      {/* v0.19.2 WS1 · 选中 model 自报属性类型却不含 class → 跑完属性恒空 (端点会 422)。 */}
      {showNoClassWarning && (
        <div className={styles.stageWarn}>
          <Icon name="warning" size={12} />
          <span>
            该模型不产类别属性（output_attribute_types 不含
            class），作分类下游属性恒空，运行将被端点拒绝。
          </span>
        </div>
      )}

      <div className={styles.field}>
        <span className={styles.fieldLabel}>父框类别（留空=对全部父框跑；按检测框类名匹配）</span>
        <ChipMultiSelect
          // 选项优先取上游筛完的类别 (下游只会见到这些框); 取不到回落项目类别。另支持自由文本输入。
          options={(parentClassOptions?.length ? parentClassOptions : projectClasses).map((c) => ({
            value: c,
            label: c,
          }))}
          selected={classFilter}
          onChange={setClassFilter}
          allowFreeText
          freeTextPlaceholder="输入类名添加（匹配检测框类名）"
          emptyHint="留空=对全部父框跑"
        />
      </div>

      {/* ROI pad 仅 crop 投递有意义; geometry 投递传整图 + 框列表, 不裁 crop。 */}
      {!isBoxSegGeometry && (
        <div className={styles.field}>
          <span className={styles.fieldLabel}>ROI 扩展 pad（0–0.5）</span>
          <input
            className={styles.textInput}
            type="number"
            min={0}
            max={0.5}
            step={0.01}
            value={pad}
            onChange={(e) => {
              const v = parseFloat(e.target.value);
              if (!Number.isNaN(v)) setPad(Math.min(0.5, Math.max(0, v)));
            }}
          />
        </div>
      )}

      {/* v0.18.15 · 子物体命名 (写回属性键前缀): 父是几何阶段时, 给本阶段属性加命名空间,
          如父=hat 检测、本阶段写 color → hat_color。留空=写原始键 (双阶段零退化)。 */}
      {!isGeometryDownstream && (
        <div className={styles.field}>
          <span className={styles.fieldLabel}>
            子物体命名（可选，写回属性键前缀，如 hat → hat_color）
          </span>
          <input
            className={styles.textInput}
            type="text"
            placeholder="留空=不加前缀"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
          />
        </div>
      )}

      {/* 写回属性键仅分类下游有意义; box-seg geometry 下游产 polygon (追加预测), 不写属性。 */}
      {!isGeometryDownstream && (
        <div className={styles.field}>
          <span className={styles.fieldLabel}>
            写回属性键（留空=接收下游返回的全部键）
            {backendAttrOptions.length === 0 && attrKeyOptions.length > 0 && (
              <span className={styles.fieldHint}> · 选项来自项目属性 schema</span>
            )}
          </span>
          <ChipMultiSelect
            options={attrKeyOptions}
            selected={writeKeys}
            onChange={setWriteKeys}
            conflictKeys={conflictKeys}
            emptyHint="下游 backend 未自报属性 schema，且项目无属性字段；留空=接收全部键"
          />
          {/* v0.19.2 WS1 · write.keys 对账: 选的键不在 model 自报 schema 内 → 可能永不产出 (仅警告)。 */}
          {unknownWriteKeys.length > 0 && (
            <div className={styles.stageWarn}>
              <Icon name="warning" size={12} />
              <span>
                属性键 {unknownWriteKeys.join("、")} 不在该模型自报的属性 schema
                中，可能不会被产出。
              </span>
            </div>
          )}
        </div>
      )}

      {/* 模型版本 / 推理参数: 置于卡片最后 —— 先定下游模型与父框类别, 再调该模型的版本/尺寸/阈值。
          backend 选择已上移到顶部, 这里不再传 backends/onSelectBackend (后端下拉由顶部渲染)。
          下游卡恒收起整图「模型任务」下拉与类别白名单 (真值是上方「下游模型」, 整图轴/类别白名单分属
          源整图模型, 与下游阶段无关; payload 已按下游 model 自己的轴过滤、不透传源 class_filter)。 */}
      <PreannotateConfigForm
        cfg={cfg}
        projectMlBackendId={projectMlBackendId}
        hideModelTaskSelector
        hideClassWhitelist={!isCropDetectGeometry}
      />
    </Card>
  );
}
