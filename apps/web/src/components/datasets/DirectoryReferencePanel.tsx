import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { datasetSourcesApi as api } from "@/api/datasetSources";
import { Button } from "@/components/ui/Button";
import { useAuthStore } from "@/stores/authStore";
import { useVerifiedImage, verifiedContentError } from "@/hooks/useVerifiedImage";
import { DirectoryRegistrationProgress } from "./DirectoryRegistrationProgress";
import { DirectoryCachedPreview, DirectorySourceControls } from "./DirectorySourceControls";
import { parseReference } from "@/utils/referenceContent";

const field = "rounded-md border border-input bg-background px-2 py-1.5 text-xs text-foreground";

export function ReferenceImagePreview({ itemId, sha256 }: { itemId: string; sha256: string }) {
  const image = useVerifiedImage(`/dataset-items/${itemId}/content?sha256=${sha256}`);
  return (
    <div className="mt-2 rounded-md border border-border p-2">
      {image.url ? (
        <img
          src={image.url}
          alt="已核验的来源原图"
          className="max-h-56 max-w-full object-contain"
        />
      ) : (
        <p role="status" className="text-xs text-muted-foreground">
          {image.error ?? "正在核验并读取原图…"}
        </p>
      )}
      <Button size="sm" onClick={image.retry}>
        重读原图
      </Button>
    </div>
  );
}

export function ReferenceItemPreview({ filePath }: { filePath: string }) {
  const [open, setOpen] = useState(false);
  const identity = parseReference(filePath);
  if (!identity) return null;
  return (
    <>
      <Button size="sm" onClick={() => setOpen((v) => !v)}>
        {open ? "关闭原图" : "核验预览"}
      </Button>
      {open && <ReferenceImagePreview itemId={identity.itemId} sha256={identity.sha256} />}
    </>
  );
}

export function DirectoryReferencePanel(props: { datasetId: string }) {
  const revision = useSessionRevision();
  return <DirectoryReferencePanelOwner key={revision} {...props} />;
}

