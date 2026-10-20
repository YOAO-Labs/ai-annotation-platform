/**
 * 预标"配置区"共享状态 hook.
 *
 * 把 ProjectDetailPanel 的批跑配置逻辑 (任务类型 / 几何 task / 类别白名单 / variant / 参数 /
 * prompt / 预设 / 输出形态 + buildArgs) 抽成自包含 hook, 供批量页与工作台 AI 面板共用 (单一事实源)。
 * 批量专属的 backend 多选 / predict_mode / 并发 / 批次选择 / run 编排仍由调用方持有。
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { useToastStore } from "@/components/ui/Toast";
import { useProject, useUpdateProject } from "@/hooks/useProjects";
import { type TextOutputMode, type PredictMode } from "@/hooks/usePreannotation";
import { aliasFrequencyApi } from "@/api/aliasFrequency";
import { hasInput, INPUT_FULL_IMAGE_ID } from "@/api/capabilityInputs";
import { mlBackendsApi, mlBackendSetupQueryKey, type MLModelCapability } from "@/api/ml-backends";
import {
  VARIANT_FIELD_KEYS,
  deriveDefaults,
  type JsonSchemaObject,
} from "@/pages/Workbench/components/SchemaForm";
import { useAiToolParamPrefs } from "@/pages/Workbench/state/useAiToolParamPrefs";
import { isVariantHot, markVariantHot } from "@/pages/Workbench/state/sessionVariantCache";
import {
  derivePanelShape,
  deriveTextPanelShape,
  deriveVariantSource,
  type VariantSource,
} from "../utils/panelShape";
import { useAiParamPresets } from "../utils/useAiParamPresets";
import { isImageDetectionEntry, useProjectDetectionCatalog } from "@/api/modelCatalog";
import { useAuthStore } from "@/stores/authStore";

// v0.14.9 · 文本 / OCR / 文档版面三态任务类型 (按选中 backend 的 models[].task 派生).
export type PreannotateTaskType = "text" | "ocr" | "doc_layout";

// v0.14.17 · YOLO 等"多 task 几何 backend"(闭集, supported_prompts=['none']) 的可选 task.
export const GEOMETRIC_TASKS = ["detect", "segment", "pose", "obb"];

// 整图预标注(单图/批量)只接受能吃 full_image 投递的 model;显式声明 crop-only
// 的识别原子(如 rapidocr 的 ocr-rec,只能吃裁剪图)被排除——否则整图喂进 crop 原子,
// 后端把整图当一个 crop、识别不出有效文本 → 返回空、画布无框(job 仍记成功)。
// supported_inputs 缺字段 = 老 backend,按平台默认视为支持 full_image(向后兼容)。
export function supportsFullImageInput(m: MLModelCapability): boolean {
  const inputs = m.supported_inputs;
  return !inputs || inputs.length === 0 || hasInput(inputs, INPUT_FULL_IMAGE_ID);
}

export interface PreannotateAlias {
  name: string;
  alias: string;
  count: number;
}

/** buildArgs 产出的单次请求字段袋 (不含 task_ids / batch_id, 由调用方补). */
export interface PreannotateArgs {
  ml_backend_id: string;
  task?: string;
  model_id?: string;
  model_variants?: Record<string, string>;
  prompt?: string;
  output_mode?: TextOutputMode;
  params: Record<string, unknown>;
  predict_mode: PredictMode;
  class_filter?: number[];
}

export interface UsePreannotateConfigArgs {
  projectId: string;
  /** 当前选中的 backend id (批量页可多选; 工作台为项目绑定值). null 时多数派生为空/禁用. */
  backendId: string | null;
  /**
   * v0.21.7 · 视频项目的执行单位, 决定源模型过滤:
   * - "video" (整段序列): 源模型只列 tracker (detect-then-track, 落 video_track)。
   * - "frame" (逐帧): 源模型只列图像检测 (yolo detect/seg 逐帧跑, 落 video_bbox)。
   * 图像项目忽略此参数 (恒图像检测)。缺省视为 "video" (视频项目主选择)。
   */
  executionUnit?: "video" | "frame";
  executionScope?: "bulk" | "workbench";
  inputMode?: "full_image" | "crop";
  skipBackendDiscovery?: boolean;
}

