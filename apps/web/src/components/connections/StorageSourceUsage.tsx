import type { StorageSourceUsage as Usage } from "@/api/generated/types.gen";
import { evidenceDate } from "./StorageSourceDiagnostics";

function bytes(value: number) {
  if (value === 0) return "0 B";
  const unit = Math.min(4, Math.floor(Math.log(value) / Math.log(1024)));
  return `${(value / 1024 ** unit).toLocaleString("zh-CN", { maximumFractionDigits: 2 })} ${["B", "KiB", "MiB", "GiB", "TiB"][unit]}`;
}

export function StorageSourceUsage({ value }: { value?: Usage }) {
  const total = value?.coverage === "complete" ? value.total_size_bytes : null;
  const complete = total != null;
  const observed =
    value && (value.observed_from || value.observed_through)
      ? `${evidenceDate(value.observed_from)} 至 ${evidenceDate(value.observed_through)}`
      : "暂无记录";
  return (
    <section aria-label="当前已采纳源索引规模" className="rounded-md border border-border p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold">当前已采纳源索引规模</h3>
        <p className="text-sm font-medium tabular-nums">{complete ? bytes(total) : "总量未知"}</p>
      </div>
      {value ? (
        <>
          {value.coverage === "none" && value.reference_count === 0 ? (
            <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
              此位置尚无可用的来源规模统计。
            </p>
          ) : (
            <p className="mt-2 text-sm tabular-nums">
              {complete ? "去重后" : "已确认部分"} {value.file_count.toLocaleString("zh-CN")} 个文件
              {!complete && ` · ${bytes(value.known_size_bytes)}`}
              {` · ${value.reference_count.toLocaleString("zh-CN")} 条采纳记录`}
              {value.duplicate_reference_count > 0 &&
                ` · 合并 ${value.duplicate_reference_count.toLocaleString("zh-CN")} 条重复引用`}
            </p>
          )}
          {value.unknown_reference_count > 0 && (
            <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
              {value.unknown_reference_count} 条记录缺少一致的定位或内容证据，未计入已确认部分。
              {value.conflict_file_count > 0 && ` ${value.conflict_file_count} 组同址内容冲突。`}
              {value.incomparable_file_count > 0 &&
                ` ${value.incomparable_file_count} 组挂载或节点不可比较。`}
            </p>
          )}
          {value.inactive_reference_count > 0 && (
            <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
              含暂停或注销范围的 {value.inactive_reference_count} 条保留记录。
            </p>
          )}
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
            索引观测：{observed}
            <br />
            计算时间：{evidenceDate(value.measured_at)}
          </p>
        </>
      ) : (
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground">尚无可用统计。</p>
      )}
      <details className="mt-3 text-xs leading-relaxed text-muted-foreground">
        <summary className="cursor-pointer">统计口径</summary>
        <p className="mt-2">
          统计当前已采纳的来源文件，原地引用和复制接入均包含；按本位置的冻结地址去重。
          这是来源文件的逻辑规模，不能与平台对象字节相加，也不代表原件现在可读或磁盘占用。
        </p>
        <p className="mt-2">
          历史修订、未采纳扫描和缓存不在范围内。不同位置、硬链接及无法比较的挂载不作全局去重。
          {value?.includes_versions && "已采纳的不同 S3 对象版本分别计量。"}
        </p>
      </details>
    </section>
  );
}
