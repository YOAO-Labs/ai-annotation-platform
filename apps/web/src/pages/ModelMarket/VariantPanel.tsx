import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import {
  mlBackendsApi,
  type MLBackendSupportedVariantGroup,
  type MLBackendVariant,
  type MLModelCapability,
  mlBackendSetupQueryKey,
} from "@/api/ml-backends";
import type { MLBackendItem } from "@/api/adminMlIntegrations";
import {
  loadedKeysAsGsam2ImageVariants,
  loadedKeysLastUsedMap,
  gsam2ImageVariantsAsCacheBucketKey,
  gsam2ImageVariantsAsLoadedKey,
} from "./poolKeyParse";

interface EnumField {
  enum?: string[];
  default?: string;
}

const SECTION_CLASS = "flex flex-col gap-2";
const SECTION_TITLE_CLASS = "flex items-center gap-2 text-xs font-semibold text-muted-foreground";
const CAP_CLASS = "text-2xs font-normal text-muted-foreground";
const NOTE_CLASS = "text-xs text-muted-foreground";
const HINT_CLASS = "text-2xs leading-normal text-muted-foreground";
const FIELD_CLASS = "flex flex-col gap-1";
const FIELD_LABEL_CLASS = "text-2xs text-muted-foreground";
const SELECT_CLASS =
  "appearance-none rounded-md border border-border bg-card px-2 py-1 text-xs text-foreground";
const WARM_ROW_CLASS = "flex flex-wrap items-end gap-2.5";
const TABLE_CLASS =
  "w-full min-w-[520px] border-separate border-spacing-0 text-xs [&_td]:border-t [&_td]:border-border [&_td]:px-2 [&_td]:py-1 [&_th]:px-2 [&_th]:py-1 [&_th]:text-left [&_th]:text-2xs [&_th]:font-medium [&_th]:whitespace-nowrap [&_th]:text-muted-foreground";

export interface VariantWarmTarget {
  task?: string;
  variants?: MLBackendVariant;
  taskType?: "image" | "video";
}

const SAM2_VIDEO_VARIANTS = ["tiny", "small", "base_plus", "large"];

