import type { StorageUsageBreakdown as Breakdown } from "@/api/generated/types.gen";

const LABELS: Record<string, string> = {
  managed_original: "托管原件",
  preview_cache: "预览与缓存",
  training_version: "训练版本文件",
  temporary: "临时产物",
  other_platform: "其他平台对象",
  unclassified: "未归类",
};

function bytes(value: number) {
  if (value === 0) return "0 B";
  const unit = Math.min(4, Math.floor(Math.log(value) / Math.log(1024)));
  return `${(value / 1024 ** unit).toLocaleString("zh-CN", { maximumFractionDigits: 2 })} ${["B", "KiB", "MiB", "GiB", "TiB"][unit]}`;
}

export function StorageUsageBreakdown({ value }: { value?: Breakdown | null }) {
  const measured = value && value.coverage !== "none";
  return (
    <section aria-label="平台对象分类" className="border-t border-border px-4 py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold">对象分类</h3>
        {measured && (
          <span className="text-xs text-muted-foreground">
            分项已测 {value.measured_bucket_count} / {value.bucket_count} 个桶
            {value.has_stale_data && " · 含过期快照"}
          </span>
        )}
      </div>
      {measured ? (
        <>
          <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3 md:grid-cols-3">
            {value.categories.map((item) => (
              <div key={item.category}>
                <dt className="text-xs text-muted-foreground">{LABELS[item.category]}</dt>
                <dd className="mt-1 text-sm font-medium tabular-nums">{bytes(item.size_bytes)}</dd>
                <dd className="mt-1 text-xs text-muted-foreground">
                  {item.object_count.toLocaleString("zh-CN")} 个对象
                </dd>
              </div>
            ))}
          </dl>
          {value.coverage === "partial" && (
            <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
              {value.measured_bucket_count < value.bucket_count
                ? "这些分项仅覆盖已测桶，未测部分未知。"
                : "部分对象缺少一致归属，字节保留在“未归类”中。"}
              {value.conflict_object_count > 0 &&
                ` 含 ${value.conflict_object_count} 个跨分类归属冲突的对象。`}
            </p>
          )}
          <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
            采样窗口：
            {value.measured_from ? new Date(value.measured_from).toLocaleString("zh-CN") : "未知"}
            {" 至 "}
            {value.measured_through
              ? new Date(value.measured_through).toLocaleString("zh-CN")
              : "未知"}
          </p>
        </>
      ) : (
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
          分项尚未统计。重新盘点后按现有归属分类，已有对象总量继续保留。
        </p>
      )}
      <details className="mt-3 text-xs leading-relaxed text-muted-foreground">
        <summary className="cursor-pointer">分类口径</summary>
        <p className="mt-2">
          按已保存的对象地址或归属前缀分类，同一地址只计一次。训练版本文件包含准备中及保留的实际文件；
          到期、退休或业务删除不表示物理字节已消失。分类不证明文件可用，也不提供删除许可。
        </p>
        <p className="mt-2">
          只统计当前配置平台桶的当前对象，不含外部源、历史版本、未完成分片、本地临时目录和磁盘开销。
          未归类字节仍在对象总量中，不能再次相加。
        </p>
      </details>
    </section>
  );
}
