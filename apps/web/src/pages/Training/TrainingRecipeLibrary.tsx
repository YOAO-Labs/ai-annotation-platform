import { ListPagination } from "@/components/ui/ListPagination";
import { randomId } from "@/utils/id";
import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  recipeCandidate,
  trainingRecipesApi,
  type TrainingRecipeWrite,
  type TrainingOrigin,
  type TrainingRecipeOut,
  type TrainingReuseCandidate,
} from "@/api/trainingRecipes";
import { Button } from "@/components/ui/Button";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { Modal } from "@/components/ui/Modal";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { trainingInputClass } from "./TrainingParameterField";
import { getAuthSessionRevision } from "@/stores/authSession";
import { useAuthStore } from "@/stores/authStore";
import { useTrainingViewOwner } from "./trainingViewOwner";

function useRequestOwner(projectId: string) {
  const session = useTrainingViewOwner(projectId, "recipes");
  const alive = useRef(true);
  const scope = useRef(projectId);
  scope.current = projectId;
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const capture = () => ({
    projectId: scope.current,
    userId: useAuthStore.getState().user?.id,
    token: useAuthStore.getState().token,
    revision: getAuthSessionRevision(),
  });
  return {
    scope: [session.owner.userId, session.owner.revision],
    capture,
    matches: (owner: ReturnType<typeof capture>) =>
      alive.current &&
      session.owns() &&
      getAuthSessionRevision() === owner.revision &&
      scope.current === owner.projectId &&
      useAuthStore.getState().user?.id === owner.userId &&
      useAuthStore.getState().token === owner.token,
  };
}

export type RecipeSaveSource = Pick<
  TrainingRecipeWrite,
  "configuration" | "origin" | "source_run_id" | "source_attempt_id"
>;

export function TrainingRecipeStatus({
  projectId,
  origin,
}: {
  projectId: string;
  origin: TrainingOrigin;
}) {
  const owner = useRequestOwner(projectId);
  const pinned = useQuery({
    queryKey: ["training-recipe-pin", projectId, origin.recipe_id, origin.revision_id, owner.scope],
    queryFn: () => trainingRecipesApi.get(projectId, origin.recipe_id!, origin.revision_id!),
    enabled: origin.kind === "recipe",
    refetchInterval: 5000,
  });
  const clone = useQuery({
    queryKey: [
      "training-clone-source",
      projectId,
      origin.run_id,
      origin.attempt_id,
      origin.digest,
      owner.scope,
    ],
    queryFn: () =>
      trainingRecipesApi.clone(projectId, origin.run_id!, origin.attempt_id ?? undefined),
    enabled: origin.kind === "clone",
    refetchInterval: 5000,
  });
  if (origin.kind === "clone")
    return (
      <div className="mt-2 space-y-1 text-xs text-muted-foreground">
        <p className="flex flex-wrap items-center gap-1">
          来源实验 <CopyValueButton value={origin.run_id} kind="id" label="复制来源训练实验 ID" />
          {origin.attempt_id && (
            <>
              · 执行尝试{" "}
              <CopyValueButton value={origin.attempt_id} kind="id" label="复制来源执行尝试 ID" />
            </>
          )}
        </p>
        {clone.error && (
          <p role="alert" className="text-status-caution">
            来源暂不可用：
            <CopyableError message={clone.error.message} />
            。草稿已保留，可明确解除来源后继续。
          </p>
        )}
        {clone.data && !clone.data.compatible && (
          <p role="alert" className="text-status-caution">
            {clone.data.incompatibility}
          </p>
        )}
      </div>
    );
  return (
    <div className="mt-2 space-y-1 text-xs text-muted-foreground">
      {pinned.data && (
        <p>
          {pinned.data.revision.name} · 已固定修订 {pinned.data.revision.ordinal}
          {pinned.data.head_revision > pinned.data.revision.ordinal
            ? ` · 修订 ${pinned.data.head_revision} 可更新，请在配方库明确核对采纳`
            : ""}
          {pinned.data.archived ? " · 已归档，当前固定配置继续保留" : ""}
        </p>
      )}
      {pinned.data && !pinned.data.source_available && <p>原实验已删除；配方完整快照仍可用。</p>}
      {pinned.error && (
        <p role="alert" className="text-status-caution">
          无法核验来源：
          <CopyableError message={pinned.error.message} />
          。当前草稿保留，提交前需重新核验或明确解除来源。
        </p>
      )}
    </div>
  );
}

