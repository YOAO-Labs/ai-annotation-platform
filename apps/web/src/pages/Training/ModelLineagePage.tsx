import { randomId } from "@/utils/id";
import { useMemo, useRef, useState } from "react";
import { Link, useLocation, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ExternalLink } from "lucide-react";
import {
  lineageReturn,
  modelLineageApi,
  type LineageNode,
  type NodeKind,
  type ModelDecisionCreate,
} from "@/api/modelLineage";
import { PageContainer } from "@/components/layout/PageContainer";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { useTrainingSessionRevision, useTrainingViewOwner } from "./trainingViewOwner";
import { trainingInputClass } from "./TrainingParameterField";

const kindNames: Record<NodeKind, string> = {
  dataset: "数据版本",
  run: "训练实验",
  attempt: "执行尝试",
  model: "候选模型",
  deployment: "部署检查",
  release: "历史发布",
  recipe: "项目配方",
  recipe_revision: "配方修订",
  evaluation: "样本评估",
};
const relationNames: Record<string, string> = {
  dataset_parent: "父数据版本",
  training_data: "训练数据",
  current_attempt: "当前执行尝试",
  experiment: "训练实验",
  exact_attempt: "来源执行尝试",
  checkpoint_resume: "断点续训来源",
  configuration_clone: "配置克隆来源",
  clone_context_attempt: "克隆时参考的尝试",
  recipe_configuration: "采用的配方修订",
  recipe: "所属配方",
  candidate: "候选模型",
  published_candidate: "发布的候选",
  published_deployment: "发布时的部署",
  previous_selection: "操作前的发布选择",
  rollback_target: "采用的回退目标",
  evaluation_candidate: "评估的候选",
  evaluation_deployment: "评估使用的部署",
  evaluation_data: "冻结评估数据",
  child_datasets: "后续数据版本",
  runs: "使用此数据的实验",
  attempts: "执行历史",
  models: "注册的候选",
  resumed_attempts: "后续断点续训",
  deployments: "部署检查",
  releases: "发布记录",
  evaluations: "逐样本评估",
  subsequent_releases: "后续发布选择",
  rollback_uses: "采用此目标的回退",
  revisions: "配方修订",
  predictions: "可归属的预测记录",
  frozen_provenance: "冻结训练来源",
  frozen_release: "冻结发布身份",
  frozen_evaluation: "冻结评估身份",
  configuration_origin: "配置来源",
  weights_initialization: "初始化权重来源",
  initialized_runs: "由此权重初始化的实验",
  initialization_recipes: "固定此权重的配方修订",
};
const dispositions = {
  active: "恢复默认显示",
  research: "保留研究",
  declined: "不予发布",
  archived: "归档候选",
};
const factNames: Record<string, string> = {
  run_id: "训练实验 ID",
  ordinal: "执行序号",
  generation: "发布代次",
  snapshot_digest: "快照摘要",
  config_digest: "训练配置摘要",
  spec_digest: "评估配置摘要",
  framework: "框架",
  weights_sha256: "权重 SHA-256",
  binding_digest: "类别绑定摘要",
  dataset_digest: "数据摘要",
  validation_digest: "验证基准摘要",
  checked_at: "最近检查时间",
  archived: "配方已归档",
  deletion_requested_at: "删除请求时间",
  frozen_run_id: "冻结实验 ID",
  frozen_attempt_id: "冻结尝试 ID",
  frozen_dataset_id: "冻结数据 ID",
  frozen_dataset_digest: "冻结数据摘要",
  frozen_validation_digest: "冻结验证基准",
  frozen_config_digest: "冻结配置摘要",
  frozen_model_version_id: "冻结候选 ID",
  frozen_deployment_id: "冻结部署 ID",
  frozen_sha256: "冻结权重 SHA-256",
  frozen_reference_digest: "冻结引用摘要",
  frozen_project_id: "冻结项目 ID",
  initialization_model_id: "初始化模型引用",
  initialization_kind: "初始化方式",
  initialization_project_id: "初始化来源项目",
  initialization_model_version_id: "初始化来源模型",
  initialization_run_id: "初始化来源实验",
  initialization_attempt_id: "初始化来源执行尝试",
  initialization_source_digest: "初始化来源摘要",
  initialization_policy_digest: "初始化资格摘要",
};
const hashFactKeys = new Set([
  "snapshot_digest",
  "config_digest",
  "spec_digest",
  "weights_sha256",
  "binding_digest",
  "dataset_digest",
  "validation_digest",
  "frozen_dataset_digest",
  "frozen_validation_digest",
  "frozen_config_digest",
  "frozen_sha256",
  "frozen_reference_digest",
  "initialization_source_digest",
  "initialization_policy_digest",
]);
const idFactKeys = new Set([
  "run_id",
  "frozen_run_id",
  "frozen_attempt_id",
  "frozen_dataset_id",
  "frozen_model_version_id",
  "frozen_deployment_id",
  "frozen_project_id",
  "initialization_model_id",
  "initialization_project_id",
  "initialization_model_version_id",
  "initialization_run_id",
  "initialization_attempt_id",
]);

