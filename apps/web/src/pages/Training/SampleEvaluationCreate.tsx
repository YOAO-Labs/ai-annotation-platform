import { randomId } from "@/utils/id";
import { CopyableError } from "@/components/ui/CopyableError";
import { useMemo, useState } from "react";
import { ListPagination } from "@/components/ui/ListPagination";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowLeft, Check, Plus } from "lucide-react";
import { sampleEvaluationsApi, type SampleEvaluationCreate } from "@/api/sampleEvaluations";
import { trainedModelsApi } from "@/api/trainedModels";
import { trainingDatasetsApi } from "@/api/trainingDatasets";
import { PageContainer } from "@/components/layout/PageContainer";
import { Button } from "@/components/ui/Button";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { useMLBackends } from "@/hooks/useMLBackends";
import { useAuthStore } from "@/stores/authStore";
import { trainingInputClass } from "./TrainingParameterField";
import { useTrainingSessionRevision, useTrainingViewOwner } from "./trainingViewOwner";

export function SampleEvaluationCreatePage() {
  const { id = "" } = useParams();
  const userId = useAuthStore((s) => s.user?.id);
  const token = useAuthStore((s) => s.token);
  const revision = useTrainingSessionRevision();
  const scope = useMemo(
    () => ({ key: randomId(), id, userId, token, revision }),
    [id, userId, token, revision],
  );
  return <CreateContent key={scope.key} projectId={id} scope={scope.key} />;
}

