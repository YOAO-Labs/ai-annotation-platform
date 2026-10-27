import type { TrainingDatasetSampleDetail } from "@/api/trainingDatasets";
import { CopyValueButton } from "@/components/ui/CopyValueButton";

type Props = { lineage: TrainingDatasetSampleDetail["source_lineage"] };

const reasons: Record<string, string> = {
  historical_contract_missing: "历史版本未保存来源证据",
  not_reference: "此成员未保存引用来源证据",
  not_in_package: "此成员未进入保留训练包",
  accepted_revision_unknown: "采纳修订证据不完整",
  scan_time_unknown: "扫描完成时间未保留",
  verification_time_unknown: "内容验证时间未保留",
  managed_source_unrecorded: "此成员的托管原件证据未保留",
};

export function TrainingDatasetSourceLineage({ lineage }: Props) {
  const evidence = lineage?.evidence;
  const managed = lineage?.managed_evidence;
  return (
    <details className="mb-4 rounded-lg border border-border p-3">
      <summary className="cursor-pointer text-sm font-medium">
        冻结来源
        <span className="ml-2 text-xs font-normal text-muted-foreground">
          {lineage?.status === "recorded"
            ? "已记录"
            : lineage?.status === "partial"
              ? "部分证据"
              : "未记录"}
        </span>
      </summary>
      <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
        来自此版本已保存的冻结记录。来源后续变更不会改写这些记录；未保留的历史事实无法确认。本页不查询当前来源是否在线。
      </p>
      {(lineage?.reasons.length || !lineage) && (
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
          {lineage
            ? lineage.reasons.map((reason) => reasons[reason] ?? "部分来源证据未保留").join("；")
            : "历史版本未保存来源证据"}
          。
        </p>
      )}
      {managed && (
        <dl className="mt-3 grid min-w-0 gap-3 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-xs text-muted-foreground">原件类型</dt>
            <dd className="mt-1">
              {managed.owner_kind === "dataset_asset" ? "数据集原件" : "任务自有原件"}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">原图字节数</dt>
            <dd className="mt-1 tabular-nums">{managed.size_bytes.toLocaleString("zh-CN")}</dd>
          </div>
          {[
            ["内容 ID", managed.content_id, "id"],
            ["位置 ID", managed.location_id, "id"],
            ["原图 SHA-256", managed.sha256, "hash"],
          ].map(([label, value, kind]) => (
            <div key={label} className="min-w-0">
              <dt className="text-xs text-muted-foreground">{label}</dt>
              <dd className="mt-1 flex min-w-0 items-center gap-1">
                <span className="min-w-0 truncate font-mono" title={value}>
                  {value}
                </span>
                <CopyValueButton
                  value={value}
                  kind={kind === "hash" ? "hash" : "id"}
                  label={`复制冻结${label}`}
                />
              </dd>
            </div>
          ))}
        </dl>
      )}
      {evidence && (
        <dl className="mt-3 grid min-w-0 gap-3 text-sm sm:grid-cols-2">
          {[
            ["来源 ID", evidence.source_id, "id"],
            ["位置 ID", evidence.location_id, "id"],
            ["路径摘要", evidence.path_digest, "hash"],
            ["扫描轮次 ID", evidence.scan_run_id, "id"],
            ["采纳修订 ID", evidence.accepted_revision_id, "id"],
            ["前一修订 ID", evidence.previous_revision_id, "id"],
          ].map(([label, value, kind]) => (
            <div key={label} className="min-w-0">
              <dt className="text-xs text-muted-foreground">{label}</dt>
              <dd className="mt-1 flex min-w-0 items-center gap-1">
                {value ? (
                  <>
                    <span className="min-w-0 truncate font-mono" title={value}>
                      {value}
                    </span>
                    <CopyValueButton
                      value={value}
                      kind={kind === "hash" ? "hash" : "id"}
                      label={`复制冻结${label}`}
                    />
                  </>
                ) : label === "前一修订 ID" && evidence.accepted_revision_id ? (
                  "无前一修订"
                ) : (
                  "未知"
                )}
              </dd>
            </div>
          ))}
          <div>
            <dt className="text-xs text-muted-foreground">采纳修订号</dt>
            <dd className="mt-1 tabular-nums">{evidence.revision_number ?? "未知"}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">冻结时配置修订</dt>
            <dd className="mt-1 tabular-nums">
              来源 {evidence.source_config_revision} · 位置 {evidence.location_config_revision}
            </dd>
          </div>
          {[
            ["采纳时间", evidence.accepted_at],
            ["扫描完成时间", evidence.scan_completed_at],
            ["内容验证时间", evidence.content_verified_at],
          ].map(([label, time]) => (
            <div key={label}>
              <dt className="text-xs text-muted-foreground">{label}</dt>
              <dd className="mt-1">{time ? new Date(time).toLocaleString("zh-CN") : "未知"}</dd>
            </div>
          ))}
        </dl>
      )}
    </details>
  );
}
