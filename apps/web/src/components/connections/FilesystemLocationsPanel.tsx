import { StorageLocationChangeButton } from "./StorageLocationChangeButton";
import { StorageCacheFields } from "./StorageCacheFields";
import { originalCacheValues, type CachePolicy } from "@/utils/cachePolicy";
import { storageAccessDenied } from "./storageQueryState";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { useId, useState } from "react";
import { StorageLocationAccessFields } from "./StorageLocationAccessFields";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { datasetSourcesApi } from "@/api/datasetSources";
import { usePermissions } from "@/hooks/usePermissions";
import { useAuthStore } from "@/stores/authStore";
import { verifiedContentError } from "@/hooks/useVerifiedImage";
import { Button } from "@/components/ui/Button";
import type { StorageLocationOut, StorageProbeOut } from "@/api/generated/types.gen";

const field = "rounded-md border border-input bg-background px-2 py-1.5 text-sm text-foreground";
interface FilesystemLocationsPanelProps {
  locationId?: string;
  showProbe?: boolean;
  onCreated?: (location: StorageLocationOut) => void;
}
export function FilesystemLocationsPanel(props: FilesystemLocationsPanelProps = {}) {
  const revision = useSessionRevision();
  const { role } = usePermissions();
  return (
    <FilesystemLocationsPanelOwner
      key={`${revision}/${role}/${props.locationId ?? "new"}`}
      {...props}
    />
  );
}

