import { ListPagination, listPageSize } from "@/components/ui/ListPagination";
import { randomId } from "@/utils/id";
import { CopyableError } from "@/components/ui/CopyableError";
import { useMemo, useRef, useState } from "react";
import { Link, Navigate, useLocation, useParams, useSearchParams } from "react-router-dom";
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
import { trainingRecordPath } from "./TrainingListControls";

function pageTrail(raw: string | null): string[] {
  try {
    const value: unknown = JSON.parse(raw ?? "[]");
    return Array.isArray(value) && value.every((item) => typeof item === "string")
      ? value.slice(-100)
      : [];
  } catch {
    return [];
  }
}

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
  predictions: "使用此模型的预标注",
  frozen_provenance: "冻结训练来源",
  frozen_release: "冻结发布身份",
  frozen_evaluation: "冻结评估身份",
  configuration_origin: "配置来源",
  weights_initialization: "初始化权重来源",
  initialized_runs: "由此权重初始化的实验",
  initialization_recipes: "固定此权重的配方修订",
};
const dispositions = {
  active: "普通候选",
  research: "待进一步评估",
  declined: "暂不采用",
  archived: "已归档",
};
const dispositionHelp = {
  active: "取消其他标记，在默认候选列表中显示。以往的备注仍会保留。",
  research: "标记为需要继续测试或评估，仍保留在默认候选列表中。不会自动创建评估任务。",
  declined:
    "记录暂时不采用这个模型的意见，仍保留在默认候选列表中。不会改变默认预标选择，也不会停用已加载的模型。",
  archived:
    "从默认候选列表中隐藏，可在「全部模型，包含归档」或「仅已归档模型」中查看；默认预标模型仍会显示。不会删除权重或停用模型。",
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
  if (node.kind === "model") return `${base}/models/${encodeURIComponent(node.id)}`;
  if (node.kind === "evaluation") return `${base}/evaluations/${node.id}`;
  return undefined;
}