function existingDetail(projectId: string, node: LineageNode) {
  const base = `/projects/${projectId}/learning`;
  if (node.kind === "dataset") return `${base}?tab=data&dataset=${node.id}`;
  if (node.kind === "run") return `${base}?tab=training&run=${node.id}`;
  if (node.kind === "attempt" && node.facts?.run_id)
    return `${base}?tab=training&run=${node.facts.run_id}&attempt=${node.id}`;
  if (node.kind === "model") return `${base}?tab=models&model=${node.id}&archive=include`;
  if (node.kind === "evaluation") return `${base}/evaluations/${node.id}`;
  return undefined;
}

export function ModelLineagePage() {
  const { id = "", kind = "", nodeId = "" } = useParams();
  const revision = useTrainingSessionRevision();
  const scope = useMemo(
    () => ({ key: randomId(), id, kind, nodeId, revision }),
    [id, kind, nodeId, revision],
  );
  if (!Object.prototype.hasOwnProperty.call(kindNames, kind))
    return (
      <PageContainer>
        <p role="alert">谱系节点类型无效</p>
        <Link to={`/projects/${id}/learning?tab=models`}>返回模型</Link>
      </PageContainer>
    );
  return (
    <LineageContent
      key={scope.key}
      projectId={id}
      kind={kind as NodeKind}
      nodeId={nodeId}
      scope={scope.key}
    />
  );
}
function LineageContent({
  projectId,
  kind,
  nodeId,
  scope,
}: {
  projectId: string;
  kind: NodeKind;
  nodeId: string;
  scope: string;
}) {
  const session = useTrainingViewOwner(projectId, scope);
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const viewIdentity = useMemo(() => ({ key: randomId(), location: location.key }), [location.key]);
  const viewKey = viewIdentity.key;
  const view = useRef(viewKey);
  view.current = viewKey;
  const relation = params.get("relation") || undefined;
  const cursor = params.get("cursor") || undefined;
  const before = Number(params.get("before")) > 0 ? Number(params.get("before")) : undefined;
  const back = lineageReturn(projectId, params.get("returnTo"));
  const trail = (params.get("trail") ?? "").split(",").filter(Boolean).slice(-32);
  const identity = `${kind}:${nodeId}`;
  const cyclic = trail.includes(identity);
  const update = (values: Record<string, string | undefined>) =>
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      Object.entries(values).forEach(([key, value]) =>
        value === undefined ? next.delete(key) : next.set(key, value),
      );
      return next;
    });
  const query = useQuery({
    queryKey: ["model-lineage", projectId, scope, kind, nodeId, relation, cursor],
    queryFn: async ({ signal }) => {
      session.assert(signal);
      const result = await modelLineageApi.get(projectId, kind, nodeId, relation, cursor, {
        signal,
      });
      session.assert(signal);
      if (result.node.id !== nodeId || result.node.kind !== kind)
        throw new Error("谱系节点身份不符");
      return result;
    },
    retry: false,
  });
  const history = useQuery({
    queryKey: ["model-decisions", projectId, scope, nodeId, before],
    queryFn: async ({ signal }) => {
      session.assert(signal);
      const result = await modelLineageApi.decisions(projectId, nodeId, before, { signal });
      session.assert(signal);
      if (
        result.items.some(
          (item) => item.project_id !== projectId || item.model_version_id !== nodeId,
        )
      )
        throw new Error("决策记录归属不符");
      return result;
    },
    enabled: kind === "model",
    retry: false,
  });
  const [disposition, setDisposition] = useState<ModelDecisionCreate["disposition"]>("research");
  const [note, setNote] = useState("");
  const [notice, setNotice] = useState("");
  const request = useRef({ signature: "", id: randomId() });
  const client = useQueryClient();
  const append = useMutation({
    mutationFn: async ({ body, viewKey }: { body: ModelDecisionCreate; viewKey: string }) => {
      session.assert();
      if (view.current !== viewKey) throw new DOMException("查看位置已变化", "AbortError");
      return modelLineageApi.append(projectId, nodeId, body);
    },
    onSuccess: (_, variables) => {
      if (!session.owns() || view.current !== variables.viewKey) return;
      setNote("");
      setNotice("决策已记录，项目发布选择保持不变。");
      request.current = { signature: "", id: randomId() };
      update({ before: undefined });
      void client.invalidateQueries({ queryKey: ["model-decisions", projectId] });
      void client.invalidateQueries({ queryKey: ["trained-models", projectId] });
      void client.invalidateQueries({ queryKey: ["trained-model", projectId] });
    },
    onError: (error: Error, variables) => {
      if (session.owns() && view.current === variables.viewKey) setNotice(error.message);
    },
  });
  const nodeHref = (node: LineageNode) => {
    const next = new URLSearchParams({
      returnTo: back,
      trail: [...trail, identity].slice(-32).join(","),
    });
    return `/projects/${projectId}/learning/lineage/${node.kind}/${node.id}?${next}`;
  };
  const nodeLink = (node: LineageNode) => (
    <Link
      className="break-words font-medium text-primary hover:underline focus-visible:ring-2 focus-visible:ring-ring"
      to={nodeHref(node)}
    >
      {node.name} <span className="font-normal text-muted-foreground">{kindNames[node.kind]}</span>
    </Link>
  );
  const node = query.data?.node;
  const detailUrl = node ? existingDetail(projectId, node) : undefined;
  return (
    <PageContainer>
      <Link
        to={back}
        className="mb-4 inline-flex items-center gap-2 text-xs text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-3.5" />
        返回来源页面
      </Link>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs text-muted-foreground">{kindNames[kind]} · 来源与候选决策</p>
          <h1 className="mt-1 break-words text-lg font-semibold">{node?.name ?? "模型谱系"}</h1>
        </div>
        {detailUrl && (
          <Link
            to={`${detailUrl}${detailUrl.includes("?") ? "&" : "?"}returnTo=${encodeURIComponent(location.pathname + location.search)}`}
            className="inline-flex items-center gap-1 rounded-md border border-border px-3 py-2 text-xs text-primary"
          >
            打开原有详情
            <ExternalLink className="size-3.5" />
          </Link>
        )}
      </div>
      <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
        逐层核对来源与发布记录。部署展示最近一次检查，历史发布保留当时的身份；配置克隆、断点续训分别记录。
      </p>
      {cyclic && (
        <p
          role="status"
          className="mt-3 rounded-md bg-status-warning-soft p-3 text-xs text-status-warning"
        >
          导航路径再次访问了此节点，可能存在历史循环；页面仅加载当前节点的直接关联。
        </p>
      )}
      {query.isPending && (
        <p role="status" className="mt-6 text-sm text-muted-foreground">
          正在读取谱系…
        </p>
      )}
      {query.error && (
        <p role="alert" className="mt-4 text-sm text-status-danger">
          {query.error.message}
        </p>
      )}
      {node && (
        <>
          <section className="mt-6 border-y border-border py-4" aria-label="节点身份">
            <div className="flex flex-wrap items-center gap-2">
              <span className="inline-flex items-center gap-1">
                <Badge>{kindNames[kind]}</Badge>
                <CopyValueButton value={node.id} kind="id" label={`复制${kindNames[kind]} ID`} />
              </span>
              {node.status && <span className="text-xs text-muted-foreground">{node.status}</span>}
              <time className="text-xs tabular-nums text-muted-foreground">
                {new Date(node.created_at).toLocaleString("zh-CN")}
              </time>
            </div>
            <details className="mt-3 text-xs">
              <summary className="cursor-pointer">冻结身份与摘要</summary>
              <dl className="mt-3 grid gap-3 sm:grid-cols-2">
                {Object.entries(node.facts ?? {}).map(([key, value]) => {
                  const kind = hashFactKeys.has(key) ? "hash" : idFactKeys.has(key) ? "id" : null;
                  return (
                    <div key={key} className="flex min-w-0 items-center gap-2">
                      <dt className="shrink-0 text-muted-foreground">{factNames[key] ?? key}</dt>
                      <dd className="min-w-0 break-all font-mono">
                        {kind ? (
                          <CopyValueButton
                            value={typeof value === "string" ? value : null}
                            kind={kind}
                            label={`复制${factNames[key] ?? key}`}
                          />
                        ) : (
                          value
                        )}
                      </dd>
                    </div>
                  );
                })}
              </dl>
              {Object.keys(node.facts ?? {}).length === 0 && (
                <p className="mt-2 text-muted-foreground">此节点没有额外冻结摘要。</p>
              )}
            </details>
          </section>
          <section className="mt-6" aria-labelledby="lineage-inputs">
            <h2 id="lineage-inputs" className="text-sm font-semibold">
              直接来源
            </h2>
            <ul className="mt-3 divide-y divide-border">
              {query.data?.inputs.map((input, index) => (
                <li
                  key={`${input.relation}-${index}`}
                  className="grid gap-1 py-3 text-xs sm:grid-cols-[180px_minmax(0,1fr)]"
                >
                  <span className="text-muted-foreground">
                    {relationNames[input.relation] ?? input.relation}
                  </span>
                  {input.target ? (
                    nodeLink(input.target)
                  ) : (
                    <span className="text-status-warning">{input.gap}</span>
                  )}
                </li>
              ))}
            </ul>
            {query.data?.inputs.length === 0 && (
              <p className="mt-3 text-xs text-muted-foreground">没有记录上游来源。</p>
            )}
          </section>
          {!!query.data?.sections.length && (
            <section className="mt-6" aria-labelledby="lineage-related">
              <h2 id="lineage-related" className="text-sm font-semibold">
                关联记录
              </h2>
              <div className="mt-3 flex flex-wrap gap-2">
                {query.data.sections.map((section) => (
                  <Button
                    key={section}
                    size="sm"
                    variant={relation === section ? "primary" : "default"}
                    aria-pressed={relation === section}
                    onClick={() => update({ relation: section, cursor: undefined })}
                  >
                    {relationNames[section] ?? section}
                  </Button>
                ))}
              </div>
              {!relation && (
                <p className="mt-3 text-xs text-muted-foreground">
                  选择一类记录加载，每页最多 20 条。
                </p>
              )}
              {relation && (
                <>
                  <ul className="mt-3 divide-y divide-border">
                    {query.data.items?.map((item) => (
                      <li
                        key={item.id}
                        className="flex flex-wrap items-center justify-between gap-2 py-3 text-xs"
                      >
                        {nodeLink(item)}
                        <time className="tabular-nums text-muted-foreground">
                          {new Date(item.created_at).toLocaleString("zh-CN")}
                        </time>
                      </li>
                    ))}
                  </ul>
                  {query.data.usage ? (
                    <p className="mt-3 text-xs leading-relaxed">
                      {query.data.usage.unavailable_reason
                        ? "候选缺少完整权重或类别绑定身份，无法计数"
                        : "保留且可归属的预测记录："}
                      {query.data.usage.truncated ? "至少 " : ""}
                      {query.data.usage.retained_count}
                      。仅统计匹配冻结候选身份的现存记录，旧记录及已删除记录不在范围内；0
                      不表示从未使用。{query.data.usage.truncated && "已达到本次读取上限。"}
                    </p>
                  ) : (
                    query.data.items?.length === 0 && (
                      <p className="mt-3 text-xs text-muted-foreground">没有可用关联记录。</p>
                    )
                  )}
                  <div className="mt-3 flex gap-2">
                    <Button
                      size="sm"
                      disabled={!cursor}
                      onClick={() => update({ cursor: undefined })}
                    >
                      回到首批记录
                    </Button>
                    <Button
                      size="sm"
                      disabled={!query.data.next_cursor}
                      onClick={() => update({ cursor: query.data?.next_cursor ?? undefined })}
                    >
                      下一批关联记录
                    </Button>
                  </div>
                </>
              )}
            </section>
          )}
        </>
      )}
      {kind === "model" && node && (
        <section className="mt-8 border-t border-border pt-5" aria-labelledby="candidate-decisions">
          <h2 id="candidate-decisions" className="text-sm font-semibold">
            候选决策记录
          </h2>
          <p className="mt-2 text-xs text-muted-foreground">
            追加记录保留历史。归档只影响默认列表；部署、发布与回退仍需原有人工操作。
          </p>
          <form
            className="mt-4 space-y-3"
            onSubmit={(event) => {
              event.preventDefault();
              session.assert();
              const signature = JSON.stringify([disposition, note.trim()]);
              if (signature !== request.current.signature)
                request.current = { signature, id: randomId() };
              setNotice("");
              append.mutate({
                body: { request_id: request.current.id, disposition, note: note.trim() },
                viewKey: view.current,
              });
            }}
          >
            <label className="block max-w-xs space-y-1 text-xs">
              候选处置
              <select
                className={trainingInputClass}
                value={disposition}
                disabled={append.isPending}
                onChange={(event) =>
                  setDisposition(event.target.value as ModelDecisionCreate["disposition"])
                }
              >
                {Object.entries(dispositions).map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label className="block space-y-1 text-xs">
              决策依据
              <textarea
                className={`${trainingInputClass} min-h-24`}
                maxLength={2000}
                required
                value={note}
                disabled={append.isPending}
                onChange={(event) => setNote(event.target.value)}
              />
            </label>
            <Button type="submit" size="sm" disabled={append.isPending || !note.trim()}>
              {append.isPending ? "正在记录…" : "追加决策"}
            </Button>
          </form>
          {notice && (
            <p role="status" className="mt-3 text-xs">
              {notice}
            </p>
          )}
          {history.error && (
            <p role="alert" className="mt-3 text-xs text-status-danger">
              {history.error.message}
            </p>
          )}
          {history.isPending && (
            <p role="status" className="mt-3 text-xs text-muted-foreground">
              正在读取决策历史…
            </p>
          )}
          <ol className="mt-4 divide-y divide-border">
            {history.data?.items.map((item) => (
              <li key={item.id} className="py-4 text-xs">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge>{dispositions[item.disposition]}</Badge>
                  <span>
                    #{item.sequence} · {item.creator_label}
                  </span>
                  <time className="tabular-nums text-muted-foreground">
                    {new Date(item.created_at).toLocaleString("zh-CN")}
                  </time>
                </div>
                <p className="mt-2 whitespace-pre-wrap break-words leading-relaxed">{item.note}</p>
              </li>
            ))}
          </ol>
          {history.data?.items.length === 0 && (
            <p className="mt-3 text-xs text-muted-foreground">尚未记录候选决策。</p>
          )}
          <div className="mt-3 flex gap-2">
            <Button size="sm" disabled={!before} onClick={() => update({ before: undefined })}>
              最新决策
            </Button>
            <Button
              size="sm"
              disabled={!history.data?.next_before}
              onClick={() => update({ before: String(history.data?.next_before) })}
            >
              更早决策
            </Button>
          </div>
        </section>
      )}
    </PageContainer>
  );
}
