import { useState } from "react";
import type { DatasetSourceOut, SourceRulesImpact } from "@/api/generated/types.gen";
import { datasetSourcesApi as api } from "@/api/datasetSources";
import { Button } from "@/components/ui/Button";
import { useSessionGuard } from "@/hooks/useSessionGuard";
const labels = {
  within_scope: "仍在扫描范围",
  leaving_scope: "将移出扫描范围",
  entering_scope: "将重新纳入范围",
  outside_scope: "仍在扫描范围外",
};
export function DirectorySourceRules({
  source,
  act,
}: {
  source: DatasetSourceOut;
  act: (work: (current: () => boolean) => Promise<unknown>) => Promise<void>;
}) {
  const capture = useSessionGuard();
  const [recursive, setRecursive] = useState(source.recursive);
  const [include, setInclude] = useState([...source.include_globs]);
  const [exclude, setExclude] = useState(
    source.exclude_globs?.length ? [...source.exclude_globs] : [""],
  );
  const [impact, setImpact] = useState<SourceRulesImpact | null>(null);
  const [offset, setOffset] = useState(0);
  const [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState(false);
  const rules = {
    expected_revision: source.config_revision,
    recursive,
    include_globs: include.filter((value) => value.length > 0),
    exclude_globs: exclude.filter((value) => value.length > 0),
  };
  const reset = () => {
    setImpact(null);
    setOffset(0);
    setReviewed(false);
  };
  const run = async (work: (current: () => boolean) => Promise<unknown>) => {
    const current = capture();
    setBusy(true);
    try {
      await act(work);
    } finally {
      if (current()) setBusy(false);
    }
  };
  const preview = (page: number) =>
    void run(async (current) => {
      const result = await api.previewRules(source.id, rules, page);
      if (current()) {
        setImpact(result);
        setOffset(page);
        setReviewed(false);
      }
    });
  if (source.unregistered_at) return null;
  return (
    <section aria-label="扫描规则变更" className="mt-3 rounded-md border border-border p-3">
      <h5 className="text-sm font-semibold">调整扫描规则</h5>
      <p className="mt-1 text-muted-foreground">
        只改变后续扫描范围。保存前核对已接纳路径影响；不会删除样本、任务或标注，已有任务仍严格读取原内容。扩大范围后新增文件数需要重新扫描确定。
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <label>
          <input
            type="checkbox"
            checked={recursive}
            disabled={busy}
            onChange={(e) => {
              setRecursive(e.target.checked);
              reset();
            }}
          />{" "}
          规则包含子目录
        </label>
        <PatternFields
          label="匹配规则"
          values={include}
          onChange={(values) => {
            setInclude(values);
            reset();
          }}
          busy={busy}
        />
        <PatternFields
          label="排除规则"
          values={exclude}
          onChange={(values) => {
            setExclude(values);
            reset();
          }}
          busy={busy}
        />
        <Button size="sm" disabled={busy || !rules.include_globs.length} onClick={() => preview(0)}>
          预览规则影响
        </Button>
      </div>
      {impact && (
        <div role="region" aria-label="扫描规则影响预览" className="mt-2">
          <p>
            仅评估已接纳路径 {impact.total} 条；仍在范围 {impact.counts.within_scope ?? 0}，将移出{" "}
            {impact.counts.leaving_scope ?? 0}，重新纳入 {impact.counts.entering_scope ?? 0}
            ，仍在范围外 {impact.counts.outside_scope ?? 0}。活动尝试 {impact.active_attempt_count}
            。
          </p>
          <table className="mt-1 w-full text-left">
            <thead>
              <tr>
                <th>已接纳相对路径</th>
                <th>规则影响</th>
              </tr>
            </thead>
            <tbody>
              {impact.items.map((i) => (
                <tr key={i.path_digest}>
                  <td className="break-all py-1">{i.relative_path}</td>
                  <td>{labels[i.impact]}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <Button size="sm" disabled={busy || offset === 0} onClick={() => preview(offset - 20)}>
              上一页规则影响
            </Button>
            <span>
              {impact.total ? offset + 1 : 0}–{Math.min(offset + 20, impact.total)} / {impact.total}
            </span>
            <Button
              size="sm"
              disabled={busy || offset + 20 >= impact.total}
              onClick={() => preview(offset + 20)}
            >
              下一页规则影响
            </Button>
          </div>
          <label className="mt-2 flex items-center gap-1">
            <input
              type="checkbox"
              checked={reviewed}
              disabled={busy}
              onChange={(e) => setReviewed(e.target.checked)}
            />{" "}
            已核对影响，保存后重新扫描、预检并重新启用周期计划
          </label>
          <Button
            size="sm"
            className="mt-2"
            disabled={busy || !reviewed || impact.active_attempt_count > 0}
            onClick={() =>
              void run(async (current) => {
                await api.applyRules(source.id, { ...rules, impact_proof: impact.proof });
                if (current()) reset();
              })
            }
          >
            确认保存扫描规则
          </Button>
          {impact.active_attempt_count > 0 && (
            <p className="text-status-caution">请先结束或取消活动扫描、登记，再重新预览。</p>
          )}
        </div>
      )}
    </section>
  );
}

function PatternFields({
  label,
  values,
  onChange,
  busy,
}: {
  label: string;
  values: string[];
  onChange: (values: string[]) => void;
  busy: boolean;
}) {
  return (
    <fieldset className="flex flex-wrap items-center gap-1">
      <legend>{label}（每框一条，保留逗号与空格）</legend>
      {values.map((value, index) => (
        <label key={index} className="flex items-center gap-1">
          <textarea
            aria-label={`新的${label} ${index + 1}`}
            rows={1}
            className="rounded-md border border-input bg-background px-2 py-1"
            value={value}
            disabled={busy}
            onChange={(event) =>
              onChange(values.map((current, i) => (i === index ? event.target.value : current)))
            }
          />
          <Button
            size="sm"
            disabled={busy}
            onClick={() => onChange(values.filter((_, i) => i !== index))}
          >{`移除${label} ${index + 1}`}</Button>
        </label>
      ))}
      <Button
        size="sm"
        disabled={busy || values.length >= 20}
        onClick={() => onChange([...values, ""])}
      >{`新增${label}`}</Button>
    </fieldset>
  );
}
