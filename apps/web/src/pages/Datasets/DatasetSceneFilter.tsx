import { useEffect, useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { datasetsApi } from "@/api/datasets";
import { Button } from "@/components/ui/Button";
import { useSessionRevision } from "@/hooks/useSessionGuard";
import { useAuthStore } from "@/stores/authStore";
import { ApiError } from "@/api/client";

const control =
  "w-full rounded-md border border-input bg-background px-2.5 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

interface Props {
  datasetId: string;
  sceneId?: string;
  onChange: (id: string | undefined) => void;
}

export function DatasetSceneFilter(props: Props) {
  const revision = useSessionRevision();
  return <SceneFilterOwner key={`${revision}/${props.datasetId}`} {...props} revision={revision} />;
}

function SceneFilterOwner({
  datasetId,
  sceneId,
  onChange,
  revision,
}: Props & { revision: number }) {
  const hint = useId();
  const actor = useAuthStore((state) => state.user?.id);
  const session = useAuthStore((state) => state.sessionId);
  const [offset, setOffset] = useState(0);
  const [draft, setDraft] = useState("");
  const [search, setSearch] = useState("");
  const options = useQuery({
    queryKey: [
      "dataset-scene-options",
      actor,
      session,
      revision,
      datasetId,
      offset,
      search,
      sceneId,
    ],
    queryFn: ({ signal }) =>
      datasetsApi.sceneOptions(
        datasetId,
        { limit: 20, offset, search: search || undefined, selected_id: sceneId },
        { signal },
      ),
    enabled: !!datasetId,
  });
  const denied =
    options.error instanceof ApiError && [401, 403, 404].includes(options.error.status);
  const data = denied ? undefined : options.data;
  useEffect(() => {
    if (!data || options.isFetching) return;
    const last = Math.max(0, Math.ceil(data.total / 20) - 1) * 20;
    if (offset > last) setOffset(last);
  }, [data, offset, options.isFetching]);
  const choices = [...(data?.items ?? [])];
  if (data?.selected && !choices.some((row) => row.id === data.selected!.id))
    choices.unshift(data.selected);
  return (
    <div className="rounded-md border border-border bg-muted/20 p-3">
      <div className="grid items-end gap-3 sm:grid-cols-2">
        <label className="space-y-1 text-xs text-muted-foreground">
          Scene 筛选
          <select
            className={control}
            value={sceneId ?? ""}
            aria-describedby={hint}
            onChange={(event) => onChange(event.target.value || undefined)}
          >
            <option value="">全部 Scene</option>
            {sceneId && !choices.some((row) => row.id === sceneId) && (
              <option value={sceneId}>指定 Scene</option>
            )}
            {choices.map((scene) => (
              <option key={scene.id} value={scene.id}>
                {scene.name}
              </option>
            ))}
          </select>
        </label>
        <form
          className="flex items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            setSearch(draft.trim());
            setOffset(0);
          }}
        >
          <label className="min-w-0 flex-1 space-y-1 text-xs text-muted-foreground">
            查找 Scene
            <input
              className={control}
              value={draft}
              maxLength={200}
              placeholder="按 Scene 名称查找"
              onChange={(event) => setDraft(event.target.value)}
            />
          </label>
          <Button type="submit" size="sm" disabled={options.isFetching}>
            查找
          </Button>
          {search && (
            <Button
              type="button"
              size="sm"
              onClick={() => {
                setDraft("");
                setSearch("");
                setOffset(0);
              }}
            >
              清除查找
            </Button>
          )}
        </form>
      </div>
      <p id={hint} className="mt-2 text-xs leading-relaxed text-muted-foreground">
        Scene 条件按场景 ID 筛选，包含同场景的多模态与校准成员。查找或翻页只改变选项，保留当前筛选。
      </p>
      {options.isPending && (
        <p role="status" className="mt-2 text-xs text-muted-foreground">
          正在读取 Scene 选项…
        </p>
      )}
      {options.error && (
        <p role="alert" className="mt-2 text-xs leading-relaxed text-status-danger">
          Scene 选项读取失败，当前筛选已保留。请重试，或选择全部 Scene 清除此条件。
          <Button type="button" variant="ghost" size="sm" onClick={() => void options.refetch()}>
            重试选项
          </Button>
        </p>
      )}
      {data && !data.total && (
        <p className="mt-2 text-xs text-muted-foreground">
          {search ? "没有匹配的 Scene 选项。" : "暂无 Scene 选项。"}
        </p>
      )}
      {data && (data.total > 20 || offset > 0) && (
        <nav
          aria-label="Scene 选项分页"
          className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground"
        >
          <Button
            type="button"
            size="sm"
            disabled={!offset || options.isFetching}
            onClick={() => setOffset(Math.max(0, offset - 20))}
          >
            上一页 Scene
          </Button>
          <span>
            第 {offset / 20 + 1} 页 · 共 {data.total} 个
          </span>
          <Button
            type="button"
            size="sm"
            disabled={offset + 20 >= data.total || options.isFetching}
            onClick={() => setOffset(offset + 20)}
          >
            下一页 Scene
          </Button>
        </nav>
      )}
    </div>
  );
}
