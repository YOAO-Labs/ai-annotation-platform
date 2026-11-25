import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import {
  INFERENCE_PLUGIN_INSTALLATIONS_QUERY_KEY,
  adminInferencePluginsApi,
  formatInferencePluginError,
  resolveInferencePluginSummary,
  type DirectPluginProbeResponse,
  type InferencePluginSummary,
  type ManagedModelCompatibility,
  type PluginInstallationResponse,
} from "@/api/adminInferencePlugins";
import { FilterGroup, FilterSelect } from "@/components/filters/FilterControls";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/shadcn/ui/alert-dialog";
import { Input } from "@/components/shadcn/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/shadcn/ui/table";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { CopyableError } from "@/components/ui/CopyableError";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { useToastStore } from "@/components/ui/Toast";
import { listStyles } from "@/components/ui/listStyles";

import { DirectInferencePluginModal } from "./DirectInferencePluginModal";

export interface InferencePluginInstallationsSectionProps {
  onChanged?: () => void;
}

type ExecutionModeFilter = "all" | "direct" | "container";
type StatusFilter = "all" | "active" | "disabled" | "prepared" | "retired";
type ConfirmAction =
  | { kind: "retire"; item: PluginInstallationResponse }
  | { kind: "cancelPrepared"; item: PluginInstallationResponse };

const STATUS_META: Record<
  InferencePluginSummary["status"],
  { label: string; variant: "success" | "warning" | "accent" | "default" }
> = {
  active: { label: "已启用", variant: "success" },
  disabled: { label: "已停用", variant: "warning" },
  prepared: { label: "已预留", variant: "accent" },
  retired: { label: "已退役", variant: "default" },
};

const SOURCE_TIER_META: Record<
  InferencePluginSummary["source_tier"],
  { label: string; variant: "accent" | "outline" }
> = {
  official: { label: "官方", variant: "accent" },
  third_party: { label: "第三方", variant: "outline" },
};

const EXECUTION_MODE_META: Record<
  InferencePluginSummary["execution_mode"],
  { label: string; variant: "ai" | "default" }
> = {
  direct: { label: "直连", variant: "ai" },
  container: { label: "容器", variant: "default" },
};

const EMPTY_INSTALLATIONS: PluginInstallationResponse[] = [];

const CAPABILITY_LABELS: Record<string, string> = {
  inference: "推理",
  detection: "检测",
  segmentation: "分割",
  interactive: "交互式",
  video_tracking: "视频跟踪",
  ocr: "OCR",
  classification: "分类",
  obb: "旋转框",
  pose: "姿态",
  doc_layout: "版面分析",
  managed_models: "受控模型",
  managed_evaluation: "受控评估",
};

function invalidatePluginQueries(qc: QueryClient): void {
  qc.invalidateQueries({ queryKey: INFERENCE_PLUGIN_INSTALLATIONS_QUERY_KEY });
  qc.invalidateQueries({ queryKey: ["admin", "ml-integrations", "topology"] });
  qc.invalidateQueries({ queryKey: ["admin", "ml-integrations", "all"] });
  qc.invalidateQueries({ queryKey: ["admin", "ml-integrations", "runtime-snapshot"] });
  qc.invalidateQueries({ queryKey: ["admin", "ml-integrations", "overview"] });
  qc.invalidateQueries({ queryKey: ["admin", "ml-integrations", "gpu-resources"] });
}

function formatCompatibilityItem(item: ManagedModelCompatibility): string {
  const fmt = item.format ?? "原生权重";
  return `${item.framework} · ${fmt} · ${item.runtime_device.toUpperCase()}`;
}

function labelForInstallation(item: PluginInstallationResponse): string {
  const summary = resolveInferencePluginSummary(item);
  return summary.display_name || summary.plugin_id;
}

