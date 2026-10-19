import { randomId } from "@/utils/id";
import { CopyableError } from "@/components/ui/CopyableError";
import { useMemo } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { trainedModelsApi } from "@/api/trainedModels";
import { trainingRunsApi, type TrainingInitialization } from "@/api/trainingRuns";
import { Button } from "@/components/ui/Button";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { trainingInputClass } from "./TrainingParameterField";
import { useTrainingViewOwner } from "./trainingViewOwner";

export function useTrainingInitializationSource(
  projectId: string,
  framework: string,
  baseModel: string,
  datasetId: string,
  value?: TrainingInitialization,
  task = "detect",
) {
  const supportsProjectWeights = task === "detect" && !baseModel.endsWith("-seg");
  const selectedId = value?.kind === "trained_model" ? (value.model_version_id ?? "") : "";
  const view = useTrainingViewOwner(
    projectId,
    JSON.stringify([framework, baseModel, datasetId, selectedId]),
  );
  const scope = useMemo(() => ({ owner: view.owner, key: randomId() }), [view.owner]).key;
  const source = useQuery({
    queryKey: ["training-initialization-source", projectId, scope, selectedId],
    enabled: supportsProjectWeights && !!selectedId && !!baseModel,
    queryFn: async ({ signal }) => {
      view.assert(signal);
      const result = await trainingRunsApi.initializationSource(
        projectId,
        selectedId,
        framework,
        baseModel,
        datasetId || undefined,
        { signal },
      );
      view.assert(signal);
      return result;
    },
    refetchInterval: 10000,
  });
  const trained = value?.kind === "trained_model";
  const valid =
    !trained ||
    (supportsProjectWeights &&
      !!source.data?.eligible &&
      !source.isError &&
      !source.isFetching &&
      !!value.source_digest &&
      value.source_digest === source.data.pin?.source_digest);
  return { source, valid, selectedId, supportsProjectWeights };
}

