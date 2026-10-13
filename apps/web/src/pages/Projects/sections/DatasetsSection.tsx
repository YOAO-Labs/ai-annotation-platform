import { useEffect, useMemo, useState } from "react";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { useToastStore } from "@/components/ui/Toast";
import {
  useDatasets,
  useLinkProject,
  useUnlinkProject,
  useProjectDatasets,
} from "@/hooks/useDatasets";
import { datasetsApi } from "@/api/datasets";
import { LinkJobProgress } from "@/components/datasets/LinkJobProgress";
import type { ProjectResponse } from "@/api/projects";

function cn(...xs: Array<string | false | null | undefined>): string {
  return xs.filter(Boolean).join(" ");
}

const TABLE_HEAD_CELL =
  "whitespace-nowrap px-3 py-2 text-left text-xs font-medium text-muted-foreground";
const TABLE_CELL = "whitespace-nowrap px-3 py-2.5 align-middle";

export function DatasetsSection({ project }: { project: ProjectResponse }) {
  const pushToast = useToastStore((s) => s.push);
  const { data: linked = [], isLoading } = useProjectDatasets(project.id);
  const { data: allDatasetsRes } = useDatasets();
  const allDatasets = allDatasetsRes?.items ?? [];

  const [linkOpen, setLinkOpen] = useState(false);
  const [unlinkTarget, setUnlinkTarget] = useState<{
    dataset_id: string;
    name: string;
  } | null>(null);

  const linkedIds = useMemo(() => new Set(linked.map((d) => d.id)), [linked]);
  const candidates = allDatasets.filter((d) => !linkedIds.has(d.id));

  return (
    <>
      <Card>
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3.5">
          <h3 className="text-sm font-semibold">关联数据集</h3>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setLinkOpen(true)}
            disabled={candidates.length === 0}
          >
            <Icon name="plus" /> 关联数据集
          </Button>
        </div>

        {isLoading && (
          <div className="p-8 text-center text-sm text-muted-foreground">加载中...</div>
        )}

        {!isLoading && linked.length === 0 && (
          <div className="p-8 text-center text-sm text-muted-foreground">
            尚未关联任何数据集。点击右上角「关联数据集」开始。
          </div>
        )}

        {!isLoading && linked.length > 0 && (
          <div className="w-full overflow-x-auto">
            <table className="w-full min-w-[840px] border-collapse text-sm">
              <thead>
                <tr className="border-b border-border">
                  {["数据集", "类型", "原数据集条目", "本项目任务", "关联时间", "操作"].map((h) => (
                    <th key={h} className={TABLE_HEAD_CELL}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {linked.map((d) => (
                  <tr key={d.id} className="border-b border-border">
                    <td className={TABLE_CELL}>
                      <div className="max-w-[240px] truncate font-medium" title={d.name}>
                        {d.name}
                      </div>
                      <div
                        className={cn("mono", "whitespace-nowrap text-xs text-muted-foreground")}
                      >
                        {d.display_id}
                      </div>
                    </td>
                    <td className={cn(TABLE_CELL, "text-muted-foreground")}>{d.data_type}</td>
                    <td className={TABLE_CELL}>{d.items_count}</td>
                    <td className={TABLE_CELL}>{d.tasks_in_project}</td>
                    <td className={cn(TABLE_CELL, "text-xs text-muted-foreground")}>
                      {d.linked_at ? new Date(d.linked_at).toLocaleString() : "—"}
                    </td>
                    <td className={cn(TABLE_CELL, "text-right")}>
                      <Button
                        onClick={() => setUnlinkTarget({ dataset_id: d.id, name: d.name })}
                        title="取消关联（会清理对应的任务、标注与空批次）"
                      >
                        <Icon name="x" size={12} /> 取消关联
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {linkOpen && (
        <LinkDatasetModal
          projectId={project.id}
          candidates={candidates}
          onClose={() => setLinkOpen(false)}
          onLinked={(name) => pushToast({ msg: `已关联数据集：${name}`, kind: "success" })}
        />
      )}

      {unlinkTarget && (
        <UnlinkConfirmModal
          projectId={project.id}
          datasetId={unlinkTarget.dataset_id}
          datasetName={unlinkTarget.name}
          onClose={() => setUnlinkTarget(null)}
          onDone={() => setUnlinkTarget(null)}
        />
      )}
    </>
  );
}

function LinkDatasetModal({
  projectId,
  candidates,
  onClose,
  onLinked,
}: {
  projectId: string;
  candidates: { id: string; name: string; display_id: string; data_type: string }[];
  onClose: () => void;
  onLinked: (name: string) => void;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  // v0.12.0 · 大 dataset 异步建 task 时返回的 job id，非空则在弹窗内显示进度条
  const [linkJobId, setLinkJobId] = useState<string | null>(null);
  // useLinkProject 是按 datasetId 维度的 hook；这里临时绕开 — 直接调 mutation
  // 但现有 useLinkProject 只能 useMutation 化为 datasetId-bound 实例。
  // 为简洁，我们直接调 datasetsApi.linkProject + invalidate by hand。
  const link = useLinkProject(selected ?? "");
  const pushToast = useToastStore((s) => s.push);

  const onSubmit = async () => {
    if (!selected) return;
    const ds = candidates.find((c) => c.id === selected);
    link.mutate(projectId, {
      onSuccess: (res) => {
        onLinked(ds?.name ?? "数据集");
        // 大 dataset 异步建 task：留在弹窗里看进度；小 dataset 同步建完直接关闭
        if (res.async_job_id) {
          setLinkJobId(res.async_job_id);
        } else {
          onClose();
        }
      },
      onError: (e) => pushToast({ msg: "关联失败", sub: (e as Error).message, kind: "error" }),
    });
  };

  return (
    <Modal open onClose={onClose} title="关联数据集" width={520}>
      <div className="mb-3 text-sm text-muted-foreground">
        选择一个尚未关联到本项目的数据集。关联后该数据集的全部条目会作为「未归类任务」加入项目，
        在批次管理顶部点击「去分包」即可划分到批次。
      </div>
      {candidates.length === 0 && (
        <div className="p-4 text-center text-sm text-muted-foreground">
          暂无可关联的数据集 · 请先在「数据集」页面创建
        </div>
      )}
      {candidates.length > 0 && (
        <div className="max-h-80 overflow-y-auto rounded-md border border-border bg-muted p-1.5">
          {candidates.map((d) => {
            const checked = selected === d.id;
            return (
              <button
                key={d.id}
                type="button"
                onClick={() => setSelected(d.id)}
                className={cn(
                  "mb-0.5 flex w-full cursor-pointer appearance-none items-center gap-2.5 rounded-sm border border-transparent bg-transparent px-2.5 py-2 text-left text-foreground [font-family:inherit]",
                  checked && "border-brand bg-brand/10",
                )}
              >
                <span
                  className={cn(
                    "relative size-3.5 shrink-0 rounded-full border border-border bg-background",
                    checked && "bg-brand",
                  )}
                >
                  {checked && <span className="absolute inset-[3px] rounded-full bg-white" />}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="text-sm font-medium">{d.name}</span>
                  <span className={cn("mono", "ml-1.5 text-xs text-muted-foreground")}>
                    {d.display_id}
                  </span>
                  <span className="ml-1.5 rounded-full border border-border px-1.5 py-px text-xs text-muted-foreground">
                    {d.data_type}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      )}
      {linkJobId && (
        <LinkJobProgress
          jobId={linkJobId}
          projectId={projectId}
          onDone={() => {
            setLinkJobId(null);
            onClose();
          }}
        />
      )}
      <div className="mt-4 flex justify-end gap-2">
        <Button onClick={onClose} disabled={!!linkJobId}>
          取消
        </Button>
        <Button
          variant="primary"
          onClick={onSubmit}
          disabled={!selected || link.isPending || !!linkJobId}
        >
          {link.isPending ? "关联中…" : "确认关联"}
        </Button>
      </div>
    </Modal>
  );
}

function UnlinkConfirmModal({
  projectId,
  datasetId,
  datasetName,
  onClose,
  onDone,
}: {
  projectId: string;
  datasetId: string;
  datasetName: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const [preview, setPreview] = useState<{
    tasks: number;
    annotations: number;
    batches: number;
  } | null>(null);
  const [confirmText, setConfirmText] = useState("");
  const dangerous = (preview?.tasks ?? 0) > 0;
  const canSubmit = dangerous ? confirmText.trim() === datasetName : true;
  const unlink = useUnlinkProject(datasetId);
  const pushToast = useToastStore((s) => s.push);

  useEffect(() => {
    let cancelled = false;
    datasetsApi
      .previewUnlink(datasetId, projectId)
      .then((r) => {
        if (!cancelled)
          setPreview({
            tasks: r.will_delete_tasks,
            annotations: r.will_delete_annotations,
            batches: r.will_delete_batches,
          });
      })
      .catch(() => {
        if (!cancelled) setPreview({ tasks: 0, annotations: 0, batches: 0 });
      });
    return () => {
      cancelled = true;
    };
  }, [datasetId, projectId]);

  const onConfirm = () => {
    if (!canSubmit) return;
    unlink.mutate(projectId, {
      onSuccess: (res) => {
        const parts: string[] = [];
        if (res?.deleted_tasks) parts.push(`${res.deleted_tasks} 个任务`);
        if (res?.deleted_annotations) parts.push(`${res.deleted_annotations} 个标注`);
        if (res?.deleted_batches) parts.push(`${res.deleted_batches} 个空批次`);
        pushToast({
          msg: "已取消关联",
          sub: parts.length ? `已清理 ${parts.join(" · ")}` : undefined,
          kind: "success",
        });
        onDone();
      },
      onError: (e) => pushToast({ msg: "取消关联失败", sub: (e as Error).message, kind: "error" }),
    });
  };

  return (
    <Modal open onClose={onClose} title="确认取消关联">
      <div className="text-sm leading-relaxed">
        <p className="mb-2 mt-0">
          确认取消数据集 <strong>{datasetName}</strong> 与本项目的关联？
        </p>
        <div className="mb-2 text-muted-foreground">
          {preview === null ? (
            "正在统计影响范围…"
          ) : preview.tasks === 0 ? (
            "项目中没有由该数据集创建的任务，可放心取消。"
          ) : (
            <>
              <strong className="text-status-danger">将一并删除</strong>项目中由该数据集创建的{" "}
              <strong>{preview.tasks}</strong> 个任务
              {preview.annotations > 0 && (
                <>
                  （含 <strong className="text-status-danger">{preview.annotations}</strong>{" "}
                  个已有标注）
                </>
              )}
              {preview.batches > 0 && (
                <>
                  ，并清理 <strong className="text-status-danger">{preview.batches}</strong>{" "}
                  个失去全部任务的空批次
                </>
              )}
              。<br />
              此操作不可恢复。
            </>
          )}
        </div>
        {dangerous && (
          <div className="my-2.5">
            <label className="mb-1 block text-xs text-muted-foreground">
              请输入数据集名称 <strong>{datasetName}</strong> 以确认：
            </label>
            <input
              type="text"
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              placeholder={datasetName}
              autoFocus
              className={cn(
                "box-border w-full appearance-none rounded-md border border-border bg-muted px-2.5 py-2 text-sm text-foreground [font-family:inherit]",
                canSubmit && "border-emerald-500",
              )}
            />
          </div>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <Button onClick={onClose}>取消</Button>
          <Button
            variant={canSubmit ? "danger" : "default"}
            onClick={onConfirm}
            disabled={!canSubmit || unlink.isPending}
          >
            {unlink.isPending ? "处理中…" : "确认取消关联"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
