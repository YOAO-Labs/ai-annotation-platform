import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { useToastStore } from "@/components/ui/Toast";
import { useProject, useUpdateProject } from "@/hooks/useProjects";
import { type TextOutputMode, type PredictMode } from "@/hooks/usePreannotation";
import { aliasFrequencyApi } from "@/api/aliasFrequency";
import { hasInput, INPUT_CROP_ID, INPUT_FULL_IMAGE_ID } from "@/api/capabilityInputs";
import { mlBackendsApi, mlBackendSetupQueryKey, type MLModelCapability } from "@/api/ml-backends";
import {
  VARIANT_FIELD_KEYS,
  deriveDefaults,
  isVariantField,
  type JsonSchemaField,
  type JsonSchemaObject,
} from "@/pages/Workbench/components/SchemaForm";
import { useAiToolParamPrefs } from "@/pages/Workbench/state/useAiToolParamPrefs";
import {
  derivePanelShape,
  deriveTextPanelShape,
  deriveVariantSource,
  type VariantSource,
} from "../utils/panelShape";
import { useAiParamPresets } from "../utils/useAiParamPresets";
import { isImageDetectionEntry, useProjectDetectionCatalog } from "@/api/modelCatalog";
import { useAuthStore } from "@/stores/authStore";

export type PreannotateTaskType = "text" | "ocr" | "doc_layout";

export const GEOMETRIC_TASKS = ["detect", "segment", "pose", "obb"];

export function supportsFullImageInput(m: MLModelCapability): boolean {
  const inputs = m.supported_inputs;
  return !inputs || inputs.length === 0 || hasInput(inputs, INPUT_FULL_IMAGE_ID);
}

