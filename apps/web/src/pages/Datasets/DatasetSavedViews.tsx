import { useEffect, useRef, useState } from "react";
import type { DatasetItemFilters, DatasetSavedView } from "@/api/datasets";
import {
  useDatasetSavedViews,
  useDeleteDatasetView,
  useSaveDatasetView,
} from "@/hooks/useDatasets";
import { useSessionGuard } from "@/hooks/useSessionGuard";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { CopyableError } from "@/components/ui/CopyableError";
import { datasetFilterSignature } from "./datasetsUrlState";

export function DatasetSavedViews({
  datasetId,
  filters,
  selectedId,
  restore,
  onApply,
  onSelect,
}: {
  datasetId: string;
  filters: DatasetItemFilters;
  selectedId?: string;
  restore: boolean;
  onApply: (filters: DatasetItemFilters, id?: string) => void;
  onSelect: (id?: string) => void;
}) {
  const views = useDatasetSavedViews(datasetId);
  const save = useSaveDatasetView(datasetId);
  const remove = useDeleteDatasetView(datasetId);
  const capture = useSessionGuard();
  const [dialog, setDialog] = useState<"save" | "rename" | "delete" | "update" | null>(null);
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const identity = `${datasetId}/${selectedId ?? ""}/${datasetFilterSignature(filters)}`;
  const identityRef = useRef(identity);
  identityRef.current = identity;
  const [target, setTarget] = useState<{
    identity: string;
    view?: DatasetSavedView;
    filters: DatasetItemFilters;
  } | null>(null);
  useEffect(() => {
    if (target && target.identity !== identity) {
      setDialog(null);
      setTarget(null);
      setError("");
    }
  }, [identity, target]);
  const trigger = useRef<HTMLElement | null>(null);
  const selected = views.data?.items.find((view) => view.id === selectedId);
  useEffect(() => {
    if (restore && selected) onApply(selected.query as DatasetItemFilters, selected.id);
  }, [restore, selected, onApply]);
  const modified =
    !restore &&
    !!selected &&
    datasetFilterSignature(filters) !==
      datasetFilterSignature(selected.query as DatasetItemFilters);
  const pending = save.isPending || remove.isPending;
  const open = (kind: typeof dialog, target: HTMLElement) => {
    if (pending) return;
    setTarget({ identity, view: selected, filters: { ...filters } });
    trigger.current = target;
    setName(kind === "save" ? "" : (selected?.name ?? ""));
    setError("");
    setDialog(kind);
  };
  const apply = (view: DatasetSavedView) => onApply(view.query as DatasetItemFilters, view.id);
  return (
    <div className="space-y-2 border-b border-border p-3">
      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-0 basis-full space-y-1 sm:max-w-xs sm:flex-1 sm:basis-auto">
          <label className="text-xs text-muted-foreground" htmlFor="dataset-saved-view">
            个人视图
          </label>
          <select
            id="dataset-saved-view"
            className="w-full rounded-md border border-input bg-background px-2.5 py-2 text-sm text-foreground"
            value={selectedId ?? (Object.keys(filters).length ? "__custom__" : "")}
            disabled={!views.data}
            aria-describedby="dataset-saved-view-hint"
            onChange={(e) => {
              const view = views.data?.items.find((v) => v.id === e.target.value);
              if (view) apply(view);
              else onApply({});
            }}
          >
            <option value="">全部样本</option>
            {Object.keys(filters).length > 0 && !selectedId && (
              <option value="__custom__" disabled hidden>
                自定义筛选
              </option>
            )}
            {selectedId && !selected && <option value={selectedId}>视图不可用</option>}
            {views.data?.items.map((view) => (
              <option key={view.id} value={view.id}>
                {view.name}
              </option>
            ))}
          </select>
        </div>
        <Button size="sm" disabled={restore} onClick={(e) => open("save", e.currentTarget)}>
          保存为新视图
        </Button>
        {selected && (
          <>
            {modified && (
              <Button size="sm" onClick={(e) => open("update", e.currentTarget)}>
                更新条件
              </Button>
            )}
            <Button size="sm" onClick={(e) => open("rename", e.currentTarget)}>
              重命名
            </Button>
            <Button size="sm" onClick={(e) => open("delete", e.currentTarget)}>
              删除视图
            </Button>
            {modified && (
              <Button size="sm" onClick={() => apply(selected)}>
                恢复已存条件
              </Button>
            )}
          </>
        )}
        {Object.keys(filters).length > 0 && (
          <Button size="sm" variant="ghost" onClick={() => onApply({})}>
            清除筛选
          </Button>
        )}
      </div>
      <p id="dataset-saved-view-hint" className="text-xs leading-relaxed text-muted-foreground">
        仅自己可见，保存筛选条件；每次打开显示当前匹配样本。
        {modified && <span> 当前条件有未保存修改。</span>}
      </p>
      {selectedId && views.data && !selected && (
        <p role="alert" className="text-xs text-status-danger">
          此个人视图已删除或不属于当前账号。当前链接中的筛选条件仍保留。
        </p>
      )}
      {views.error && (
        <div role="alert" className="flex items-center gap-2 text-xs text-status-danger">
          <CopyableError
            message={views.error instanceof Error ? views.error.message : "个人视图加载失败"}
          />
          <Button size="sm" onClick={() => void views.refetch()}>
            重试视图
          </Button>
        </div>
      )}
      <Modal
        open={dialog !== null}
        onClose={() => {
          if (!pending) setDialog(null);
        }}
        returnFocusRef={trigger}
        title={
          {
            save: "保存个人视图",
            rename: "重命名个人视图",
            delete: "删除个人视图",
            update: "更新视图条件",
          }[dialog ?? "save"]
        }
      >
        <form
          className="space-y-4"
          onSubmit={async (e) => {
            e.preventDefault();
            if (pending || !dialog || !target || target.identity !== identityRef.current) return;
            const session = capture();
            const current = () => session() && target.identity === identityRef.current;
            const selected = target.view;
            try {
              if (dialog === "delete" && selected) {
                await remove.mutateAsync(selected.id);
                if (!current()) return;
                onSelect(undefined);
              } else {
                if (dialog !== "save" && !selected) return;
                const view = await save.mutateAsync({
                  viewId: dialog === "save" ? undefined : selected?.id,
                  payload: {
                    name: name.trim(),
                    query: dialog === "rename" ? selected!.query : target.filters,
                  },
                });
                if (!current()) return;
                onSelect(view.id);
              }
              setDialog(null);
            } catch (err) {
              if (current()) setError(err instanceof Error ? err.message : "视图保存失败");
            }
          }}
        >
          {dialog === "delete" ? (
            <p className="text-sm">删除“{target?.view?.name}”？样本与当前筛选条件会保留。</p>
          ) : dialog === "update" ? (
            <p className="text-sm">
              用当前筛选条件更新“{target?.view?.name}”？此视图仍随数据变化动态匹配。
            </p>
          ) : (
            <label className="block space-y-1 text-xs text-muted-foreground">
              视图名称
              <input
                autoFocus
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground"
                maxLength={120}
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
          )}
          {error && (
            <div role="alert" className="text-xs text-status-danger">
              <CopyableError message={error} />
            </div>
          )}
          <div className="flex justify-end gap-2">
            <Button type="button" disabled={pending} onClick={() => setDialog(null)}>
              取消
            </Button>
            <Button
              type="submit"
              variant={dialog === "delete" ? "danger" : "primary"}
              disabled={pending || (dialog !== "delete" && !name.trim())}
            >
              {pending ? "处理中…" : dialog === "delete" ? "确认删除" : "保存视图"}
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
