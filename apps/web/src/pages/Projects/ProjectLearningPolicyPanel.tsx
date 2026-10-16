import { normalizeTask } from "@/api/generated/capabilityVocab.gen";
import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  learningPoliciesApi,
  type LearningPolicyOut,
  type LearningPolicySave,
} from "@/api/learningPolicies";
import { trainingRunsApi } from "@/api/trainingRuns";
import { trainingDatasetsApi } from "@/api/trainingDatasets";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { useAuthStore } from "@/stores/authStore";
import {
  recipeCandidate,
  initializationReference,
  trainingRecipesApi,
  type ReusableSnapshot,
  type TrainingReuseCandidate,
} from "@/api/trainingRecipes";
import {
  TrainingRecipeLibrary,
  TrainingRecipeStatus,
} from "@/pages/Training/TrainingRecipeLibrary";
import { TrainingReuseDialog } from "@/pages/Training/TrainingReuseDialog";
import {
  useTrainingSessionRevision,
  useTrainingViewOwner,
} from "@/pages/Training/trainingViewOwner";

const inputClass =
  "w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus-visible:ring-2 focus-visible:ring-ring";
const labels: Record<string, string> = {
  disabled: "已关闭",
  waiting: "等待变化",
  building: "冻结数据中",
  active: "自动实验进行中",
  cooldown: "冷却中",
  invalid_data: "数据需处理",
  suspended: "已停止触发",
  failed: "失败待处理",
  succeeded: "训练完成",
  cancelled: "已取消",
  queued: "实验排队中",
  running: "实验进行中",
  skipped: "未触发训练",
};
type Form = {
  enabled: boolean;
  parent: string;
  batch: string | null;
  framework: "ultralytics" | "libreyolo";
  base: string;
  threshold: string;
  interval: string;
  cooldown: string;
  parameters: Record<string, string>;
  originalParameters?: Record<string, string>;
  configuration?: LearningPolicySave["configuration"];
  origin?: LearningPolicySave["origin"];
  initialization?: LearningPolicySave["initialization"];
};
const empty: Form = {
  enabled: false,
  parent: "",
  batch: null,
  framework: "ultralytics",
  base: "",
  threshold: "",
  interval: "5",
  cooldown: "60",
  parameters: { epochs: "50", batch: "8", imgsz: "640", lr0: "0.01", seed: "42", patience: "50" },
};
function initial(policy: LearningPolicyOut | null): Form {
  if (!policy) return empty;
  const c = policy.config as {
    parent_id: string;
    batch_id?: string | null;
    template: { framework: Form["framework"]; base_model: { id: string } };
    min_changed_samples: number;
    check_interval_minutes: number;
    cooldown_minutes: number;
    parameters: Record<string, number>;
    configuration?: ReusableSnapshot["configuration"];
    origin?: LearningPolicySave["origin"];
    initialization?: ReusableSnapshot["initialization"];
  };
  return {
    enabled: policy.enabled,
    parent: c.parent_id,
    batch: c.batch_id ?? null,
    framework: c.template.framework,
    base: c.template.base_model.id,
    threshold: String(c.min_changed_samples),
    interval: String(c.check_interval_minutes),
    cooldown: String(c.cooldown_minutes),
    initialization: initializationReference(c.initialization),
    parameters: Object.fromEntries(Object.entries(c.parameters).map(([k, v]) => [k, String(v)])),
    originalParameters: Object.fromEntries(
      Object.entries(c.parameters).map(([k, v]) => [k, String(v)]),
    ),
    ...(c.configuration
      ? {
          configuration: {
            schema_id: c.configuration.identity.schema_id,
            schema_digest: c.configuration.identity.schema_digest,
            values: c.configuration.user_values,
          },
        }
      : {}),
    ...(c.origin
      ? {
          origin: {
            kind: "recipe" as const,
            recipe_id: c.origin.recipe_id,
            revision_id: c.origin.revision_id,
            digest: c.origin.digest,
          },
        }
      : {}),
  };
}