function CreateContent({ projectId, scope }: { projectId: string; scope: string }) {
  const navigate = useNavigate();
  const { assert } = useTrainingViewOwner(projectId, scope);
  const [modelPage, setModelPage] = useState(0);
  const [datasetPage, setDatasetPage] = useState(0);
  const [samplePage, setSamplePage] = useState(0);
  const [modelId, setModelId] = useState("");
  const [deploymentId, setDeploymentId] = useState("");
  const [datasetId, setDatasetId] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [name, setName] = useState("");
  const [role, setRole] =
    useState<NonNullable<SampleEvaluationCreate["role"]>>("validation_selection");
  const [confirmed, setConfirmed] = useState(false);
  const [confidence, setConfidence] = useState("0.25");
  const [matchingIou, setMatchingIou] = useState("0.5");
  const [nmsIou, setNmsIou] = useState("0.7");
  const guarded = async <T,>(signal: AbortSignal, request: () => Promise<T>) => {
    assert(signal);
    const value = await request();
    assert(signal);
    return value;
  };
  const models = useQuery({
    queryKey: ["evaluation-models", projectId, scope, modelPage],
    queryFn: ({ signal }) =>
      guarded(signal, () => trainedModelsApi.list(projectId, modelPage * 20, { signal })),
    retry: false,
  });
  const model = useQuery({
    queryKey: ["evaluation-model", projectId, scope, modelId],
    queryFn: async ({ signal }) => {
      const data = await guarded(signal, () =>
        trainedModelsApi.get(projectId, modelId, { signal }),
      );
      if (data.model.id !== modelId || data.model.project_id !== projectId)
        throw new Error("候选模型身份不符");
      return data;
    },
    enabled: !!modelId,
    retry: false,
  });
  const backends = useMLBackends(projectId);
  const task = model.data?.model.task === "segment" ? "segment" : "detect";
  const datasets = useQuery({
    queryKey: ["evaluation-datasets", projectId, scope, datasetPage],
    queryFn: ({ signal }) =>
      guarded(signal, () =>
        trainingDatasetsApi.list(projectId, datasetPage * 20, "ready", { signal }),
      ),
    retry: false,
  });
  const dataset = useQuery({
    queryKey: ["evaluation-dataset", projectId, scope, datasetId, task],
    queryFn: async ({ signal }) => {
      const data = await guarded(signal, () =>
        trainingDatasetsApi.getSummary(projectId, datasetId, { signal }),
      );
      if (data.id !== datasetId || data.project_id !== projectId)
        throw new Error("冻结数据版本身份不符");
      if ((data.task ?? "detect") !== task) throw new Error("冻结数据版本须与候选模型属于同一任务");
      return data;
    },
    enabled: !!datasetId && !!model.data,
    retry: false,
  });
  const samples = useQuery({
    queryKey: ["evaluation-selection", projectId, scope, datasetId, samplePage],
    queryFn: async ({ signal }) => {
      const data = await guarded(signal, () =>
        trainingDatasetsApi.samples(
          projectId,
          datasetId,
          { split: "val", limit: 12, offset: samplePage * 12 },
          { signal },
        ),
      );
      if (data.source_version_id !== datasetId) throw new Error("验证成员所属版本不符");
      return data;
    },
    enabled: !!datasetId && dataset.isSuccess && !dataset.isError,
    retry: false,
  });
  const qualification = useQuery({
    queryKey: ["evaluation-qualification", projectId, scope, modelId, deploymentId],
    queryFn: async ({ signal }) => {
      const data = await guarded(signal, () =>
        sampleEvaluationsApi.qualify(projectId, modelId, deploymentId, { signal }),
      );
      if (
        data.model_ref.model_version_id !== modelId ||
        data.model_ref.deployment_id !== deploymentId ||
        data.model_ref.project_id !== projectId
      )
        throw new Error("推理资格返回的模型身份不符");
      return data;
    },
    enabled: !!modelId && !!deploymentId && model.data?.model.framework === "ultralytics",
    retry: false,
  });
  const body = {
    name: name.trim(),
    model_version_id: modelId,
    deployment_id: deploymentId,
    dataset_id: datasetId,
    sample_task_ids: selected,
    role,
    held_out_confirmed: role === "held_out_test" && confirmed,
    confidence: Number(confidence),
    matching_iou: Number(matchingIou),
    nms_iou: Number(nmsIou),
  };
  const identity = JSON.stringify(body);
  const request = useMemo(() => ({ id: randomId(), identity }), [identity]);
  const requestId = request.id;
  const create = useMutation({
    mutationFn: async () => {
      assert();
      const result = await sampleEvaluationsApi.create(projectId, {
        ...body,
        request_id: requestId,
      });
      assert();
      if (result.project_id !== projectId || result.request_id !== requestId)
        throw new Error("创建结果身份不符，请从评估历史核对");
      return result;
    },
    onSuccess: (result) => {
      assert();
      navigate(
        `/projects/${projectId}/learning/evaluations/${result.id}?attempt=${result.current_attempt_id}`,
      );
    },
  });
  const numericValid = [confidence, matchingIou, nmsIou].every(
    (value) =>
      value.trim() && Number.isFinite(Number(value)) && Number(value) >= 0 && Number(value) <= 1,
  );
  const canCreate =
    model.data?.model.framework === "ultralytics" &&
    !!name.trim() &&
    !!qualification.data &&
    !qualification.error &&
    !model.error &&
    model.data?.deployments.some((item) => item.id === deploymentId && item.status === "ready") &&
    !dataset.error &&
    dataset.data?.status === "ready" &&
    (dataset.data.task ?? "detect") === task &&
    selected.length > 0 &&
    selected.length <= 32 &&
    numericValid &&
    (role !== "held_out_test" || confirmed);
  const error = models.error ?? model.error ?? datasets.error ?? dataset.error ?? samples.error;
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
  const selectedDeployment = model.data?.deployments.find((item) => item.id === deploymentId);
  return (
    <PageContainer>
      <Link
        to={`/projects/${projectId}/learning/evaluations`}
        className="mb-4 inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-3.5" />
        评估历史
      </Link>
      <h1 className="text-xl font-semibold">新建逐样本评估</h1>
      <p className="mb-5 mt-2 text-sm text-muted-foreground">
        在冻结验证成员上核对一个已安装候选的误检与漏检。设置和结果会保留为独立历史记录。
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (canCreate) create.mutate();
        }}
      >
        <fieldset disabled={create.isPending} className="min-w-0 space-y-5 border-0 p-0">
          <div className="grid gap-5 lg:grid-cols-2">
            <section
              className="min-w-0 rounded-lg border border-border bg-card p-4"
              aria-labelledby="evaluation-candidate"
            >
              <h2 id="evaluation-candidate" className="mb-4 text-sm font-semibold">
                1. 候选与运行环境
              </h2>
              <div className="space-y-1 text-xs">
                <label htmlFor="evaluation-model">已注册模型</label>
                <div className="flex items-center gap-2">
                  <select
                    id="evaluation-model"
                    className={`${trainingInputClass} min-w-0 flex-1`}
                    value={modelId}
                    onChange={(e) => {
                      setModelId(e.target.value);
                      setDeploymentId("");
                      setDatasetId("");
                      setSelected([]);
                      setSamplePage(0);
                    }}
                  >
                    <option value="" disabled hidden>
                      选择候选模型
                    </option>
                    {modelId && !models.data?.items.some((item) => item.id === modelId) && (
                      <option value={modelId}>
                        {model.data?.model.name ?? "已选候选模型（暂不可用）"}
                      </option>
                    )}
                    {models.data?.items.map((item) => (
                      <option
                        key={item.id}
                        value={item.id}
                        disabled={item.framework !== "ultralytics"}
                      >
                        {item.name} · {item.framework}
                        {item.framework !== "ultralytics" ? " · 尚未通过共享评估资格" : ""}
                      </option>
                    ))}
                  </select>
                  {modelId && (
                    <CopyValueButton value={modelId} kind="id" label="复制候选模型版本 ID" />
                  )}
                </div>
              </div>
              <p className="mt-2 text-xs text-muted-foreground">
                共享样本评估当前仅支持 Ultralytics。LibreYOLO 可在模型面板试用、人工发布与回滚。
              </p>
              <PickerPage
                page={modelPage}
                total={models.data?.total ?? 0}
                onChange={setModelPage}
                label="模型"
              />
              <div className="mt-4 space-y-1 text-xs">
                <label htmlFor="evaluation-deployment">已安装部署</label>
                <div className="flex items-center gap-2">
                  <select
                    id="evaluation-deployment"
                    className={`${trainingInputClass} min-w-0 flex-1`}
                    value={deploymentId}
                    onChange={(e) => setDeploymentId(e.target.value)}
                    disabled={
                      !model.data ||
                      model.data.model.framework !== "ultralytics" ||
                      !!model.error ||
                      create.isPending
                    }
                  >
                    <option value="" disabled hidden>
                      选择就绪部署
                    </option>
                    {deploymentId &&
                      !deploymentOptions.some(({ item }) => item.id === deploymentId) && (
                        <option value={deploymentId} disabled hidden>
                          已选部署（暂不可用，请重新选择）
                        </option>
                      )}
                    {deploymentOptions.map(({ item, label }) => (
                      <option key={item.id} value={item.id}>
                        {label}
                      </option>
                    ))}
                  </select>
                  {deploymentId && (
                    <CopyValueButton value={deploymentId} kind="id" label="复制已选部署 ID" />
                  )}
                  {selectedDeployment?.registry_id && (
                    <CopyValueButton
                      value={selectedDeployment.registry_id}
                      kind="id"
                      label="复制推理后端 ID"
                    />
                  )}
                </div>
              </div>
              {model.data && !model.data.deployments.some((item) => item.status === "ready") && (
                <p className="mt-2 text-xs text-muted-foreground">
                  请先在项目模型面板安装并试用这个候选。
                </p>
              )}
              {deploymentId && qualification.isPending && (
                <p role="status" className="mt-3 text-xs text-muted-foreground">
                  正在核对权重、类别与推理环境…
                </p>
              )}
              {qualification.error && (
                <p role="alert" className="mt-3 text-xs text-status-danger">
                  当前部署不具备评估资格：
                  <CopyableError message={qualification.error.message} />
                </p>
              )}
              {qualification.data && !qualification.error && (
                <div className="mt-3 rounded-md bg-status-success-soft p-3 text-xs text-status-success">
                  <p className="flex items-center gap-1.5">
                    <Check className="size-3.5" />
                    部署已通过资格核对
                  </p>
                  <p className="mt-1">
                    训练版本 {String(qualification.data.model_ref.framework_version ?? "未知")} ·
                    推理版本 Ultralytics {qualification.data.profile.ultralytics_version} ·{" "}
                    {qualification.data.profile.effective_device} · 640 像素 · 每图最多 100 个预测
                  </p>
                </div>
              )}
            </section>
            <section
              className="min-w-0 rounded-lg border border-border bg-card p-4"
              aria-labelledby="evaluation-settings"
            >
              <h2 id="evaluation-settings" className="mb-4 text-sm font-semibold">
                2. 固定评估口径
              </h2>
              <label className="block space-y-1 text-xs">
                评估名称
                <input
                  required
                  maxLength={120}
                  className={trainingInputClass}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </label>
              <label className="mt-3 block space-y-1 text-xs">
                数据用途
                <select
                  className={trainingInputClass}
                  value={role}
                  aria-describedby="evaluation-purpose-help"
                  onChange={(e) => {
                    setRole(e.target.value as typeof role);
                    setConfirmed(false);
                  }}
                >
                  <option value="validation_selection">验证集</option>
                  <option value="held_out_test">独立测试</option>
                </select>
              </label>
              <p id="evaluation-purpose-help" className="mt-1 text-xs text-muted-foreground">
                {role === "held_out_test"
                  ? "使用未参与候选训练或模型选择的数据进行独立测试。"
                  : "使用验证集评估结果进行模型选择。"}
              </p>
              {role === "held_out_test" && (
                <label className="mt-3 flex items-start gap-2 text-xs">
                  <input
                    type="checkbox"
                    checked={confirmed}
                    onChange={(e) => setConfirmed(e.target.checked)}
                  />
                  我确认这些成员未用于候选训练或模型选择。系统还会拒绝与候选训练、验证图片 SHA
                  重叠的成员。
                </label>
              )}
              <div className="mt-4 grid gap-3 sm:grid-cols-3">
                {[
                  ["预测置信度", confidence, setConfidence],
                  [task === "segment" ? "匹配 Mask IoU" : "匹配 IoU", matchingIou, setMatchingIou],
                  ["NMS IoU", nmsIou, setNmsIou],
                ].map(([label, value, setter]) => (
                  <label key={label as string} className="space-y-1 text-xs">
                    {label as string}
                    <input
                      type="number"
                      min="0"
                      max="1"
                      step="0.01"
                      required
                      className={trainingInputClass}
                      value={value as string}
                      onChange={(e) => (setter as (v: string) => void)(e.target.value)}
                    />
                  </label>
                ))}
              </div>
              <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
                按置信度排序后进行同类一对一匹配，使用包含等号的阈值。报告 TP、FP、FN
                和精确率、召回率、F1，不报告 AP/mAP。
                {task === "segment" &&
                  "分割按原图掩码像素匹配；平均 Mask IoU、Dice 仅统计已匹配实例，漏检另计 FN。"}
              </p>
            </section>
          </div>
          <section
            className="rounded-lg border border-border bg-card p-4"
            aria-labelledby="evaluation-members"
          >
            <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
              <h2 id="evaluation-members" className="text-sm font-semibold">
                3. 冻结验证成员
              </h2>
              <span className="text-xs tabular-nums text-muted-foreground">
                已选 {selected.length} / 32
              </span>
            </div>
            <div className="max-w-xl space-y-1 text-xs">
              <label htmlFor="evaluation-dataset">就绪数据版本</label>
              <div className="flex items-center gap-2">
                <select
                  id="evaluation-dataset"
                  className={`${trainingInputClass} min-w-0 flex-1`}
                  value={datasetId}
                  disabled={!model.data || !!model.error}
                  onChange={(e) => {
                    setDatasetId(e.target.value);
                    setSelected([]);
                    setSamplePage(0);
                  }}
                >
                  <option value="" disabled hidden>
                    选择冻结数据版本
                  </option>
                  {datasetId && !datasets.data?.items.some((item) => item.id === datasetId) && (
                    <option value={datasetId}>
                      {dataset.data?.name ?? "已选数据版本（暂不可用）"}
                    </option>
                  )}
                  {datasets.data?.items
                    .filter((item) => (item.task ?? "detect") === task)
                    .map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                </select>
                {datasetId && !datasets.data?.items.some((item) => item.id === datasetId) && (
                  <CopyValueButton value={datasetId} kind="id" label="复制已选数据版本 ID" />
                )}
              </div>
            </div>
            <PickerPage
              page={datasetPage}
              total={datasets.data?.total ?? 0}
              onChange={setDatasetPage}
              label="数据版本"
            />
            {datasetId && samples.isPending && (
              <p role="status" className="mt-4 text-xs text-muted-foreground">
                正在读取冻结验证成员…
              </p>
            )}
            {samples.data && !samples.error && (
              <>
                <div className="mt-4 divide-y divide-border">
                  {samples.data.items.map((item, index) => {
                    const checked = selected.includes(item.task_id);
                    const unsupported =
                      !item.included ||
                      !!item.issue ||
                      !item.sha256 ||
                      !item.label_sha256 ||
                      !item.width ||
                      !item.height ||
                      (item.instances ?? 201) > 200 ||
                      (item.size_bytes ?? Infinity) > 16 * 1024 * 1024 ||
                      (item.width ?? Infinity) * (item.height ?? Infinity) >
                        (qualification.data?.limits?.pixels ??
                          (task === "segment" ? 1_280_000 : 16_000_000)) ||
                      (task === "segment" &&
                        Math.max(item.width ?? Infinity, item.height ?? Infinity) >
                          (qualification.data?.limits?.max_dimension ?? 4096));
                    return (
                      <div key={item.task_id} className="flex items-start gap-3 py-3 text-xs">
                        <label className="flex min-w-0 flex-1 items-start gap-3">
                          <input
                            aria-label={`选择验证成员 ${samplePage * 12 + index + 1}（${item.width ?? "?"} × ${item.height ?? "?"}）`}
                            type="checkbox"
                            className="mt-0.5"
                            checked={checked}
                            disabled={
                              unsupported || (!checked && selected.length >= 32) || create.isPending
                            }
                            onChange={() =>
                              setSelected((old) =>
                                checked
                                  ? old.filter((id) => id !== item.task_id)
                                  : [...old, item.task_id],
                              )
                            }
                          />
                          <span className="min-w-0">
                            <span className="block font-medium">
                              样本 {samplePage * 12 + index + 1}
                            </span>
                            <span className="block text-muted-foreground">
                              {item.width ?? "?"} × {item.height ?? "?"} · {item.instances ?? "?"}{" "}
                              个真值{item.is_negative ? " · 冻结负样本" : ""}
                              {unsupported
                                ? ` · 不支持：${item.issue ?? "成员或预算不符合要求"}`
                                : ""}
                            </span>
                          </span>
                        </label>
                        <CopyValueButton
                          value={item.task_id}
                          kind="id"
                          label="复制验证成员任务 ID"
                        />
                      </div>
                    );
                  })}
                </div>
                {samples.data.total === 0 && (
                  <p className="mt-4 text-xs text-muted-foreground">此版本没有可选验证成员。</p>
                )}
                <PickerPage
                  page={samplePage}
                  total={samples.data.total}
                  pageSize={12}
                  onChange={setSamplePage}
                  label="成员"
                />
              </>
            )}
            <p className="mt-3 text-xs text-muted-foreground">
              选择会跨页保留；相同内容的不同成员分别计数。总真值不超过 2,000
              个，开始前会再次验证完整性与类别绑定。
            </p>
          </section>
          {error && (
            <p role="alert" className="text-sm text-status-danger">
              <CopyableError message={error.message} />
            </p>
          )}
          {create.error && create.error.name !== "AbortError" && (
            <p role="alert" className="text-sm text-status-danger">
              <CopyableError message={create.error.message} />
              。网络响应不确定时，可保持设置再次提交同一请求，或先查看评估历史。
            </p>
          )}
          <div className="flex flex-wrap items-center gap-3">
            <Button type="submit" variant="primary" disabled={!canCreate || create.isPending}>
              <Plus className="size-4" />
              {create.isPending ? "正在冻结并排队…" : "开始逐样本评估"}
            </Button>
            <span className="text-xs text-muted-foreground">不会写入项目预测或修改标注。</span>
          </div>
        </fieldset>
      </form>
    </PageContainer>
  );
}