export function VariantPanel({
  projectId,
  backend,
  onWarm,
  isWarming,
}: {
  projectId: string;
  backend: Pick<MLBackendItem, "id" | "health_meta">;
  onWarm: (target?: VariantWarmTarget) => void;
  isWarming: boolean;
}) {
  const {
    data: setup,
    isLoading,
    isError,
  } = useQuery({
    queryKey: mlBackendSetupQueryKey(projectId, backend.id),
    queryFn: () => mlBackendsApi.setup(projectId, backend.id),
    staleTime: 30_000,
  });

  const props = (setup?.params?.properties ?? {}) as Record<string, EnumField>;
  const samField = props.sam_variant;
  const dinoField = props.dino_variant;
  const samEnum = samField?.enum ?? [];
  const dinoEnum = dinoField?.enum ?? [];
  const supportsVariants = samEnum.length > 0 || dinoEnum.length > 0;
  const genericVariantGroups = (setup?.supported_variants ?? []).filter(
    (group) => Array.isArray(group.variants) && group.variants.length > 0,
  );
  const modalities = backend.health_meta?.capabilities?.modalities ?? [];
  const hasModalitySnapshot = modalities.length > 0;

  const pool = backend.health_meta?.pool;
  const buckets = backend.health_meta?.cache?.buckets ?? {};
  const loaded = useMemo(() => {
    const fromKeys = loadedKeysAsGsam2ImageVariants(pool?.loaded_keys);
    if (fromKeys.length > 0) return fromKeys;
    return pool?.loaded_variants ?? [];
  }, [pool?.loaded_keys, pool?.loaded_variants]);
  const lruTs = useMemo(() => {
    if (pool?.loaded_keys && pool.loaded_keys.length > 0) {
      return loadedKeysLastUsedMap(pool.loaded_keys);
    }
    return pool?.per_variant_lru_ts ?? {};
  }, [pool?.loaded_keys, pool?.per_variant_lru_ts]);
  const useRelativeAgo = (pool?.loaded_keys?.length ?? 0) > 0;

  const videoPool = backend.health_meta?.video_pool;
  const hasVideoMeta = backend.health_meta != null && "video_pool" in backend.health_meta;
  const supportedTrackers = setup?.supported_trackers ?? [];
  const supportsVideo = supportedTrackers.length > 0;
  const showImageGroup = hasModalitySnapshot
    ? modalities.includes("image")
    : supportsVariants || genericVariantGroups.length > 0;
  const showVideoGroup = hasModalitySnapshot ? modalities.includes("video") : supportsVideo;
  const videoLoaded = useMemo<string[]>(() => {
    const keys = videoPool?.loaded_keys;
    if (keys && keys.length > 0) return keys.map((k) => k.key);
    return videoPool?.loaded_variants ?? [];
  }, [videoPool?.loaded_keys, videoPool?.loaded_variants]);
  const videoSamEnum = samEnum.length > 0 ? samEnum : SAM2_VIDEO_VARIANTS;
  const videoSingleModel = supportsVideo && samEnum.length === 0;
  const videoWeightLabel =
    setup?.video_model_version ?? videoLoaded[0] ?? supportedTrackers[0] ?? "视频模型";

  const [sam, setSam] = useState("");
  const [dino, setDino] = useState("");
  const [videoSam, setVideoSam] = useState("");
  const [collapsed, setCollapsed] = useState(true);

  useEffect(() => {
    if (samEnum.length > 0 && !sam) setSam(samField?.default ?? samEnum[0]);
    if (dinoEnum.length > 0 && !dino) setDino(dinoField?.default ?? dinoEnum[0]);
    if (videoSamEnum.length > 0 && !videoSam) setVideoSam(videoSamEnum[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setup]);

  if (isLoading) return <div className={NOTE_CLASS}>加载变体能力…</div>;
  if (isError)
    return (
      <div className="text-xs text-status-danger">
        无法获取 /setup（模型服务不可达或未实现该接口）
      </div>
    );

  const isMultiModelBackend =
    !supportsVariants && genericVariantGroups.length === 0 && (setup?.models?.length ?? 0) > 0;

  const isSelectedLoaded = loaded.some((v) => v.sam_variant === sam && v.dino_variant === dino);
  const isVideoSelectedLoaded = videoSingleModel
    ? videoLoaded.length > 0
    : videoLoaded.includes(videoSam);

  const genericImageMode =
    !supportsVariants && genericVariantGroups.length > 0 && !isMultiModelBackend;
  const loadedRawKeys = pool?.loaded_keys?.map((k) => k.key) ?? [];
  const imageLoadedCount = genericImageMode ? loadedRawKeys.length : loaded.length;
  const canGenericWarm = setup?.warmup_endpoint === true;
  const genericWarmVariants: Record<string, string> = {};
  for (const g of genericVariantGroups) {
    const opts = g.variants ?? [];
    const rec = opts.find((v) => v.recommended) ?? opts[0];
    if (rec) genericWarmVariants[g.key] = rec.value;
  }
  const genericWarmTargetKey = Object.values(genericWarmVariants)[0];
  const genericWarmLoaded =
    genericWarmTargetKey != null && loadedRawKeys.includes(genericWarmTargetKey);

  const body = isMultiModelBackend ? (
    setup!.models!.map((model) => (
      <ModelVariantWarmSection
        key={model.id}
        model={model}
        loadedKeys={pool?.loaded_keys?.map((key) => key.key) ?? []}
        isWarming={isWarming}
        onWarm={onWarm}
      />
    ))
  ) : (
    <>
      {showImageGroup && (
        <>
          {genericVariantGroups.length > 0 && (
            <div className={SECTION_CLASS}>
              <div className={SECTION_TITLE_CLASS}>通用变体目录</div>
              <GenericVariantDirectory groups={genericVariantGroups} />
              {!supportsVariants && !canGenericWarm && (
                <div className={HINT_CLASS}>模型服务未声明支持预热，变体目录仅供查看。</div>
              )}
            </div>
          )}

          <div className={SECTION_CLASS}>
            <div className={SECTION_TITLE_CLASS}>
              图像推理变体 · 已加载
              {pool?.cap != null && (
                <span className={CAP_CLASS}>
                  {imageLoadedCount}/{pool.cap}
                </span>
              )}
            </div>
            {imageLoadedCount === 0 ? (
              <div className={NOTE_CLASS}>
                {pool ? "暂无已加载的变体" : "暂无图像池观测，无法确认模型是否已加载。"}
              </div>
            ) : genericImageMode ? (
              <div className="max-w-full overflow-x-auto">
                <table className={TABLE_CLASS}>
                  <thead>
                    <tr>
                      {["变体", "最近使用"].map((h) => (
                        <th key={h}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {loadedRawKeys.map((key) => {
                      const ts = lruTs[key];
                      return (
                        <tr key={key}>
                          <td
                            className="max-w-[220px] overflow-hidden text-ellipsis whitespace-nowrap"
                            title={key}
                          >
                            <span className="mono">{key}</span>
                          </td>
                          <td className="whitespace-nowrap text-muted-foreground">
                            {ts != null ? `t-${ts.toFixed(0)}s` : "—"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="max-w-full overflow-x-auto">
                <table className={TABLE_CLASS}>
                  <thead>
                    <tr>
                      {["变体", "cache 命中", "最近使用"].map((h) => (
                        <th key={h}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {loaded.map((v) => {
                      const bucketKey = gsam2ImageVariantsAsCacheBucketKey(v);
                      const lruKey = useRelativeAgo ? gsam2ImageVariantsAsLoadedKey(v) : bucketKey;
                      const bucket = buckets[bucketKey];
                      const ts = lruTs[lruKey];
                      return (
                        <tr key={bucketKey}>
                          <td
                            className="max-w-[220px] overflow-hidden text-ellipsis whitespace-nowrap"
                            title={bucketKey}
                          >
                            <span className="mono">{bucketKey}</span>
                          </td>
                          <td className="whitespace-nowrap">
                            {bucket?.hit_rate != null
                              ? `${(bucket.hit_rate * 100).toFixed(1)}% (${bucket.hits ?? 0}/${(bucket.hits ?? 0) + (bucket.misses ?? 0)})`
                              : "—"}
                          </td>
                          <td className="whitespace-nowrap text-muted-foreground">
                            {ts != null
                              ? useRelativeAgo
                                ? `t-${ts.toFixed(0)}s`
                                : `t+${ts.toFixed(0)}s`
                              : "—"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className={SECTION_CLASS}>
            <div className={SECTION_TITLE_CLASS}>图像推理变体 · 预热</div>
            {genericImageMode ? (
              <div className={WARM_ROW_CLASS}>
                <Button
                  size="sm"
                  onClick={() => onWarm({ variants: genericWarmVariants })}
                  disabled={isWarming || !canGenericWarm}
                  title={canGenericWarm ? "请求预热该变体" : "模型服务未声明支持预热"}
                >
                  <Icon name="play" size={11} />
                  预热
                </Button>
                {genericWarmLoaded && <Badge variant="success">已在显存</Badge>}
              </div>
            ) : (
              <div className={WARM_ROW_CLASS}>
                {samEnum.length > 0 && (
                  <label className={FIELD_CLASS}>
                    <span className={FIELD_LABEL_CLASS}>SAM</span>
                    <select
                      value={sam}
                      onChange={(e) => setSam(e.target.value)}
                      className={SELECT_CLASS}
                    >
                      {samEnum.map((o) => (
                        <option key={o} value={o}>
                          {o}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                {dinoEnum.length > 0 && (
                  <label className={FIELD_CLASS}>
                    <span className={FIELD_LABEL_CLASS}>DINO</span>
                    <select
                      value={dino}
                      onChange={(e) => setDino(e.target.value)}
                      className={SELECT_CLASS}
                    >
                      {dinoEnum.map((o) => (
                        <option key={o} value={o}>
                          {o}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                <Button
                  size="sm"
                  onClick={() =>
                    onWarm({
                      variants: {
                        ...(sam ? { sam_variant: sam } : {}),
                        ...(dino ? { dino_variant: dino } : {}),
                      },
                    })
                  }
                  disabled={isWarming || !supportsVariants}
                  title={supportsVariants ? "请求预热所选 SAM/DINO 变体" : "模型服务未声明支持预热"}
                >
                  <Icon name="play" size={11} />
                  预热
                </Button>
                {isSelectedLoaded && <Badge variant="success">已在显存</Badge>}
              </div>
            )}
            <div className={HINT_CLASS}>
              预热请求会尝试将所选变体载入模型池；池容量有限，可能释放近期未使用的变体。
              请求完成后请执行「健康检查」，更新上方状态。
            </div>
          </div>
        </>
      )}

      {showVideoGroup && (
        <div className={SECTION_CLASS}>
          <div className={SECTION_TITLE_CLASS}>
            视频追踪变体 · 已加载（视频池）
            {videoPool?.cap != null && (
              <span className={CAP_CLASS}>
                {videoLoaded.length}/{videoPool.cap}
                {videoPool.active_sessions != null && ` · ${videoPool.active_sessions} 会话`}
              </span>
            )}
          </div>
          {videoSingleModel && (
            <div className="flex flex-col gap-1.5">
              <div className="text-2xs font-semibold text-muted-foreground">视频权重</div>
              <div className="flex flex-wrap gap-1.5">
                <span className="inline-flex items-center rounded-full border border-brand bg-brand/10 px-2 py-px text-2xs leading-relaxed text-brand">
                  <span className="mono">{videoWeightLabel}</span>
                </span>
              </div>
            </div>
          )}
          {!hasVideoMeta ? (
            <div className={NOTE_CLASS}>暂无视频池观测，无法确认视频模型是否已加载。</div>
          ) : (
            <>
              {!videoSingleModel &&
                (videoLoaded.length === 0 ? (
                  <div className={NOTE_CLASS}>视频池暂无已加载的变体</div>
                ) : (
                  <div className="max-w-full overflow-x-auto">
                    <table className={TABLE_CLASS}>
                      <thead>
                        <tr>
                          <th>SAM 变体</th>
                        </tr>
                      </thead>
                      <tbody>
                        {videoLoaded.map((v) => (
                          <tr key={v}>
                            <td
                              className="max-w-[220px] overflow-hidden text-ellipsis whitespace-nowrap"
                              title={v}
                            >
                              <span className="mono">{v}</span>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ))}
              <div className={WARM_ROW_CLASS}>
                {!videoSingleModel && (
                  <label className={FIELD_CLASS}>
                    <span className={FIELD_LABEL_CLASS}>SAM</span>
                    <select
                      value={videoSam}
                      onChange={(e) => setVideoSam(e.target.value)}
                      className={SELECT_CLASS}
                    >
                      {videoSamEnum.map((o) => (
                        <option key={o} value={o}>
                          {o}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                <Button
                  size="sm"
                  onClick={() =>
                    onWarm({
                      taskType: "video",
                      variants: videoSingleModel || !videoSam ? {} : { sam_variant: videoSam },
                    })
                  }
                  disabled={isWarming}
                >
                  <Icon name="play" size={11} />
                  预热
                </Button>
                {isVideoSelectedLoaded && <Badge variant="success">已在显存</Badge>}
              </div>
              <div className={HINT_CLASS}>
                视频追踪使用独立模型池；预热可请求提前加载视频权重，不使用 DINO。
              </div>
            </>
          )}
        </div>
      )}
    </>
  );

  return (
    <div className="flex flex-col gap-3.5 border-t border-border bg-muted px-4 py-3">
      <button
        type="button"
        className="flex w-full cursor-pointer appearance-none items-center gap-2 border-0 bg-transparent p-0 text-left text-xs font-semibold text-muted-foreground hover:text-foreground"
        onClick={() => setCollapsed((c) => !c)}
        aria-expanded={!collapsed}
      >
        <Icon name={collapsed ? "chevRight" : "chevDown"} size={12} />
        <span className="tracking-[0.02em]">模型预热 · 变体</span>
        <span className="ml-auto text-2xs font-normal text-muted-foreground">
          {collapsed ? "展开" : "收起"}
        </span>
      </button>
      {!collapsed && <div className="flex flex-col gap-3.5">{body}</div>}
    </div>
  );
}

function ModelVariantWarmSection({
  model,
  loadedKeys,
  isWarming,
  onWarm,
}: {
  model: MLModelCapability;
  loadedKeys: string[];
  isWarming: boolean;
  onWarm: (target?: VariantWarmTarget) => void;
}) {
  const groups = (model.supported_variants ?? []).filter(
    (group) => Array.isArray(group.variants) && group.variants.length > 0,
  );
  const [variants, setVariants] = useState<Record<string, string>>(() =>
    pickInitialVariants(model),
  );

  useEffect(() => {
    setVariants(pickInitialVariants(model));
  }, [model]);

  const selectedKey = selectedLoadedKey(model.task, variants);
  const isLoaded = selectedKey ? loadedKeys.includes(selectedKey) : false;

  if (groups.length === 0) {
    const loaded = loadedKeys.includes(model.id);
    return (
      <div className={SECTION_CLASS}>
        <div className={SECTION_TITLE_CLASS}>
          {model.task ?? model.id}
          <span className={CAP_CLASS}>{model.display_name ?? model.id}</span>
        </div>
        <div className={WARM_ROW_CLASS}>
          <Button
            size="sm"
            onClick={() => onWarm({ task: model.task, variants: {} })}
            disabled={isWarming}
          >
            <Icon name="play" size={11} />
            预热
          </Button>
          {loaded && <Badge variant="success">已在显存</Badge>}
        </div>
      </div>
    );
  }

  return (
    <div className={SECTION_CLASS}>
      <div className={SECTION_TITLE_CLASS}>
        {model.task ?? model.id}
        <span className={CAP_CLASS}>{model.display_name ?? model.id}</span>
      </div>
      <div className={WARM_ROW_CLASS}>
        {groups.map((group, idx) => {
          const options = filterModelOptions(model, groups, idx, variants);
          return (
            <label key={group.key} className={FIELD_CLASS}>
              <span className={FIELD_LABEL_CLASS}>{group.title ?? group.key}</span>
              <select
                value={variants[group.key] ?? ""}
                onChange={(event) =>
                  setVariants((current) =>
                    sanitizeVariantSelection(model, groups, {
                      ...current,
                      [group.key]: event.target.value,
                    }),
                  )
                }
                className={SELECT_CLASS}
              >
                {options.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label ?? option.value}
                    {option.vram_gb != null ? ` · ${option.vram_gb}GB` : ""}
                    {option.tier ? ` · ${option.tier}` : ""}
                  </option>
                ))}
              </select>
            </label>
          );
        })}
        <Button
          size="sm"
          onClick={() => onWarm({ task: model.task, variants })}
          disabled={isWarming || Object.keys(variants).length === 0}
        >
          <Icon name="play" size={11} />
          预热
        </Button>
        {isLoaded && <Badge variant="success">已在显存</Badge>}
      </div>
      <GenericVariantDirectory groups={groups} />
    </div>
  );
}

function pickInitialVariants(model: MLModelCapability): Record<string, string> {
  const out: Record<string, string> = { ...(model.default_variants ?? {}) };
  for (const group of model.supported_variants ?? []) {
    if (out[group.key]) continue;
    const options = group.variants ?? [];
    const recommended = options.find((option) => option.recommended);
    const picked = recommended ?? options[0];
    if (picked?.value) out[group.key] = picked.value;
  }
  return sanitizeVariantSelection(
    model,
    (model.supported_variants ?? []).filter(
      (group) => Array.isArray(group.variants) && group.variants.length > 0,
    ),
    out,
  );
}

function filterModelOptions(
  model: MLModelCapability,
  groups: MLBackendSupportedVariantGroup[],
  axisIndex: number,
  variants: Record<string, string>,
) {
  const group = groups[axisIndex]!;
  const options = group.variants ?? [];
  const combos = model.variant_combinations ?? [];
  if (combos.length === 0) return options;
  const allowed = new Set<string>();
  for (const combo of combos) {
    let matches = true;
    for (let i = 0; i < axisIndex; i++) {
      const key = groups[i]!.key;
      if (variants[key] && combo[i] !== variants[key]) {
        matches = false;
        break;
      }
    }
    if (matches && combo[axisIndex]) allowed.add(combo[axisIndex]!);
  }
  const filtered = options.filter((option) => allowed.has(option.value));
  return filtered.length > 0 ? filtered : options;
}

function sanitizeVariantSelection(
  model: MLModelCapability,
  groups: MLBackendSupportedVariantGroup[],
  variants: Record<string, string>,
): Record<string, string> {
  const next = { ...variants };
  for (let idx = 0; idx < groups.length; idx++) {
    const options = filterModelOptions(model, groups, idx, next);
    const current = next[groups[idx]!.key];
    if (!current || !options.some((option) => option.value === current)) {
      const recommended = options.find((option) => option.recommended);
      next[groups[idx]!.key] = (recommended ?? options[0])?.value ?? "";
    }
  }
  return Object.fromEntries(Object.entries(next).filter(([, value]) => value));
}

function selectedLoadedKey(
  task: string | undefined,
  variants: Record<string, string>,
): string | null {
  const series = variants.series;
  const size = variants.size;
  if (task && series && size) return `${series}/${size}/${task}`;
  if (variants.sam_variant && variants.dino_variant) {
    return `sam=${variants.sam_variant}/dino=${variants.dino_variant}`;
  }
  if (variants.model_variant) return variants.model_variant;
  return null;
}

function GenericVariantDirectory({ groups }: { groups: MLBackendSupportedVariantGroup[] }) {
  return (
    <div className="grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-2">
      {groups.map((group) => (
        <div key={group.key} className="flex flex-col gap-1.5">
          <div className="text-2xs font-semibold text-muted-foreground">
            {group.title ?? group.key}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {group.variants!.map((option) => (
              <span
                key={option.value}
                className={`inline-flex items-center rounded-full border px-2 py-px text-2xs leading-relaxed ${
                  option.recommended
                    ? "border-brand bg-brand/10 text-brand"
                    : "border-border bg-card text-muted-foreground"
                }`}
              >
                <span className="mono">{option.label ?? option.value}</span>
                {option.vram_gb != null && (
                  <span className="text-muted-foreground"> · {option.vram_gb}GB</span>
                )}
                {option.tier && <span className="text-muted-foreground"> · {option.tier}</span>}
              </span>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