function FilesystemLocationsPanelOwner({
  locationId,
  onCreated,
  showProbe = true,
}: FilesystemLocationsPanelProps) {
  const rootHint = useId();
  const capture = useSessionGuard();
  const { role } = usePermissions();
  const session = useAuthStore((s) => s.sessionId);
  const client = useQueryClient();
  const allowed = role === "super_admin";
  const [name, setName] = useState("");
  const [root, setRoot] = useState("");
  const [path, setPath] = useState("");
  const [node, setNode] = useState("api");
  const [sample, setSample] = useState("");
  const [scope, setScope] = useState<"restricted" | "global">("restricted");
  const [cachePolicy, setCachePolicy] = useState<CachePolicy>("preview");
  const [quotaMiB, setQuotaMiB] = useState("0");
  const [ttl, setTTL] = useState("86400");
  const cacheValues = originalCacheValues(cachePolicy, quotaMiB, ttl);
  const [userIds, setUserIds] = useState<string[]>([]);
  const [editing, setEditing] = useState<StorageLocationOut | null>(null);
  const [editScope, setEditScope] = useState<"restricted" | "global">("restricted");
  const [editUsers, setEditUsers] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [probe, setProbe] = useState<{ name: string; result: StorageProbeOut } | null>(null);
  const roots = useQuery({
    queryKey: ["storage-roots", session],
    queryFn: ({ signal }) => apiClient.get<string[]>("/storage-locations/roots", { signal }),
    enabled: allowed && !locationId,
  });
  const locations = useQuery({
    queryKey: ["filesystem-locations", session, locationId],
    queryFn: async ({ signal }) =>
      locationId
        ? { items: [await datasetSourcesApi.location(locationId, signal)], total: 1 }
        : { items: [], total: 0 },
    enabled: allowed,
  });
  const act = async (work: (current: () => boolean) => Promise<unknown>) => {
    const current = capture();
    setBusy(true);
    setError("");
    try {
      await work(current);
      if (!current()) return;
      await Promise.all([
        client.invalidateQueries({ queryKey: ["filesystem-locations", session] }),
        client.invalidateQueries({ queryKey: ["storage-source-locations"] }),
        client.invalidateQueries({ queryKey: ["storage-location"] }),
        client.invalidateQueries({ queryKey: ["storage-location-probes"] }),
      ]);
    } catch (e) {
      if (current()) setError(verifiedContentError(e));
    } finally {
      if (current()) setBusy(false);
    }
  };
  if (!allowed) return null;
  return (
    <section aria-label="受控目录位置" className="mb-5 rounded-lg border border-border bg-card p-4">
      <h2 className="mb-1 text-sm font-semibold">受控目录位置</h2>
      <p className="mb-3 text-xs text-muted-foreground">
        仅登记部署时已挂载的根内子目录。新增子目录和平台登记无需重启；新增宿主机挂载通常需重建相关容器，无需重建镜像。
      </p>
      {!locationId && (
        <>
          <div className="grid items-start gap-4 sm:grid-cols-2">
            <label className="grid min-w-0 gap-1 text-sm">
              位置名称
              <input
                className={field}
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={100}
              />
            </label>
            <label className="grid min-w-0 gap-1 text-sm">
              受控根
              <select
                className={field}
                aria-label="受控根"
                aria-describedby={rootHint}
                value={root}
                onChange={(e) => setRoot(e.target.value)}
              >
                <option value="" disabled hidden />
                {roots.data?.map((r) => (
                  <option key={r}>{r}</option>
                ))}
              </select>
              <span id={rootHint} className="text-xs leading-relaxed text-muted-foreground">
                选择部署时已挂载的存储根目录。
              </span>
            </label>
            <label className="grid min-w-0 gap-1 text-sm">
              根内相对目录
              <input
                className={field}
                value={path}
                onChange={(e) => setPath(e.target.value)}
                placeholder="留空使用受控根"
              />
            </label>
            <div className="sm:col-span-2">
              <StorageCacheFields
                policy={cachePolicy}
                quotaMiB={quotaMiB}
                ttl={ttl}
                onPolicy={setCachePolicy}
                onQuota={setQuotaMiB}
                onTTL={setTTL}
                disabled={busy}
              />
            </div>
            <Button
              size="sm"
              disabled={!name.trim() || !root || busy || !cacheValues.valid}
              onClick={() =>
                void act(async (current) => {
                  const location = await apiClient.post<StorageLocationOut>("/storage-locations", {
                    name: name.trim(),
                    root_handle: root,
                    relative_path: path,
                    scope,
                    use_user_ids: userIds,
                    cache_policy: cachePolicy,
                    original_cache_quota_bytes: cacheValues.original_cache_quota_bytes,
                    original_cache_ttl_seconds: cacheValues.original_cache_ttl_seconds,
                  });
                  if (current()) {
                    setName("");
                    onCreated?.(location);
                  }
                })
              }
            >
              登记目录位置
            </Button>
          </div>
          <StorageLocationAccessFields
            session={session}
            scope={scope}
            userIds={userIds}
            disabled={busy}
            onScope={setScope}
            onUsers={setUserIds}
          />
          {roots.data?.length === 0 && (
            <p className="mt-2 text-xs text-muted-foreground">
              当前 API 没有可用受控根，请由运维配置根映射与只读挂载。
            </p>
          )}
        </>
      )}
      {locationId && showProbe && (
        <div className="my-3 flex flex-wrap gap-2 text-sm">
          <label className="grid gap-1">
            探测节点
            <input
              className={field}
              value={node}
              onChange={(e) => setNode(e.target.value)}
              placeholder="api 或 worker hostname"
            />
          </label>
          <label className="grid gap-1">
            可选样例文件
            <input
              className={field}
              value={sample}
              onChange={(e) => setSample(e.target.value)}
              placeholder="相对位置路径，用于验证 read"
            />
          </label>
        </div>
      )}
      <ul className="divide-y divide-border text-sm">
        {(storageAccessDenied(locations.error) ? undefined : locations.data)?.items
          .filter((l) => l.kind === "filesystem")
          .map((l) => (
            <li key={l.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
              <span>
                <span className="block font-medium">
                  {l.name}{" "}
                  <span className="ml-2 text-xs font-normal text-muted-foreground">
                    {l.enabled ? "已启用" : "已停用"}
                  </span>
                </span>
                <span className="mt-1 block break-all text-xs text-muted-foreground">
                  {l.root_handle}/{l.relative_path}
                </span>
              </span>
              <Button
                size="sm"
                disabled={busy}
                onClick={() => {
                  setEditing(l);
                  setEditScope(l.scope === "global" ? "global" : "restricted");
                  setEditUsers(l.use_user_ids ?? []);
                }}
              >
                配置使用范围 {l.name}
              </Button>
              {showProbe && (
                <Button
                  size="sm"
                  disabled={busy || !l.enabled || !node}
                  onClick={() =>
                    void act(async (current) => {
                      const result = await datasetSourcesApi.probe(l.id, node, sample || undefined);
                      if (current()) setProbe({ name: l.name, result });
                    })
                  }
                >
                  探测位置 {l.name}
                </Button>
              )}
            </li>
          ))}
      </ul>
      {editing && !storageAccessDenied(locations.error) && (
        <div
          className="mt-3 rounded-md border border-border p-3"
          aria-label={`配置使用范围 ${editing.name}`}
        >
          <h3 className="text-sm font-medium">{editing.name} · 配置使用范围</h3>
          <StorageLocationAccessFields
            key={editing.id}
            session={session}
            scope={editScope}
            userIds={editUsers}
            disabled={busy}
            onScope={setEditScope}
            onUsers={setEditUsers}
          />
          <p className="my-2 text-xs text-muted-foreground">
            保存会更新位置修订，使旧扫描与登记任务停止继续写入；已登记样本和外部原图保留。撤回人员使用权不撤销既有任务的分配权限。
          </p>
          <div className="flex gap-2">
            <StorageLocationChangeButton
              location={editing}
              payload={{
                expected_revision: editing.config_revision,
                scope: editScope,
                use_user_ids: editUsers,
              }}
              label="预览使用范围变更"
              disabled={busy}
              onApplied={() =>
                act(async (current) => {
                  if (current()) setEditing(null);
                })
              }
              onDenied={() => {
                setEditing(null);
                void client.invalidateQueries({ queryKey: ["filesystem-locations", session] });
              }}
            />
            <Button size="sm" disabled={busy} onClick={() => setEditing(null)}>
              取消使用范围修改
            </Button>
          </div>
        </div>
      )}
      {probe && !storageAccessDenied(locations.error) && (
        <p role="status" className="mt-2 text-xs">
          {probe.name} · {probe.result.node_id} · {probe.result.ok ? "探测成功" : probe.result.code}{" "}
          ·{" "}
          {Object.entries(probe.result.capabilities)
            .map(([k, v]) => `${k}: ${v}`)
            .join("，")}
        </p>
      )}
      {(error || (!locationId && roots.error) || locations.error) && (
        <p role="alert" className="mt-2 text-xs text-status-danger">
          {error || verifiedContentError((!locationId && roots.error) || locations.error)}
        </p>
      )}
    </section>
  );
}