export function PickerPage({
  page,
  total,
  pageSize = 20,
  onChange,
  label,
  onPageSizeChange,
  loading,
}: {
  page: number;
  total: number;
  pageSize?: number;
  onChange: (page: number) => void;
  label: string;
  onPageSizeChange?: (size: number) => void;
  loading?: boolean;
}) {
  if (onPageSizeChange) {
    return (
      <ListPagination
        label={label}
        page={page}
        pageSize={pageSize}
        total={total}
        loading={loading}
        onPageChange={onChange}
        onPageSizeChange={onPageSizeChange}
        previousLabel={`${label}上一页`}
        nextLabel={`${label}下一页`}
      />
    );
  }
  if (total <= pageSize && page === 0) return null;
  return (
    <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
      <Button
        type="button"
        size="xs"
        disabled={page === 0}
        onClick={() => onChange(page - 1)}
        aria-label={`${label}上一页`}
      >
        上一页
      </Button>
      <span className="tabular-nums">
        第 {page + 1} 页 · 共 {total} 项
      </span>
      <Button
        type="button"
        size="xs"
        disabled={(page + 1) * pageSize >= total}
        onClick={() => onChange(page + 1)}
        aria-label={`${label}下一页`}
      >
        下一页
      </Button>
    </div>
  );
}
