import { storageAccessDenied } from "./storageQueryState";
import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { datasetSourcesApi } from "@/api/datasetSources";
import { storageApi } from "@/api/storage";
import type { StorageLocationOut } from "@/api/generated/types.gen";
import { useAuthStore } from "@/stores/authStore";
import { usePermissions } from "@/hooks/usePermissions";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { verifiedContentError } from "@/hooks/useVerifiedImage";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { listStyles } from "@/components/ui/listStyles";
import { Modal } from "@/components/ui/Modal";
import { Card } from "@/components/ui/Card";
import { ListPagination, listPageSize } from "@/components/ui/ListPagination";
import { StorageOriginalCachePanel } from "./StorageOriginalCachePanel";
import { FilesystemLocationsPanel } from "./FilesystemLocationsPanel";
import { StorageConnectionsPanel } from "./StorageConnectionsPanel";
import { StorageLocationSourceSummary } from "./StorageLocationSourceSummary";
import { StorageMaintenanceImpact } from "./StorageMaintenanceImpact";
import { StorageLocationMaintenance } from "./StorageLocationMaintenance";
import { StorageBindingChangeButton } from "./StorageBindingChangeButton";
import { safeDatasetReturn } from "@/components/datasets/storageNavigation";

const PURPOSES = [
  ["annotations", "标注文件"],
  ["datasets", "托管数据集"],
  ["bug-reports", "问题附件"],
  ["media-cache", "派生媒体缓存"],
  ["audit-archive", "审计归档"],
  ["import", "导入暂存"],
  ["export", "导出文件"],
  ["avatars", "用户头像"],
  ["training", "冻结训练数据"],
] as const;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const KIND: Record<string, string> = {
  filesystem: "服务器目录",
  s3: "S3 兼容对象存储",
  sftp: "SFTP",
};
const CAPABILITY: Record<string, string> = {
  reference: "原地引用",
  list: "列举",
  read: "读取",
  range: "范围读取",
  write: "写入",
  delete: "删除",
  presign: "签名访问",
};
type Location = StorageLocationOut & { allowed_actions?: string[] };

export function StorageLocationsConsole() {
  const revision = useSessionRevision();
  const { role } = usePermissions();
  return <ConsoleOwner key={`${revision}/${role}`} />;
}

