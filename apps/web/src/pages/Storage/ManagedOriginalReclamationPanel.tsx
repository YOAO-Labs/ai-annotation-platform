import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type {
  ManagedOriginalReclamationItem,
  ManagedOriginalReclamationPreview,
} from "@/api/generated/types.gen";
import { storageReclamationApi } from "@/api/storageReclamation";
import { useAuthStore } from "@/stores/authStore";
import { useSessionRevision } from "@/hooks/useSessionGuard";
import { storageAccessDenied } from "@/components/connections/storageQueryState";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { ListPagination } from "@/components/ui/ListPagination";
import { cacheBytes } from "@/utils/cachePolicy";
import { ManagedOriginalRetentionPanel } from "./ManagedOriginalRetentionPanel";
import { ManagedOriginalVerificationPanel } from "./ManagedOriginalVerificationPanel";

const statusLabels = { retained: "保留中", protected: "仍有保护要求", blocked: "资格待核实" };
const identityLabels = {
  current: "当前配置匹配",
  unknown: "存储身份未确认",
  mismatch: "存储配置已变化",
};
const reasons: Record<string, string> = {
  current_asset_location: "仍有内容位置引用",
  current_dataset_item: "仍有数据集样本引用",
  current_task: "仍有任务引用",
  current_task_dataset_link: "仍有任务与数据集关联",
  retained_training_version: "仍有保留的训练快照引用",
  active_learning_history: "仍有主动学习历史引用",
  indefinite_retention: "未指定保留期限，按无限期保护",
  retention_window: "尚在保留期内",
  related_indefinite_retention: "相关记录仍为无限期保护",
  related_retention_window: "相关记录仍在保留期内",
  declared_backup_hold: "存在有效的已登记备份保留要求",
  declared_migration_hold: "存在有效的已登记迁移观察要求",
  storage_identity_unknown: "无法确认保留记录的存储身份",
  storage_identity_mismatch: "保留记录与当前存储配置不一致",
  content_identity_unverified: "内容身份尚未核验",
  locator_unverified: "原图定位尚未核验",
  unsupported_training_snapshot: "存在无法完整解析的训练快照",
  unsupported_active_learning_snapshot: "存在无法完整解析的主动学习历史",
  legacy_task_bucket_unknown: "旧任务的物理桶归属未确认",
  exclusive_ownership_unproven: "尚未证明原件由平台独占管理",
  backup_holds_unknown: "未登记备份要求的覆盖仍未知",
  migration_holds_unknown: "未登记迁移要求的覆盖仍未知",
  object_presence_not_checked: "本次预演未核验现场对象及最小副本",
};
const sharedReasons = new Set([
  "exclusive_ownership_unproven",
  "backup_holds_unknown",
  "migration_holds_unknown",
  "object_presence_not_checked",
]);
const scope =
  "只查看本功能启用后，在删除数据集或样本时保存的托管原件位置记录。历史未登记或已丢失的位置不在覆盖内；外部原件、缓存与训练副本由各自规则管理。";

function date(value: string) {
  return new Date(value).toLocaleString("zh-CN");
}

function validPreview(data: ManagedOriginalReclamationPreview) {
  return (
    data.scope === "registered_managed_original_retirement_assertions" &&
    data.unit === "retirement_assertion" &&
    data.execution_permission === false &&
    data.reclaimable_bytes === null &&
    Number.isFinite(Date.parse(data.measured_at)) &&
    Array.isArray(data.items) &&
    Number.isSafeInteger(data.counts?.total_assertions)
  );
}

export function ManagedOriginalReclamationPanel() {
  const role = useAuthStore((state) => state.user?.role);
  const revision = useSessionRevision();
  return role === "super_admin" ? <ReclamationDisclosure key={revision} /> : null;
}

function ReclamationDisclosure() {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="border-t border-border"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer px-4 py-3 text-sm font-medium">
        托管原件保留与回收预演
      </summary>
      {open && <ReclamationOwner />}
    </details>
  );
}