export interface PreannotateAlias {
  name: string;
  alias: string;
  count: number;
}

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
  backendId: string | null;
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

  const setupQ = useQuery({
    queryKey: mlBackendSetupQueryKey(projectId, backendId),
    queryFn: () => mlBackendsApi.setup(projectId, backendId as string),
    enabled: !!backendId && !skipBackendDiscovery,
    staleTime: 60_000,
    retry: false,
  });
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
      const composite = docModels.find((m) => m.composition === "composite");
      return (composite ?? docModels[0])?.id ?? null;
    });
  }, [backendId, docModels]);
  const activeDocModel = docModels.find((m) => m.id === docTaskId) ?? docModels[0];

  const primaryModel = useMemo<MLModelCapability | undefined>(() => {
    if (isDocMode) return activeDocModel;
    const models = capabilitiesQ.data?.models ?? [];
    return models.find((m) => m.task !== "ocr" && m.task !== "doc_layout") ?? models[0];
  }, [isDocMode, activeDocModel, capabilitiesQ.data]);

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
    const preferSeg = project?.type_key !== "image-det";
    setTextTaskId((prev) => {
      if (prev && textModels.some((m) => m.id === prev)) return prev;
      const seg = textModels.find((m) => m.task === "segment");
      const det = textModels.find((m) => m.task === "detect");
      return (preferSeg ? (seg ?? det) : (det ?? seg))?.id ?? textModels[0]?.id ?? null;
    });
  }, [backendId, textModels, project?.type_key]);
  const textModel = textModels.find((m) => m.id === textTaskId) ?? textModels[0];

  const isTextPath = !isDocMode && !isGeometricBackend;

  const selectableModels = useMemo<MLModelCapability[]>(() => {
    const seen = new Set<string>();
    const out: MLModelCapability[] = [];
    for (const m of capabilitiesQ.data?.models ?? []) {
      if (m.is_interactive) continue;
      if (isVideoTracking) {
        if (m.task !== "tracker" || seen.has(m.id)) continue;
        seen.add(m.id);
        out.push(m);
        continue;
      }
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
      setTaskType("text");
      setGeometricTaskId(id);
      setTextTaskId(id);
    }
  };

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

  const panelShape = useMemo(() => {
    if (isTextPath) {
      return deriveTextPanelShape(textModel?.supported_text_outputs);
    }
    return derivePanelShape(primaryModel, isDocMode);
  }, [isTextPath, isDocMode, primaryModel, textModel]);

  const textOutputOptions = useMemo<TextOutputMode[]>(() => {
    if (!isTextPath || textModel?.task !== "segment") return [];
    const outs = new Set(textModel?.supported_text_outputs ?? ["mask", "both"]);
    return (["mask", "both"] as TextOutputMode[]).filter((o) => outs.has(o));
  }, [isTextPath, textModel]);

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

  const variantDefaults = useMemo<Record<string, string>>(() => {
    const fromBackend = variantSource.defaults ?? {};
    const fromProject = (backendId ? project?.default_variants?.[backendId] : undefined) ?? {};
    return { ...fromBackend, ...fromProject };
  }, [variantSource, project?.default_variants, backendId]);

  const paramsValue = useMemo(
    () => ({ ...deriveDefaults(paramsSchema), ...variantDefaults, ...savedParams }),
    [paramsSchema, variantDefaults, savedParams],
  );

  const updateProjectMu = useUpdateProject(projectId);
  const variantAxisKeys = useMemo(
    () =>
      new Set(
        (variantSource.groups ?? [])
          .map((g) => g.key)
          .filter((k): k is string => typeof k === "string")
          .sort(),
      ),
    [variantSource.groups],
  );
  const currentVariantSlice = useMemo<Record<string, string>>(() => {
    const out: Record<string, string> = {};
    for (const k of variantAxisKeys) {
      const value = paramsValue[k];
      if (typeof value === "string") out[k] = value;
    }
    return out;
  }, [paramsValue, variantAxisKeys]);
  const patchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (patchTimerRef.current) clearTimeout(patchTimerRef.current);
    },
    [],
  );

  const onVariantOrParamsChange = (next: Record<string, unknown>) => {
    saveParams(next);
    if (!backendId) return;
    if (variantAxisKeys.size === 0) return;
    const variantSlice: Record<string, string> = {};
    for (const k of variantAxisKeys) {
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

  const {
    presets,
    save: savePreset,
    remove: removePreset,
  } = useAiParamPresets(backendId, taskType);
  const applyPreset = (values: Record<string, unknown>) => {
    onVariantOrParamsChange({ ...deriveDefaults(paramsSchema), ...variantDefaults, ...values });
  };

  const warmMut = useMutation({
    mutationFn: () =>
      mlBackendsApi.warmup(projectId, backendId as string, {
        task: geometricModel?.task,
        variants: currentVariantSlice,
      }),
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

  useEffect(() => {
    setOutputMode(project?.type_key === "image-det" ? "box" : "mask");
    setPrompt("");
    defaultPromptAppliedRef.current = "";
  }, [projectId, project?.type_key]);

  useEffect(() => {
    if (isTextPath && textModel?.task === "segment" && outputMode === "box") {
      setOutputMode("mask");
    }
  }, [isTextPath, textModel, outputMode]);

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

  const classIdentity = JSON.stringify([
    projectId,
    backendId,
    isGeometricBackend ? geometricModel?.id : null,
    currentVariantSlice,
  ]);
  const [classSelection, setClassSelection] = useState(() => ({
    identity: classIdentity,
    indices: new Set<number>(),
  }));
  if (classSelection.identity !== classIdentity) {
    setClassSelection({ identity: classIdentity, indices: new Set() });
  }
  const selectedClassIdx = classSelection.indices;
  const setSelectedClassIdx = (indices: Set<number>) =>
    setClassSelection({ identity: classIdentity, indices });

  const configReady = !!backendId && (isDocMode || isGeometricBackend || !!prompt.trim());

  const buildArgs = (predictMode: PredictMode): PreannotateArgs | null => {
    if (!backendId) return null;
    const effectiveOutputMode = panelShape.forcedOutputMode ?? outputMode;
    const nonVariantParams: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(paramsValue)) {
      if (!variantAxisKeys.has(k)) nonVariantParams[k] = v;
    }
    if (isGeometricBackend) {
      return {
        ml_backend_id: backendId,
        task: geometricModel?.task,
        model_id: geometricModel?.id,
        model_variants: currentVariantSlice,
        params: nonVariantParams,
        predict_mode: predictMode,
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
        model_variants: currentVariantSlice,
        params: nonVariantParams,
        predict_mode: predictMode,
      };
    }
    return {
      ml_backend_id: backendId,
      model_id: textModel?.id,
      task: textModel?.task,
      model_variants: currentVariantSlice,
      prompt: prompt.trim(),
      output_mode: textModel?.task === "detect" ? "box" : effectiveOutputMode,
      params: nonVariantParams,
      predict_mode: predictMode,
    };
  };

  const sourceModel = isGeometricBackend ? geometricModel : isDocMode ? activeDocModel : textModel;
  const sourceBatchableWarning =
    sourceModel?.resource_profile?.batchable === false
      ? "该模型为交互/有状态模型（batchable=false），不能用于批量预标流水线"
      : null;

  return {
    backendId,
    setupQ,
    capabilitiesQ,
    taskType,
    setTaskType,
    availableTaskTypes,
    hasDocTasks,
    isDocMode,
    activeDocModel,
    selectableModels,
    selectedModelId,
    selectTaskModel,
    isGeometricBackend,
    geometricModels,
    geometricModel,
    geometricTaskId,
    setGeometricTaskId,
    isTextPath,
    textModels,
    textModel,
    textTaskId,
    setTextTaskId,
    textOutputOptions,
    selectedClassIdx,
    setSelectedClassIdx,
    warmMut,
    primaryModel,
    panelShape,
    paramsSchema,
    hasAnyParams,
    hasNonVariantParams,
    variantGroups: variantSource.groups,
    variantCombinations: variantSource.combinations,
    paramsValue,
    variantDefaults,
    onParamsChange: saveParams,
    onVariantOrParamsChange,
    presets,
    savePreset,
    removePreset,
    applyPreset,
    prompt,
    setPrompt,
    promptTokenSet,
    toggleAlias,
    aliases,
    outputMode,
    setOutputMode,
    sourceBatchableWarning,
    isVideoProject,
    configReady,
    buildArgs,
    currentVariantSlice,
  };
}

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

export function useCropRecognitionConfig(
  projectId: string,
  backendId: string | null,
  model: MLModelCapability | null,
) {
  const key = backendId && model ? `${projectId}:${backendId}:${model.id}` : null;
  const variantGroups = model?.supported_variants;
  const variantCombinations = model?.variant_combinations;
  const axisKeys = useMemo(() => new Set(variantGroups?.map(({ key }) => key)), [variantGroups]);
  const paramsSchema = useMemo<JsonSchemaObject | undefined>(() => {
    const schema = model?.params as JsonSchemaObject | undefined;
    if (!schema?.properties) return schema;
    return {
      ...schema,
      properties: Object.fromEntries(
        Object.entries(schema.properties).filter(([name]) => !axisKeys.has(name)),
      ),
    };
  }, [axisKeys, model?.params]);
  const normalizeVariants = (values: Record<string, unknown>): Record<string, string> => {
    const slice: Record<string, string> = {};
    let combinations = variantCombinations;
    for (const [index, { key, variants = [] }] of (variantGroups ?? []).entries()) {
      const options = combinations?.length
        ? variants.filter(({ value }) => combinations?.some((row) => row[index] === value))
        : variants;
      const field = model?.params?.properties?.[key] as JsonSchemaField | undefined;
      const selected =
        options.find(({ value }) => value === values[key]) ??
        options.find(({ value }) => value === model?.default_variants?.[key]) ??
        options.find(({ recommended }) => recommended) ??
        options.find(({ value }) => value === field?.default) ??
        options[0];
      if (!selected) break;
      slice[key] = selected.value;
      combinations = combinations?.filter((row) => row[index] === selected.value);
    }
    return slice;
  };
  const [edit, setEdit] = useState<{ key: string; values: Record<string, unknown> }>();
  const edits = edit && edit.key === key ? edit.values : {};
  const variantDefaults = normalizeVariants({});
  const currentVariantSlice = normalizeVariants(edits);
  const defaults = deriveDefaults(paramsSchema);
  const paramsValue = { ...defaults, ...edits, ...currentVariantSlice };
  const change = (values: Record<string, unknown>) => {
    if (key) setEdit({ key, values });
  };
  const presets = useAiParamPresets(backendId && model ? `${backendId}:${model.id}` : null, "crop");
  const properties = Object.entries(paramsSchema?.properties ?? {});

  return {
    paramsSchema,
    hasAnyParams: properties.length > 0,
    hasNonVariantParams: properties.some(
      ([name, field]) => !isVariantField(name, field as JsonSchemaField),
    ),
    variantGroups,
    variantCombinations,
    variantDefaults,
    paramsValue,
    onParamsChange: change,
    onVariantOrParamsChange: change,
    presets: presets.presets,
    savePreset: presets.save,
    removePreset: presets.remove,
    applyPreset: change,
    buildArgs: (predictMode: PredictMode): PreannotateArgs | null =>
      backendId &&
      model &&
      hasInput(model.supported_inputs, INPUT_CROP_ID) &&
      Object.keys(currentVariantSlice).length === axisKeys.size
        ? {
            ml_backend_id: backendId,
            model_id: model.id,
            task: model.task,
            model_variants: currentVariantSlice,
            params: Object.fromEntries(
              properties
                .filter(
                  ([name, field]) => name in paramsValue && !(field as JsonSchemaField)?.readOnly,
                )
                .map(([name]) => [name, paramsValue[name]]),
            ),
            predict_mode: predictMode,
          }
        : null,
  };
}