function ConsoleOwner() {
  const { role } = usePermissions();
  const owner = useAuthStore((s) => `${s.user?.id}/${s.sessionId}/${s.user?.role}`);
  const location = useLocation();
  const client = useQueryClient();
  const [params, setParams] = useSearchParams();
  const allowed = role === "super_admin" || role === "project_admin";
  const tab = params.get("tab") === "bindings" ? "bindings" : "locations";
  const rawId = params.get("location") ?? "";
  const locationId = UUID.test(rawId) ? rawId : "";
  const pageValue = Number(params.get("page") ?? 1);
  const page =
    Number.isSafeInteger(pageValue) && pageValue > 0 && pageValue <= 1_000_000 ? pageValue - 1 : 0;
  const pageSize = listPageSize(params.get("page_size"));
  const returnTo = safeDatasetReturn(params.get("returnTo"));
  const storageParams = new URLSearchParams(location.search);
  storageParams.delete("returnTo");
  const storageContext = `${location.pathname}${storageParams.size ? `?${storageParams}` : ""}`;
  const [createKind, setCreateKind] = useState<"filesystem" | "connection" | null>(null);
  const dialogTrigger = useRef<HTMLElement | null>(null);
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(timer);
  }, []);
  const locations = useQuery({
    queryKey: ["storage-source-locations", owner, page * pageSize, pageSize],
    queryFn: ({ signal }) => datasetSourcesApi.locationPage(page * pageSize, signal, pageSize),
    enabled: allowed && tab === "locations",
    retry: false,
  });
  const detail = useQuery({
    queryKey: ["storage-location", owner, locationId],
    queryFn: ({ signal }) => datasetSourcesApi.location(locationId, signal),
    enabled: allowed && tab === "locations" && !!locationId,
    retry: false,
  });
  const probes = useQuery({
    queryKey: ["storage-location-probes", owner, locationId, detail.data?.config_revision],
    queryFn: ({ signal }) => storageApi.probes(locationId, signal),
    enabled: allowed && tab === "locations" && !!detail.data,
    retry: false,
  });
  const bindings = useQuery({
    queryKey: ["storage-bindings", owner],
    queryFn: ({ signal }) => storageApi.bindings(signal),
    enabled: role === "super_admin" && tab === "bindings",
    retry: false,
  });
  const locationData = storageAccessDenied(locations.error) ? undefined : locations.data;
  const bindingData = storageAccessDenied(bindings.error) ? undefined : bindings.data;
  const probeData = storageAccessDenied(probes.error) ? undefined : probes.data;
  const patch = (values: Record<string, string | null>) => {
    setParams((current) => {
      const next = new URLSearchParams(current);
      Object.entries(values).forEach(([key, value]) =>
        value === null ? next.delete(key) : next.set(key, value),
      );
      return next;
    });
  };
  useEffect(() => {
    if (!locationData || locations.isFetching) return;
    const lastPage = Math.max(0, Math.ceil(locationData.total / pageSize) - 1);
    if (page > lastPage)
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          next.set("page", String(lastPage + 1));
          return next;
        },
        { replace: true },
      );
  }, [locationData, locations.isFetching, page, pageSize, setParams]);
  const selected = (storageAccessDenied(detail.error) ? undefined : detail.data) as
    | Location
    | undefined;
  const returnLink = returnTo && (
    <Link className="mb-4 inline-block text-sm text-brand underline" to={returnTo}>
      返回数据集
      {new URL(returnTo, "https://dataset.invalid").searchParams.has("import") ? "导入" : "工作台"}
    </Link>
  );
  if (!allowed) return <p className="text-sm text-muted-foreground">当前账号没有存储管理权限。</p>;
  return (
    <>
      {returnLink}
      <nav aria-label="存储管理视图" className="mb-4 flex gap-2">
        <Button
          aria-pressed={tab === "locations"}
          variant={tab === "locations" ? "primary" : "default"}
          onClick={() => patch({ tab: "locations" })}
        >
          存储位置
        </Button>
        <Button
          aria-pressed={tab === "bindings"}
          variant={tab === "bindings" ? "primary" : "default"}
          onClick={() => patch({ tab: "bindings" })}
        >
          用途映射
        </Button>
      </nav>
      {tab === "bindings" ? (
        <Card>
          <div className="p-4">
            <h2 className="mb-1 text-sm font-semibold">平台用途映射</h2>
            <p className="mb-3 text-xs text-muted-foreground">
              用途与物理桶独立计列；已配置表示用途映射有效，不代表位置在线或迁移完成。
            </p>
            {role !== "super_admin" ? (
              <p className="text-xs text-muted-foreground">全局用途映射仅超级管理员可查看。</p>
            ) : (
              <>
                {bindings.isFetching && (
                  <p role="status" className="mb-2 text-xs">
                    正在读取用途配置…
                  </p>
                )}
                {bindings.error && (
                  <p role="alert" className="mb-2 text-xs text-status-danger">
                    {bindingData ? "刷新失败，保留上次配置。" : "无法读取用途配置。"}
                    {verifiedContentError(bindings.error)}
                  </p>
                )}
                <div className="overflow-x-auto">
                  <table aria-label="平台用途映射" className={`${listStyles.table} min-w-max`}>
                    <thead>
                      <tr className="border-b border-border text-muted-foreground">
                        {["用途", "位置", "桶 / 前缀", "读写策略", "用途代次", "状态"].map(
                          (label) => (
                            <th key={label} className="px-2 py-2 font-medium">
                              {label}
                            </th>
                          ),
                        )}
                      </tr>
                    </thead>
                    <tbody>
                      {PURPOSES.map(([purpose, label]) => {
                        const binding = bindingData?.find((row) => row.purpose === purpose);
                        return (
                          <tr key={purpose} className="border-b border-border">
                            <td className="px-2 py-2">
                              <div>{label}</div>
                              <div className="mt-1 text-xs text-muted-foreground">{purpose}</div>
                            </td>
                            <td className="px-2 py-2">
                              {binding?.location_id ? (
                                <Link
                                  className="text-brand underline"
                                  to={`/storage?${new URLSearchParams({ tab: "locations", location: binding.location_id, ...(returnTo ? { returnTo } : {}) })}`}
                                >
                                  查看位置
                                </Link>
                              ) : (
                                "未配置"
                              )}
                            </td>
                            <td className="px-2 py-2">
                              <span className="block max-w-56 break-all">
                                {binding?.bucket ?? "未配置"}
                              </span>
                            </td>
                            <td className="min-w-44 px-2 py-2">
                              <div>
                                读取：
                                {binding?.read_policy === "consumer_owned_compatibility"
                                  ? "对象记录与兼容路径"
                                  : "未提供"}
                              </div>
                              <div className="mt-1">
                                写入：
                                {binding?.write_policy === "deployment_mapping"
                                  ? "当前部署目标"
                                  : binding?.write_policy === "current_binding"
                                    ? "当前登记目标"
                                    : "未提供"}
                              </div>
                            </td>
                            <td className="px-2 py-2 tabular-nums">
                              {binding?.generation ?? "—"}
                              <div className="mt-1 whitespace-nowrap text-xs text-muted-foreground">
                                位置修订 {binding?.location_config_revision ?? "未提供"}
                              </div>
                            </td>
                            <td className="px-2 py-2">
                              {!bindingData
                                ? "待读取"
                                : binding?.state === "configured"
                                  ? "已配置"
                                  : binding?.state === "mismatch"
                                    ? "配置不匹配"
                                    : "未配置"}
                              {(purpose === "avatars" || purpose === "bug-reports") &&
                                binding?.state === "configured" &&
                                [
                                  "reviewed_avatar_switch",
                                  "reviewed_bug_attachment_switch",
                                ].includes(binding.change_policy) && (
                                  <StorageBindingChangeButton
                                    purpose={purpose}
                                    binding={binding}
                                    onApplied={async () => {
                                      await Promise.all(
                                        [
                                          ["storage-bindings"],
                                          ["storage-source-locations"],
                                          ["storage-location"],
                                          ["avatar-binding-candidates"],
                                          ["bug-attachment-binding-candidates"],
                                          ["storage-buckets"],
                                        ].map((queryKey) => client.invalidateQueries({ queryKey })),
                                      );
                                    }}
                                  />
                                )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                {!!bindingData?.length &&
                  bindingData.every((row) => row.change_policy === "legacy_mapping_locked") && (
                    <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
                      当前部署映射锁定，受控用途切换尚未开放。已固定位置的对象按记录读取，其余入口保留既有兼容规则。
                    </p>
                  )}
                {bindingData?.some((row) => row.change_policy === "reviewed_avatar_switch") && (
                  <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
                    当前开放头像和报告附件用途的位置切换，其余用途保持锁定。已有对象保留保存位置，切换不搬迁历史对象。
                  </p>
                )}
              </>
            )}
          </div>
        </Card>
      ) : (
        <>
          <Card>
            <div className="p-4">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <h2 className="text-sm font-semibold">存储位置</h2>
                <span className="text-xs text-muted-foreground">
                  {locationData?.total ?? "…"} 个
                </span>
              </div>
              <div className="mb-3 flex flex-wrap gap-2">
                {role === "super_admin" && (
                  <Button
                    size="sm"
                    onClick={(event) => {
                      dialogTrigger.current = event.currentTarget;
                      setCreateKind("filesystem");
                    }}
                  >
                    登记目录
                  </Button>
                )}
                <Button
                  size="sm"
                  onClick={(event) => {
                    dialogTrigger.current = event.currentTarget;
                    setCreateKind("connection");
                  }}
                >
                  新建远端连接
                </Button>
              </div>
              {locations.isPending && (
                <p role="status" className="text-xs text-muted-foreground">
                  正在加载位置…
                </p>
              )}
              {locations.error && (
                <p role="alert" className="mb-2 text-xs text-status-danger">
                  {locationData ? "刷新失败，位置列表可能过期。" : "无法读取位置。"}
                  {verifiedContentError(locations.error)}
                </p>
              )}
              <div className={listStyles.scroll}>
                <table className={listStyles.table} aria-label="存储位置列表">
                  <thead>
                    <tr>
                      <th>名称</th>
                      <th>类型</th>
                      <th>状态</th>
                      <th className="text-right">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {locationData?.items.map((item) => (
                      <tr key={item.id}>
                        <td>
                          <button
                            className="text-sm font-medium text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            onClick={(event) => {
                              dialogTrigger.current = event.currentTarget;
                              setCreateKind(null);
                              patch({ location: item.id });
                            }}
                            aria-haspopup="dialog"
                          >
                            {item.name}
                          </button>
                        </td>
                        <td>{KIND[item.kind] ?? item.kind}</td>
                        <td>
                          <Badge variant={item.enabled ? "success" : "outline"}>
                            {item.enabled ? "已启用" : "已停用"}
                          </Badge>
                        </td>
                        <td className="text-right">
                          <Button
                            size="sm"
                            aria-label={`查看 ${item.name} 详情`}
                            aria-haspopup="dialog"
                            onClick={(event) => {
                              dialogTrigger.current = event.currentTarget;
                              setCreateKind(null);
                              patch({ location: item.id });
                            }}
                          >
                            查看详情
                          </Button>
                        </td>
                      </tr>
                    ))}
                    {locationData?.total === 0 && (
                      <tr>
                        <td colSpan={4} className={listStyles.empty}>
                          暂无获授权的存储位置。
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
              <ListPagination
                label="存储位置"
                page={page}
                pageSize={pageSize}
                total={locationData?.total ?? 0}
                loading={locations.isFetching}
                onPageChange={(next) => patch({ page: String(next + 1) })}
                onPageSizeChange={(size) => patch({ page_size: String(size), page: null })}
              />
            </div>
          </Card>
          <Modal
            open={!!createKind || !!rawId}
            title={
              createKind === "filesystem"
                ? "登记目录"
                : createKind === "connection"
                  ? "新建远端连接"
                  : "存储位置详情"
            }
            width={960}
            returnFocusRef={dialogTrigger}
            onClose={() => {
              setCreateKind(null);
              if (rawId) patch({ location: null });
            }}
          >
            {returnLink}
            {createKind === "filesystem" ? (
              <>
                <FilesystemLocationsPanel
                  onCreated={(item) => {
                    void client.invalidateQueries({ queryKey: ["storage-source-locations"] });
                    setCreateKind(null);
                    patch({ location: item.id });
                  }}
                />
              </>
            ) : createKind === "connection" ? (
              <StorageConnectionsPanel
                hideList
                hideHeaderAction
                showForm
                onShowFormChange={(open) => {
                  if (!open) setCreateKind(null);
                }}
                onCreated={(connection) => {
                  setCreateKind(null);
                  /* The refreshed list exposes the new location identity. */ void connection;
                }}
              />
            ) : (
              <>
                {rawId && !locationId && (
                  <p role="alert" className="text-xs text-status-danger">
                    存储位置链接无效。
                  </p>
                )}
                {locationId && detail.isPending && (
                  <p role="status" className="text-xs text-muted-foreground">
                    正在读取位置详情…
                  </p>
                )}
                {detail.error && (
                  <p role="alert" className="mb-3 text-xs text-status-danger">
                    {selected ? "刷新失败，详情可能过期。" : "位置不可用或当前账号无权查看。"}
                    {verifiedContentError(detail.error)}
                  </p>
                )}
                {selected && (
                  <>
                    <Card>
                      <div className="p-4">
                        <div className="mb-3 flex items-start justify-between gap-3">
                          <div>
                            <h2 className="text-md font-semibold">{selected.name}</h2>
                            <p className="mt-1 text-xs text-muted-foreground">
                              {KIND[selected.kind] ?? selected.kind} · 配置修订{" "}
                              {selected.config_revision}
                            </p>
                          </div>
                          <Badge variant={selected.enabled ? "success" : "outline"}>
                            {selected.enabled ? "已启用" : "已停用"}
                          </Badge>
                        </div>
                        <p className="mb-2 text-sm font-medium">声明能力</p>
                        <div className="flex flex-wrap gap-2">
                          {Object.entries(selected.declared_capabilities).map(([key, value]) => (
                            <Badge key={key} variant={value ? "outline" : "default"}>
                              {CAPABILITY[key] ?? key}：{value ? "支持" : "不支持"}
                            </Badge>
                          ))}
                        </div>
                        <dl className="mt-4 grid grid-cols-2 gap-3 text-xs">
                          {[
                            ["物理容量", "未提供"],
                            ["存储配额", "未提供"],
                          ].map(([label, value]) => (
                            <div key={label}>
                              <dt className="text-muted-foreground">{label}</dt>
                              <dd className="mt-1 text-sm">{value}</dd>
                            </div>
                          ))}
                        </dl>
                        <p className="mt-3 text-xs text-muted-foreground">
                          物理容量与配额尚未提供。源索引规模见下方来源诊断。
                          {role === "super_admin" &&
                            "平台托管桶的对象字节见本页“平台对象占用”，不代表磁盘容量。"}
                        </p>
                        <h3 className="mb-2 mt-4 text-sm font-semibold">节点能力探测</h3>
                        {selected.allowed_actions?.includes("probe") && (
                          <LocationProbeForm
                            key={`${selected.id}/${selected.config_revision}`}
                            locationId={selected.id}
                            enabled={selected.enabled}
                          />
                        )}
                        {probes.error && (
                          <p role="alert" className="mb-2 text-xs text-status-danger">
                            {probeData ? "刷新失败，保留上次探测记录。" : "无法读取探测记录。"}
                            {verifiedContentError(probes.error)}
                          </p>
                        )}
                        {probes.isPending ? (
                          <p role="status" className="text-xs text-muted-foreground">
                            正在读取探测记录…
                          </p>
                        ) : probeData?.length === 0 ? (
                          <p className="text-xs text-muted-foreground">
                            尚无探测记录；声明能力尚未在节点核验。
                          </p>
                        ) : (
                          <ul className="space-y-2 text-xs">
                            {probeData?.map((probe) => {
                              const stale =
                                new Date(probe.expires_at).getTime() <= now ||
                                probe.code === "stale";
                              return (
                                <li key={probe.node_id} className="rounded-md bg-muted p-2">
                                  <div className="text-sm font-medium">
                                    {probe.node_id} ·{" "}
                                    {stale ? "已过期" : probe.ok ? "探测通过" : "探测未通过"} ·{" "}
                                    {probe.code}
                                  </div>
                                  <div className="mt-1 text-muted-foreground">
                                    观察于 {new Date(probe.observed_at).toLocaleString("zh-CN")} ·
                                    到期 {new Date(probe.expires_at).toLocaleString("zh-CN")}
                                  </div>
                                  <div className="mt-1">
                                    {Object.entries(probe.capabilities)
                                      .map(([key, value]) => `${CAPABILITY[key] ?? key}：${value}`)
                                      .join(" · ")}
                                  </div>
                                </li>
                              );
                            })}
                          </ul>
                        )}
                        <p className="mt-2 text-xs text-muted-foreground">
                          探测仅说明指定节点当时的能力；启用状态与连接健康分别展示。
                        </p>
                      </div>
                    </Card>
                    <div className="mt-4">
                      <StorageMaintenanceImpact location={selected} />
                      <StorageLocationMaintenance location={selected} />
                      {selected.kind === "filesystem" &&
                      selected.allowed_actions?.includes("manage_location") ? (
                        <FilesystemLocationsPanel locationId={selected.id} showProbe={false} />
                      ) : selected.connection_id ? (
                        <StorageConnectionsPanel
                          connectionId={selected.connection_id}
                          hideHeaderAction
                          allowManage={
                            selected.allowed_actions?.includes("manage_connection") ?? false
                          }
                        />
                      ) : null}
                    </div>
                    {selected.allowed_actions?.includes("manage_location") &&
                      ["filesystem", "s3", "sftp"].includes(selected.kind) && (
                        <StorageOriginalCachePanel
                          key={`${selected.id}/${selected.config_revision}`}
                          location={selected}
                        />
                      )}
                    <StorageLocationSourceSummary
                      locationId={selected.id}
                      returnTo={storageContext}
                    />
                  </>
                )}
              </>
            )}
          </Modal>
        </>
      )}
    </>
  );
}

function LocationProbeForm({ locationId, enabled }: { locationId: string; enabled: boolean }) {
  const capture = useSessionGuard();
  const client = useQueryClient();
  const [node, setNode] = useState("api");
  const [sample, setSample] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const probe = async () => {
    const current = capture();
    setBusy(true);
    setError("");
    try {
      await datasetSourcesApi.probe(locationId, node.trim(), sample || undefined);
      if (current()) await client.invalidateQueries({ queryKey: ["storage-location-probes"] });
    } catch (error) {
      if (current()) setError(verifiedContentError(error));
    } finally {
      if (current()) setBusy(false);
    }
  };
  const field = "rounded-md border border-input bg-background px-2 py-1.5 text-sm text-foreground";
  return (
    <div className="mb-3 text-sm">
      <div className="flex flex-wrap items-end gap-2">
        <label className="grid gap-1">
          探测节点
          <input
            className={field}
            value={node}
            onChange={(event) => setNode(event.target.value)}
            placeholder="api 或 worker hostname"
            disabled={busy || !enabled}
          />
        </label>
        <label className="grid gap-1">
          可选样例文件
          <input
            className={field}
            value={sample}
            onChange={(event) => setSample(event.target.value)}
            placeholder="位置内相对路径"
            disabled={busy || !enabled}
          />
        </label>
        <Button size="sm" disabled={busy || !enabled || !node.trim()} onClick={() => void probe()}>
          {busy ? "探测中…" : "探测此位置"}
        </Button>
      </div>
      {!enabled && <p className="mt-2 text-muted-foreground">位置已停用，保留历史探测记录。</p>}
      {error && (
        <p role="alert" className="mt-2 text-status-danger">
          {error}
        </p>
      )}
    </div>
  );
}
