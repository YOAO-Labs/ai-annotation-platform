import { ListPagination } from "@/components/ui/ListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { useListPagination } from "@/hooks/useListPagination";
import { CopyableError } from "@/components/ui/CopyableError";
/**
 * v0.9.7 · pre_annotated 批次历史表 (含 client-side 搜索 / 排序 / 分页).
 * v0.9.12 · BUG B-16 加 checkbox 多选 + 底部浮窗 + 批量重激活/删除 prediction.
 */

import { Fragment, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import type {
  PreannotateQueueItem,
  BulkClearMode,
  BulkClearResponse,
} from "@/api/adminPreannotate";
import { useBulkPreannotateClear } from "@/hooks/useBulkPreannotateActions";
import { buildWorkbenchUrl, currentWorkbenchReturnTo } from "@/utils/workbenchNavigation";
import styles from "./HistoryTable.module.css";

type SortKey = "last_run_at" | "total_tasks" | "prediction_count" | "failed_count";
type SortDir = "asc" | "desc";

interface Props {
  items: PreannotateQueueItem[];
  isLoading: boolean;
}

export function HistoryTable({ items, isLoading }: Props) {
  const navigate = useNavigate();
  const location = useLocation();
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("last_run_at");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  // v0.9.12 B-16 · 多选 state (按 batch_id 索引, 跨折叠/分页保留)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [confirmMode, setConfirmMode] = useState<BulkClearMode | null>(null);
  const [reasonInput, setReasonInput] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [bulkResult, setBulkResult] = useState<BulkClearResponse | null>(null);
  const bulkClear = useBulkPreannotateClear();

  const filtered = useMemo(() => {
    const s = search.trim().toLowerCase();
    if (!s) return items;
    return items.filter(
      (it) =>
        it.batch_name.toLowerCase().includes(s) ||
        it.project_name.toLowerCase().includes(s) ||
        (it.project_display_id ?? "").toLowerCase().includes(s),
    );
  }, [items, search]);

  const sorted = useMemo(() => {
    const arr = [...filtered];
    arr.sort((a, b) => {
      const av = sortValue(a, sortKey);
      const bv = sortValue(b, sortKey);
      if (av === bv) return 0;
      const cmp = av < bv ? -1 : 1;
      return sortDir === "asc" ? cmp : -cmp;
    });
    return arr;
  }, [filtered, sortKey, sortDir]);

  const pagination = useListPagination(sorted, `${search}|${sortKey}|${sortDir}`);
  const { items: pageItems, setPage } = pagination;

  // B-2 · 项目→batch 分组,折叠展开;按当前页 pageItems 聚合
  const grouped = useMemo(() => {
    const m = new Map<
      string,
      { name: string; displayId: string | null; batches: PreannotateQueueItem[] }
    >();
    for (const it of pageItems) {
      const k = it.project_id;
      const cur = m.get(k);
      if (cur) cur.batches.push(it);
      else
        m.set(k, {
          name: it.project_name,
          displayId: it.project_display_id ?? null,
          batches: [it],
        });
    }
    return Array.from(m.entries()).map(([id, g]) => ({ id, ...g }));
  }, [pageItems]);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const toggleProject = (pid: string) => {
    setCollapsed((s) => {
      const n = new Set(s);
      if (n.has(pid)) n.delete(pid);
      else n.add(pid);
      return n;
    });
  };

  const onSort = (key: SortKey) => {
    setPage(0);
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  };

  const sortIndicator = (key: SortKey) => {
    if (sortKey !== key) return null;
    return <span className={styles.sortIndicator}>{sortDir === "asc" ? "↑" : "↓"}</span>;
  };

  const toggleOne = (bid: string) => {
    setSelectedIds((s) => {
      const n = new Set(s);
      if (n.has(bid)) n.delete(bid);
      else n.add(bid);
      return n;
    });
  };

  const pageBatchIds = pageItems.map((it) => it.batch_id);
  const allOnPageSelected =
    pageBatchIds.length > 0 && pageBatchIds.every((id) => selectedIds.has(id));
  const togglePageAll = () => {
    setSelectedIds((s) => {
      const n = new Set(s);
      if (allOnPageSelected) {
        for (const id of pageBatchIds) n.delete(id);
      } else {
        for (const id of pageBatchIds) n.add(id);
      }
      return n;
    });
  };

  const clearSelection = () => setSelectedIds(new Set());

  const openConfirm = (mode: BulkClearMode) => {
    setConfirmMode(mode);
    setReasonInput("");
    setBulkResult(null);
  };

  const closeConfirm = () => {
    setConfirmMode(null);
    setReasonInput("");
  };

  const submitBulk = async () => {
    if (!confirmMode) return;
    const trimmed = reasonInput.trim();
    if (trimmed.length < 10) return;
    if (submitting) return;
    setSubmitting(true);
    const batchIds = Array.from(selectedIds);
    const res: BulkClearResponse = { succeeded: [], skipped: [], failed: [] };
    try {
      for (let start = 0; start < batchIds.length; start += 50) {
        const chunk = batchIds.slice(start, start + 50);
        try {
          const next = await bulkClear.mutateAsync({
            batch_ids: chunk,
            mode: confirmMode,
            reason: trimmed,
          });
          res.succeeded.push(...next.succeeded);
          res.skipped.push(...next.skipped);
          res.failed.push(...next.failed);
        } catch (error) {
          const reason = error instanceof Error ? error.message : "批量操作失败";
          res.failed.push(...chunk.map((batch_id) => ({ batch_id, reason })));
        }
      }
    } finally {
      setSubmitting(false);
    }
    setBulkResult(res);
    if (res.failed.length === 0 && res.skipped.length === 0) {
      // 全成功 → 直接关闭 + 清选中
      setConfirmMode(null);
      setReasonInput("");
      clearSelection();
    } else {
      // 部分失败 → 保留 modal 展示结果, 但移除已成功项
      setSelectedIds((s) => {
        const n = new Set(s);
        for (const id of res.succeeded) n.delete(id);
        return n;
      });
    }
  };

  return (
    <Card>
      <div className={styles.cardHeader}>
        <span>AI 预标已就绪批次（{filtered.length}）</span>
        <input
          type="text"
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setPage(0);
          }}
          placeholder="搜索批次/项目..."
          className={styles.searchInput}
        />
      </div>
      <div className={styles.cardBody}>
        {isLoading ? (
          <div className={styles.message}>加载中…</div>
        ) : items.length === 0 ? (
          <EmptyState />
        ) : sorted.length === 0 ? (
          <div className={styles.message}>无匹配批次（搜索：{search}）</div>
        ) : (
          <>
            <div className={listStyles.surface}>
              <div className={listStyles.scroll}>
                <table className={listStyles.table}>
                  <thead>
                    <tr className={styles.headerRow}>
                      <th className={`${styles.tableHeaderCell} ${styles.checkboxHeaderCell}`}>
                        <input
                          type="checkbox"
                          aria-label="全选当前页"
                          checked={allOnPageSelected}
                          onChange={togglePageAll}
                        />
                      </th>
                      <th className={styles.tableHeaderCell}>项目</th>
                      <th className={styles.tableHeaderCell}>批次</th>
                      <th
                        className={styles.sortableHeaderCell}
                        onClick={() => onSort("total_tasks")}
                      >
                        总数{sortIndicator("total_tasks")}
                      </th>
                      <th
                        className={styles.sortableHeaderCell}
                        onClick={() => onSort("prediction_count")}
                      >
                        已预标{sortIndicator("prediction_count")}
                      </th>
                      <th
                        className={styles.sortableHeaderCell}
                        onClick={() => onSort("failed_count")}
                      >
                        失败{sortIndicator("failed_count")}
                      </th>
                      <th
                        className={styles.sortableHeaderCell}
                        onClick={() => onSort("last_run_at")}
                      >
                        最近预标{sortIndicator("last_run_at")}
                      </th>
                      <th className={styles.tableHeaderCell}>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {grouped.map((g) => {
                      const isCollapsed = collapsed.has(g.id);
                      const totalBatches = g.batches.length;
                      const totalFailed = g.batches.reduce((s, b) => s + b.failed_count, 0);
                      const totalTasks = g.batches.reduce((s, b) => s + b.total_tasks, 0);
                      return (
                        <Fragment key={g.id}>
                          <tr onClick={() => toggleProject(g.id)} className={styles.groupRow}>
                            <td colSpan={8} className={styles.groupCell}>
                              <span className={styles.groupLabel}>
                                <Icon name={isCollapsed ? "chevRight" : "chevDown"} size={11} />
                                <strong>{g.name}</strong>
                                {g.displayId && (
                                  <span className={styles.subtle}>({g.displayId})</span>
                                )}
                                <span className={styles.mutedText}>
                                  · {totalBatches} 批 · {totalTasks} 任务
                                  {totalFailed > 0 && (
                                    <span className={styles.failedCount}>· {totalFailed} 失败</span>
                                  )}
                                </span>
                              </span>
                            </td>
                          </tr>
                          {!isCollapsed &&
                            g.batches.map((it) => {
                              const isSel = selectedIds.has(it.batch_id);
                              return (
                                <tr
                                  key={it.batch_id}
                                  className={isSel ? styles.selectedRow : undefined}
                                >
                                  <td className={styles.tableCell}>
                                    <input
                                      type="checkbox"
                                      aria-label={`选择 ${it.batch_name}`}
                                      checked={isSel}
                                      onChange={() => toggleOne(it.batch_id)}
                                    />
                                  </td>
                                  <td className={`${styles.tableCell} ${styles.childMarkerCell}`}>
                                    ↳
                                  </td>
                                  <td className={styles.tableCell}>{it.batch_name}</td>
                                  <td className={`${styles.tableCell} ${styles.numeric}`}>
                                    {it.total_tasks}
                                  </td>
                                  <td className={styles.tableCell}>
                                    <Badge variant="ai">{it.prediction_count}</Badge>
                                  </td>
                                  <td className={styles.tableCell}>
                                    {it.failed_count > 0 ? (
                                      <Badge variant="danger">{it.failed_count}</Badge>
                                    ) : (
                                      <span className={styles.subtle}>0</span>
                                    )}
                                  </td>
                                  <td className={`${styles.tableCell} ${styles.mutedCell}`}>
                                    {formatRelative(it.last_run_at)}
                                  </td>
                                  <td className={styles.tableCell}>
                                    <div className={styles.rowActions}>
                                      <Button
                                        size="sm"
                                        variant="ghost"
                                        onClick={() =>
                                          navigate(
                                            buildWorkbenchUrl(it.project_id, {
                                              batchId: it.batch_id,
                                              returnTo: currentWorkbenchReturnTo(location),
                                            }),
                                          )
                                        }
                                        title="打开工作台接管 review"
                                      >
                                        <Icon name="chevRight" size={11} />
                                      </Button>
                                      {it.can_retry && (
                                        <Button
                                          size="sm"
                                          variant="ghost"
                                          onClick={() =>
                                            navigate(`/ai-pre?failed=1&batch_id=${it.batch_id}`)
                                          }
                                          title="到下方失败 prediction 列表重试"
                                        >
                                          <Icon name="refresh" size={11} />
                                        </Button>
                                      )}
                                    </div>
                                  </td>
                                </tr>
                              );
                            })}
                        </Fragment>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          </>
        )}
        <ListPagination
          label="已就绪批次"
          {...pagination}
          loading={isLoading}
          onPageChange={pagination.setPage}
          onPageSizeChange={pagination.setPageSize}
        />
      </div>

      {selectedIds.size > 0 && (
        <BulkActionBar
          count={selectedIds.size}
          onClear={clearSelection}
          onReactivate={() => openConfirm("predictions_only")}
          onReset={() => openConfirm("reset_to_draft")}
        />
      )}

      <Modal
        open={confirmMode !== null}
        onClose={() => {
          if (!submitting) closeConfirm();
        }}
        title={
          confirmMode === "reset_to_draft"
            ? "批量重置 batch 到 draft"
            : "批量删除 prediction (重激活)"
        }
        width={520}
      >
        {bulkResult ? (
          <BulkResultView result={bulkResult} onClose={closeConfirm} />
        ) : (
          <BulkConfirmForm
            mode={confirmMode}
            count={selectedIds.size}
            reason={reasonInput}
            onReasonChange={setReasonInput}
            onCancel={closeConfirm}
            onSubmit={submitBulk}
            isPending={submitting}
            error={bulkClear.error instanceof Error ? bulkClear.error.message : null}
          />
        )}
      </Modal>
    </Card>
  );
}

function BulkActionBar(props: {
  count: number;
  onClear: () => void;
  onReactivate: () => void;
  onReset: () => void;
}) {
  return (
    <div className={styles.bulkActionBar}>
      <div className={styles.bulkActionSummary}>
        <strong>已选 {props.count} 项</strong>
        <Button size="sm" variant="ghost" onClick={props.onClear}>
          清除
        </Button>
      </div>
      <div className={styles.bulkActionButtons}>
        <Button
          size="sm"
          variant="ghost"
          onClick={props.onReactivate}
          title="清空 prediction, batch 回 active"
        >
          <Icon name="refresh" size={11} /> 批量重激活
        </Button>
        <Button
          size="sm"
          variant="danger"
          onClick={props.onReset}
          title="重置 batch 到 draft + 清空 task / prediction / lock"
        >
          <Icon name="trash" size={11} /> 批量重置 draft
        </Button>
      </div>
    </div>
  );
}

function BulkConfirmForm(props: {
  mode: BulkClearMode | null;
  count: number;
  reason: string;
  onReasonChange: (v: string) => void;
  onCancel: () => void;
  onSubmit: () => void;
  isPending: boolean;
  error: string | null;
}) {
  const isDestructive = props.mode === "reset_to_draft";
  const tooShort = props.reason.trim().length < 10;
  return (
    <div className={styles.confirmForm}>
      <p className={styles.confirmCopy}>
        将对 <strong>{props.count}</strong> 个批次执行
        {isDestructive ? (
          <strong className={styles.dangerText}> reset_to_draft</strong>
        ) : (
          <strong className={styles.aiText}> 清空 prediction</strong>
        )}
        操作：
      </p>
      <ul className={styles.confirmList}>
        {isDestructive ? (
          <>
            <li>所有 task 回 pending（保留 annotation 记录）</li>
            <li>清 task_locks / prediction / failed_prediction / prediction_job</li>
            <li>batch.status → draft</li>
          </>
        ) : (
          <>
            <li>清 prediction / failed_prediction / prediction_job</li>
            <li>batch.status: pre_annotated → active（其他状态不变）</li>
            <li>task / annotation / lock 保留</li>
          </>
        )}
      </ul>
      <label className={styles.field}>
        <span className={styles.fieldLabel}>原因（≥10 字，写入 audit log）</span>
        <textarea
          value={props.reason}
          onChange={(e) => props.onReasonChange(e.target.value)}
          rows={3}
          placeholder="例：批次配置错误，需要重新跑一次预标"
          className={styles.textarea}
        />
      </label>
      {props.error && (
        <div className={styles.errorText}>
          <CopyableError message={props.error} />
        </div>
      )}
      <div className={styles.formActions}>
        <Button size="sm" variant="ghost" onClick={props.onCancel} disabled={props.isPending}>
          取消
        </Button>
        <Button
          size="sm"
          variant={isDestructive ? "danger" : "primary"}
          onClick={props.onSubmit}
          disabled={tooShort || props.isPending}
        >
          {props.isPending ? "执行中..." : "确认"}
        </Button>
      </div>
    </div>
  );
}

function BulkResultView(props: { result: BulkClearResponse; onClose: () => void }) {
  const { succeeded, skipped, failed } = props.result;
  return (
    <div data-testid="bulk-result" className={styles.bulkResult}>
      <div>
        <strong>{succeeded.length}</strong> 成功 ·{" "}
        <span className={styles.warningText}>{skipped.length} 跳过</span> ·{" "}
        <span className={styles.dangerText}>{failed.length} 失败</span>
      </div>
      {skipped.length > 0 && (
        <details>
          <summary className={styles.mutedSummary}>跳过详情 ({skipped.length})</summary>
          <ul className={styles.resultListMuted}>
            {skipped.map((it) => (
              <li key={it.batch_id}>
                <code>{it.batch_id.slice(0, 8)}</code> — {it.reason}
              </li>
            ))}
          </ul>
        </details>
      )}
      {failed.length > 0 && (
        <details open>
          <summary className={styles.dangerSummary}>失败详情 ({failed.length})</summary>
          <ul className={styles.resultListDanger}>
            {failed.map((it) => (
              <li key={it.batch_id}>
                <CopyableError message={`${it.batch_id} — ${it.reason}`} />
              </li>
            ))}
          </ul>
        </details>
      )}
      <div className={styles.formActions}>
        <Button size="sm" variant="ghost" onClick={props.onClose}>
          关闭
        </Button>
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className={styles.emptyState}>
      <Icon name="sparkles" size={28} />
      <div className={styles.emptyTitle}>暂无 AI 预标已就绪的批次</div>
      <div className={styles.emptyHint}>在上方跑一次预标，结果会出现在这里。</div>
    </div>
  );
}

function sortValue(it: PreannotateQueueItem, key: SortKey): number | string {
  switch (key) {
    case "total_tasks":
      return it.total_tasks;
    case "prediction_count":
      return it.prediction_count;
    case "failed_count":
      return it.failed_count;
    case "last_run_at":
      return it.last_run_at ? Date.parse(it.last_run_at) : 0;
  }
}

function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const diffMs = Date.now() - d.getTime();
  const sec = Math.round(diffMs / 1000);
  if (sec < 60) return `${sec} 秒前`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const day = Math.round(hr / 24);
  if (day < 30) return `${day} 天前`;
  return d.toLocaleDateString("zh-CN");
}