function DirectoryReferencePanelOwner({ datasetId }: { datasetId: string }) {
  const session = useAuthStore((s) => s.sessionId);
  const userId = useAuthStore((s) => s.user?.id);
  const owner = `${userId}/${session}/${datasetId}`;
  const capture = useSessionGuard();
  const client = useQueryClient();
  const [sourceId, setSourceId] = useState("");
  const [locationId, setLocationId] = useState("");
  const [directory, setDirectory] = useState("");
  const [node, setNode] = useState("");
  const [ownership, setOwnership] = useState<"reference" | "managed_copy">("reference");
  const [cache, setCache] = useState<"none" | "preview">("preview");
  const [recursive, setRecursive] = useState(true);
  const [include, setInclude] = useState("*.png,*.jpg,*.jpeg,*.webp,*.PNG,*.JPG,*.JPEG,*.WEBP");
  const [exclude, setExclude] = useState("");
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [browseCursor, setBrowseCursor] = useState<string | undefined>();
  const [browsePath, setBrowsePath] = useState<string | null>(null);
  const sources = useQuery({
    queryKey: ["dataset-sources", owner],
    queryFn: ({ signal }) => api.sources(datasetId, signal),
  });
  const locations = useQuery({
    queryKey: ["storage-locations", owner],
    queryFn: ({ signal }) => api.locations(signal),
  });
  const activeSource = sourceId || sources.data?.items[0]?.id;
  const source = sources.data?.items.find((s) => s.id === activeSource);
  const scans = useQuery({
    queryKey: ["dataset-scans", owner, activeSource],
    queryFn: ({ signal }) => api.scans(activeSource!, signal),
    enabled: !!activeSource,
    refetchInterval: (q) =>
      q.state.data?.items.some((r) => ["pending", "running"].includes(r.status)) ? 2000 : false,
  });
  const run = scans.data?.items[0];
  const entries = useQuery({
    queryKey: ["dataset-scan-entries", owner, run?.id, offset],
    queryFn: ({ signal }) => api.entries(run!.id, offset, signal),
    enabled: !!run?.complete,
  });
  const dirs = useQuery({
    queryKey: ["reference-directories", owner, locationId, browsePath, browseCursor],
    queryFn: ({ signal }) => api.browse(locationId, browsePath!, browseCursor, signal),
    enabled: !!locationId && browsePath !== null,
  });
  const act = async (work: (current: () => boolean) => Promise<unknown>) => {
    const current = capture();
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await work(current);
      if (!current()) return;
      await Promise.all([
        client.invalidateQueries({ queryKey: ["dataset-sources", owner] }),
        client.invalidateQueries({ queryKey: ["dataset-scans", owner] }),
        client.invalidateQueries({ queryKey: ["dataset-scan-entries", owner] }),
        client.invalidateQueries({ queryKey: ["dataset-items", datasetId] }),
        client.invalidateQueries({ queryKey: ["datasets"] }),
      ]);
    } catch (e) {
      if (current()) setError(verifiedContentError(e));
    } finally {
      if (current()) setBusy(false);
    }
  };
  return (
    <section
      aria-label="服务器目录接入"
      className="mb-4 rounded-lg border border-border bg-card p-3 text-xs"
    >
      <h4 className="mb-1 text-sm font-semibold">服务器目录 · 登记与复制</h4>
      <p className="mb-3 text-muted-foreground">
        选择原图保留在来源或复制到平台，预览缓存单独设置和计量。先扫描、预检，再确认登记；已提交结果保留。本机文件夹通过上传接入。
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <label className="grid gap-1">
          存储位置
          <select
            aria-label="原地引用存储位置"
            className={field}
            value={locationId}
            onChange={(e) => {
              setLocationId(e.target.value);
              setBrowsePath(null);
            }}
          >
            <option value="">选择已登记位置</option>
            {locations.data?.items
              .filter((l) => l.kind === "filesystem" && l.enabled)
              .map((l) => (
                <option key={l.id} value={l.id}>
                  {l.name}
                </option>
              ))}
          </select>
        </label>
        <label className="grid gap-1">
          位置下相对目录
          <input
            className={field}
            value={directory}
            onChange={(e) => setDirectory(e.target.value)}
            placeholder="例如 traffic/vehicle"
          />
        </label>
        <Button
          size="sm"
          disabled={!locationId || busy}
          onClick={() => {
            setBrowseCursor(undefined);
            setBrowsePath(directory);
          }}
        >
          浏览目录
        </Button>
        <label className="grid gap-1">
          扫描节点
          <input
            className={field}
            value={node}
            onChange={(e) => setNode(e.target.value)}
            placeholder="worker 的完整 hostname"
          />
        </label>
        <label className="grid gap-1">
          原图策略
          <select
            aria-label="原图策略"
            className={field}
            value={ownership}
            onChange={(e) => setOwnership(e.target.value as typeof ownership)}
          >
            <option value="reference">原地引用（不复制原图）</option>
            <option value="managed_copy">托管复制</option>
          </select>
        </label>
        <label className="grid gap-1">
          预览策略
          <select
            aria-label="预览策略"
            className={field}
            value={cache}
            onChange={(e) => setCache(e.target.value as typeof cache)}
          >
            <option value="preview">预览缓存</option>
            <option value="none">无缓存</option>
          </select>
        </label>
        <label className="flex items-center gap-1 py-1">
          <input
            type="checkbox"
            checked={recursive}
            onChange={(e) => setRecursive(e.target.checked)}
          />
          包含子目录
        </label>
        <label className="grid gap-1">
          图片匹配规则
          <input
            aria-label="图片匹配规则"
            className={field}
            value={include}
            onChange={(e) => setInclude(e.target.value)}
          />
        </label>
        <label className="grid gap-1">
          排除规则
          <input
            aria-label="排除规则"
            className={field}
            value={exclude}
            onChange={(e) => setExclude(e.target.value)}
            placeholder="逗号分隔，例如 temp/*,*.tmp"
          />
        </label>
        <Button
          size="sm"
          disabled={!locationId || !node || busy}
          onClick={() =>
            void act(async (current) => {
              const probe = await api.probe(locationId, node);
              if (!current()) return;
              if (!probe.ok) throw new Error(`扫描节点不可用：${probe.code}`);
              const created = await api.create({
                dataset_id: datasetId,
                location_id: locationId,
                name: directory.split("/").pop()?.slice(0, 100) || "目录根",
                relative_path: directory,
                node_id: node,
                recursive,
                ownership_mode: ownership,
                cache_policy: cache,
                include_globs: include
                  .split(",")
                  .map((s) => s.trim())
                  .filter(Boolean),
                exclude_globs: exclude
                  .split(",")
                  .map((s) => s.trim())
                  .filter(Boolean),
              });
              if (current()) {
                setSourceId(created.id);
                setOffset(0);
                setMessage("来源已配置，请运行只读预扫描");
              }
            })
          }
        >
          配置目录来源
        </Button>
        <a className="text-brand underline" href="/storage">
          配置存储位置
        </a>
      </div>
      {browsePath !== null && (
        <div className="mt-2 flex flex-wrap items-center gap-2 rounded-md bg-muted p-2">
          <span>目录：{browsePath || "根目录"}</span>
          {dirs.data?.entries.map((d) => (
            <Button
              size="sm"
              key={d.relative_path}
              onClick={() => {
                setDirectory(d.relative_path);
                setBrowsePath(d.relative_path);
                setBrowseCursor(undefined);
              }}
            >
              {d.name}
            </Button>
          ))}
          {dirs.data?.next_cursor && (
            <Button size="sm" onClick={() => setBrowseCursor(dirs.data!.next_cursor!)}>
              下一页目录
            </Button>
          )}
          <Button
            size="sm"
            onClick={() => {
              setDirectory("");
              setBrowsePath("");
              setBrowseCursor(undefined);
            }}
          >
            返回位置根
          </Button>
          {dirs.error && <span role="alert">{verifiedContentError(dirs.error)}</span>}
        </div>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-border pt-3">
        <select
          aria-label="已有目录来源"
          className={field}
          value={activeSource ?? ""}
          onChange={(e) => {
            setSourceId(e.target.value);
            setOffset(0);
          }}
        >
          <option value="">选择来源</option>
          {sources.data?.items.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
              {s.unregistered_at ? "（已注销）" : !s.enabled ? "（停用）" : ""}
            </option>
          ))}
        </select>
        <Button
          size="sm"
          disabled={
            !source?.enabled || busy || run?.status === "running" || run?.status === "pending"
          }
          onClick={() =>
            void act(async (current) => {
              const probe = await api.probe(source!.location_id, source!.node_id);
              if (!current()) return;
              if (!probe.ok) throw new Error(`扫描节点不可用：${probe.code}`);
              await api.start(source!.id);
              if (current()) setOffset(0);
            })
          }
        >
          只读预扫描
        </Button>
        {run && (
          <>
            <span role="status">
              {run.status === "completed" ? "扫描完成，尚需确认登记" : `扫描 ${run.status}`} ·{" "}
              {JSON.stringify(run.counts)}
            </span>
            <Button
              size="sm"
              onClick={() => {
                void scans.refetch();
                if (run.complete) void entries.refetch();
              }}
            >
              刷新扫描
            </Button>
            {["failed", "cancelled"].includes(run.status) && (
              <Button size="sm" disabled={busy} onClick={() => void act(() => api.resume(run.id))}>
                恢复扫描
              </Button>
            )}
          </>
        )}
      </div>
      {source && (
        <DirectorySourceControls
          key={`${source.id}/${source.config_revision}`}
          source={source}
          act={act}
        />
      )}
      {run?.complete && source?.enabled && !source.unregistered_at && (
        <DirectoryRegistrationProgress
          key={run.id}
          scanId={run.id}
          datasetId={datasetId}
          owner={owner}
          sourceRevision={source.config_revision}
        />
      )}
      {run?.complete && (
        <div className="mt-2 overflow-x-auto">
          <table className="w-full text-left">
            <thead>
              <tr className="text-muted-foreground">
                <th className="p-2">相对路径</th>
                <th>观测字节</th>
                <th>登记</th>
              </tr>
            </thead>
            <tbody>
              {entries.data?.items.map((entry) => (
                <tr key={entry.path_digest} className="border-t border-border">
                  <td className="p-2">{entry.relative_path}</td>
                  <td>{entry.size_bytes ?? "—"}</td>
                  <td>
                    {entry.registered_item_id ? (
                      <span>
                        已登记 ·{" "}
                        {source?.ownership_mode === "managed_copy" ? "托管复制" : "原地引用"}
                      </span>
                    ) : entry.kind === "file" && entry.state === "observed" ? (
                      <Button
                        size="sm"
                        disabled={busy || !source?.enabled || !!source.unregistered_at}
                        onClick={() =>
                          void act(async (current) => {
                            const sample = [source!.relative_path, entry.relative_path]
                              .filter(Boolean)
                              .join("/");
                            const probe = await api.probe(source!.location_id, "api", sample);
                            if (!current()) return;
                            if (!probe.ok) throw new Error(`API 原图不可读：${probe.code}`);
                            await api.register(run.id, entry.path_digest);
                            if (current())
                              setMessage("已重新核验并登记，可在文件列表预览、关联项目后标注");
                          })
                        }
                      >
                        核验并登记
                      </Button>
                    ) : (
                      entry.state
                    )}
                    {entry.preview_state === "ready" &&
                      source?.enabled &&
                      !source.unregistered_at &&
                      source.cache_policy === "preview" && (
                        <DirectoryCachedPreview
                          key={`${source.id}/${source.config_revision}/${entry.path_digest}`}
                          sourceId={source.id}
                          digest={entry.path_digest}
                        />
                      )}
                    {entry.preview_state === "failed" && (
                      <span className="text-status-caution">预览生成失败；原图登记已保留</span>
                    )}
                    {entry.preview_state === "failed" &&
                      source?.enabled &&
                      !source.unregistered_at && (
                        <Button
                          size="sm"
                          disabled={busy}
                          onClick={() =>
                            void act(async (current) => {
                              const sample = [source.relative_path, entry.relative_path]
                                .filter(Boolean)
                                .join("/");
                              const probe = await api.probe(source.location_id, "api", sample);
                              if (!current()) return;
                              if (!probe.ok) throw new Error(`API 原图不可读：${probe.code}`);
                              await api.register(run.id, entry.path_digest);
                            })
                          }
                        >
                          重新核验并生成预览
                        </Button>
                      )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-2 flex items-center gap-2">
            <Button
              size="sm"
              disabled={!offset}
              onClick={() => setOffset((n) => Math.max(0, n - 20))}
            >
              上一页条目
            </Button>
            <span>
              {offset + 1}–{Math.min(offset + 20, entries.data?.total ?? 0)} /{" "}
              {entries.data?.total ?? 0}
            </span>
            <Button
              size="sm"
              disabled={offset + 20 >= (entries.data?.total ?? 0)}
              onClick={() => setOffset((n) => n + 20)}
            >
              下一页条目
            </Button>
          </div>
        </div>
      )}
      {(error || sources.error || locations.error || scans.error || entries.error) && (
        <p role="alert" className="mt-2 text-status-danger">
          {error ||
            verifiedContentError(sources.error || locations.error || scans.error || entries.error)}
        </p>
      )}
      {message && (
        <p role="status" className="mt-2 text-status-positive">
          {message}
        </p>
      )}
    </section>
  );
}
