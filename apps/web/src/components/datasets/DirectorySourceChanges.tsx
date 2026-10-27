import { useState } from "react";
import { useSessionGuard } from "@/hooks/useSessionGuard";
import { DirectoryMoveConfirmation } from "./DirectoryMoveConfirmation";
import { DirectoryRevisionConfirmation } from "./DirectoryRevisionConfirmation";
import type {
  DatasetSourceChangeOut,
  DatasetSourceChangeList,
  SourceMoveCandidate,
} from "@/api/generated/types.gen";
import { listStyles } from "@/components/ui/listStyles";
import { Button } from "@/components/ui/Button";
import { verifiedContentError } from "@/hooks/useVerifiedImage";

const labels: Record<string, string> = {
  added: "新增路径",
  unchanged: "内容与观测相同",
  changed: "已变化，保留旧绑定",
  not_seen: "本轮未见",
  excluded: "本轮排除",
  unreadable: "核验失败",
};
const reasons: Record<string, string> = {
  content_changed: "内容摘要不同，不覆盖旧样本",
  observation_changed: "内容相同，文件观测已变化，等待人工重新核验",
  qualification_changed: "来源资格已变化，不重绑旧引用",
  outside_selection_rules: "已接纳路径在当前扫描规则范围外，保留样本和标注",
  not_enumerated: "本轮未枚举到，可能未遍历；不代表删除",
  not_a_candidate: "已观察到该路径，但不属于本轮普通图片候选",
  invalid_image: "图片损坏或无法解码",
  source_changed: "读取期间来源变化，请重新扫描",
  content_mismatch: "与固定摘要不符，请重新扫描",
  permission_denied: "没有读取权限",
  path_unavailable: "读取时路径不可用",
};

export function DirectorySourceChanges({
  complete,
  data,
  pending,
  error,
  kind,
  offset,
  onKind,
  onOffset,
  scanId,
  busy = false,
  actionError = "",
  onAccept,
  registrationId,
  onRelocate,
}: {
  scanId?: string;
  registrationId?: string;
  onRelocate?: (entry: DatasetSourceChangeOut, target: SourceMoveCandidate) => Promise<boolean>;
  busy?: boolean;
  actionError?: string;
  onAccept?: (entry: DatasetSourceChangeOut) => Promise<boolean>;
  complete: boolean;
  data?: DatasetSourceChangeList;
  pending: boolean;
  error: unknown;
  kind: string;
  offset: number;
  onKind: (value: string) => void;
  onOffset: (value: number) => void;
}) {
  const [selected, setSelected] = useState<DatasetSourceChangeOut | null>(null);
  const [moving, setMoving] = useState<DatasetSourceChangeOut | null>(null);
  const capture = useSessionGuard();
  return (
    <section aria-label="本轮增量差异" className="mt-4 border-t border-border pt-4 text-sm">
      <h3 className="text-sm font-semibold">本轮增量差异</h3>
      <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
        对比预检开始时已接纳的样本，内容与观测均核验后才列为相同。本轮未见不代表删除，旧样本、任务和标注保留；使用原图时仍会严格核验。
      </p>
      {!complete ? (
        <p role="status" className="mt-2">
          预检未完成，尚不发布差异与未见候选。
        </p>
      ) : (
        <>
          <dl className="my-4 grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-6">
            {Object.entries(labels).map(([key, label]) => (
              <div key={key} className="rounded-md bg-muted/50 p-3">
                <dt className="text-xs leading-relaxed text-muted-foreground">{label}</dt>
                <dd className="mt-1 text-md font-semibold tabular-nums">
                  {data?.counts[key] ?? "—"}
                </dd>
              </div>
            ))}
          </dl>
          <label>
            差异类型{" "}
            <select
              aria-label="差异类型"
              className="rounded-md border border-input bg-background px-2 py-1"
              value={kind}
              onChange={(e) => onKind(e.target.value)}
            >
              <option value="">全部差异</option>
              {Object.entries(labels).map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          {error ? (
            <p role="alert" className="mt-2 text-status-danger">
              {verifiedContentError(error)}
            </p>
          ) : null}
          <div className="mt-2 overflow-x-auto">
            <table className={listStyles.table}>
              <thead>
                <tr className="text-muted-foreground">
                  <th className="p-2">相对路径</th>
                  <th>差异</th>
                  <th>依据与处理</th>
                </tr>
              </thead>
              <tbody>
                {data?.items.map((entry) => (
                  <tr key={entry.path_digest} className="border-t border-border">
                    <td className="max-w-64 break-words">{entry.relative_path}</td>
                    <td>{labels[entry.kind]}</td>
                    <td>
                      {entry.relocated_to_path
                        ? `已重定位 · 目标 ${entry.relocated_to_path} · 保留样本 ${entry.accepted_item_id}`
                        : entry.accepted_revision_id
                          ? entry.accepted_item_id === entry.before_item_id
                            ? `已重新核验 · 保留样本 ${entry.accepted_item_id}，可重新读取或重开原任务`
                            : `已接受为新修订 · 新样本 ${entry.accepted_item_id}`
                          : entry.reason
                            ? (reasons[entry.reason] ?? entry.reason)
                            : entry.registration_state === "error"
                              ? "本轮登记异常，详见下方结果"
                              : entry.registration_state === "registered"
                                ? "本轮已登记"
                                : entry.registration_state === "unchanged"
                                  ? "已有样本保留"
                                  : entry.kind === "added"
                                    ? "确认后登记为新增样本"
                                    : "保留已有样本与内容绑定"}
                      {entry.can_relocate && onRelocate && (
                        <Button size="sm" disabled={busy} onClick={() => setMoving({ ...entry })}>
                          选择移动目标
                        </Button>
                      )}
                      {(entry.can_accept_revision || entry.can_revalidate) && onAccept && (
                        <Button size="sm" disabled={busy} onClick={() => setSelected({ ...entry })}>
                          {entry.can_revalidate ? "重新核验并保留样本" : "接受为新修订"}
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <Button
              size="sm"
              disabled={pending || !offset}
              onClick={() => onOffset(Math.max(0, offset - 20))}
            >
              上一页差异
            </Button>
            <span>
              {Math.min(offset + 1, data?.total ?? 0)}–{Math.min(offset + 20, data?.total ?? 0)} /{" "}
              {data?.total ?? 0}
            </span>
            <Button
              size="sm"
              disabled={pending || offset + 20 >= (data?.total ?? 0)}
              onClick={() => onOffset(offset + 20)}
            >
              下一页差异
            </Button>
            {pending && <span role="status">正在刷新差异…</span>}
          </div>
        </>
      )}
      {moving && registrationId && scanId && onRelocate && (
        <DirectoryMoveConfirmation
          key={moving.path_digest}
          entry={moving}
          registrationId={registrationId}
          scanId={scanId}
          busy={busy}
          error={actionError}
          onClose={() => setMoving(null)}
          onConfirm={(target) => {
            const current = capture();
            void onRelocate(moving, target).then((accepted) => {
              if (accepted && current()) setMoving(null);
            });
          }}
        />
      )}
      {selected && scanId && onAccept && (
        <DirectoryRevisionConfirmation
          key={selected.path_digest}
          entry={selected}
          scanId={scanId}
          busy={busy}
          error={actionError}
          onClose={() => setSelected(null)}
          onConfirm={() => {
            const current = capture();
            void onAccept(selected).then((accepted) => {
              if (accepted && current()) setSelected(null);
            });
          }}
        />
      )}
    </section>
  );
}
