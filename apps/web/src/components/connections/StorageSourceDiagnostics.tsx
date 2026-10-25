import { Link } from "react-router-dom";
import type { StorageSourceDiagnostics, StorageSourceRunHistory } from "@/api/generated/types.gen";
import { Button } from "@/components/ui/Button";
import { JOB_STATUS_LABEL } from "@/components/jobs/asyncJobPresentation";

export function evidenceDate(value: string | null) {
  return value ? new Date(value).toLocaleString("zh-CN") : "暂无记录";
}

export function sourceHref(
  datasetId: string,
  sourceId: string,
  returnTo?: string,
  scanId?: string,
) {
  return `/datasets/${datasetId}?${new URLSearchParams({
    tab: "sources",
    source: sourceId,
    ...(scanId ? { scan: scanId } : {}),
    ...(returnTo ? { return: returnTo } : {}),
  })}`;
}

const PHASE_LABEL = {
  enumerating: "枚举目录",
  validating: "核对完整性",
  resume_validation: "恢复校验",
  preflight: "内容预检",
  registering: "登记样本",
  unknown: "阶段未知",
};

function RunEvidence({
  label,
  history,
  datasetId,
  sourceId,
  returnTo,
  onJob,
}: {
  label: string;
  history: StorageSourceRunHistory;
  datasetId: string | null;
  sourceId: string;
  returnTo?: string;
  onJob: (id: string, trigger: HTMLElement) => void;
}) {
  const run = history.recent;
  return (
    <div className="min-w-0 space-y-1">
      <h4 className="text-xs text-muted-foreground">{label}</h4>
      {run ? (
        <>
          <p className={run.status === "failed" ? "text-status-danger" : undefined}>
            {run.status === "pending" || run.status === "running" ? "当前活动轮次" : "最近轮次"}
            {" · "}
            {JOB_STATUS_LABEL[run.status]} · {PHASE_LABEL[run.phase]}
          </p>
          <p className="text-xs text-muted-foreground">
            {run.complete
              ? label === "扫描"
                ? "扫描完整"
                : "比较完整"
              : label === "扫描"
                ? "扫描未完整"
                : "比较未完整"}
            {" · "}
            {run.matches_current_config ? "配置匹配" : "历史配置"}
          </p>
          <p className="text-xs text-muted-foreground">
            {evidenceDate(run.completed_at ?? run.created_at)}
          </p>
          <div className="flex flex-wrap items-center gap-2">
            {datasetId && (
              <Link
                className="text-xs text-brand underline"
                to={sourceHref(datasetId, sourceId, returnTo, run.scan_run_id)}
              >
                查看轮次
              </Link>
            )}
            {run.job_id ? (
              <Button
                size="sm"
                variant="ghost"
                onClick={(event) => onJob(run.job_id!, event.currentTarget)}
              >
                {label}任务详情
              </Button>
            ) : (
              <span className="text-xs text-muted-foreground">任务记录未保留</span>
            )}
          </div>
        </>
      ) : (
        <p className="text-muted-foreground">尚无{label}记录</p>
      )}
      {history.last_complete && history.last_complete.run_id !== run?.run_id && (
        <p className="text-xs leading-relaxed text-muted-foreground">
          上次完整成功：{evidenceDate(history.last_complete.completed_at)}
          {history.last_complete.matches_current_config ? " · 配置匹配" : " · 历史配置"}
          {datasetId && (
            <>
              {" "}
              ·{" "}
              <Link
                className="text-brand underline"
                to={sourceHref(datasetId, sourceId, returnTo, history.last_complete.scan_run_id)}
              >
                查看成功轮次
              </Link>
            </>
          )}
        </p>
      )}
    </div>
  );
}

export function SourceDiagnostics({
  value,
  showFaults = false,
  ...navigation
}: {
  value: StorageSourceDiagnostics;
  datasetId: string | null;
  sourceId: string;
  showFaults?: boolean;
  returnTo?: string;
  onJob: (id: string, trigger: HTMLElement) => void;
}) {
  const evidence = value.references;
  const original = evidence.original_counts;
  const preview = evidence.preview_counts;
  const originalProblems =
    original.missing + original.changed + original.preview_only + original.unavailable;
  return (
    <>
      <div className="mt-3 grid gap-4 sm:grid-cols-2">
        <div className="min-w-0 space-y-1">
          <h4 className="text-xs text-muted-foreground">原件最近观测</h4>
          {evidence.total ? (
            <>
              <p>
                已验证 {original.verified} · 待验证 {original.unverified}
              </p>
              {!!originalProblems && (
                <p className="text-status-caution">
                  失踪 {original.missing} · 内容变化 {original.changed} · 不可读{" "}
                  {original.unavailable + original.preview_only}
                </p>
              )}
              <p className="text-xs text-muted-foreground">
                最近记录：{evidenceDate(evidence.observed_through)}
              </p>
            </>
          ) : (
            <p className="text-muted-foreground">尚无原地引用样本</p>
          )}
        </div>
        <div className="min-w-0 space-y-1">
          <h4 className="text-xs text-muted-foreground">预览生成记录</h4>
          {evidence.total ? (
            <>
              <p>
                已有记录 {preview.ready} · 未生成或记录失效 {preview.unrecorded}
              </p>
              {!!preview.failed && <p className="text-status-danger">生成失败 {preview.failed}</p>}
              {!!(preview.not_requested + preview.blocked) && (
                <p className="text-muted-foreground">
                  未启用 {preview.not_requested} · 已停用 {preview.blocked}
                </p>
              )}
              <p className="text-xs text-muted-foreground">
                最近记录：{evidenceDate(evidence.preview_recorded_at)}
              </p>
            </>
          ) : (
            <p className="text-muted-foreground">暂无可统计样本</p>
          )}
        </div>
        <RunEvidence label="扫描" history={value.scan} {...navigation} />
        <RunEvidence label="预检 / 登记" history={value.preflight} {...navigation} />
      </div>
      {showFaults && (
        <p className="mt-3 text-xs">
          <Link
            className="text-brand underline"
            to={`/audit?${new URLSearchParams({
              action: "storage.fault",
              target_type: "storage_location",
              detail_key: "source_id",
              detail_value: navigation.sourceId,
            })}`}
          >
            查看此来源故障记录
          </Link>
        </p>
      )}
      {!!evidence.total && (
        <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
          完整原图缓存记录 {evidence.original_cache_recorded_count ?? 0} 个；最近登记：
          {evidenceDate(evidence.original_cache_recorded_at ?? null)}
          。读取时复核实际副本，不表示来源已恢复。
        </p>
      )}
      {!!evidence.total && (
        <details className="mt-3 text-xs text-muted-foreground">
          <summary className="cursor-pointer">观测范围与数量明细</summary>
          <div className="mt-2 space-y-1 leading-relaxed">
            <p>
              原地引用业务样本 {evidence.total} 个，包含保留的历史修订；不含托管复制和未登记路径。
            </p>
            <p>
              原件观测时间：{evidenceDate(evidence.observed_from)} 至{" "}
              {evidenceDate(evidence.observed_through)}
            </p>
            <p>
              仅预览可用 {original.preview_only} · 来源不可用 {original.unavailable}
            </p>
            <p>已有预览记录不保证对象此刻存在；预览缺失不等于原件故障。原件读取时仍会重新核验。</p>
          </div>
        </details>
      )}
    </>
  );
}