export function ProjectLearningPolicyPanel({ projectId }: { projectId: string }) {
  const userId = useAuthStore((state) => state.user?.id ?? "");
  const token = useAuthStore((state) => state.token ?? "");
  const sessionRevision = useTrainingSessionRevision();
  const owner = useMemo(
    () => ({ projectId, userId, sessionToken: token, sessionRevision, key: crypto.randomUUID() }),
    [projectId, userId, token, sessionRevision],
  );
  return <ProjectLearningPolicyContent key={owner.key} projectId={projectId} />;
}

function ProjectLearningPolicyContent({ projectId }: { projectId: string }) {
  const view = useTrainingViewOwner(projectId, "policy");
  const [scope] = useState(() => crypto.randomUUID());
  const queryClient = useQueryClient();
  const policy = useQuery({
    queryKey: ["learning-policy", projectId, scope],
    queryFn: () => learningPoliciesApi.get(projectId),
    refetchInterval: 3000,
  });
  const [page, setPage] = useState(0);
  const history = useQuery({
    queryKey: ["learning-triggers", projectId, page, scope],
    queryFn: () => learningPoliciesApi.triggers(projectId, page * 20),
    refetchInterval: 3000,
  });
  const catalog = useQuery({
    queryKey: ["training-catalog", projectId, scope],
    queryFn: () => trainingRunsApi.catalog(projectId),
  });
  const [datasetPage, setDatasetPage] = useState(0);
  const datasets = useQuery({
    queryKey: ["policy-datasets", projectId, datasetPage, scope],
    queryFn: () => trainingDatasetsApi.list(projectId, datasetPage * 20),
  });
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Form>(empty);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState("");
  const [reuseBusy, setReuseBusy] = useState(false);
  const reuseLock = useRef(false);
  const [preview, setPreview] = useState<{
    candidate: TrainingReuseCandidate;
    before: ReusableSnapshot | null;
    draft: Form;
    revision: number;
  } | null>(null);
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const refresh = () => {
    for (const key of [
      "learning-policy",
      "learning-triggers",
      "training-runs",
      "training-datasets",
    ])
      void queryClient.invalidateQueries({ queryKey: [key, projectId] });
  };
  const action = useMutation({
    mutationFn: async (kind: "save" | "disable" | "check" | "remove") => {
      view.assert();
      setError("");
      if (kind === "save") {
        for (const value of [draft.threshold, draft.interval, draft.cooldown])
          if (!value.trim() || !Number.isFinite(Number(value)))
            throw new Error("门槛、检查间隔和冷却时间必须为明确的有限数值");
        const numeric = Object.fromEntries(
          Object.entries(draft.parameters).map(([key, value]) => {
            if (!value.trim() || !Number.isFinite(Number(value)))
              throw new Error(`${key} 必须为明确的有限数值`);
            return [key, Number(value)];
          }),
        );
        const configuration = draft.configuration
          ? {
              ...draft.configuration,
              values: {
                ...draft.configuration.values,
                ...Object.fromEntries(
                  Object.entries(numeric).filter(
                    ([key]) => draft.parameters[key] !== draft.originalParameters?.[key],
                  ),
                ),
              },
            }
          : null;
        const body: LearningPolicySave = {
          expected_revision: revision,
          enabled: draft.enabled,
          parent_id: draft.parent,
          batch_id: draft.batch,
          framework: draft.framework,
          base_model: draft.base,
          ...(configuration
            ? { configuration }
            : { parameters: numeric as LearningPolicySave["parameters"] }),
          ...(draft.origin ? { origin: draft.origin } : {}),
          ...(draft.initialization ? { initialization: draft.initialization } : {}),
          min_changed_samples: Number(draft.threshold),
          check_interval_minutes: Number(draft.interval),
          cooldown_minutes: Number(draft.cooldown),
        };
        await learningPoliciesApi.save(projectId, body);
      } else if (kind === "disable")
        await learningPoliciesApi.disable(projectId, policy.data!.revision);
      else if (kind === "check") await learningPoliciesApi.check(projectId);
      else if (
        await confirmDialog({
          title: "删除自动策略与触发记录",
          description: "仅在没有被引用的实验后删除。训练数据与当前项目发布保留。",
          confirmLabel: "删除策略",
          tone: "danger",
        })
      ) {
        view.assert();
        await learningPoliciesApi.remove(projectId);
      }
    },
    onSuccess: () => {
      if (!view.owns()) return;
      setEditing(false);
      refresh();
    },
    onError: (e: Error) => {
      if (!view.owns()) return;
      setError(e.message);
      refresh();
    },
  });
  const begin = () => {
    setDraft(initial(policy.data ?? null));
    setRevision(policy.data?.revision ?? 0);
    setEditing(true);
    setError("");
  };
  const change = (
    key:
      | "enabled"
      | "parent"
      | "batch"
      | "framework"
      | "base"
      | "threshold"
      | "interval"
      | "cooldown",
    value: string | boolean,
  ) => setDraft((d) => ({ ...d, [key]: value }));
  const template = catalog.data?.items.find((t) => t.framework === draft.framework);
  const selectedParent = datasets.data?.items.find((d) => d.id === draft.parent);
  const currentConfig = policy.data?.config as
    | { min_changed_samples?: number; check_interval_minutes?: number; cooldown_minutes?: number }
    | undefined;
  const busy = action.isPending || reuseBusy || !!preview;
  const previewRecipe = async (candidate: TrainingReuseCandidate) => {
    if (!editing || busy || reuseLock.current) return;
    const captured = draftRef.current;
    const capturedRevision = revision;
    reuseLock.current = true;
    setReuseBusy(true);
    setError("");
    try {
      const latest = recipeCandidate(
        await trainingRecipesApi.get(
          projectId,
          candidate.origin.recipe_id!,
          candidate.origin.revision_id!,
        ),
      );
      if (!latest.compatible) throw new Error(latest.incompatibility ?? "配方不兼容");
      if (
        normalizeTask(
          (latest.snapshot as ReusableSnapshot).configuration.identity.task ?? "detect",
        ) !== "detect"
      )
        throw new Error("自动学习策略目前仅支持图片检测配方");
      let before: ReusableSnapshot | null = null;
      if (captured.configuration)
        before = await trainingRecipesApi.resolve(projectId, {
          framework: captured.framework,
          base_model: captured.base,
          ...(captured.initialization ? { initialization: captured.initialization } : {}),
          configuration: {
            ...captured.configuration,
            values: {
              ...captured.configuration.values,
              ...Object.fromEntries(
                Object.entries(captured.parameters)
                  .filter(([key, value]) => value !== captured.originalParameters?.[key])
                  .map(([key, value]) => {
                    if (!value.trim() || !Number.isFinite(Number(value)))
                      throw new Error("请先修正当前参数");
                    return [key, Number(value)];
                  }),
              ),
            },
          },
        });
      if (view.owns() && draftRef.current === captured)
        setPreview({ candidate: latest, before, draft: captured, revision: capturedRevision });
    } catch (reason) {
      if (view.owns() && draftRef.current === captured)
        setError(reason instanceof Error ? reason.message : "核对失败");
    } finally {
      reuseLock.current = false;
      if (view.owns()) setReuseBusy(false);
    }
  };
  const acceptRecipe = async () => {
    if (
      !preview ||
      reuseLock.current ||
      draftRef.current !== preview.draft ||
      revision !== preview.revision
    )
      return;
    reuseLock.current = true;
    setReuseBusy(true);
    setError("");
    try {
      const latest = recipeCandidate(
        await trainingRecipesApi.get(
          projectId,
          preview.candidate.origin.recipe_id!,
          preview.candidate.origin.revision_id!,
        ),
      );
      if (!latest.compatible || latest.snapshot_digest !== preview.candidate.snapshot_digest)
        throw new Error(latest.incompatibility ?? "配方已变化");
      if (
        normalizeTask(
          (latest.snapshot as ReusableSnapshot).configuration.identity.task ?? "detect",
        ) !== "detect"
      )
        throw new Error("自动学习策略目前仅支持图片检测配方");
      const current = await learningPoliciesApi.get(projectId);
      if ((current?.revision ?? 0) !== preview.revision)
        throw new Error("策略已变化，保留编辑内容；请刷新后再采纳");
      if (!view.owns() || draftRef.current !== preview.draft) return;
      const snapshot = latest.snapshot as ReusableSnapshot;
      const parameters = Object.fromEntries(
        Object.entries(snapshot.configuration.effective_values)
          .filter(([key]) => ["epochs", "batch", "imgsz", "lr0", "seed", "patience"].includes(key))
          .map(([key, value]) => [key, String(value)]),
      );
      setDraft({
        ...preview.draft,
        framework: snapshot.framework,
        base: snapshot.base_model.id,
        parameters,
        originalParameters: parameters,
        configuration: {
          schema_id: snapshot.configuration.identity.schema_id,
          schema_digest: snapshot.configuration.identity.schema_digest,
          values: snapshot.configuration.user_values,
        },
        origin: latest.origin,
        initialization: initializationReference(snapshot.initialization),
      });
      setPreview(null);
    } catch (reason) {
      if (view.owns()) setError(reason instanceof Error ? reason.message : "采纳失败");
    } finally {
      reuseLock.current = false;
      if (view.owns()) setReuseBusy(false);
    }
  };
  return (
    <section
      aria-label="自动训练策略"
      className="space-y-4 rounded-lg border border-border bg-card p-5"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-base font-semibold">自动训练策略</h2>
        <Badge variant={policy.data?.enabled ? "success" : "default"}>
          {policy.data ? (labels[policy.data.state] ?? policy.data.state) : "尚未配置 · 默认关闭"}
        </Badge>
      </div>
      <p className="text-sm text-muted-foreground">
        只使用已审核图片；按训练分区的新增或修正内容计数，相同图片合并。训练完成后仍需人工注册、试用与发布模型。
      </p>
      {(policy.error || history.error || error) && (
        <p role="alert" className="text-sm text-status-danger">
          {error || policy.error?.message || history.error?.message}
        </p>
      )}
      {policy.data && (
        <div className="space-y-1 text-sm">
          <p>
            策略 {policy.data.revision} · 门槛 {currentConfig?.min_changed_samples} 个 · 每{" "}
            {currentConfig?.check_interval_minutes} 分钟检查 · 冷却{" "}
            {currentConfig?.cooldown_minutes} 分钟
          </p>
          <p className="text-muted-foreground">{policy.data.message}</p>
          {initial(policy.data).origin && (
            <TrainingRecipeStatus projectId={projectId} origin={initial(policy.data).origin!} />
          )}
          {initial(policy.data).initialization?.kind === "trained_model" && (
            <p className="break-all text-xs text-muted-foreground">
              初始化候选：{initial(policy.data).initialization?.model_version_id} · 来源摘要{" "}
              {initial(policy.data).initialization?.source_digest}
              。每轮创建全新实验，来源不会跟随发布变化。
            </p>
          )}
          <p className="text-muted-foreground">
            成功基线：
            {policy.data.baseline_dataset_id
              ? "最近成功的自动训练数据"
              : "尚无，首轮统计当前合格训练样本"}
          </p>
        </div>
      )}
      {!editing && (
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            onClick={begin}
            disabled={busy || policy.isPending || !catalog.data?.enabled}
          >
            {policy.data ? "编辑自动策略" : "配置自动策略"}
          </Button>
          {policy.data && (
            <>
              <Button
                size="sm"
                variant="default"
                onClick={() => action.mutate("check")}
                disabled={busy}
              >
                立即检查与同步
              </Button>
              {policy.data.enabled ? (
                <Button
                  size="sm"
                  variant="default"
                  onClick={() => action.mutate("disable")}
                  disabled={busy}
                >
                  关闭自动训练
                </Button>
              ) : (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => action.mutate("remove")}
                  disabled={busy}
                >
                  删除策略与记录
                </Button>
              )}
            </>
          )}
        </div>
      )}
      {!catalog.data?.enabled && (
        <p className="text-sm text-muted-foreground">
          管理员配置受控训练执行器后可保存策略；已有策略仍可关闭。
        </p>
      )}
      {editing && (
        <div className="space-y-4">
          <TrainingRecipeLibrary
            projectId={projectId}
            disabled={busy}
            onChoose={(candidate) => void previewRecipe(candidate)}
          />
          {draft.origin && (
            <div className="rounded-md border border-border p-3 text-xs">
              <p>已固定具体配方修订。参数保持只读；另存新修订后可明确采纳。</p>
              <TrainingRecipeStatus projectId={projectId} origin={draft.origin} />
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() => setDraft((d) => ({ ...d, origin: undefined }))}
              >
                解除策略配方
              </Button>
            </div>
          )}
          {draft.configuration && (
            <p className="text-xs text-muted-foreground">
              保留完整高级配置。更换训练器、基础权重或高级参数，请在配置工作区保存配方后在此核对采纳。
            </p>
          )}
          {draft.initialization?.kind === "trained_model" && (
            <p className="break-all text-xs text-muted-foreground">
              已固定初始化模型 {draft.initialization.model_version_id}，摘要{" "}
              {draft.initialization.source_digest}
              。解除配方来源会保留权重选择；更换权重请在配置工作区另存配方后采用。
            </p>
          )}
          <fieldset disabled={busy} className="space-y-4 rounded-md border border-border p-4">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              <label className="space-y-1 text-sm">
                初始划分数据
                <select
                  aria-label="初始划分数据"
                  className={inputClass}
                  value={draft.parent}
                  onChange={(e) => change("parent", e.target.value)}
                >
                  <option value="">选择已就绪版本</option>
                  {draft.parent && !selectedParent && (
                    <option value={draft.parent}>当前策略的初始数据</option>
                  )}
                  {datasets.data?.items
                    .filter((d) => d.status === "ready" && (d.task ?? "detect") === "detect")
                    .map((d) => (
                      <option value={d.id} key={d.id}>
                        {d.name}
                      </option>
                    ))}
                </select>
              </label>
              <label className="space-y-1 text-sm">
                自动训练器
                <select
                  aria-label="自动训练器"
                  className={inputClass}
                  value={draft.framework}
                  disabled={!!draft.configuration}
                  onChange={(e) => {
                    change("framework", e.target.value);
                    change("base", "");
                  }}
                >
                  {catalog.data?.items.map((t) => (
                    <option key={t.framework} value={t.framework}>
                      {t.framework} {t.version}
                    </option>
                  ))}
                </select>
              </label>
              <label className="space-y-1 text-sm">
                自动基础权重
                <select
                  aria-label="自动基础权重"
                  className={inputClass}
                  value={draft.base}
                  disabled={!!draft.configuration}
                  onChange={(e) => change("base", e.target.value)}
                >
                  <option value="">明确选择基础权重</option>
                  {draft.base &&
                    !template?.base_models.some((model) => model.id === draft.base) && (
                      <option value={draft.base}>{draft.base}（当前不可用）</option>
                    )}
                  {template?.base_models
                    .filter((m) => (m.task ?? "detect") === "detect")
                    .map((m) => (
                      <option value={m.id} key={m.id}>
                        {m.id}
                      </option>
                    ))}
                </select>
              </label>
              {[
                ["threshold", "变化样本门槛", 1, 20000],
                ["interval", "检查间隔（分钟）", 1, 1440],
                ["cooldown", "冷却时间（分钟）", 0, 10080],
              ].map(([key, label, min, max]) => (
                <label className="space-y-1 text-sm" key={key}>
                  {label}
                  <input
                    aria-label={String(label)}
                    className={inputClass}
                    type="number"
                    min={min}
                    max={max}
                    value={draft[key as "threshold" | "interval" | "cooldown"]}
                    onChange={(e) =>
                      change(key as "threshold" | "interval" | "cooldown", e.target.value)
                    }
                  />
                </label>
              ))}
              {(
                [
                  ["epochs", "自动训练轮数"],
                  ["batch", "自动批大小"],
                  ["imgsz", "自动图片尺寸"],
                  ["lr0", "自动初始学习率"],
                  ["seed", "自动训练种子"],
                  ["patience", "自动早停耐心轮数"],
                ] as const
              ).map(([key, label]) => (
                <label className="space-y-1 text-sm" key={key}>
                  {label}
                  <input
                    aria-label={label}
                    className={inputClass}
                    type="number"
                    step={key === "lr0" ? "0.001" : "1"}
                    value={draft.parameters[key]}
                    disabled={!!draft.origin}
                    onChange={(e) =>
                      setDraft((d) => ({
                        ...d,
                        parameters: { ...d.parameters, [key]: e.target.value },
                      }))
                    }
                  />
                </label>
              ))}
            </div>
            <div className="flex gap-2">
              <Button
                size="sm"
                variant="ghost"
                disabled={datasetPage === 0}
                onClick={() => setDatasetPage((p) => p - 1)}
              >
                上一页初始数据
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={(datasetPage + 1) * 20 >= (datasets.data?.total ?? 0)}
                onClick={() => setDatasetPage((p) => p + 1)}
              >
                下一页初始数据
              </Button>
            </div>
            <p className="text-sm text-muted-foreground">
              范围：{draft.batch ? "保留当前策略的指定批次" : "整个项目"}
              。继承初始划分，之后继承最近成功基线；验证集变化不会增加训练样本计数。保存会产生新的策略修订，并取消旧修订尚未开始的候选和排队实验，已经开始的实验继续。
            </p>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={draft.enabled}
                onChange={(e) => change("enabled", e.target.checked)}
              />
              启用自动训练
            </label>
            <div className="flex gap-2">
              <Button
                size="sm"
                disabled={
                  !draft.parent ||
                  !draft.base ||
                  !draft.threshold ||
                  Number(draft.threshold) < 1 ||
                  !template
                }
                onClick={() => action.mutate("save")}
              >
                保存自动策略
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
                取消编辑
              </Button>
            </div>
          </fieldset>
        </div>
      )}
      {preview && (
        <TrainingReuseDialog
          purpose="policy"
          candidate={preview.candidate}
          before={preview.before}
          busy={reuseBusy}
          error={error}
          onCancel={() => {
            setPreview(null);
            setError("");
          }}
          onAccept={() => void acceptRecipe()}
        />
      )}
      <details>
        <summary className="cursor-pointer text-sm">
          自动触发记录（{history.data?.total ?? 0}）
        </summary>
        <div className="mt-3 space-y-3">
          {history.data?.items.map((t) => (
            <div className="space-y-1 rounded-md border border-border p-3 text-sm" key={t.id}>
              <p>
                策略 {t.policy_revision} · {labels[t.status] ?? t.status} · 变化 {t.changed_samples}{" "}
                个
              </p>
              <p className="text-muted-foreground">{t.message ?? "等待执行结果"}</p>
              <p className="break-all text-xs text-muted-foreground">
                数据版本：{t.dataset_id}
                {t.run_id && ` · 实验：${t.run_id}`}
              </p>
            </div>
          ))}
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="ghost"
              disabled={page === 0}
              onClick={() => setPage((p) => p - 1)}
            >
              上一页触发记录
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={(page + 1) * 20 >= (history.data?.total ?? 0)}
              onClick={() => setPage((p) => p + 1)}
            >
              下一页触发记录
            </Button>
          </div>
        </div>
      </details>
    </section>
  );
}