export function InferencePluginInstallationsSection({
  onChanged,
}: InferencePluginInstallationsSectionProps): ReactNode {
  const queryClient = useQueryClient();
  const pushToast = useToastStore((s) => s.push);

  const [searchQuery, setSearchQuery] = useState("");
  const [modeFilter, setModeFilter] = useState<ExecutionModeFilter>("all");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [registerOpen, setRegisterOpen] = useState(false);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [confirmAction, setConfirmAction] = useState<ConfirmAction | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const listQ = useQuery({
    queryKey: INFERENCE_PLUGIN_INSTALLATIONS_QUERY_KEY,
    queryFn: () => adminInferencePluginsApi.list(),
    staleTime: 60_000,
  });

  const installations = listQ.data ?? EMPTY_INSTALLATIONS;

  const syncAfterMutation = () => {
    invalidatePluginQueries(queryClient);
    onChanged?.();
  };

  const activateMut = useMutation({
    mutationFn: ({ id, revision }: { id: string; revision: number }) =>
      adminInferencePluginsApi.activateDirect(id, revision),
    onMutate: ({ id }) => {
      setBusyId(id);
      setActionError(null);
      setActionNotice(null);
    },
    onSuccess: (updated) => {
      const title = labelForInstallation(updated);
      pushToast({ msg: `已启用直连插件「${title}」`, kind: "success" });
      syncAfterMutation();
    },
    onError: (err) => {
      const message = formatInferencePluginError(err);
      setActionError(message);
      pushToast({ msg: "启用插件失败", sub: message, kind: "error" });
    },
    onSettled: () => setBusyId(null),
  });

  const disableMut = useMutation({
    mutationFn: ({ id, revision }: { id: string; revision: number }) =>
      adminInferencePluginsApi.disable(id, revision),
    onMutate: ({ id }) => {
      setBusyId(id);
      setActionError(null);
      setActionNotice(null);
    },
    onSuccess: (updated) => {
      const title = labelForInstallation(updated);
      pushToast({ msg: `已停用插件「${title}」`, kind: "success" });
      syncAfterMutation();
    },
    onError: (err) => {
      const message = formatInferencePluginError(err);
      setActionError(message);
      pushToast({ msg: "停用插件失败", sub: message, kind: "error" });
    },
    onSettled: () => setBusyId(null),
  });

  const quiesceMut = useMutation({
    mutationFn: ({ id, revision }: { id: string; revision: number }) =>
      adminInferencePluginsApi.quiesce(id, revision),
    onMutate: ({ id }) => {
      setBusyId(id);
      setActionError(null);
      setActionNotice(null);
    },
    onSuccess: (result) => {
      const title = labelForInstallation(result);
      const notice = result.safe_to_stop
        ? `插件「${title}」已完成排空（safe_to_stop: 是），现在可安全停止外部插件进程或执行退役。`
        : `插件「${title}」排空请求已返回（safe_to_stop: 否），请待活动工作结束后再停止进程。`;
      setActionNotice(notice);
      pushToast({ msg: `已完成排空「${title}」`, sub: notice, kind: "success" });
      syncAfterMutation();
    },
    onError: (err) => {
      const message = formatInferencePluginError(err);
      setActionError(message);
      pushToast({ msg: "排空插件失败", sub: message, kind: "error" });
    },
    onSettled: () => setBusyId(null),
  });

  const retireMut = useMutation({
    mutationFn: ({ id, revision }: { id: string; revision: number }) =>
      adminInferencePluginsApi.retire(id, revision),
    onMutate: ({ id }) => {
      setBusyId(id);
      setActionError(null);
      setActionNotice(null);
    },
    onSuccess: (updated) => {
      const title = labelForInstallation(updated);
      setConfirmAction(null);
      pushToast({ msg: `已退役插件「${title}」`, kind: "success" });
      syncAfterMutation();
    },
    onError: (err) => {
      setConfirmAction(null);
      const message = formatInferencePluginError(err);
      setActionError(message);
      pushToast({ msg: "退役插件失败", sub: message, kind: "error" });
    },
    onSettled: () => setBusyId(null),
  });

  const cancelPreparedMut = useMutation({
    mutationFn: ({ id, revision }: { id: string; revision: number }) =>
      adminInferencePluginsApi.cancelPrepared(id, revision),
    onMutate: ({ id }) => {
      setBusyId(id);
      setActionError(null);
      setActionNotice(null);
    },
    onSuccess: (updated) => {
      const title = labelForInstallation(updated);
      setConfirmAction(null);
      pushToast({ msg: `已取消预留安装「${title}」`, kind: "success" });
      syncAfterMutation();
    },
    onError: (err) => {
      setConfirmAction(null);
      const message = formatInferencePluginError(err);
      setActionError(message);
      pushToast({ msg: "取消预留失败", sub: message, kind: "error" });
    },
    onSettled: () => setBusyId(null),
  });

  const filteredItems = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return installations.filter((item) => {
      const summary = resolveInferencePluginSummary(item);
      if (modeFilter !== "all" && summary.execution_mode !== modeFilter) return false;
      if (statusFilter !== "all" && summary.status !== statusFilter) return false;
      if (q) {
        const hay = [
          summary.display_name ?? "",
          summary.plugin_id,
          summary.version,
          summary.description ?? "",
          item.installation.manifest.publisher,
          item.installation.installation_id,
          item.registry_id,
        ]
          .join(" ")
          .toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [installations, modeFilter, statusFilter, searchQuery]);

  const detailItem = useMemo(
    () =>
      detailId
        ? (installations.find((item) => item.installation.installation_id === detailId) ?? null)
        : null,
    [detailId, installations],
  );

  const hasFilters =
    searchQuery.trim().length > 0 || modeFilter !== "all" || statusFilter !== "all";
  const anyPending =
    activateMut.isPending ||
    disableMut.isPending ||
    quiesceMut.isPending ||
    retireMut.isPending ||
    cancelPreparedMut.isPending;

  const toolbar = (
    <div className="flex flex-wrap items-center gap-2">
      <div className="relative">
        <Icon
          name="search"
          size={12}
          className="pointer-events-none absolute top-1/2 left-2 -translate-y-1/2 text-muted-foreground"
        />
        <Input
          aria-label="搜索插件"
          placeholder="搜索名称 / 插件 ID / 版本"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="h-8 w-56 pl-7 text-xs"
        />
      </div>
      <FilterGroup label="运行模式" hideLabel compact>
        <FilterSelect
          aria-label="按运行模式筛选插件"
          value={modeFilter}
          onChange={(e) => setModeFilter(e.target.value as ExecutionModeFilter)}
          className="h-8 text-xs"
        >
          <option value="all">全部模式</option>
          <option value="direct">直连</option>
          <option value="container">容器</option>
        </FilterSelect>
      </FilterGroup>
      <FilterGroup label="状态" hideLabel compact>
        <FilterSelect
          aria-label="按状态筛选插件"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as StatusFilter)}
          className="h-8 text-xs"
        >
          <option value="all">全部状态</option>
          <option value="active">已启用</option>
          <option value="disabled">已停用</option>
          <option value="prepared">已预留</option>
          <option value="retired">已退役</option>
        </FilterSelect>
      </FilterGroup>
      {hasFilters && (
        <button
          type="button"
          onClick={() => {
            setSearchQuery("");
            setModeFilter("all");
            setStatusFilter("all");
          }}
          className="text-xs text-brand no-underline hover:underline"
        >
          清除条件
        </button>
      )}
      <div className="ml-auto flex items-center gap-2">
        <Button size="sm" variant="primary" onClick={() => setRegisterOpen(true)}>
          <Icon name="plus" size={11} />
          注册直连插件
        </Button>
      </div>
    </div>
  );

  return (
    <div className="flex flex-col gap-3">
      {toolbar}

      {actionError && (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-md border border-status-danger bg-status-danger-soft px-3 py-2 text-xs text-status-danger"
        >
          <Icon name="warning" size={13} className="mt-0.5 shrink-0" />
          <div className="min-w-0 flex-1">
            <CopyableError message={actionError} />
          </div>
          <Button
            type="button"
            size="xs"
            variant="ghost"
            aria-label="关闭错误提示"
            onClick={() => setActionError(null)}
            className="h-6 px-1.5 text-status-danger"
          >
            <Icon name="x" size={12} />
          </Button>
        </div>
      )}

      {actionNotice && (
        <div
          role="status"
          className="flex items-start gap-2 rounded-md border border-status-positive bg-status-positive-soft px-3 py-2 text-xs text-status-positive"
        >
          <Icon name="checkCircle" size={13} className="mt-0.5 shrink-0" />
          <span className="min-w-0 flex-1">{actionNotice}</span>
          <Button
            type="button"
            size="xs"
            variant="ghost"
            aria-label="关闭状态提示"
            onClick={() => setActionNotice(null)}
            className="h-6 px-1.5 text-status-positive"
          >
            <Icon name="x" size={12} />
          </Button>
        </div>
      )}

      {listQ.isLoading ? (
        <div className="flex items-center justify-center gap-2 rounded-xl border border-border bg-card p-8 text-sm text-muted-foreground">
          <Icon name="loader2" size={14} className="spin" />
          加载推理插件安装列表…
        </div>
      ) : listQ.isError && !listQ.data ? (
        <div className="flex flex-col items-center gap-2 rounded-xl border border-border bg-card p-8 text-center text-sm text-status-danger">
          <Icon name="warning" size={18} />
          <CopyableError message={formatInferencePluginError(listQ.error)} />
          <Button size="sm" variant="ghost" onClick={() => listQ.refetch()}>
            <Icon name="refresh" size={11} />
            重试
          </Button>
        </div>
      ) : installations.length === 0 ? (
        <div className="flex flex-col items-center gap-1.5 rounded-xl border border-border bg-card p-10 text-center text-sm text-muted-foreground">
          <Icon name="box" size={28} className="opacity-25" />
          <div>尚无已安装的推理插件</div>
          <div className="text-xs">
            可点击「注册直连插件」接入非容器推理服务，或使用{" "}
            <span className="font-mono">yoao plugins install</span> 安装容器插件。
          </div>
        </div>
      ) : (
        <Table containerClassName={listStyles.surface}>
          <TableHeader>
            <TableRow>
              <TableHead>插件</TableHead>
              <TableHead>来源 / 模式</TableHead>
              <TableHead>状态</TableHead>
              <TableHead>设备 / 仲裁</TableHead>
              <TableHead>模型与能力</TableHead>
              <TableHead className="text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filteredItems.map((item) => (
              <PluginInstallationRow
                key={item.installation.installation_id}
                item={item}
                busy={busyId === item.installation.installation_id && anyPending}
                onOpenDetail={() => setDetailId(item.installation.installation_id)}
                onActivate={() =>
                  activateMut.mutate({
                    id: item.installation.installation_id,
                    revision: item.installation.revision,
                  })
                }
                onDisable={() =>
                  disableMut.mutate({
                    id: item.installation.installation_id,
                    revision: item.installation.revision,
                  })
                }
                onQuiesce={() =>
                  quiesceMut.mutate({
                    id: item.installation.installation_id,
                    revision: item.installation.revision,
                  })
                }
                onRequestRetire={() => setConfirmAction({ kind: "retire", item })}
                onRequestCancelPrepared={() => setConfirmAction({ kind: "cancelPrepared", item })}
              />
            ))}
            {filteredItems.length === 0 && (
              <TableRow>
                <TableCell colSpan={6}>
                  <div className="p-6 text-center text-sm text-muted-foreground">
                    没有匹配的推理插件安装
                  </div>
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      )}

      <DirectInferencePluginModal
        open={registerOpen}
        onClose={() => setRegisterOpen(false)}
        onSuccess={() => {
          setActionError(null);
          syncAfterMutation();
        }}
      />

      <PluginInstallationDetailModal item={detailItem} onClose={() => setDetailId(null)} />

      <AlertDialog
        open={confirmAction !== null}
        onOpenChange={(open) => {
          if (!open && !retireMut.isPending && !cancelPreparedMut.isPending) {
            setConfirmAction(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {confirmAction?.kind === "retire" ? "确认退役插件安装" : "确认取消预留安装"}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {confirmAction?.kind === "retire"
                ? `确认退役插件「${labelForInstallation(confirmAction.item)}」？退役会不可逆地清除运行态凭据，并要求不存在活跃模型发布或冻结执行；直连插件需先停用并完成排空。`
                : confirmAction
                  ? `确认取消预留插件「${labelForInstallation(confirmAction.item)}」？该操作将释放预留占位并要求无活跃引用。`
                  : ""}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={retireMut.isPending || cancelPreparedMut.isPending}>
              取消
            </AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={retireMut.isPending || cancelPreparedMut.isPending}
              onClick={(event) => {
                event.preventDefault();
                if (!confirmAction) return;
                const payload = {
                  id: confirmAction.item.installation.installation_id,
                  revision: confirmAction.item.installation.revision,
                };
                if (confirmAction.kind === "retire") {
                  retireMut.mutate(payload);
                } else {
                  cancelPreparedMut.mutate(payload);
                }
              }}
            >
              {confirmAction?.kind === "retire" ? "确认退役" : "确认取消预留"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function PluginInstallationRow({
  item,
  busy,
  onOpenDetail,
  onActivate,
  onDisable,
  onQuiesce,
  onRequestRetire,
  onRequestCancelPrepared,
}: {
  item: PluginInstallationResponse;
  busy: boolean;
  onOpenDetail: () => void;
  onActivate: () => void;
  onDisable: () => void;
  onQuiesce: () => void;
  onRequestRetire: () => void;
  onRequestCancelPrepared: () => void;
}): ReactNode {
  const inst = item.installation;
  const summary = resolveInferencePluginSummary(item);
  const statusMeta = STATUS_META[summary.status];
  const sourceMeta = SOURCE_TIER_META[summary.source_tier];
  const modeMeta = EXECUTION_MODE_META[summary.execution_mode];
  const isDirect = summary.execution_mode === "direct";
  const isRetired = summary.status === "retired";
  const capabilities = inst.manifest.capabilities;
  const highlightedCapabilities = capabilities.filter(
    (cap) => cap === "managed_models" || cap === "managed_evaluation",
  );
  const taskCapabilities = capabilities.filter(
    (cap) => cap !== "inference" && cap !== "managed_models" && cap !== "managed_evaluation",
  );
  const displayedCapabilities = [...highlightedCapabilities, ...taskCapabilities].slice(0, 3);
  const overflowCount =
    highlightedCapabilities.length + taskCapabilities.length - displayedCapabilities.length;

  return (
    <TableRow>
      <TableCell>
        <div className="flex max-w-[280px] flex-col gap-0.5">
          <div className="flex items-center gap-1.5">
            <span
              className="truncate text-sm font-medium text-foreground"
              title={labelForInstallation(item)}
            >
              {labelForInstallation(item)}
            </span>
            <CopyValueButton value={inst.installation_id} kind="id" label="复制安装 ID" />
          </div>
          <div className="truncate font-mono text-xs text-muted-foreground">
            {summary.plugin_id} · v{summary.version} · r{inst.revision}
          </div>
          {summary.description && (
            <span className="truncate text-xs text-muted-foreground" title={summary.description}>
              {summary.description}
            </span>
          )}
        </div>
      </TableCell>
      <TableCell>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge variant={sourceMeta.variant}>{sourceMeta.label}</Badge>
          <Badge variant={modeMeta.variant}>{modeMeta.label}</Badge>
        </div>
      </TableCell>
      <TableCell>
        <Badge variant={statusMeta.variant} dot>
          {statusMeta.label}
        </Badge>
      </TableCell>
      <TableCell>
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-1.5 text-sm">
            <span className="font-medium uppercase">{summary.device}</span>
            <span className="font-mono text-xs text-muted-foreground">
              {inst.resource.logical_device}
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            {summary.locally_arbitrated_gpu ? (
              <Badge variant="ai">本机仲裁</Badge>
            ) : (
              <Badge variant="outline">非本机仲裁</Badge>
            )}
            {inst.resource.gpu_resource_id && (
              <span
                className="max-w-[140px] truncate font-mono text-xs text-muted-foreground"
                title={inst.resource.gpu_resource_id}
              >
                {inst.resource.gpu_resource_id}
              </span>
            )}
          </div>
        </div>
      </TableCell>
      <TableCell>
        <div className="flex flex-col gap-1">
          <span className="text-sm text-foreground">{inst.manifest.models.length} 个模型</span>
          <div className="flex flex-wrap items-center gap-1">
            {displayedCapabilities.map((cap) => (
              <Badge
                key={cap}
                variant={
                  cap === "managed_models" || cap === "managed_evaluation" ? "accent" : "default"
                }
                title={cap}
              >
                {CAPABILITY_LABELS[cap] ?? cap}
              </Badge>
            ))}
            {overflowCount > 0 && <Badge variant="outline">+{overflowCount}</Badge>}
            {displayedCapabilities.length === 0 && (
              <span className="text-xs text-muted-foreground">基础推理</span>
            )}
          </div>
        </div>
      </TableCell>
      <TableCell className="text-right">
        <div className="flex flex-col items-end gap-1">
          <div className="flex flex-wrap items-center justify-end gap-1.5">
            <Button size="xs" variant="ghost" onClick={onOpenDetail}>
              <Icon name={isRetired ? "info" : "activity"} size={12} />
              {isRetired ? "详情" : "详情 / 探活"}
            </Button>

            {summary.status === "active" && (
              <Button size="xs" variant="default" onClick={onDisable} disabled={busy}>
                <Icon
                  name={busy ? "loader2" : "pause"}
                  size={12}
                  className={busy ? "spin" : undefined}
                />
                停用
              </Button>
            )}

            {summary.status === "disabled" && isDirect && (
              <>
                <Button size="xs" variant="default" onClick={onActivate} disabled={busy}>
                  <Icon
                    name={busy ? "loader2" : "play"}
                    size={12}
                    className={busy ? "spin" : undefined}
                  />
                  启用
                </Button>
                <Button size="xs" variant="default" onClick={onQuiesce} disabled={busy}>
                  排空
                </Button>
                <Button size="xs" variant="danger" onClick={onRequestRetire} disabled={busy}>
                  退役
                </Button>
              </>
            )}

            {summary.status === "disabled" && !isDirect && (
              <>
                <Button
                  size="xs"
                  variant="default"
                  disabled
                  title="容器插件激活需通过 yoao plugins install CLI 提供启动收据"
                >
                  启用
                </Button>
                <Button
                  size="xs"
                  variant="default"
                  disabled
                  title="容器插件排空需通过 yoao plugins CLI 执行"
                >
                  排空
                </Button>
                <Button size="xs" variant="danger" onClick={onRequestRetire} disabled={busy}>
                  退役
                </Button>
              </>
            )}

            {summary.status === "prepared" && (
              <>
                {!isDirect && (
                  <Button
                    size="xs"
                    variant="default"
                    disabled
                    title="容器插件激活需通过 yoao plugins install CLI 提供启动收据"
                  >
                    启用
                  </Button>
                )}
                <Button
                  size="xs"
                  variant="danger"
                  onClick={onRequestCancelPrepared}
                  disabled={busy}
                >
                  取消预留
                </Button>
              </>
            )}
          </div>

          {!isDirect && (summary.status === "disabled" || summary.status === "prepared") && (
            <span className="text-xs text-muted-foreground">
              容器启用与排空请使用 <span className="font-mono">yoao plugins install</span> CLI
            </span>
          )}
        </div>
      </TableCell>
    </TableRow>
  );
}

function PluginInstallationDetailModal({
  item,
  onClose,
}: {
  item: PluginInstallationResponse | null;
  onClose: () => void;
}): ReactNode {
  const [probeResult, setProbeResult] = useState<DirectPluginProbeResponse | null>(null);
  const [probeError, setProbeError] = useState<string | null>(null);

  const installationId = item?.installation.installation_id ?? null;

  useEffect(() => {
    setProbeResult(null);
    setProbeError(null);
  }, [installationId]);

  const probeMut = useMutation({
    mutationFn: (id: string) => adminInferencePluginsApi.probe(id),
    onMutate: () => {
      setProbeError(null);
    },
    onSuccess: (snapshot) => {
      setProbeResult(snapshot);
    },
    onError: (err) => {
      setProbeResult(null);
      setProbeError(formatInferencePluginError(err));
    },
  });

  if (!item) {
    return null;
  }

  const inst = item.installation;
  const summary = resolveInferencePluginSummary(item);
  const statusMeta = STATUS_META[summary.status];
  const sourceMeta = SOURCE_TIER_META[summary.source_tier];
  const modeMeta = EXECUTION_MODE_META[summary.execution_mode];
  const isRetired = summary.status === "retired";
  const isContainer = summary.execution_mode === "container";
  const poolEntries = Object.entries(inst.manifest.pools);
  const compatibility = inst.manifest.managed_model_compatibility ?? [];
  const allowedAssetSources = inst.manifest.allowed_asset_sources ?? [];
  const safeHomepage =
    summary.homepage && /^https:\/\//i.test(summary.homepage) ? summary.homepage : null;

  return (
    <Modal
      open={Boolean(item)}
      onClose={onClose}
      title={`插件安装详情 · ${labelForInstallation(item)}`}
      width={720}
      footer={
        <div className="flex justify-end">
          <Button type="button" variant="ghost" onClick={onClose}>
            关闭
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={sourceMeta.variant}>{sourceMeta.label}</Badge>
          <Badge variant={modeMeta.variant}>{modeMeta.label}</Badge>
          <Badge variant={statusMeta.variant} dot>
            {statusMeta.label}
          </Badge>
          <Badge variant="outline">修订版本 r{inst.revision}</Badge>
          {summary.locally_arbitrated_gpu ? (
            <Badge variant="ai">本机仲裁 GPU</Badge>
          ) : (
            <Badge variant="outline">非本机仲裁</Badge>
          )}
        </div>

        {isContainer && (summary.status === "prepared" || summary.status === "disabled") && (
          <div className="flex items-start gap-2 rounded-md border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
            <Icon name="info" size={13} className="mt-0.5 shrink-0" />
            <span>
              容器档插件的启动收据校验、激活与排空需由管理员在本机通过{" "}
              <span className="font-mono text-foreground">yoao plugins install</span> CLI 执行，平台
              API 不直接访问本地容器运行时。
            </span>
          </div>
        )}

        <section className="flex flex-col gap-2 rounded-lg border border-border bg-muted/20 p-3">
          <h3 className="m-0 text-md font-semibold text-foreground">清单与身份</h3>
          <dl className="m-0 grid grid-cols-2 gap-x-4 gap-y-2 text-sm max-sm:grid-cols-1">
            <DetailField
              label="插件 ID"
              value={<span className="font-mono">{summary.plugin_id}</span>}
            />
            <DetailField
              label="版本"
              value={<span className="font-mono">{summary.version}</span>}
            />
            <DetailField label="发布方" value={inst.manifest.publisher} />
            <DetailField label="许可证" value={inst.manifest.license} />
            <DetailField
              label="安装 ID"
              value={
                <span className="inline-flex items-center gap-1 font-mono text-xs">
                  <span className="truncate">{inst.installation_id}</span>
                  <CopyValueButton value={inst.installation_id} kind="id" label="复制安装 ID" />
                </span>
              }
            />
            <DetailField
              label="注册表 ID"
              value={
                <span className="inline-flex items-center gap-1 font-mono text-xs">
                  <span className="truncate">{item.registry_id}</span>
                  <CopyValueButton value={item.registry_id} kind="id" label="复制注册表 ID" />
                </span>
              }
            />
            {inst.manifest.protocol_version && (
              <DetailField
                label="协议版本"
                value={<span className="font-mono">{inst.manifest.protocol_version}</span>}
              />
            )}
            {inst.manifest.manifest_schema && (
              <DetailField
                label="清单规范"
                value={<span className="font-mono">{inst.manifest.manifest_schema}</span>}
              />
            )}
            {safeHomepage && (
              <DetailField
                label="主页"
                value={
                  <a
                    href={safeHomepage}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="break-all font-mono text-xs text-brand hover:underline"
                  >
                    {safeHomepage}
                  </a>
                }
              />
            )}
            {inst.manifest.artifact && (
              <DetailField
                label="OCI 制品"
                value={
                  <span className="break-all font-mono text-xs">{inst.manifest.artifact}</span>
                }
              />
            )}
            {inst.manifest.asset_manifest_sha256 && (
              <DetailField
                label="资产清单 SHA-256"
                value={
                  <span className="inline-flex items-center gap-1 font-mono text-xs">
                    <span className="truncate">{inst.manifest.asset_manifest_sha256}</span>
                    <CopyValueButton
                      value={inst.manifest.asset_manifest_sha256}
                      kind="hash"
                      label="复制资产清单哈希"
                    />
                  </span>
                }
              />
            )}
          </dl>
          {summary.description && (
            <p className="m-0 text-xs leading-normal text-muted-foreground">
              {summary.description}
            </p>
          )}
        </section>

        <section className="flex flex-col gap-2 rounded-lg border border-border bg-muted/20 p-3">
          <h3 className="m-0 text-md font-semibold text-foreground">资源与仲裁绑定</h3>
          <dl className="m-0 grid grid-cols-2 gap-x-4 gap-y-2 text-sm max-sm:grid-cols-1">
            <DetailField label="计算设备" value={summary.device.toUpperCase()} />
            <DetailField
              label="逻辑设备"
              value={<span className="font-mono">{inst.resource.logical_device}</span>}
            />
            <DetailField
              label="本机 GPU 仲裁"
              value={
                summary.locally_arbitrated_gpu ? "是（受本机 Arbiter 仲裁）" : "否（非本机仲裁）"
              }
            />
            <DetailField
              label="GPU 资源 ID"
              value={
                inst.resource.gpu_resource_id ? (
                  <span className="font-mono text-xs">{inst.resource.gpu_resource_id}</span>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )
              }
            />
            {inst.resource.physical_gpu_uuid && (
              <DetailField
                label="物理 GPU UUID"
                value={<span className="font-mono text-xs">{inst.resource.physical_gpu_uuid}</span>}
              />
            )}
            <DetailField
              label="内存 / 显存声明"
              value={`${inst.manifest.resource.memory_mb ?? "—"} MiB / ${inst.manifest.resource.gpu_memory_mb ?? 0} MiB`}
            />
          </dl>
        </section>

        <section className="flex flex-col gap-2.5 rounded-lg border border-border bg-muted/20 p-3">
          <h3 className="m-0 text-md font-semibold text-foreground">已批准能力与模型拓扑</h3>
          <div>
            <div className="mb-1 text-xs font-medium text-muted-foreground">已批准能力</div>
            <div className="flex flex-wrap gap-1.5">
              {inst.manifest.capabilities.map((cap) => (
                <Badge
                  key={cap}
                  variant={
                    cap === "managed_models" || cap === "managed_evaluation" ? "accent" : "default"
                  }
                >
                  {CAPABILITY_LABELS[cap] ? `${CAPABILITY_LABELS[cap]} (${cap})` : cap}
                </Badge>
              ))}
            </div>
          </div>

          <div>
            <div className="mb-1 text-xs font-medium text-muted-foreground">
              模型列表（{inst.manifest.models.length}）
            </div>
            <div className="flex flex-col gap-1.5">
              {inst.manifest.models.map((model) => (
                <div
                  key={model.id}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border bg-card px-2.5 py-1.5 text-sm"
                >
                  <div className="flex items-center gap-2">
                    <span className="font-mono font-medium">{model.id}</span>
                    <Badge variant="outline">{model.task}</Badge>
                    {model.is_interactive && <Badge variant="ai">交互式</Badge>}
                  </div>
                  <span className="text-xs text-muted-foreground">
                    提示：{model.supported_prompts.join(", ")} · 输入：
                    {model.supported_inputs.join(", ")}
                  </span>
                </div>
              ))}
            </div>
          </div>

          <div>
            <div className="mb-1 text-xs font-medium text-muted-foreground">
              模型池（{poolEntries.length}）
            </div>
            <div className="flex flex-wrap gap-1.5">
              {poolEntries.map(([poolName, topology]) => (
                <Badge key={poolName} variant="outline">
                  <span className="font-mono">{poolName}</span> · cap {topology.cap}
                </Badge>
              ))}
            </div>
          </div>

          <div>
            <div className="mb-1 text-xs font-medium text-muted-foreground">
              已批准受控模型兼容性（{compatibility.length}）
            </div>
            {compatibility.length > 0 ? (
              <div className="flex flex-wrap gap-1.5">
                {compatibility.map((entry, idx) => (
                  <Badge
                    key={`${entry.framework}-${entry.format ?? "native"}-${entry.runtime_device}-${idx}`}
                    variant="accent"
                  >
                    {formatCompatibilityItem(entry)}
                  </Badge>
                ))}
              </div>
            ) : (
              <span className="text-xs text-muted-foreground">未批准受控模型部署</span>
            )}
          </div>

          {allowedAssetSources.length > 0 && (
            <div>
              <div className="mb-1 text-xs font-medium text-muted-foreground">允许的资产来源</div>
              <div className="flex flex-wrap gap-1.5">
                {allowedAssetSources.map((src) => (
                  <Badge key={src} variant="outline">
                    {src}
                  </Badge>
                ))}
              </div>
            </div>
          )}
        </section>

        <section className="flex flex-col gap-2.5 rounded-lg border border-border bg-card p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h3 className="m-0 text-md font-semibold text-foreground">只读实时探活</h3>
              <p className="m-0 mt-0.5 text-xs text-muted-foreground">
                只读请求端点 /health 与 /setup，绝不改写已冻结的安装清单与审批状态。
              </p>
            </div>
            <Button
              type="button"
              size="sm"
              variant="primary"
              disabled={isRetired || probeMut.isPending}
              onClick={() => probeMut.mutate(inst.installation_id)}
            >
              <Icon
                name={probeMut.isPending ? "loader2" : "activity"}
                size={12}
                className={probeMut.isPending ? "spin" : undefined}
              />
              {probeMut.isPending ? "探活中…" : "只读探活"}
            </Button>
          </div>

          {isRetired && (
            <p className="m-0 text-xs text-muted-foreground">已退役的插件安装不支持只读探活。</p>
          )}

          {probeError && (
            <div
              role="alert"
              className="flex items-start gap-1.5 rounded-md border border-status-danger bg-status-danger-soft px-3 py-2 text-xs text-status-danger"
            >
              <Icon name="warning" size={13} className="mt-0.5 shrink-0" />
              <div className="min-w-0 flex-1">
                <CopyableError message={probeError} />
              </div>
            </div>
          )}

          {probeResult && <ProbeSnapshotView snapshot={probeResult} />}
        </section>
      </div>
    </Modal>
  );
}

function ProbeSnapshotView({ snapshot }: { snapshot: DirectPluginProbeResponse }): ReactNode {
  const observedModels = snapshot.observed_models ?? [];
  const observedCompat = snapshot.observed_managed_model_compatibility ?? [];
  const observedPools = Object.entries(snapshot.observed_pools ?? {});

  return (
    <div className="flex flex-col gap-2.5 rounded-md border border-border bg-muted/30 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={snapshot.healthy ? "success" : "danger"} dot>
          {snapshot.healthy ? "端点健康" : "端点异常"}
        </Badge>
        <Badge variant={snapshot.matches_manifest ? "success" : "warning"}>
          {snapshot.matches_manifest ? "与清单一致" : "与清单不一致"}
        </Badge>
        <Badge variant={snapshot.terminal_drain_supported ? "ai" : "outline"}>
          {snapshot.terminal_drain_supported ? "支持终端排空" : "不支持终端排空"}
        </Badge>
        <Badge variant={snapshot.observed_managed_evaluation ? "accent" : "outline"}>
          {snapshot.observed_managed_evaluation ? "端点声明受控评估" : "未声明受控评估"}
        </Badge>
      </div>

      {snapshot.mismatch_reason && (
        <div
          role="alert"
          className="rounded-md border border-status-danger bg-status-danger-soft px-2.5 py-2 text-xs text-status-danger"
        >
          清单偏差原因：
          <CopyableError message={snapshot.mismatch_reason} />
        </div>
      )}

      <dl className="m-0 grid grid-cols-3 gap-3 text-sm max-sm:grid-cols-1">
        <DetailField
          label="Boot ID"
          value={
            snapshot.boot_id ? (
              <span className="break-all font-mono text-xs">{snapshot.boot_id}</span>
            ) : (
              <span className="text-muted-foreground">—</span>
            )
          }
        />
        <DetailField
          label="线协议版本"
          value={
            snapshot.protocol_version ? (
              <span className="font-mono">{snapshot.protocol_version}</span>
            ) : (
              <span className="text-muted-foreground">—</span>
            )
          }
        />
        <DetailField
          label="观测设备"
          value={
            snapshot.device ? (
              snapshot.device.toUpperCase()
            ) : (
              <span className="text-muted-foreground">—</span>
            )
          }
        />
      </dl>

      <div>
        <div className="mb-1 text-xs font-medium text-muted-foreground">
          观测模型（{observedModels.length}）
        </div>
        {observedModels.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {observedModels.map((model) => (
              <Badge key={model.id} variant="outline">
                <span className="font-mono">{model.id}</span> · {model.task}
              </Badge>
            ))}
          </div>
        ) : (
          <span className="text-xs text-muted-foreground">未返回模型列表</span>
        )}
      </div>

      {observedPools.length > 0 && (
        <div>
          <div className="mb-1 text-xs font-medium text-muted-foreground">
            观测模型池（{observedPools.length}）
          </div>
          <div className="flex flex-wrap gap-1.5">
            {observedPools.map(([poolName, pool]) => (
              <Badge key={poolName} variant="outline">
                <span className="font-mono">{poolName}</span> · cap {pool.cap}
              </Badge>
            ))}
          </div>
        </div>
      )}

      <div>
        <div className="mb-1 text-xs font-medium text-muted-foreground">
          观测受控模型兼容性（{observedCompat.length}）
        </div>
        {observedCompat.length > 0 ? (
          <div className="flex flex-wrap gap-1.5">
            {observedCompat.map((entry, idx) => (
              <Badge
                key={`${entry.framework}-${entry.format ?? "native"}-${entry.runtime_device}-${idx}`}
                variant="accent"
              >
                {formatCompatibilityItem(entry)}
              </Badge>
            ))}
          </div>
        ) : (
          <span className="text-xs text-muted-foreground">未声明受控模型兼容性</span>
        )}
      </div>
    </div>
  );
}

function DetailField({ label, value }: { label: string; value: ReactNode }): ReactNode {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="m-0 min-w-0 text-sm text-foreground">{value}</dd>
    </div>
  );
}