function ReclamationOwner() {
  const session = useAuthStore((state) => state.sessionId);
  const revision = useSessionRevision();
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const query = useQuery({
    queryKey: ["storage-managed-original-reclamation", session, revision, pageSize, page],
    queryFn: ({ signal }) => storageReclamationApi.preview(pageSize, page * pageSize, signal),
    retry: false,
    staleTime: 0,
    refetchOnWindowFocus: false,
  });
  const denied = storageAccessDenied(query.error);
  const valid = query.data && validPreview(query.data);
  const data = denied || !valid ? undefined : query.data;
  const selected = data?.items.find((item) => item.id === selectedId);
  useEffect(() => {
    if (denied || (!query.isFetching && !selected)) setSelectedId(null);
  }, [denied, query.isFetching, selected]);
  useEffect(() => {
    if (!data || query.isFetching) return;
    const last = Math.max(0, Math.ceil(data.counts.total_assertions / pageSize) - 1);
    if (page > last) setPage(last);
  }, [data, page, pageSize, query.isFetching]);
  return (
    <div className="min-w-0 border-t border-border text-sm">
      <div className="space-y-3 px-4 py-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <p className="min-w-0 flex-1 text-xs leading-relaxed text-muted-foreground">{scope}</p>
          <Button size="sm" disabled={query.isFetching} onClick={() => void query.refetch()}>
            刷新保留预演
          </Button>
        </div>
        <p className="rounded-md border border-border bg-muted p-3 text-xs leading-relaxed text-muted-foreground">
          预演不会删除文件或提交回收任务。独占所有权、现场副本、备份恢复点和迁移保留尚未完成资格核验，当前没有物理删除许可。
        </p>
        {query.isPending && (
          <p role="status" className="text-xs text-muted-foreground">
            正在核对保留记录…
          </p>
        )}
        {(query.error || (query.data && !valid)) && (
          <p role="alert" className="text-xs leading-relaxed text-status-danger">
            {denied
              ? "当前权限已失效，已隐藏保留记录。"
              : data
                ? "刷新失败，以下保留上次观测及时间；请稍后重试。"
                : "无法读取符合范围的保留预演，请重新核对权限与存储配置。"}
          </p>
        )}
        {data && (
          <>
            <p className="text-xs text-muted-foreground">观测于 {date(data.measured_at)}</p>
            <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              {(
                [
                  ["记录总数", data.counts.total_assertions],
                  ["仍有保护要求", data.counts.protected_assertions],
                  ["保留中", data.counts.retained_assertions],
                  ["资格待核实", data.counts.blocked_assertions],
                ] as const
              ).map(([label, count]) => (
                <div key={label}>
                  <dt className="text-xs text-muted-foreground">{label}</dt>
                  <dd className="mt-1 tabular-nums font-medium">{count}</dd>
                </div>
              ))}
            </dl>
            <p className="text-xs leading-relaxed text-muted-foreground">
              已记录大小合计 {cacheBytes(data.counts.known_asserted_bytes)}，大小未知{" "}
              {data.counts.unknown_size_assertions} 条。
              同一原件可能对应多条记录，大小会重复累计，不代表可释放容量。 策略条件候选{" "}
              {data.counts.policy_candidate_assertions} 条，仍需完成全部删除门禁。
            </p>
            {(data.counts.unknown_training_versions > 0 ||
              data.counts.unknown_active_learning_rounds > 0) && (
              <p className="text-xs leading-relaxed text-status-warning">
                无法完整解析的训练快照 {data.counts.unknown_training_versions} 份、主动学习历史{" "}
                {data.counts.unknown_active_learning_rounds} 份，相关保留证据不能视为零。
              </p>
            )}
            {data.items.length === 0 && (
              <p className="py-3 text-xs text-muted-foreground">
                此范围暂无托管原件保留记录；不代表存储中没有遗留原件或保留要求。
              </p>
            )}
          </>
        )}
      </div>
      {data && data.items.length > 0 && (
        <ul className="divide-y divide-border border-t border-border">
          {data.items.map((item) => (
            <li
              key={item.id}
              className="flex flex-wrap items-center justify-between gap-3 px-4 py-3"
            >
              <div className="min-w-0 flex-1 space-y-1">
                <p className="font-medium">原件记录 {item.id.slice(0, 8)}</p>
                <p className="text-xs text-muted-foreground">
                  {statusLabels[item.status]} ·{" "}
                  {item.size_bytes === null ? "大小未知" : cacheBytes(item.size_bytes)} · 关系移除于{" "}
                  {date(item.retired_at)}
                </p>
                <p className="text-xs leading-relaxed text-muted-foreground">
                  {item.blockers
                    .filter((code) => !sharedReasons.has(code))
                    .map((code) => reasons[code] ?? "存在待核实的保护要求")
                    .join("；") || "仍需核实物理删除资格"}
                </p>
              </div>
              <Button
                size="sm"
                onClick={(event) => {
                  trigger.current = event.currentTarget;
                  setSelectedId(item.id);
                }}
                aria-label={`查看原件记录 ${item.id.slice(0, 8)} 详情`}
              >
                查看详情
              </Button>
            </li>
          ))}
        </ul>
      )}
      {data && (
        <ListPagination
          label="托管原件保留记录"
          page={page}
          pageSize={pageSize}
          total={data.counts.total_assertions}
          loading={query.isFetching}
          hasNext={data.has_more}
          onPageChange={(next) => {
            setSelectedId(null);
            setPage(next);
          }}
          onPageSizeChange={(next) => {
            setSelectedId(null);
            setPageSize(next);
            setPage(0);
          }}
        />
      )}
      <Modal
        open={!!selected}
        onClose={() => setSelectedId(null)}
        title="托管原件保留详情"
        width={640}
        returnFocusRef={trigger}
        footer={
          <Button size="sm" onClick={() => setSelectedId(null)}>
            关闭详情
          </Button>
        }
      >
        {selected && data && (
          <ReclamationDetails
            item={selected}
            measuredAt={data.measured_at}
            onChanged={() => query.refetch()}
            onDenied={() => setSelectedId(null)}
          />
        )}
      </Modal>
    </div>
  );
}

