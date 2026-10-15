import { useState } from "react";
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
    parameters: Object.fromEntries(Object.entries(c.parameters).map(([k, v]) => [k, String(v)])),
  };
}

export function ProjectLearningPolicyPanel({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const policy = useQuery({
    queryKey: ["learning-policy", projectId],
    queryFn: () => learningPoliciesApi.get(projectId),
    refetchInterval: 3000,
  });
  const [page, setPage] = useState(0);
  const history = useQuery({
    queryKey: ["learning-triggers", projectId, page],
    queryFn: () => learningPoliciesApi.triggers(projectId, page * 20),
    refetchInterval: 3000,
  });
  const catalog = useQuery({
    queryKey: ["training-catalog", projectId],
    queryFn: () => trainingRunsApi.catalog(projectId),
  });
  const [datasetPage, setDatasetPage] = useState(0);
  const datasets = useQuery({
    queryKey: ["policy-datasets", projectId, datasetPage],
    queryFn: () => trainingDatasetsApi.list(projectId, datasetPage * 20),
  });
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Form>(empty);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState("");
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
      setError("");
      if (kind === "save") {
        const template = catalog.data?.items.find((t) => t.framework === draft.framework);
        const body: LearningPolicySave = {
          expected_revision: revision,
          enabled: draft.enabled,
          parent_id: draft.parent,
          batch_id: draft.batch,
          framework: draft.framework,
          base_model: draft.base || template?.base_models[0]?.id || "",
          parameters: Object.fromEntries(
            Object.entries(draft.parameters).map(([k, v]) => [k, Number(v)]),
          ) as LearningPolicySave["parameters"],
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
      )
        await learningPoliciesApi.remove(projectId);
    },
    onSuccess: () => {
      setEditing(false);
      refresh();
    },
    onError: (e: Error) => {
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
  const change = (key: keyof Omit<Form, "parameters">, value: string | boolean) =>
    setDraft((d) => ({ ...d, [key]: value }));
  const template = catalog.data?.items.find((t) => t.framework === draft.framework);
  const selectedParent = datasets.data?.items.find((d) => d.id === draft.parent);
  const currentConfig = policy.data?.config as
    | { min_changed_samples?: number; check_interval_minutes?: number; cooldown_minutes?: number }
    | undefined;
  const busy = action.isPending;
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
                  .filter((d) => d.status === "ready")
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
                value={draft.base || template?.base_models[0]?.id || ""}
                onChange={(e) => change("base", e.target.value)}
              >
                {template?.base_models.map((m) => (
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
            。继承初始划分，之后继承最近成功基线；验证集变化不会增加训练样本计数。保存策略会固定当前训练器版本，失败后修改并保存可明确重试。
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
                !draft.parent || !draft.threshold || Number(draft.threshold) < 1 || !template
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