export function TrainingRecipeSave({
  projectId,
  source,
  disabled,
  onSaved,
}: {
  projectId: string;
  source: RecipeSaveSource;
  disabled?: boolean;
  onSaved?: () => void;
}) {
  const client = useQueryClient();
  const owner = useRequestOwner(projectId);
  const [capture, setCapture] = useState<RecipeSaveSource | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [target, setTarget] = useState("");
  const [targetHead, setTargetHead] = useState(0);
  const [conflict, setConflict] = useState<TrainingRecipeOut | null>(null);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const request = useRef<{ body: TrainingRecipeWrite; recipe?: string } | null>(null);
  const recipes = useQuery({
    queryKey: ["training-recipes", projectId, page, pageSize, false, owner.scope],
    queryFn: () => trainingRecipesApi.list(projectId, page * pageSize, false, pageSize),
    enabled: !!capture,
  });
  const save = async () => {
    if (!capture || busy || disabled) return;
    const capturedOwner = owner.capture();
    if (!request.current) {
      const selected = recipes.data?.items.find((item) => item.id === target);
      if (target && !selected) {
        setError("目标配方已不在当前页，请重新选择");
        return;
      }
      request.current = {
        body: {
          ...capture,
          name: name.trim(),
          description,
          request_id: randomId(),
          expected_head_revision: target ? targetHead : 0,
        },
        recipe: target || undefined,
      };
    }
    setBusy(true);
    setError("");
    const submitted = request.current;
    try {
      await trainingRecipesApi.save(projectId, submitted.body, submitted.recipe);
      if (!owner.matches(capturedOwner)) return;
      void client.invalidateQueries({ queryKey: ["training-recipes", projectId] });
      setCapture(null);
      request.current = null;
      onSaved?.();
    } catch (reason) {
      if (!owner.matches(capturedOwner)) return;
      setError(reason instanceof Error ? reason.message : "保存失败");
      if (
        reason &&
        typeof reason === "object" &&
        "status" in reason &&
        reason.status === 409 &&
        submitted.recipe
      ) {
        try {
          const latest = await trainingRecipesApi.get(projectId, submitted.recipe);
          if (owner.matches(capturedOwner)) setConflict(latest);
        } catch {
          /* Keep the captured request and editor if the conflict cannot be inspected. */
        }
      }
    } finally {
      if (owner.matches(capturedOwner)) setBusy(false);
    }
  };
  return (
    <>
      <Button
        size="sm"
        disabled={disabled}
        onClick={() => {
          setCapture(structuredClone(source));
          setName("");
          setDescription("");
          setTarget("");
          setTargetHead(0);
          setConflict(null);
          setPage(0);
          setError("");
          request.current = null;
        }}
      >
        保存为配方
      </Button>
      {capture && (
        <Modal open onClose={() => !busy && setCapture(null)} title="保存训练配方">
          <div className="space-y-4 text-xs">
            <p className="text-muted-foreground">
              保存完整参数、初始化权重和来源。不会创建实验或更新自动策略。
            </p>
            {error && (
              <p role="alert" className="text-status-danger">
                <CopyableError message={error} />
                。编辑内容已保留；网络失败可重试原请求。
              </p>
            )}
            {conflict && (
              <div className="space-y-2 rounded-md bg-status-caution-soft p-3 text-status-caution">
                <p>
                  当前配方：{conflict.revision.name} · 最新修订 {conflict.head_revision}
                  {conflict.archived ? " · 已归档" : ""}
                </p>
                <p>请查看新修订，确认后以它为基准提交当前编辑内容。</p>
                <Button
                  size="sm"
                  disabled={conflict.archived || busy}
                  onClick={() => {
                    setTargetHead(conflict.head_revision);
                    request.current = null;
                    setConflict(null);
                    setError("");
                  }}
                >
                  保留编辑并以此修订为基准
                </Button>
              </div>
            )}
            <fieldset disabled={busy || !!request.current} className="space-y-3">
              <label className="block">
                保存位置
                <select
                  aria-label="配方保存位置"
                  className={`${trainingInputClass} mt-1`}
                  value={target}
                  onChange={(event) => {
                    setTarget(event.target.value);
                    setTargetHead(
                      recipes.data?.items.find((item) => item.id === event.target.value)
                        ?.head_revision ?? 0,
                    );
                  }}
                >
                  <option value="">新建项目配方</option>
                  {recipes.data?.items.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.revision.name}（当前修订 {item.head_revision}）
                    </option>
                  ))}
                </select>
              </label>
              <ListPagination
                label="配方"
                page={page}
                pageSize={pageSize}
                total={recipes.data?.total ?? 0}
                loading={recipes.isFetching}
                previousLabel="上一页配方"
                nextLabel="下一页配方"
                onPageChange={(next) => {
                  setPage(next);
                  setTarget("");
                }}
                onPageSizeChange={(size) => {
                  setPageSize(size);
                  setPage(0);
                  setTarget("");
                }}
              />
              <label className="block">
                配方名称
                <input
                  aria-label="配方名称"
                  className={`${trainingInputClass} mt-1`}
                  maxLength={120}
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                />
              </label>
              <label className="block">
                说明
                <textarea
                  aria-label="配方说明"
                  className={`${trainingInputClass} mt-1`}
                  maxLength={2000}
                  value={description}
                  onChange={(event) => setDescription(event.target.value)}
                />
              </label>
            </fieldset>
            <div className="flex justify-end gap-2">
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => setCapture(null)}>
                取消保存
              </Button>
              <Button
                size="sm"
                disabled={busy || disabled || !name.trim()}
                onClick={() => void save()}
              >
                {busy
                  ? "保存中…"
                  : request.current
                    ? "重试原保存请求"
                    : target
                      ? "创建新修订"
                      : "创建配方"}
              </Button>
            </div>
          </div>
        </Modal>
      )}
    </>
  );
}

