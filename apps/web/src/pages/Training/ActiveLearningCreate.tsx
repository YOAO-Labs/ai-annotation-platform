import { randomId } from "@/utils/id";
import { CopyableError } from "@/components/ui/CopyableError";
import { useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import {
  activeLearningApi,
  type CreateRound,
  type Preview,
  type SelectionInput,
} from "@/api/activeLearning";
import { trainedModelsApi } from "@/api/trainedModels";
import { useAuthStore } from "@/stores/authStore";
import { useMLBackends } from "@/hooks/useMLBackends";
import { Button } from "@/components/ui/Button";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { PageContainer } from "@/components/layout/PageContainer";
import { trainingInputClass } from "./TrainingParameterField";
import { PickerPage } from "./SampleEvaluationCreate";
import { useTrainingViewOwner } from "./trainingViewOwner";
import {
  armLabel,
  activeLearningTask,
  definitiveRejection,
  loadPending,
  savePending,
  useActiveLearningScope,
} from "./useActiveLearning";

export function ActiveLearningCreatePage() {
  const { id = "" } = useParams();
  const scope = useActiveLearningScope(id);
  return <CreateContent key={scope} projectId={id} scope={scope} />;
}

export function CreateContent({ projectId, scope }: { projectId: string; scope: string }) {
  const { assert } = useTrainingViewOwner(projectId, scope);
  const userId = useAuthStore((state) => state.user?.id);
  const pendingKey = `active-learning-create:${userId}:${projectId}`;
  const navigate = useNavigate();
  const [pending, setPending] = useState(() => loadPending<CreateRound>(pendingKey));
  const [name, setName] = useState("");
  const [modelId, setModelId] = useState("");
  const [deploymentId, setDeploymentId] = useState("");
  const [modelPage, setModelPage] = useState(0);
  const [poolPage, setPoolPage] = useState(0);
  const [selected, setSelected] = useState<string[]>([]);
  const [seed, setSeed] = useState("42");
  const [batchSize, setBatchSize] = useState("2");
  const [dispatchBudget, setDispatchBudget] = useState("32");
  const [reviewBudget, setReviewBudget] = useState("4");
  const [confidence, setConfidence] = useState("0.1");
  const [nms, setNms] = useState("0.7");
  const [preview, setPreview] = useState<{ key: string; value: Preview } | null>(null);
  const [storageError, setStorageError] = useState("");
  const input = useMemo<SelectionInput>(
    () => ({
      model_version_id: modelId,
      deployment_id: deploymentId,
      sample_task_ids: [...selected].sort(),
      seed: Number(seed),
      batch_size: Number(batchSize),
      max_inference_dispatches: Number(dispatchBudget),
      max_review_tasks: Number(reviewBudget),
      confidence: Number(confidence),
      nms_iou: Number(nms),
    }),
    [
      modelId,
      deploymentId,
      selected,
      seed,
      batchSize,
      dispatchBudget,
      reviewBudget,
      confidence,
      nms,
    ],
  );
  const guarded = async <T,>(signal: AbortSignal, operation: () => Promise<T>) => {
    assert(signal);
    const value = await operation();
    assert(signal);
    return value;
  };
  const models = useQuery({
    queryKey: ["active-learning-models", projectId, scope, modelPage],
    retry: false,
    queryFn: async ({ signal }) => {
      const value = await guarded(signal, () =>
        trainedModelsApi.list(projectId, modelPage * 20, { signal }),
      );
      if (value.items.some((item) => item.project_id !== projectId))
        throw new Error("候选模型所属项目不符");
      return value;
    },
  });
  const model = useQuery({
    queryKey: ["active-learning-model", projectId, scope, modelId],
    enabled: !!modelId,
    retry: false,
    queryFn: async ({ signal }) => {
      const value = await guarded(signal, () =>
        trainedModelsApi.get(projectId, modelId, { signal }),
      );
      if (value.model.id !== modelId || value.model.project_id !== projectId)
        throw new Error("候选模型身份不符");
      return value;
    },
  });
  const modelTask = activeLearningTask(model.data?.model.task);
  const inputKey = JSON.stringify([input, modelTask]);
  const currentInput = useRef(inputKey);
  currentInput.current = inputKey;
  const checkedPreview = preview?.key === inputKey ? preview.value : null;
  const backends = useMLBackends(projectId);
  const deploymentOptions = useMemo(() => {
    const ready = (model.data?.deployments ?? [])
      .filter((item) => item.status === "ready")
      .sort(
        (left, right) =>
          left.registry_id.localeCompare(right.registry_id) || left.id.localeCompare(right.id),
      );
    const backendNames = new Map(
      (backends.data ?? []).map(({ id: backendId, name }) => [backendId, name.trim()] as const),
    );
    return ready.map((item, index) => {
      const name = backendNames.get(item.registry_id);
      const checkedAt = item.checked_at ?? item.created_at;
      return {
        item,
        label: [
          `部署 ${index + 1}`,
          name,
          "就绪",
          `${item.checked_at ? "检查于" : "创建于"} ${checkedAt.slice(0, 10)}`,
        ]
          .filter(Boolean)
          .join(" · "),
      };
    });
  }, [backends.data, model.data?.deployments]);
  const pool = useQuery({
    queryKey: ["active-learning-pool", projectId, scope, poolPage],
    retry: false,
    queryFn: ({ signal }) =>
      guarded(signal, () => activeLearningApi.pool(projectId, poolPage * 32, { signal })),
  });
  const previewMutation = useMutation({
    mutationFn: async (value: { input: SelectionInput; key: string }) => {
      assert();
      const result = await activeLearningApi.preview(projectId, value.input);
      assert();
      if (activeLearningTask(result.model_ref?.task) !== modelTask)
        throw new Error("选样预览与候选模型任务不一致");
      return { key: value.key, value: result };
    },
    onSuccess: (value) => {
      assert();
      if (currentInput.current === value.key) setPreview(value);
    },
  });
  const create = useMutation({
    mutationFn: async (body: CreateRound) => {
      assert();
      const value = await activeLearningApi.create(projectId, body);
      assert();
      if (value.project_id !== projectId || value.spec.round_id !== value.id)
        throw new Error("创建返回的轮次身份不符");
      return value;
    },
    onSuccess: (value) => {
      assert();
      savePending(pendingKey, null);
      setPending(null);
      navigate(`/projects/${projectId}/learning/active-learning/${value.id}`);
    },
    onError: (error) => {
      try {
        assert();
      } catch {
        return;
      }
      if (definitiveRejection(error)) {
        savePending(pendingKey, null);
        setPending(null);
        setPreview(null);
      }
    },
  });
  const valid =
    model.data?.model.framework === "ultralytics" &&
    modelTask !== null &&
    !model.data?.model.archived &&
    !!modelId &&
    !!deploymentId &&
    !model.error &&
    deploymentOptions.some(({ item }) => item.id === deploymentId) &&
    selected.length >= 2 &&
    selected.length <= 32 &&
    [input.seed, input.batch_size, input.max_inference_dispatches, input.max_review_tasks].every(
      Number.isInteger,
    ) &&
    seed.trim() !== "" &&
    input.seed >= 0 &&
    input.seed <= 2_147_483_647 &&
    input.batch_size >= 1 &&
    input.batch_size <= 16 &&
    selected.length >= input.batch_size * 2 &&
    input.max_inference_dispatches >= selected.length &&
    input.max_inference_dispatches <= 96 &&
    input.max_review_tasks >= input.batch_size * 2 &&
    input.max_review_tasks <= 32 &&
    input.confidence > 0 &&
    input.confidence <= 1 &&
    input.nms_iou > 0 &&
    input.nms_iou <= 1;
  const frozen = !!pending || create.isPending;
  const submit = () => {
    if (!valid || !checkedPreview || !name.trim()) return;
    const body = {
      ...input,
      request_id: randomId(),
      name: name.trim(),
      preview_digest: checkedPreview.preview_digest,
    };
    if (!savePending(pendingKey, body)) {
      setStorageError("无法保存创建请求，请允许本页使用会话存储后重试。");
      return;
    }
    setPending(body);
    create.mutate(body);
  };
  const fields = [
    { label: "随机种子", value: seed, set: setSeed, min: 0, max: 2_147_483_647, step: 1 },
    { label: "每组任务数", value: batchSize, set: setBatchSize, min: 1, max: 16, step: 1 },
    {
      label: "推理派发上限",
      value: dispatchBudget,
      set: setDispatchBudget,
      min: 1,
      max: 96,
      step: 1,
    },
    {
      label: "人工审核任务上限",
      value: reviewBudget,
      set: setReviewBudget,
      min: 2,
      max: 32,
      step: 1,
    },
    { label: "置信度阈值", value: confidence, set: setConfidence, min: 0.001, max: 1, step: 0.01 },
    { label: "NMS IoU", value: nms, set: setNms, min: 0.001, max: 1, step: 0.01 },
  ];
  return (
    <PageContainer>
      <Link
        className="inline-flex items-center gap-2 text-xs text-muted-foreground"
        to={`/projects/${projectId}/learning/active-learning`}
      >
        <ArrowLeft size={14} />
        返回选样轮次
      </Link>
      <h1 className="mb-2 mt-4 text-xl font-semibold">新建人工选样轮次</h1>
      <p className="mb-5 text-sm text-muted-foreground">
        固定一个模型，从未分派图片中提出人工标注建议，并保留等量随机对照。接纳后仍须正常分派、标注与审核。
      </p>
      {pending && (
        <div
          role="status"
          className="mb-4 rounded-md bg-status-caution-soft p-3 text-sm text-status-caution"
        >
          已保存创建请求“{pending.name}”。结果尚未确认时，重试会复用同一请求，不会新建重复轮次。
          <Button
            className="mt-2"
            size="sm"
            disabled={create.isPending}
            onClick={() => create.mutate(pending)}
          >
            重试同一创建请求
          </Button>
        </div>
      )}
      <fieldset disabled={frozen} className="m-0 min-w-0 space-y-5 border-0 p-0">
        <section
          className="rounded-lg border border-border bg-card p-4"
          aria-labelledby="active-model-title"
        >
          <h2 id="active-model-title" className="mb-4 text-sm font-semibold">
            1. 固定模型与预算
          </h2>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <label className="text-xs">
              轮次名称
              <input
                className={`${trainingInputClass} mt-2 w-full`}
                value={name}
                maxLength={120}
                onChange={(event) => setName(event.target.value)}
              />
            </label>
            <div className="text-xs">
              <label htmlFor="active-learning-model">已注册模型</label>
              <div className="mt-2 flex items-center gap-2">
                <select
                  id="active-learning-model"
                  className={`${trainingInputClass} min-w-0 flex-1`}
                  value={modelId}
                  onChange={(event) => {
                    setModelId(event.target.value);
                    setDeploymentId("");
                  }}
                >
                  <option value="" disabled hidden>
                    选择模型
                  </option>
                  {modelId &&
                    (models.error || !models.data?.items.some((item) => item.id === modelId)) && (
                      <option value={modelId}>
                        {model.data?.model.name ?? "已选候选模型（未在当前列表中）"}
                      </option>
                    )}
                  {!models.error &&
                    models.data?.items.map((item) => (
                      <option
                        key={item.id}
                        value={item.id}
                        disabled={
                          item.framework !== "ultralytics" ||
                          !activeLearningTask(item.task) ||
                          item.archived
                        }
                      >
                        {item.name} · {item.framework}
                        {activeLearningTask(item.task) === "segment" ? " · 图片实例分割" : ""}
                        {item.framework !== "ultralytics" || !activeLearningTask(item.task)
                          ? " · 尚未通过共享评估资格"
                          : ""}
                        {item.archived ? " · 已归档" : ""}
                      </option>
                    ))}
                </select>
                {modelId && (
                  <CopyValueButton value={modelId} kind="id" label="复制候选模型版本 ID" />
                )}
              </div>
            </div>
            <p className="text-xs text-muted-foreground">
              主动选样支持受控 Ultralytics
              图片目标检测与实例分割，就绪部署仍须通过本轮服务端资格核验。LibreYOLO
              可在模型面板试用、人工发布与回滚。
            </p>
            <div className="text-xs">
              <label htmlFor="active-learning-deployment">就绪部署</label>
              <div className="mt-2 flex items-center gap-2">
                <select
                  id="active-learning-deployment"
                  className={`${trainingInputClass} min-w-0 flex-1`}
                  value={deploymentId}
                  disabled={model.data?.model.framework !== "ultralytics" || !modelTask}
                  onChange={(event) => setDeploymentId(event.target.value)}
                >
                  <option value="" disabled hidden>
                    选择部署
                  </option>
                  {deploymentId &&
                    (model.error ||
                      !deploymentOptions.some(({ item }) => item.id === deploymentId)) && (
                      <option value={deploymentId} disabled hidden>
                        已选部署（暂不可用，请重新选择）
                      </option>
                    )}
                  {!model.error &&
                    deploymentOptions.map(({ item, label }) => (
                      <option key={item.id} value={item.id}>
                        {label}
                      </option>
                    ))}
                </select>
                {deploymentId && (
                  <CopyValueButton value={deploymentId} kind="id" label="复制已选部署 ID" />
                )}
              </div>
            </div>
            {fields.map((field) => (
              <label key={field.label} className="text-xs">
                {field.label}
                <input
                  className={`${trainingInputClass} mt-2 w-full`}
                  type="number"
                  min={field.min}
                  max={field.max}
                  step={field.step}
                  value={field.value}
                  onChange={(event) => field.set(event.target.value)}
                />
              </label>
            ))}
          </div>
          <PickerPage
            page={modelPage}
            total={models.data?.total ?? 0}
            onChange={setModelPage}
            label="模型"
          />
          <p className="mb-0 mt-3 text-xs text-muted-foreground">
            推理上限按实际派发计数，超时不退款。随机对照与主动建议各占一组；审核预算至少为每组数量的两倍。
          </p>
          {modelTask === "segment" && model.data && (
            <p className="mb-0 mt-3 text-xs text-muted-foreground">
              实例分割按实例置信度与跨类别原图掩码 IoU 排序，不使用像素概率。每张原图最多 1,280,000
              像素、单边 4096、全部实例 RLE 合计 2 MiB，推理最多 100 个实例；无实例信号时随机补位。
            </p>
          )}
        </section>
        <section
          className="rounded-lg border border-border bg-card p-4"
          aria-labelledby="active-pool-title"
        >
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 id="active-pool-title" className="text-sm font-semibold">
              2. 选择未标注图片 · 已选 {selected.length}/32
            </h2>
            <Button
              type="button"
              size="sm"
              disabled={!pool.data || !!pool.error}
              onClick={() => setSelected(pool.data!.items.map((item) => item.task_id))}
            >
              选择本页候选
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            仅列出无分派、标注、草稿或编辑锁的待处理图片。预览会按实际图片内容和场景去重，排除保留的训练、验证与测试成员。
          </p>
          {pool.isPending && (
            <p role="status" className="text-sm">
              读取候选池…
            </p>
          )}
          {!pool.error && pool.data?.items.length === 0 && (
            <p role="status" className="text-sm">
              当前没有符合资格的图片。请先准备未分派的待标注任务。
            </p>
          )}
          <div className="divide-y divide-border">
            {!pool.error &&
              pool.data?.items.map((item) => (
                <label
                  key={item.task_id}
                  className="flex cursor-pointer items-start gap-3 py-3 text-xs"
                >
                  <input
                    type="checkbox"
                    aria-label={`选择 ${item.display_id}`}
                    checked={selected.includes(item.task_id)}
                    disabled={!selected.includes(item.task_id) && selected.length >= 32}
                    onChange={(event) =>
                      setSelected((old) =>
                        event.target.checked
                          ? [...old, item.task_id]
                          : old.filter((id) => id !== item.task_id),
                      )
                    }
                  />
                  <span className="min-w-0 break-all">
                    <span className="font-mono">{item.display_id}</span> · {item.file_name}
                  </span>
                </label>
              ))}
          </div>
          <PickerPage
            page={poolPage}
            total={pool.data?.total ?? 0}
            pageSize={32}
            onChange={setPoolPage}
            label="候选池"
          />
        </section>
      </fieldset>
      {!valid && !pending && (
        <p role="status" className="text-xs text-muted-foreground">
          请选择模型、就绪部署和足量候选，并填写有效预算。
        </p>
      )}
      {[
        models.error,
        model.error,
        pool.error,
        previewMutation.variables?.key === inputKey ? previewMutation.error : null,
        create.error,
      ]
        .filter(Boolean)
        .map((error, index) => (
          <p key={index} role="alert" className="text-sm text-status-danger">
            <CopyableError message={error?.message} />
          </p>
        ))}
      {storageError && (
        <p role="alert" className="text-sm text-status-danger">
          {storageError}
        </p>
      )}
      <div className="mt-5 flex flex-wrap gap-3">
        <Button
          disabled={
            !valid ||
            frozen ||
            previewMutation.isPending ||
            !!models.error ||
            !!model.error ||
            !!pool.error
          }
          onClick={() => previewMutation.mutate({ input, key: inputKey })}
        >
          {previewMutation.isPending ? "核验图片与历史成员…" : "核验并预览"}
        </Button>
        <Button
          variant="primary"
          disabled={
            !valid || !checkedPreview || !name.trim() || frozen || previewMutation.isPending
          }
          onClick={submit}
        >
          {create.isPending ? "创建中…" : "创建选样轮次"}
        </Button>
      </div>
      {checkedPreview && !pending && (
        <section
          aria-labelledby="active-preview-title"
          className="mt-5 rounded-lg border border-border bg-card p-4"
        >
          <h2 id="active-preview-title" className="text-sm font-semibold">
            3. 已核验预览
          </h2>
          <p className="text-sm">
            {checkedPreview.pool_size} 张输入 → {checkedPreview.candidate_count}{" "}
            个独立候选；排除／去重 {checkedPreview.omitted.length} 张。{armLabel.active}与
            {armLabel.random_control}各建议 {input.batch_size} 张。
          </p>
          <p className="break-all font-mono text-xs text-muted-foreground">
            预览摘要{" "}
            <CopyValueButton value={checkedPreview.preview_digest} label="复制选样预览摘要" />
          </p>
          <p className="mb-0 text-xs text-muted-foreground">
            {modelTask === "segment"
              ? "本轮按实例置信度与跨类别掩码重叠提出建议；这些信号不使用像素概率，也不是 AP 或收益证明。"
              : "本轮只验证选样流程。框置信度是检测启发式，不能证明样本正确率或效率收益。"}
            修改候选、模型或预算后须重新预览。
          </p>
        </section>
      )}
    </PageContainer>
  );
}