export function TrainingInitializationSource({
  projectId,
  value,
  disabled,
  qualification,
  onChange,
}: {
  projectId: string;
  value?: TrainingInitialization;
  disabled: boolean;
  qualification: ReturnType<typeof useTrainingInitializationSource>;
  onChange: (value: TrainingInitialization | undefined) => void;
}) {
  const view = useTrainingViewOwner(projectId, "initialization-choices");
  const scope = useMemo(() => ({ owner: view.owner, key: randomId() }), [view.owner]).key;
  const choices = useInfiniteQuery({
    queryKey: ["training-initialization-choices", projectId, scope],
    initialPageParam: 0,
    enabled: qualification.supportsProjectWeights,
    queryFn: async ({ pageParam, signal }) => {
      view.assert(signal);
      const result = await trainedModelsApi.list(projectId, pageParam, { signal }, "include");
      view.assert(signal);
      return result;
    },
    getNextPageParam: (last, _pages, offset) =>
      offset + 20 < last.total ? offset + 20 : undefined,
  });
  const { source, selectedId } = qualification;
  const pin = source.data?.pin;
  const items = choices.data?.pages.flatMap((page) => page.items) ?? [];
  return (
    <section
      aria-label="训练初始化"
      className="mt-5 space-y-3 rounded-lg border border-border p-4 text-xs"
    >
      <h3 className="font-medium">训练初始化</h3>
      <p className="leading-relaxed text-muted-foreground">
        新实验从第 0
        轮开始，使用全新的优化器、调度器与最佳成绩。实验中断后，可从本实验自己的检查点续训。
      </p>
      <div>
        <label htmlFor="training-initialization">初始化权重</label>
        <div className="mt-1 flex items-center gap-2">
          <select
            id="training-initialization"
            aria-label="初始化权重"
            className={`${trainingInputClass} min-w-0 flex-1`}
            value={selectedId || ""}
            disabled={disabled}
            onChange={(event) =>
              onChange(
                event.target.value
                  ? { kind: "trained_model", model_version_id: event.target.value }
                  : undefined,
              )
            }
          >
            <option value="">预训练基础权重</option>
            {selectedId && !items.some((item) => item.id === selectedId) && (
              <option value={selectedId} disabled={!qualification.supportsProjectWeights}>
                {pin?.model_name ?? "所选候选模型"}（保留的选择）
              </option>
            )}
            {items.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name} · {item.framework}
                {item.archived ? " · 已归档" : ""}
              </option>
            ))}
          </select>
          {selectedId && (
            <CopyValueButton value={selectedId} kind="id" label="复制所选候选模型版本 ID" />
          )}
        </div>
        <p className="mt-2 leading-relaxed text-muted-foreground">
          使用上方所选基础模型的预训练权重，也可选择本项目已训练的候选模型。
        </p>
      </div>
      {!qualification.supportsProjectWeights && (
        <p className="text-muted-foreground">
          分割暂不支持从项目候选模型初始化，请使用预训练基础权重；中断后可从本实验检查点续训。
        </p>
      )}
      {choices.hasNextPage && (
        <Button
          size="sm"
          variant="ghost"
          disabled={disabled || choices.isFetchingNextPage}
          onClick={() => void choices.fetchNextPage()}
        >
          加载更早的候选模型
        </Button>
      )}
      {choices.error && (
        <p role="alert" className="text-status-danger">
          <CopyableError message={choices.error.message} />
        </p>
      )}
      {selectedId && (
        <>
          {source.isFetching && (
            <p role="status" className="text-muted-foreground">
              核验来源资格…
            </p>
          )}
          {source.error && (
            <p role="alert" className="text-status-danger">
              <CopyableError message={`来源暂不可用：${source.error.message}`} />
              。当前选择已保留。
            </p>
          )}
          {source.data?.reasons.map((reason) => (
            <p key={reason} role="alert" className="text-status-caution">
              {reason}
            </p>
          ))}
          {value?.source_digest && pin && value.source_digest !== pin.source_digest && (
            <p role="alert" className="text-status-danger">
              来源身份已变化。请明确重新选择该候选，重新核对。
            </p>
          )}
          {pin && (
            <dl className="grid gap-2 break-all text-muted-foreground">
              <div>
                <dt className="text-foreground">来源候选</dt>
                <dd>
                  {pin.model_name} ·{" "}
                  <CopyValueButton
                    value={pin.model_version_id}
                    kind="id"
                    label="复制候选模型版本 ID"
                  />
                </dd>
              </div>
              <div>
                <dt className="text-foreground">原生模型与类别顺序</dt>
                <dd>
                  {pin.base_model} · {pin.classes.join("、")}
                </dd>
              </div>
              <div className="flex items-center gap-2">
                <dt className="text-foreground">来源训练数据</dt>
                <dd>
                  <CopyValueButton value={pin.dataset_id} kind="id" label="复制来源数据版本 ID" />
                </dd>
              </div>
              <div className="flex items-center gap-2">
                <dt className="text-foreground">来源验证集摘要</dt>
                <dd>
                  <CopyValueButton value={pin.validation_digest} label="复制来源验证集摘要" />
                </dd>
              </div>
              <div className="flex items-center gap-2">
                <dt className="text-foreground">权重 SHA256 / 大小</dt>
                <dd>
                  <CopyValueButton value={pin.artifact.sha256} label="复制初始化权重哈希" /> /{" "}
                  {pin.artifact.size_bytes.toLocaleString()} 字节
                </dd>
              </div>
            </dl>
          )}
          <p className="leading-relaxed text-muted-foreground">
            当前仅检查冻结身份和对象元信息；训练预检会完整校验权重与评估对象。候选归档和项目发布不会替换此来源。须使用相同
            YOLO11 规格与完全一致的原生类别顺序。
          </p>
          <Button
            size="sm"
            variant="ghost"
            disabled={disabled || source.isFetching}
            onClick={() => {
              void source.refetch();
              onChange({ kind: "trained_model", model_version_id: selectedId });
            }}
          >
            重新选择并核验此来源
          </Button>
        </>
      )}
    </section>
  );
}