export function TrainingRecipeLibrary({
  projectId,
  disabled,
  onChoose,
}: {
  projectId: string;
  disabled?: boolean;
  onChoose: (candidate: TrainingReuseCandidate) => void;
}) {
  const client = useQueryClient();
  const owner = useRequestOwner(projectId);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [archived, setArchived] = useState(false);
  const [selected, setSelected] = useState("");
  const [historyPage, setHistoryPage] = useState(0);
  const [historyPageSize, setHistoryPageSize] = useState(20);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const recipes = useQuery({
    queryKey: ["training-recipes", projectId, page, pageSize, archived, owner.scope],
    queryFn: () => trainingRecipesApi.list(projectId, page * pageSize, archived, pageSize),
    refetchInterval: 5000,
  });
  const history = useQuery({
    queryKey: [
      "training-recipe-history",
      projectId,
      selected,
      historyPage,
      historyPageSize,
      owner.scope,
    ],
    queryFn: () =>
      trainingRecipesApi.revisions(
        projectId,
        selected,
        historyPage * historyPageSize,
        historyPageSize,
      ),
    enabled: !!selected,
    refetchInterval: 5000,
  });
  const archive = async (id: string, head: number) => {
    if (busy || disabled) return;
    const capturedOwner = owner.capture();
    if (
      !(await confirmDialog({
        title: "归档配方",
        description: "停止新选择与创建修订。已固定的实验和策略继续使用原配置。",
        confirmLabel: "归档配方",
      }))
    )
      return;
    if (!owner.matches(capturedOwner)) return;
    setBusy(true);
    setError("");
    try {
      await trainingRecipesApi.archive(projectId, id, head);
      if (!owner.matches(capturedOwner)) return;
      void client.invalidateQueries({ queryKey: ["training-recipes", projectId] });
      void client.invalidateQueries({ queryKey: ["training-recipe-history", projectId] });
    } catch (reason) {
      if (owner.matches(capturedOwner))
        setError(reason instanceof Error ? reason.message : "归档失败");
    } finally {
      if (owner.matches(capturedOwner)) setBusy(false);
    }
  };
  const items = selected ? history.data?.items : recipes.data?.items;
  return (
    <details className="mb-4 rounded-lg border border-border bg-card p-4 text-xs">
      <summary className="cursor-pointer font-medium">
        项目训练配方（{recipes.data?.total ?? 0}）
      </summary>
      <div className="mt-3 space-y-3">
        <p className="text-muted-foreground">
          选择具体修订后核对差异。新修订不会自动替换草稿或策略。
        </p>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={archived}
            disabled={disabled || busy}
            onChange={(event) => {
              setArchived(event.target.checked);
              setPage(0);
              setSelected("");
            }}
          />
          显示已归档配方
        </label>
        {(error || recipes.error || history.error) && (
          <p role="alert" className="text-status-danger">
            <CopyableError message={error || recipes.error?.message || history.error?.message} />
          </p>
        )}
        {selected && (
          <Button size="sm" variant="ghost" onClick={() => setSelected("")}>
            返回配方列表
          </Button>
        )}
        {items?.map((item) => (
          <article key={item.revision.id} className="space-y-2 rounded-md border border-border p-3">
            <p className="break-words font-medium">
              {item.revision.name} · 修订 {item.revision.ordinal}
              {item.head_revision > item.revision.ordinal ? ` · 最新 ${item.head_revision}` : ""}
              {item.archived ? " · 已归档" : ""}
            </p>
            <p className="break-words text-muted-foreground">
              {item.revision.description || "暂无说明"}
            </p>
            {"initialization" in item.revision.snapshot &&
              item.revision.snapshot.initialization != null && (
                <p className="text-muted-foreground">
                  包含固定的项目模型初始化权重；采用前将核对完整来源。
                </p>
              )}
            {!item.source_available && (
              <p className="text-muted-foreground">原实验已删除；保存的完整配置仍保留。</p>
            )}
            {!item.compatible && <p className="text-status-caution">{item.incompatibility}</p>}
            <div className="flex flex-wrap gap-2">
              <Button
                size="sm"
                disabled={disabled || busy || item.archived || !item.compatible}
                onClick={() => onChoose(recipeCandidate(item))}
              >
                核对修订 {item.revision.ordinal}
              </Button>
              {!selected && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setSelected(item.id);
                    setHistoryPage(0);
                  }}
                >
                  修订历史
                </Button>
              )}
              {!item.archived && (
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={disabled || busy}
                  onClick={() => void archive(item.id, item.head_revision)}
                >
                  归档配方
                </Button>
              )}
            </div>
          </article>
        ))}
        {items?.length === 0 && (
          <p className="text-muted-foreground">暂无配方。在参数工作区或成功实验中保存一份配置。</p>
        )}
        <ListPagination
          label={selected ? "配方修订" : "配方"}
          page={selected ? historyPage : page}
          pageSize={selected ? historyPageSize : pageSize}
          total={(selected ? history.data?.total : recipes.data?.total) ?? 0}
          loading={selected ? history.isFetching : recipes.isFetching}
          onPageChange={selected ? setHistoryPage : setPage}
          onPageSizeChange={(size) => {
            if (selected) {
              setHistoryPageSize(size);
              setHistoryPage(0);
            } else {
              setPageSize(size);
              setPage(0);
            }
          }}
        />
      </div>
    </details>
  );
}
