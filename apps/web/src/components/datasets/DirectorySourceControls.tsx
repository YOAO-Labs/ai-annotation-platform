import { DirectorySourceRules } from "./DirectorySourceRules";
import { cachePolicyLabels } from "@/utils/cachePolicy";
import { useId, useState } from "react";
import type { DatasetSourceOut, SourceLifecycleImpact } from "@/api/generated/types.gen";
import { datasetSourcesApi as api } from "@/api/datasetSources";
import { Button } from "@/components/ui/Button";
import { useSessionGuard } from "@/hooks/useSessionGuard";
import { useVerifiedImage } from "@/hooks/useVerifiedImage";

export function DirectorySourceControls({
  source,
  act,
}: {
  source: DatasetSourceOut;
  act: (work: (current: () => boolean) => Promise<unknown>) => Promise<void>;
}) {
  const hint = useId();
  const capture = useSessionGuard();
  const [cache, setCache] = useState(source.cache_policy);
  const [impact, setImpact] = useState<SourceLifecycleImpact | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async (work: (current: () => boolean) => Promise<unknown>) => {
    const current = capture();
    setBusy(true);
    try {
      await act(work);
    } finally {
      if (current()) setBusy(false);
    }
  };
  return (
    <div className="mt-4 text-sm">
      <p>
        {source.ownership_mode === "reference" ? "原地引用" : "托管复制"} ·{" "}
        {source.unregistered_at ? "已注销（保留历史）" : source.enabled ? "已启用" : "已停用"}
      </p>
      {!source.unregistered_at && (
        <div className="mt-2 flex flex-wrap items-end gap-2">
          <label className="grid gap-1">
            来源缓存策略
            <select
              aria-label="来源缓存策略"
              aria-describedby={hint}
              className="rounded-md border border-input bg-background px-2 py-1"
              value={cache}
              onChange={(e) => setCache(e.target.value as typeof cache)}
            >
              {Object.entries(cachePolicyLabels).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <Button
            size="sm"
            disabled={busy || cache === source.cache_policy}
            onClick={() =>
              void run(() =>
                api.update(source.id, {
                  expected_revision: source.config_revision,
                  cache_policy: cache,
                }),
              )
            }
          >
            保存缓存策略
          </Button>
          <Button
            size="sm"
            disabled={busy}
            onClick={() =>
              void run(async (current) => {
                const result = await api.impact(source.id);
                if (current()) setImpact(result);
              })
            }
          >
            查看停用/注销影响
          </Button>
        </div>
      )}
      <p id={hint} className="mt-2 text-xs leading-relaxed text-muted-foreground">
        当前生效：{cachePolicyLabels[source.effective_cache_policy ?? source.cache_policy]}
        ；位置上限：{cachePolicyLabels[source.location_cache_policy ?? "preview"]}
        。来源请求不能提高位置额度。变更后重新扫描、预检；旧缓存逐步回收，来源原件和标注保留。
      </p>
      <DirectorySourceRules source={source} act={act} />
      {impact && !source.unregistered_at && (
        <div role="region" aria-label="来源操作影响" className="mt-2 border-t border-border pt-2">
          <p>
            关联样本 {impact.item_count} · 项目 {impact.project_count} · 任务 {impact.task_count} ·
            标注记录 {impact.annotation_count} · 活动尝试 {impact.active_attempt_count}
          </p>
          <p>
            本来源已存预览 {impact.preview_count} 个 / {impact.preview_bytes} 字节，观测于{" "}
            {new Date(impact.measured_at).toLocaleString()}。
          </p>
          <p className="my-1 text-muted-foreground">
            停用/注销终止活动尝试，原地引用停止新读和标注；托管原图继续可用。保留样本、任务和标注，不修改外部文件。注销不可恢复；停用可恢复。
          </p>
          <div className="flex gap-2">
            {([source.enabled ? "pause" : "enable", "unregister"] as const).map((action) => (
              <Button
                key={action}
                size="sm"
                disabled={busy}
                onClick={() =>
                  void run(async (current) => {
                    await api.lifecycle(source.id, {
                      action,
                      expected_revision: impact.expected_revision,
                      impact_proof: impact.proof,
                    });
                    if (current()) setImpact(null);
                  })
                }
              >
                {action === "pause"
                  ? "确认停用来源"
                  : action === "enable"
                    ? "确认恢复来源"
                    : "确认注销来源"}
              </Button>
            ))}
            <Button size="sm" onClick={() => setImpact(null)}>
              关闭影响预览
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

function CachedImage({ sourceId, digest }: { sourceId: string; digest: string }) {
  const image = useVerifiedImage(`/dataset-sources/${sourceId}/entries/${digest}/preview`);
  return (
    <div className="p-2">
      <p className="text-muted-foreground">
        已登记内容的只读缓存预览，不能用于标注或证明原图在线。
      </p>
      {image.url ? (
        <img
          src={image.url}
          alt="目录只读缓存预览"
          className="max-h-40 max-w-full object-contain"
        />
      ) : (
        <p role="status">{image.error ?? "正在读取预览…"}</p>
      )}
      <Button size="sm" onClick={image.retry}>
        重读缓存预览
      </Button>
    </div>
  );
}

export function DirectoryCachedPreview({ sourceId, digest }: { sourceId: string; digest: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button size="sm" onClick={() => setOpen(!open)}>
        {open ? "关闭缓存预览" : "只读缓存预览"}
      </Button>
      {open && <CachedImage sourceId={sourceId} digest={digest} />}
    </>
  );
}