function ReclamationDetails({
  item,
  measuredAt,
  onChanged,
  onDenied,
}: {
  item: ManagedOriginalReclamationItem;
  measuredAt: string;
  onChanged: () => Promise<unknown>;
  onDenied: () => void;
}) {
  return (
    <div className="space-y-4 text-sm">
      <div>
        <p className="font-medium">{statusLabels[item.status]}</p>
        <p className="mt-1 text-xs text-muted-foreground">
          {identityLabels[item.identity_state]}，仅为配置证据，不代表现场原件存在或已通过内容核验。
        </p>
      </div>
      <dl className="grid gap-3 sm:grid-cols-2">
        {(
          [
            ["记录 ID", item.id],
            ["历史数据集", item.dataset_id],
            ["历史样本", item.dataset_item_id],
            ["内容身份", item.asset_id],
            ["关系移除时间", date(item.retired_at)],
            ["本条保留期限", item.retain_until ? date(item.retain_until) : "无限期保护"],
            [
              "相关记录共同保护至",
              item.indefinite_retention
                ? "无限期保护"
                : item.effective_retain_until
                  ? date(item.effective_retain_until)
                  : "期限未知",
            ],
            ["策略修订", String(item.policy_revision)],
          ] as const
        ).map(([label, value]) => (
          <div key={label} className="min-w-0">
            <dt className="text-xs text-muted-foreground">{label}</dt>
            <dd className="mt-1 break-all">{value}</dd>
          </div>
        ))}
      </dl>
      <p className="text-xs leading-relaxed text-muted-foreground">
        相关保留记录 {item.peer_assertions} 条 · 已登记有效备份保留 {item.declared_backup_holds} 条
        · 迁移观察 {item.declared_migration_holds}{" "}
        条。相关记录按同桶同键保守归组，不证明相同物理副本。
      </p>
      <div>
        <h3 className="font-medium">当前已记录的保护关系</h3>
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
          内容位置 {item.roots.asset_locations} · 数据集样本 {item.roots.dataset_items} · 任务{" "}
          {item.roots.tasks} · 任务数据集关联 {item.roots.task_dataset_links} · 训练版本{" "}
          {item.roots.training_versions} · 主动学习历史 {item.roots.active_learning_roots}
        </p>
      </div>
      <div>
        <h3 className="font-medium">保留与阻断原因</h3>
        <ul className="mt-2 list-disc space-y-1 pl-4 text-xs leading-relaxed text-muted-foreground">
          {item.blockers.map((code) => (
            <li key={code}>{reasons[code] ?? "存在待核实的保护要求"}</li>
          ))}
        </ul>
      </div>
      <ManagedOriginalRetentionPanel
        retirementId={item.id}
        onChanged={onChanged}
        onDenied={onDenied}
      />
      <ManagedOriginalVerificationPanel retirementId={item.id} onDenied={onDenied} />
      <p className="text-xs leading-relaxed text-muted-foreground">
        观测于 {date(measuredAt)}。{scope} 当前预演不提供物理删除许可。
      </p>
    </div>
  );
}