function useBackendPreannotateConfig({
  projectId,
  backendId,
  executionUnit,
  skipBackendDiscovery,
}: UsePreannotateConfigArgs) {
  const pushToast = useToastStore((s) => s.push);
  const canReadAliasFrequency = useAuthStore(
    (s) => s.user?.role === "project_admin" || s.user?.role === "super_admin",
  );
  const qc = useQueryClient();

  const projectQ = useProject(projectId);
  const project = projectQ.data as unknown as
    | {
        type_key?: string;
        data_type?: string[] | string | null;
        classes_config?: Record<string, { alias?: string | null }>;
        default_variants?: Record<string, Record<string, string>>;
      }
    | undefined;

  // v0.9.12 · alias 频率排序: prompt 默认勾选项目所有 alias (按预标频率降序).
  const freqQ = useQuery({
    queryKey: ["alias-frequency", projectId],
    queryFn: () => aliasFrequencyApi.byProject(projectId),
    enabled: !!projectId && canReadAliasFrequency,
    staleTime: 1000 * 60 * 5,
  });
  const aliases = useMemo<PreannotateAlias[]>(() => {
    const cfg = project?.classes_config ?? {};
    const freq = freqQ.data?.frequency ?? {};
    return Object.entries(cfg)
      .map(([name, entry]) => ({
        name,
        alias: entry?.alias ?? null,
        count: freq[entry?.alias ?? ""] ?? 0,
      }))
      .filter((e): e is PreannotateAlias => !!e.alias)
      .sort((a, b) => b.count - a.count || a.alias.localeCompare(b.alias));
  }, [project, freqQ.data]);

  // v0.10.38 · 按后端参数面板: 拉选中 backend 的 /setup.params 渲染 SchemaForm.
  const setupQ = useQuery({
    queryKey: mlBackendSetupQueryKey(projectId, backendId),
    queryFn: () => mlBackendsApi.setup(projectId, backendId as string),
    enabled: !!backendId && !skipBackendDiscovery,
    staleTime: 60_000,
    retry: false,
  });
  // v0.14.9 · 能力目录 (health_meta 派生): models[] 含 ocr / doc_layout / 几何条目.
  const capabilitiesQ = useQuery({
    queryKey: ["ml-backends", projectId, backendId, "capabilities"],
    queryFn: () => mlBackendsApi.capabilities(projectId, backendId as string),
    enabled: !!backendId && !skipBackendDiscovery,
    staleTime: 60_000,
    retry: false,
  });

  const ocrModel = useMemo<MLModelCapability | undefined>(
    () =>
      (capabilitiesQ.data?.models ?? []).find((m) => m.task === "ocr" && supportsFullImageInput(m)),
    [capabilitiesQ.data],
  );
  const docLayoutModel = useMemo<MLModelCapability | undefined>(
    () =>
      (capabilitiesQ.data?.models ?? []).find(
        (m) => m.task === "doc_layout" && supportsFullImageInput(m),
      ),
    [capabilitiesQ.data],
  );
  const availableTaskTypes = useMemo<PreannotateTaskType[]>(() => {
    const types: PreannotateTaskType[] = ["text"];
    if (ocrModel) types.push("ocr");
    if (docLayoutModel) types.push("doc_layout");
    return types;
  }, [ocrModel, docLayoutModel]);
  const hasDocTasks = availableTaskTypes.length > 1;

  const [taskType, setTaskType] = useState<PreannotateTaskType>("text");
  useEffect(() => {
    if (!availableTaskTypes.includes(taskType)) setTaskType("text");
  }, [availableTaskTypes, taskType]);
  const isDocMode = taskType === "ocr" || taskType === "doc_layout";

  // v0.20.5 · doc 路径也 model-first(对齐 geometric/text): 当前任务族(ocr / doc_layout)的
  //   全部 full_image 模型(排除 crop-only 识别原子如 ocr-rec)进统一「模型任务」下拉。
  //   此前 doc 模式只 `.find` 第一个、UI 不出选择器, 端到端 e2e 既看不到也选不了。
  const docModels = useMemo<MLModelCapability[]>(
    () =>
      isDocMode
        ? (capabilitiesQ.data?.models ?? []).filter(
            (m) => m.task === taskType && supportsFullImageInput(m),
          )
        : [],
    [isDocMode, taskType, capabilitiesQ.data],
  );
  const [docTaskId, setDocTaskId] = useState<string | null>(null);
  useEffect(() => {
    setDocTaskId((prev) => {
      if (prev && docModels.some((m) => m.id === prev)) return prev;
      // 默认偏端到端(composite, 出框 + 文字), 回退第一个。
      const composite = docModels.find((m) => m.composition === "composite");
      return (composite ?? docModels[0])?.id ?? null;
    });
  }, [backendId, docModels]);
  const activeDocModel = docModels.find((m) => m.id === docTaskId) ?? docModels[0];

  const primaryModel = useMemo<MLModelCapability | undefined>(() => {
    if (isDocMode) return activeDocModel;
    // 单阶段不过滤 composite: 一锅端 (vehicle-attr) 可作开箱即用默认。
    const models = capabilitiesQ.data?.models ?? [];
    return models.find((m) => m.task !== "ocr" && m.task !== "doc_layout") ?? models[0];
  }, [isDocMode, activeDocModel, capabilitiesQ.data]);

  // v0.21.6 / v0.21.7 · 视频项目源模型按**执行单位**分叉 (输入节点顶层选择, 见 母计划对等分叉):
  //   - 整段序列 (executionUnit!=='frame', 默认): 只列 tracker (detect-then-track, 落 video_track);
  //     配置层(变体 series×size / 参数 conf-iou-tracker / 类别白名单)与检测同构, 复用几何 model 机制,
  //     buildArgs 几何分支自然发 task='tracker'。
  //   - 逐帧 (executionUnit==='frame'): 只列图像检测 (GEOMETRIC_TASKS, 排除 tracker), yolo det/seg
  //     逐帧跑、落 video_bbox。
  //   图像项目恒图像检测 (GEOMETRIC_TASKS)。
  const isVideoProject = Array.isArray(project?.data_type)
    ? project.data_type.includes("video")
    : project?.data_type === "video";
  const isVideoTracking = isVideoProject && executionUnit !== "frame";
  const geometricModels = useMemo<MLModelCapability[]>(
    () =>
      (capabilitiesQ.data?.models ?? []).filter((m) =>
        isVideoTracking ? m.task === "tracker" : GEOMETRIC_TASKS.includes(m.task ?? ""),
      ),
    [capabilitiesQ.data, isVideoTracking],
  );
  const isGeometricBackend =
    !isDocMode &&
    geometricModels.length > 0 &&
    !(primaryModel?.supported_prompts ?? []).includes("text");
  const [geometricTaskId, setGeometricTaskId] = useState<string | null>(null);
  useEffect(() => {
    setGeometricTaskId((prev) =>
      prev && geometricModels.some((m) => m.id === prev) ? prev : (geometricModels[0]?.id ?? null),
    );
  }, [backendId, geometricModels]);
  const geometricModel =
    geometricModels.find((m) => m.id === geometricTaskId) ?? geometricModels[0];

  // v0.18.12 · 文本路径 (gsam2 / sam3 开集) 也 model-first: 列出该 backend 的「文本 task」model
  //   (含 text prompt、非交互、public 的 detection / segmentation), 作 task 选择器。
  //   统一 wire: 选中 model → 发 model_id + model_variants (替代旧的「一个 backend + output 开关」)。
  const textModels = useMemo<MLModelCapability[]>(
    () =>
      (capabilitiesQ.data?.models ?? []).filter(
        (m) =>
          !m.is_interactive &&
          (m.supported_prompts ?? []).includes("text") &&
          GEOMETRIC_TASKS.includes(m.task ?? ""),
      ),
    [capabilitiesQ.data],
  );
  const [textTaskId, setTextTaskId] = useState<string | null>(null);
  useEffect(() => {
    // 默认任务: image-det 项目偏检测 (出框), 其余偏分割 (出掩膜); 回退第一个。
    const preferSeg = project?.type_key !== "image-det";
    setTextTaskId((prev) => {
      if (prev && textModels.some((m) => m.id === prev)) return prev;
      const seg = textModels.find((m) => m.task === "segment");
      const det = textModels.find((m) => m.task === "detect");
      return (preferSeg ? (seg ?? det) : (det ?? seg))?.id ?? textModels[0]?.id ?? null;
    });
  }, [backendId, textModels, project?.type_key]);
  const textModel = textModels.find((m) => m.id === textTaskId) ?? textModels[0];

  // v0.14.18 / v0.18.12 · 当前路径的 variant 来源: doc → 文档 model; 几何 → 选中 task model;
  //   文本 (gsam2 / sam3) → **选中文本 task model** 的逐 model 变体 (检测=dino; 分割=sam+dino),
  //   取代旧顶层 union (选检测时不再白显 SAM)。textModel 缺位回落顶层 (能力未就位兜底)。
  const isTextPath = !isDocMode && !isGeometricBackend;

  // v0.20.5 · 统一「模型任务」选择层(对齐所有 backend): 不再为 OCR/文档单设「任务类型」tab,
  //   把该 backend 全部可批量预标的 full_image 模型(几何检测/分割 + OCR/版面, 排除交互式与
  //   crop-only 原子如 ocr-rec)铺进同一个下拉。选中 model 后由其 task 派发到内部三路机制
  //   (taskType + 对应路径选中 id),prompt/类别白名单/变体/输出/buildArgs 仍按原路径生效。
  const selectableModels = useMemo<MLModelCapability[]>(() => {
    const seen = new Set<string>();
    const out: MLModelCapability[] = [];
    for (const m of capabilitiesQ.data?.models ?? []) {
      if (m.is_interactive) continue;
      // v0.21.7 · 整段序列 (video tracking): 源下拉**只列 tracker** (吃 video 输入, 跳 full_image 门)。
      if (isVideoTracking) {
        if (m.task !== "tracker" || seen.has(m.id)) continue;
        seen.add(m.id);
        out.push(m);
        continue;
      }
      // 逐帧 / 图像: 非 tracker 几何 (det/seg/...) + doc, 须支持 full_image。
      if (!supportsFullImageInput(m)) continue;
      const isGeo = GEOMETRIC_TASKS.includes(m.task ?? "");
      const isDoc = m.task === "ocr" || m.task === "doc_layout";
      if ((!isGeo && !isDoc) || seen.has(m.id)) continue;
      seen.add(m.id);
      out.push(m);
    }
    return out;
  }, [capabilitiesQ.data, isVideoTracking]);
  const selectedModelId = isDocMode
    ? (activeDocModel?.id ?? null)
    : isGeometricBackend
      ? (geometricModel?.id ?? null)
      : (textModel?.id ?? null);
  const selectTaskModel = (id: string) => {
    const m = selectableModels.find((x) => x.id === id);
    if (!m) return;
    if (m.task === "ocr" || m.task === "doc_layout") {
      setTaskType(m.task as PreannotateTaskType);
      setDocTaskId(id);
    } else {
      // 几何/文本路径同走 taskType="text"; 两路选中 id 同步置位, 由 isGeometricBackend 裁决取哪个。
      setTaskType("text");
      setGeometricTaskId(id);
      setTextTaskId(id);
    }
  };

  // 默认选中: 当前推导出的 selectedModelId 为空/失效(如纯 OCR backend 无几何/文本 model,
  //   而 taskType 默认 "text" → 文本路径空)→ 取第一个可选 model 并派发其 taskType, 避免面板悬空。
  useEffect(() => {
    if (!selectableModels.length) return;
    if (selectedModelId && selectableModels.some((m) => m.id === selectedModelId)) return;
    const first = selectableModels[0];
    if (first.task === "ocr" || first.task === "doc_layout") {
      setTaskType(first.task as PreannotateTaskType);
      setDocTaskId(first.id);
    } else {
      setTaskType("text");
      setGeometricTaskId(first.id);
      setTextTaskId(first.id);
    }
  }, [backendId, selectableModels, selectedModelId]);

  const variantSource = useMemo<VariantSource>(
    () =>
      deriveVariantSource({
        isDocMode,
        isGeometricBackend,
        activeDocModel,
        geometricModel,
        textModel,
        topSupportedVariants:
          setupQ.data?.supported_variants ?? capabilitiesQ.data?.supported_variants,
      }),
    [
      isDocMode,
      isGeometricBackend,
      activeDocModel,
      geometricModel,
      textModel,
      setupQ.data,
      capabilitiesQ.data,
    ],
  );

  // 输出形态: 文本路径按**选中文本 model** 的 supported_text_outputs 派生 (检测→仅 box 强制隐藏;
  //   分割→{mask,both} 子开关, 见 textOutputOptions); 其余仍按选中 model 几何输出。
  const panelShape = useMemo(() => {
    if (isTextPath) {
      return deriveTextPanelShape(textModel?.supported_text_outputs);
    }
    return derivePanelShape(primaryModel, isDocMode);
  }, [isTextPath, isDocMode, primaryModel, textModel]);

  // v0.18.12 · 文本路径输出子选项 (task-first 去重): 选中分割 model → {掩膜, 全部} 二选 (删「框」,
  //   因「分割@框」与「检测」等价冗余); 选中检测 model → 无子选项 (强制 box, 结构即隐藏 SAM)。
  //   值取分割 model 的 supported_text_outputs 中的 mask/both (排除 box)。
  const textOutputOptions = useMemo<TextOutputMode[]>(() => {
    if (!isTextPath || textModel?.task !== "segment") return [];
    const outs = new Set(textModel?.supported_text_outputs ?? ["mask", "both"]);
    return (["mask", "both"] as TextOutputMode[]).filter((o) => outs.has(o));
  }, [isTextPath, textModel]);

  // v0.14.17 · 类别白名单: 选中的模型原生类别 index 子集 (空集=全部). 随 task 切换重置.
  const [selectedClassIdx, setSelectedClassIdx] = useState<Set<number>>(new Set());
  useEffect(() => {
    setSelectedClassIdx(new Set());
  }, [backendId, geometricTaskId]);

  // 文本任务用 /setup.params; OCR / 版面用所选 model 条目自带的 params schema。
  // v0.21.6 · 几何路径改用**选中几何 model** 的 per-model params (每条 model entry 都自带,
  //   见 yolo-backend `_build_model_entry`), 回落 /setup 顶层。此前恒用顶层 (= 检测的 conf/iou/max_det),
  //   导致 tracker 的「追踪算法」enum、obb 的专属 params 等 per-model 字段被吞。检测/分割的 per-model
  //   与顶层同构, 零回归。
  const paramsSchema = (
    isDocMode
      ? activeDocModel?.params
      : isGeometricBackend
        ? (geometricModel?.params ?? setupQ.data?.params)
        : setupQ.data?.params
  ) as JsonSchemaObject | undefined;
  const paramKeys = Object.keys(paramsSchema?.properties ?? {});
  const hasAnyParams = paramKeys.length > 0;
  const hasNonVariantParams = paramKeys.some(
    (key) => !VARIANT_FIELD_KEYS.includes(key as (typeof VARIANT_FIELD_KEYS)[number]),
  );
  const { savedParams, save: saveParams } = useAiToolParamPrefs(backendId);
  const [paramsValue, setParamsValue] = useState<Record<string, unknown>>({});

  // v0.14.13 · 项目级 variant 偏好 merge backend 自报默认 (按当前路径的 variant 来源, 见 variantSource).
  const variantDefaults = useMemo<Record<string, string>>(() => {
    const fromBackend = variantSource.defaults ?? {};
    const fromProject = (backendId ? project?.default_variants?.[backendId] : undefined) ?? {};
    return { ...fromBackend, ...fromProject };
  }, [variantSource, project?.default_variants, backendId]);

  useEffect(() => {
    if (!backendId) return;
    setParamsValue({
      ...deriveDefaults(paramsSchema),
      ...variantDefaults,
      ...(savedParams ?? {}),
    });
  }, [backendId, paramsSchema, savedParams, variantDefaults]);

  const onParamsChange = (next: Record<string, unknown>) => {
    setParamsValue(next);
    saveParams(next);
  };

  // v0.14.13 · variant 写回项目级偏好 (debounced 跟随 PATCH).
  const updateProjectMu = useUpdateProject(projectId);
  const variantAxisKeysRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    variantAxisKeysRef.current = new Set(
      (variantSource.groups ?? [])
        .map((g) => g.key)
        .filter((k): k is string => typeof k === "string"),
    );
  }, [variantSource]);
  const patchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (patchTimerRef.current) clearTimeout(patchTimerRef.current);
    },
    [],
  );

  const onVariantOrParamsChange = (next: Record<string, unknown>) => {
    setParamsValue(next);
    saveParams(next);
    if (!backendId) return;
    const axisKeys = variantAxisKeysRef.current;
    if (axisKeys.size === 0) return;
    const variantSlice: Record<string, string> = {};
    for (const k of axisKeys) {
      const v = next[k];
      if (typeof v === "string") variantSlice[k] = v;
    }
    const currentProjectSlice = project?.default_variants?.[backendId] ?? {};
    const same =
      Object.keys(variantSlice).length === Object.keys(currentProjectSlice).length &&
      Object.entries(variantSlice).every(([k, v]) => currentProjectSlice[k] === v);
    if (same) return;
    const merged: Record<string, Record<string, string>> = {
      ...(project?.default_variants ?? {}),
      [backendId]: variantSlice,
    };
    if (patchTimerRef.current) clearTimeout(patchTimerRef.current);
    patchTimerRef.current = setTimeout(() => {
      updateProjectMu.mutate({ default_variants: merged });
    }, 300);
  };

  // v0.14.16 · 命名预设 (variant + params 快照, 按 backend×task 分桶 localStorage).
  const {
    presets,
    save: savePreset,
    remove: removePreset,
  } = useAiParamPresets(backendId, taskType);
  const applyPreset = (values: Record<string, unknown>) => {
    onVariantOrParamsChange({ ...deriveDefaults(paramsSchema), ...variantDefaults, ...values });
  };

  // v0.14.17 · 手动预热: 加载该 task 的类别表 (model.names) 以勾选类别白名单.
  const warmMut = useMutation({
    mutationFn: () => {
      const variants: Record<string, string> = {};
      for (const k of variantAxisKeysRef.current) {
        const v = paramsValue[k];
        if (typeof v === "string") variants[k] = v;
      }
      return mlBackendsApi.warmup(projectId, backendId as string, {
        task: geometricModel?.task,
        variants,
      });
    },
    onSuccess: () => {
      pushToast({ msg: "已预热，正在加载类别…", kind: "success" });
      qc.invalidateQueries({
        queryKey: ["ml-backends", projectId, backendId, "capabilities"],
      });
      qc.invalidateQueries({
        queryKey: mlBackendSetupQueryKey(projectId, backendId),
      });
    },
    onError: (err) => pushToast({ msg: "预热失败", sub: (err as Error)?.message, kind: "error" }),
  });

  // ── prompt (开放词表文本任务) ──
  const [prompt, setPrompt] = useState("");
  const [outputMode, setOutputMode] = useState<TextOutputMode>("mask");
  const promptTokenSet = useMemo(() => {
    const tokens = prompt
      .split(",")
      .map((t) => t.trim().toLowerCase())
      .filter(Boolean);
    return new Set(tokens);
  }, [prompt]);
  const toggleAlias = (alias: string) => {
    const a = alias.trim();
    if (!a) return;
    const aLower = a.toLowerCase();
    if (promptTokenSet.has(aLower)) {
      const next = prompt
        .split(",")
        .map((t) => t.trim())
        .filter((t) => t && t.toLowerCase() !== aLower)
        .join(", ");
      setPrompt(next);
    } else {
      const trimmed = prompt.trim().replace(/,\s*$/, "");
      setPrompt(trimmed ? `${trimmed}, ${a}` : a);
    }
  };

  // 项目切换时重置 outputMode (按 type_key 分流) + prompt.
  useEffect(() => {
    setOutputMode(project?.type_key === "image-det" ? "box" : "mask");
    setPrompt("");
    defaultPromptAppliedRef.current = "";
  }, [projectId, project?.type_key]);

  // v0.18.12 · 文本 task 切到分割且 outputMode 残留 box (检测态) → 回落 mask;
  //   子开关只 {掩膜, 全部}, 不留无匹配项的 box。检测态由 buildArgs 强制 box, 无需在此处理。
  useEffect(() => {
    if (isTextPath && textModel?.task === "segment" && outputMode === "box") {
      setOutputMode("mask");
    }
  }, [isTextPath, textModel, outputMode]);

  // v0.9.12 · aliases 就绪且 prompt 仍空时, 默认勾选所有 alias 拼成逗号分隔 (按频率降序).
  const defaultPromptAppliedRef = useRef<string>("");
  useEffect(() => {
    if (!projectId) return;
    if (defaultPromptAppliedRef.current === projectId) return;
    if (!freqQ.isFetched) return;
    if (aliases.length === 0) return;
    if (prompt.trim()) {
      defaultPromptAppliedRef.current = projectId;
      return;
    }
    setPrompt(aliases.map((a) => a.alias).join(", "));
    defaultPromptAppliedRef.current = projectId;
  }, [projectId, aliases, prompt, freqQ.isFetched]);

  // v0.14.13 · 当前 variant 选择是否已 warm (用于按钮文案分两态).
  const currentVariantSlice = useMemo<Record<string, string>>(() => {
    const out: Record<string, string> = {};
    for (const k of variantAxisKeysRef.current) {
      const v = paramsValue[k];
      if (typeof v === "string") out[k] = v;
    }
    return out;
    // variantSource 变化 → variantAxisKeysRef (ref, 非响应) 可能更新, 显式入依赖一并重算.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paramsValue, variantSource]);
  // markHot 写入模块级缓存 (sessionVariantCache) 后, 仅靠 [backendId, currentVariantSlice] 无法
  // 感知缓存写入 → 同一 variant 连续跑两次时, 第二次仍读到旧的 false (按钮误显"加载模型中…")。
  // 用 warmTick 显式触发重算: markHot 写缓存后 bump, 让 isCurrentVariantWarm 重读缓存。
  const [warmTick, setWarmTick] = useState(0);
  const isCurrentVariantWarm = useMemo(() => {
    if (!backendId) return false;
    return isVariantHot(backendId, currentVariantSlice);
    // warmTick: markHot 后重算 (见 markHot); 否则模块缓存写入不被 useMemo 感知。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backendId, currentVariantSlice, warmTick]);

  // 换 variant (series/size 等) 视为换模型: 重置类别白名单选择, 避免旧 variant 的勾选套到新模型.
  // (注: backend 的类别表按 task 缓存, 同 task 各 variant 类别一致时白名单内容不变; 此处仅清选择.)
  const variantKey = JSON.stringify(currentVariantSlice);
  useEffect(() => {
    setSelectedClassIdx(new Set());
  }, [variantKey]);

  /** 配置层的"可运行"判定 (不含批次/任务选择, 由调用方自行 && 上). */
  const configReady = !!backendId && (isDocMode || isGeometricBackend || !!prompt.trim());

  /** 构造单次请求字段袋 (ml_backend_id + 模式相关字段 + predict_mode); 不含 task_ids/batch_id. */
  const buildArgs = (predictMode: PredictMode): PreannotateArgs | null => {
    if (!backendId) return null;
    const effectiveOutputMode = panelShape.forcedOutputMode ?? outputMode;
    const variantSlice: Record<string, string> = {};
    const nonVariantParams: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(paramsValue)) {
      if (variantAxisKeysRef.current.has(k)) {
        if (typeof v === "string") variantSlice[k] = v;
      } else {
        nonVariantParams[k] = v;
      }
    }
    if (isGeometricBackend) {
      return {
        ml_backend_id: backendId,
        task: geometricModel?.task,
        model_id: geometricModel?.id,
        model_variants: variantSlice,
        params: nonVariantParams,
        predict_mode: predictMode,
        // 类别白名单: 空集 = 全部类别 (不下发 class_filter).
        ...(selectedClassIdx.size > 0
          ? { class_filter: Array.from(selectedClassIdx).sort((a, b) => a - b) }
          : {}),
      };
    }
    if (isDocMode) {
      return {
        ml_backend_id: backendId,
        model_id: activeDocModel?.id,
        task: taskType,
        // v0.20.5 · 把变体轴(version/size/lang)从 params 拆出、按 model_variants 下发,
        //   否则后端 resolve() 读不到、永远跑默认档(此前 doc 分支漏发 model_variants)。
        model_variants: variantSlice,
        params: nonVariantParams,
        predict_mode: predictMode,
      };
    }
    // 文本路径 (gsam2 / sam3): v0.18.12 统一 wire 发 model_id + task + model_variants。
    //   输出形态: 检测 model 恒 box; 分割 model 取子开关 outputMode (mask/both)。
    //   variantSlice 已只含选中文本 model 的轴 (variantSource 按 textModel 派生); 阈值走 nonVariantParams。
    return {
      ml_backend_id: backendId,
      model_id: textModel?.id,
      task: textModel?.task,
      model_variants: variantSlice,
      prompt: prompt.trim(),
      output_mode: textModel?.task === "detect" ? "box" : effectiveOutputMode,
      params: nonVariantParams,
      predict_mode: predictMode,
    };
  };

  // v0.19.3 WS2 · 当前激活的「源模型」(批量预标流水线的根, 与 buildArgs 选 model 一致)。
  //   自报 resource_profile.batchable=false (交互/有状态) → 不能批量预标, 给非阻断预警
  //   (与端点 _assert_capabilities 源阶段 422 对齐; config-time 前移, 不硬挡)。
  const sourceModel = isGeometricBackend ? geometricModel : isDocMode ? activeDocModel : textModel;
  const sourceBatchableWarning =
    sourceModel?.resource_profile?.batchable === false
      ? "该模型为交互/有状态模型（batchable=false），不能用于批量预标流水线"
      : null;

  /** 推理成功后兜底标热 (异步 trigger 拿不到 cache_hit). */
  const markHot = () => {
    if (!backendId) return;
    if (Object.keys(currentVariantSlice).length > 0) {
      markVariantHot(backendId, currentVariantSlice);
      setWarmTick((n) => n + 1); // 触发 isCurrentVariantWarm 重算, 否则模块缓存写入不被感知
    }
  };

  return {
    backendId,
    // 数据查询态
    setupQ,
    capabilitiesQ,
    // 任务类型
    taskType,
    setTaskType,
    availableTaskTypes,
    hasDocTasks,
    isDocMode,
    activeDocModel,
    // v0.20.5 · 统一「模型任务」选择层: 所有 backend(含 OCR/版面)共用一个下拉。
    selectableModels,
    selectedModelId,
    selectTaskModel,
    // 几何 task
    isGeometricBackend,
    geometricModels,
    geometricModel,
    geometricTaskId,
    setGeometricTaskId,
    // v0.18.12 · 文本 task (gsam2 / sam3 model-first): 检测 / 分割 model 选择 + 分割输出子选项。
    isTextPath,
    textModels,
    textModel,
    textTaskId,
    setTextTaskId,
    textOutputOptions,
    // 类别白名单
    selectedClassIdx,
    setSelectedClassIdx,
    warmMut,
    // 模型 / 面板形态
    primaryModel,
    panelShape,
    paramsSchema,
    hasAnyParams,
    hasNonVariantParams,
    // v0.14.18 · 当前路径的 variant 来源 (文本路径=顶层两组; 几何/doc=选中 model). 供 VariantSelector.
    variantGroups: variantSource.groups,
    variantCombinations: variantSource.combinations,
    // variant / 参数
    paramsValue,
    variantDefaults,
    onParamsChange,
    onVariantOrParamsChange,
    // 预设
    presets,
    savePreset,
    removePreset,
    applyPreset,
    // prompt
    prompt,
    setPrompt,
    promptTokenSet,
    toggleAlias,
    aliases,
    // 输出形态
    outputMode,
    setOutputMode,
    // v0.19.3 WS2 · 源模型 batchable 非阻断预警 (null = 无)。
    sourceBatchableWarning,
    isVideoProject,
    // 运行
    configReady,
    buildArgs,
    markHot,
    currentVariantSlice,
    isCurrentVariantWarm,
  };
}

/** A catalog choice is separate from the backend's legacy saved configuration. */
export function usePreannotateConfig(args: UsePreannotateConfigArgs) {
  const catalogQ = useProjectDetectionCatalog(args.projectId);
  const owner = useAuthStore((state) => state.user?.id);
  const scopeKey = `${owner}:${args.projectId}`;
  const [choice, setChoice] = useState({ scope: scopeKey, id: "" });
  const selectedCatalogId = choice.scope === scopeKey ? choice.id : "";
  const setCatalogModel = (id: string) => setChoice({ scope: scopeKey, id });
  useEffect(() => {
    setChoice({ scope: scopeKey, id: "" });
  }, [scopeKey]);
  const release = catalogQ.data?.current_release;
  const entryId = selectedCatalogId === "project-release" ? release?.entry_id : selectedCatalogId;
  const selectedCatalogEntry = catalogQ.data?.items.find((entry) => entry.id === entryId);
  const operation =
    args.executionScope === "workbench"
      ? "workbench"
      : args.inputMode === "crop"
        ? "pipeline"
        : "preannotate";
  const placement = selectedCatalogEntry?.placements?.find(
    (item) =>
      item.ready &&
      (selectedCatalogId !== "project-release" || item.deployment_id === release?.deployment_id),
  );
  const backendId = selectedCatalogId ? (placement?.registry_id ?? null) : args.backendId;
  const legacy = useBackendPreannotateConfig({
    ...args,
    backendId,
    skipBackendDiscovery: !!selectedCatalogId,
  });
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [classes, setClasses] = useState<Set<number>>(new Set());
  const schema = (
    selectedCatalogEntry?.source === "project_trained"
      ? placement?.params
      : selectedCatalogEntry?.capabilities.params
  ) as JsonSchemaObject | undefined;
  const catalogPresets = useAiParamPresets(
    selectedCatalogId ? `${backendId}:${selectedCatalogId}` : null,
    "text",
  );
  useEffect(() => {
    setValues(deriveDefaults(schema));
    setClasses(new Set());
  }, [
    scopeKey,
    selectedCatalogId,
    selectedCatalogEntry?.id,
    selectedCatalogEntry?.class_metadata.classes_digest,
    schema,
  ]);
  const catalogReason = !selectedCatalogId
    ? null
    : legacy.isVideoProject
      ? "目录模型暂只支持图片项目；视频请使用后端配置"
      : !selectedCatalogEntry
        ? catalogQ.isLoading
          ? "正在读取模型…"
          : "所选模型已不可见，请重新选择"
        : !isImageDetectionEntry(selectedCatalogEntry)
          ? "此目录入口目前只支持闭集图片检测；其他任务请使用后端配置"
          : !selectedCatalogEntry.operations[operation]?.allowed
            ? (selectedCatalogEntry.operations[operation]?.reason ?? "该模型不适用于此操作")
            : !placement
              ? "所选模型没有可用的部署实例"
              : null;
  const catalogModels =
    catalogQ.data?.items.filter(
      (entry) =>
        isImageDetectionEntry(entry) &&
        entry.publication !== "candidate" &&
        entry.publication !== "archived" &&
        entry.capabilities.resource_profile?.batchable !== false &&
        (args.inputMode !== "crop" || entry.capabilities.composition !== "composite"),
    ) ?? [];
  const catalogFields = {
    catalogQ,
    catalogModels,
    selectedCatalogId,
    selectedCatalogEntry,
    setCatalogModel,
    catalogReason,
  };
  if (!selectedCatalogId) return { ...legacy, ...catalogFields };
  const capability = selectedCatalogEntry?.capabilities as MLModelCapability | undefined;
  const ready = !!capability && !catalogReason && !!backendId;
  return {
    ...legacy,
    ...catalogFields,
    backendId,
    presets: catalogPresets.presets,
    savePreset: catalogPresets.save,
    removePreset: catalogPresets.remove,
    applyPreset: (next: Record<string, unknown>) =>
      setValues({ ...deriveDefaults(schema), ...next }),
    markHot: () => {},
    isCurrentVariantWarm: false,
    isDocMode: false,
    isGeometricBackend: true,
    isTextPath: false,
    primaryModel: capability,
    geometricModel: capability,
    sourceBatchableWarning: null,
    selectedModelId: selectedCatalogEntry?.model_id ?? null,
    paramsSchema: schema,
    paramsValue: values,
    hasAnyParams: Object.keys(schema?.properties ?? {}).length > 0,
    hasNonVariantParams: Object.keys(schema?.properties ?? {}).length > 0,
    variantGroups: [],
    variantCombinations: undefined,
    variantDefaults: {},
    currentVariantSlice: selectedCatalogEntry?.model_variants ?? {},
    selectedClassIdx: classes,
    setSelectedClassIdx: setClasses,
    onParamsChange: setValues,
    onVariantOrParamsChange: setValues,
    panelShape: derivePanelShape(capability, false),
    configReady: ready,
    buildArgs: (predictMode: PredictMode): PreannotateArgs | null => {
      if (!ready || !selectedCatalogEntry || !backendId) return null;
      return {
        ml_backend_id: backendId,
        task: "detect",
        model_id:
          selectedCatalogId === "project-release"
            ? "project-release"
            : selectedCatalogEntry.model_id,
        model_variants: selectedCatalogEntry.model_variants ?? {},
        params: Object.fromEntries(
          Object.entries(values).filter(([key]) => key in (schema?.properties ?? {})),
        ),
        predict_mode: predictMode,
        ...(classes.size ? { class_filter: [...classes].sort((a, b) => a - b) } : {}),
      };
    },
  };
}

export type PreannotateConfig = ReturnType<typeof usePreannotateConfig>;