export function ModelLineagePage() {
  const { id = "", kind = "", nodeId = "" } = useParams();
  const [params] = useSearchParams();
  const revision = useTrainingSessionRevision();
  const scope = useMemo(
    () => ({ key: randomId(), id, kind, nodeId, revision }),
    [id, kind, nodeId, revision],
  );
  if (kind === "model") {
    const next = new URLSearchParams(params);
    next.set("model_view", "records");
    return <Navigate replace to={trainingRecordPath(id, "models", nodeId, next)} />;
  }
  if (!Object.prototype.hasOwnProperty.call(kindNames, kind))
    return (
      <PageContainer>
        <p role="alert">无法识别这条记录的类型</p>
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
export function ModelLineagePanel({
  projectId,
  modelId,
  view,
}: {
  projectId: string;
  modelId: string;
  view: "records" | "notes";
}) {
  const revision = useTrainingSessionRevision();
  const scope = useMemo(
    () => ({ key: randomId(), projectId, modelId, revision }),
    [projectId, modelId, revision],
  );
  return (
    <LineageContent
      key={scope.key}
      projectId={projectId}
      kind="model"
      nodeId={modelId}
      scope={scope.key}
      view={view}
    />
  );
}
function LineageContent({
  projectId,
  kind,
  nodeId,
  scope,
  view: contentView = "page",
}: {
  projectId: string;
  kind: NodeKind;
  nodeId: string;
  scope: string;
  view?: "page" | "records" | "notes";
}) {
  const embedded = contentView !== "page";
  const session = useTrainingViewOwner(projectId, scope);
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const viewIdentity = useMemo(() => ({ key: randomId(), location: location.key }), [location.key]);
  const viewKey = viewIdentity.key;
  const view = useRef(viewKey);
  view.current = viewKey;
  const relation = contentView === "notes" ? undefined : params.get("relation") || undefined;
  const cursor = contentView === "notes" ? undefined : params.get("cursor") || undefined;
  const before = Number(params.get("before")) > 0 ? Number(params.get("before")) : undefined;
  const recordsTrail = pageTrail(params.get("record_pages"));
  const notesTrail = pageTrail(params.get("note_pages"));
  const recordsPageSize = listPageSize(params.get("record_page_size"));
  const notesPageSize = listPageSize(params.get("note_page_size"));
  const back = embedded
    ? location.pathname + location.search
    : lineageReturn(projectId, params.get("returnTo"));
  const trail = (params.get("trail") ?? "").split(",").filter(Boolean).slice(-32);
  const identity = `${kind}:${nodeId}`;
  const cyclic = trail.includes(identity);
  const update = (values: Record<string, string | undefined>) =>
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      Object.entries(values).forEach(([key, value]) =>
        value === undefined ? next.delete(key) : next.set(key, value),
      );
      if ("relation" in values) next.delete("record_pages");
      if ("before" in values && values.before === undefined && !("note_pages" in values))
        next.delete("note_pages");
      return next;
    });
  const query = useQuery({
    queryKey: ["model-lineage", projectId, scope, kind, nodeId, relation, cursor, recordsPageSize],
    queryFn: async ({ signal }) => {
      session.assert(signal);
      const result = await modelLineageApi.get(
        projectId,
        kind,
        nodeId,
        relation,
        cursor,
        { signal },
        recordsPageSize,
      );
      session.assert(signal);
      if (result.node.id !== nodeId || result.node.kind !== kind)
        throw new Error("返回的来源记录与当前页面不一致，请重新打开");
      return result;
    },
    retry: false,
  });
  const history = useQuery({
    queryKey: ["model-decisions", projectId, scope, nodeId, before, notesPageSize],
    queryFn: async ({ signal }) => {
      session.assert(signal);
      const result = await modelLineageApi.decisions(
        projectId,
        nodeId,
        before,
        { signal },
        notesPageSize,
      );
      session.assert(signal);
      if (
        result.items.some(
          (item) => item.project_id !== projectId || item.model_version_id !== nodeId,
        )
      )
        throw new Error("备注记录与当前模型不一致，请重新打开");
      return result;
    },
    enabled: kind === "model" && contentView !== "records",
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
      setNotice("标记与说明已保存。默认预标选择未改变。");
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
    if (node.kind === "model") {
      next.set("model_view", "records");
      return trainingRecordPath(projectId, "models", node.id, next);
    }
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
  const Container = embedded ? "div" : PageContainer;
  return (
    <Container>
      {!embedded && (
        <Link
          to={back}
          className="mb-4 inline-flex items-center gap-2 text-xs text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="size-3.5" />
          返回来源页面
        </Link>
      )}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          {!embedded && (
            <p className="text-xs text-muted-foreground">{kindNames[kind]} · 来源与使用记录</p>
          )}
          {embedded ? (
            <h2 className="mt-4 break-words text-sm font-semibold">
              {node?.name ?? (contentView === "notes" ? "标记与备注" : "来源与使用")}
            </h2>
          ) : (
            <h1 className="mt-1 break-words text-lg font-semibold">
              {node?.name ?? "来源与使用记录"}
            </h1>
          )}
        </div>
        {!embedded && detailUrl && (
          <Link
            to={`${detailUrl}${detailUrl.includes("?") ? "&" : "?"}returnTo=${encodeURIComponent(location.pathname + location.search)}`}
            className="inline-flex items-center gap-1 rounded-md border border-border px-3 py-2 text-xs text-primary"
          >
            查看详情
            <ExternalLink className="size-3.5" />
          </Link>
        )}
      </div>
      {contentView !== "notes" && (
        <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
          查看这条记录来自哪次训练、使用了哪些数据，以及相关的部署、发布和预标注记录。部署检查显示最近结果，历史发布保留发布时的信息。
        </p>
      )}
      {contentView !== "notes" && cyclic && (
        <p
          role="status"
          className="mt-3 rounded-md bg-status-warning-soft p-3 text-xs text-status-warning"
        >
          已回到之前查看过的记录，下面仅显示与这条记录直接相关的内容。
        </p>
      )}
      {query.isPending && (
        <p role="status" className="mt-6 text-sm text-muted-foreground">
          {contentView === "notes" ? "正在加载模型标记…" : "正在加载来源与使用记录…"}
        </p>
      )}
      {query.error && (
        <p role="alert" className="mt-4 text-sm text-status-danger">
          <CopyableError message={query.error.message} />
        </p>
      )}
      {node && contentView !== "notes" && (
        <>
          <section className="mt-6 border-y border-border py-4" aria-label="记录信息">
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
              <summary className="cursor-pointer">版本与校验信息</summary>
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
                <p className="mt-2 text-muted-foreground">没有额外的版本或校验信息。</p>
              )}
            </details>
          </section>
          <section className="mt-6" aria-labelledby="lineage-inputs">
            <h2 id="lineage-inputs" className="text-sm font-semibold">
              来源记录
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
              <p className="mt-3 text-xs text-muted-foreground">没有保存来源记录。</p>
            )}
          </section>
          {!!query.data?.sections.length && (
            <section className="mt-6" aria-labelledby="lineage-related">
              <h2 id="lineage-related" className="text-sm font-semibold">
                相关记录
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
                  选择一类记录，按页查看来源和使用记录。
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
                        ? "缺少权重或类别绑定信息，无法核对预标注记录"
                        : "可核对的预标注记录："}
                      {query.data.usage.truncated ? "至少 " : ""}
                      {query.data.usage.retained_count}
                      。仅统计仍保留、且能核对为此模型生成的结果；不含已删除或无法核对的历史结果。0
                      条记录不代表从未使用过这个模型。
                      {query.data.usage.truncated && "已达到本次读取上限。"}
                    </p>
                  ) : (
                    query.data.items?.length === 0 && (
                      <p className="mt-3 text-xs text-muted-foreground">没有可用相关记录。</p>
                    )
                  )}
                  <ListPagination
                    label="相关记录"
                    page={recordsTrail.length || (cursor ? 1 : 0)}
                    pageSize={recordsPageSize}
                    count={query.data.items?.length ?? 0}
                    hasNext={!!query.data.next_cursor}
                    loading={query.isFetching}
                    previousLabel="上一页相关记录"
                    nextLabel="下一页相关记录"
                    onPageChange={(nextPage) => {
                      const currentPage = recordsTrail.length || (cursor ? 1 : 0);
                      if (nextPage < currentPage)
                        update({
                          cursor: recordsTrail[recordsTrail.length - 1] || undefined,
                          record_pages: JSON.stringify(recordsTrail.slice(0, -1)),
                        });
                      else
                        update({
                          cursor: query.data?.next_cursor ?? undefined,
                          record_pages: JSON.stringify([
                            ...(recordsTrail.length ? recordsTrail : cursor ? [""] : []),
                            cursor ?? "",
                          ]),
                        });
                    }}
                    onPageSizeChange={(size) =>
                      update({
                        record_page_size: String(size),
                        cursor: undefined,
                        record_pages: undefined,
                      })
                    }
                  />
                </>
              )}
            </section>
          )}
        </>
      )}
      {kind === "model" && node && contentView !== "records" && (
        <section className="mt-8 border-t border-border pt-5" aria-labelledby="candidate-decisions">
          <h2 id="candidate-decisions" className="text-sm font-semibold">
            模型标记与备注
          </h2>
          <p className="mt-2 text-xs text-muted-foreground">
            记录对这个模型的使用建议和原因，每次保存都会保留操作人、时间及以往说明。标记不会改变默认预标选择，在「评估与发布」中可加载试用或设置默认模型。
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
              模型标记
              <select
                className={trainingInputClass}
                aria-describedby="model-disposition-help"
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
            <p
              id="model-disposition-help"
              className="text-xs leading-relaxed text-muted-foreground"
            >
              {dispositionHelp[disposition]}
            </p>
            <label className="block space-y-1 text-xs">
              标记说明
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
              {append.isPending ? "正在记录…" : "保存标记与说明"}
            </Button>
          </form>
          {notice && (
            <p role="status" className="mt-3 text-xs">
              {notice}
            </p>
          )}
          {history.error && (
            <p role="alert" className="mt-3 text-xs text-status-danger">
              <CopyableError message={history.error.message} />
            </p>
          )}
          {history.isPending && (
            <p role="status" className="mt-3 text-xs text-muted-foreground">
              正在加载标记记录…
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
            <p className="mt-3 text-xs text-muted-foreground">还没有模型标记或备注。</p>
          )}
          <ListPagination
            label="模型标记"
            page={notesTrail.length || (before ? 1 : 0)}
            pageSize={notesPageSize}
            count={history.data?.items.length ?? 0}
            hasNext={!!history.data?.next_before}
            loading={history.isFetching}
            previousLabel="上一页标记"
            nextLabel="下一页标记"
            onPageChange={(nextPage) => {
              const currentPage = notesTrail.length || (before ? 1 : 0);
              if (nextPage < currentPage)
                update({
                  before: notesTrail[notesTrail.length - 1] || undefined,
                  note_pages: JSON.stringify(notesTrail.slice(0, -1)),
                });
              else
                update({
                  before: String(history.data?.next_before),
                  note_pages: JSON.stringify([
                    ...(notesTrail.length ? notesTrail : before ? [""] : []),
                    before ? String(before) : "",
                  ]),
                });
            }}
            onPageSizeChange={(size) =>
              update({ note_page_size: String(size), before: undefined, note_pages: undefined })
            }
          />
        </section>
      )}
    </Container>
  );
}
