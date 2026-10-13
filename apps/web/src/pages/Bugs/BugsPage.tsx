import { FilterGroup, FilterSelect } from "@/components/filters/FilterControls";
import { lazy, Suspense, useState, useEffect, useMemo, useRef } from "react";
import { bugReportsApi, type BugReportDetail } from "@/api/bug-reports";
import { useToastStore } from "@/components/ui/Toast";
import { Icon } from "@/components/ui/Icon";
import { MarkdownBlock } from "@/components/bugreport/MarkdownBlock";
import type { MarkdownEditorProps } from "@/components/markdown/MarkdownEditor";
import { useUrlFilterState } from "@/hooks/useUrlFilterState";
import { useBugReports } from "@/hooks/useBugReports";
import { isCurrentAuthOwner, useAuthStore } from "@/stores/authStore";
import { BUGS_URL_DEFAULTS, BUGS_URL_KEYS, bugsUrlCodec } from "./bugsUrlState";
import styles from "./BugsPage.module.css";

const STATUS_OPTIONS = ["new", "triaged", "in_progress", "fixed", "wont_fix", "duplicate"];
const SEVERITY_OPTIONS = ["low", "medium", "high", "critical"];
const MAX_COMMENT_LENGTH = 10_000;

type CompactMarkdownEditorProps = Pick<
  MarkdownEditorProps,
  | "value"
  | "onChange"
  | "onSubmit"
  | "placeholder"
  | "documentId"
  | "label"
  | "variant"
  | "disabled"
>;

// 管理评论使用紧凑编辑器，延迟加载编辑器依赖以保持管理页首屏轻量。
const MarkdownEditor = lazy(() =>
  import("@/components/markdown/MarkdownEditor").then((m) => ({
    default: m.MarkdownEditor,
  })),
);

const codePointLength = (value: string) => Array.from(value).length;

function CompactMarkdownEditor(props: CompactMarkdownEditorProps) {
  return (
    <Suspense fallback={<div className={styles.commentInput}>编辑器加载中…</div>}>
      <MarkdownEditor {...props} />
    </Suspense>
  );
}

const statusLabel: Record<string, string> = {
  new: "新提交",
  triaged: "已确认",
  in_progress: "处理中",
  fixed: "已修复",
  wont_fix: "不修复",
  duplicate: "重复",
};

const severityClass: Record<string, string> = {
  low: styles.severityLow,
  medium: styles.severityMedium,
  high: styles.severityHigh,
  critical: styles.severityCritical,
};

interface AuthOwnerSnapshot {
  userId: string | null;
  token: string | null;
}

function captureAuthOwner(): AuthOwnerSnapshot {
  const current = useAuthStore.getState();
  return { userId: current.user?.id ?? null, token: current.token };
}

function isCurrentOwner(snapshot: AuthOwnerSnapshot): boolean {
  return Boolean(
    snapshot.userId &&
    snapshot.token &&
    snapshot.token === useAuthStore.getState().token &&
    isCurrentAuthOwner(snapshot.userId),
  );
}

export function BugsPage() {
  const urlState = useUrlFilterState({
    codec: bugsUrlCodec,
    defaults: BUGS_URL_DEFAULTS,
    ownedKeys: BUGS_URL_KEYS,
  });
  const { state: filters, issues, patch } = urlState;
  const authOwnerId = useAuthStore((state) => state.user?.id ?? null);
  const authToken = useAuthStore((state) => state.token);
  const listParams = useMemo(
    () => ({
      status: filters.status || undefined,
      severity: filters.severity || undefined,
      limit: 50,
    }),
    [filters.severity, filters.status],
  );
  const listQuery = useBugReports(listParams);
  const items = listQuery.data?.items ?? [];
  const total = listQuery.data?.total ?? 0;
  const loading = listQuery.isLoading;
  const [detailId, setDetailId] = useState<string | null>(null);
  const [detail, setDetail] = useState<BugReportDetail | null>(null);
  const [commentText, setCommentText] = useState("");
  const [postingComment, setPostingComment] = useState(false);
  const detailRequestRef = useRef(0);
  const activeDetailIdRef = useRef<string | null>(null);
  const commentRequestRef = useRef(0);
  const authScopeRef = useRef<AuthOwnerSnapshot>({ userId: authOwnerId, token: authToken });
  const pushToast = useToastStore((s) => s.push);

  useEffect(() => {
    const previous = authScopeRef.current;
    if (previous.userId === authOwnerId && previous.token === authToken) return;
    authScopeRef.current = { userId: authOwnerId, token: authToken };
    detailRequestRef.current += 1;
    commentRequestRef.current += 1;
    activeDetailIdRef.current = null;
    setDetailId(null);
    setDetail(null);
    setCommentText("");
    setPostingComment(false);
  }, [authOwnerId, authToken]);

  useEffect(() => {
    if (listQuery.error) pushToast({ msg: "加载失败", kind: "error" });
  }, [listQuery.error, pushToast]);

  const loadDetail = async (id: string) => {
    const owner = captureAuthOwner();
    if (!isCurrentOwner(owner)) return;
    const requestId = ++detailRequestRef.current;
    commentRequestRef.current += 1;
    if (activeDetailIdRef.current !== id) setCommentText("");
    activeDetailIdRef.current = id;
    setDetailId(id);
    setDetail(null);
    setPostingComment(false);
    try {
      const data = await bugReportsApi.get(id);
      if (
        !isCurrentOwner(owner) ||
        requestId !== detailRequestRef.current ||
        activeDetailIdRef.current !== id
      )
        return;
      setDetail(data);
    } catch {
      if (
        isCurrentOwner(owner) &&
        requestId === detailRequestRef.current &&
        activeDetailIdRef.current === id
      ) {
        pushToast({ msg: "加载详情失败", kind: "error" });
      }
    }
  };

  const updateStatus = async (id: string, status: string) => {
    const owner = captureAuthOwner();
    if (!isCurrentOwner(owner)) return;
    const detailScope = detailRequestRef.current;
    try {
      await bugReportsApi.update(id, { status });
      if (!isCurrentOwner(owner)) return;
      pushToast({ msg: "状态已更新", kind: "success" });
      void listQuery.refetch();
      if (activeDetailIdRef.current === id && detailRequestRef.current === detailScope) {
        void loadDetail(id);
      }
    } catch {
      if (!isCurrentOwner(owner)) return;
      pushToast({ msg: "更新失败", kind: "error" });
    }
  };

  const addComment = async (submittedText = commentText) => {
    const owner = captureAuthOwner();
    if (!isCurrentOwner(owner) || !detailId || !submittedText.trim() || postingComment) return;
    const body = submittedText.trim();
    if (codePointLength(body) > MAX_COMMENT_LENGTH) {
      pushToast({ msg: `评论不能超过 ${MAX_COMMENT_LENGTH} 个字符`, kind: "error" });
      return;
    }
    const reportId = detailId;
    const detailScope = detailRequestRef.current;
    const requestId = ++commentRequestRef.current;
    setPostingComment(true);
    try {
      await bugReportsApi.addComment(reportId, body);
      if (
        !isCurrentOwner(owner) ||
        requestId !== commentRequestRef.current ||
        detailScope !== detailRequestRef.current ||
        activeDetailIdRef.current !== reportId
      ) {
        return;
      }
      setCommentText("");
      void loadDetail(reportId);
    } catch {
      if (
        isCurrentOwner(owner) &&
        requestId === commentRequestRef.current &&
        detailScope === detailRequestRef.current &&
        activeDetailIdRef.current === reportId
      ) {
        pushToast({ msg: "评论失败", kind: "error" });
      }
    } finally {
      if (isCurrentOwner(owner) && requestId === commentRequestRef.current) {
        setPostingComment(false);
      }
    }
  };

  const invalidateDetailScope = () => {
    detailRequestRef.current += 1;
    commentRequestRef.current += 1;
    activeDetailIdRef.current = null;
    setPostingComment(false);
  };

  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Bug 反馈管理</h1>

      {/* Filters */}
      <FilterGroup label="筛选" hideLabel className={styles.filters}>
        <FilterSelect
          aria-label="问题状态"
          value={filters.status}
          onChange={(e) => patch({ status: e.target.value }, { replace: false })}
        >
          <option value="">全部状态</option>
          {STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {statusLabel[s]}
            </option>
          ))}
        </FilterSelect>
        <FilterSelect
          aria-label="严重度"
          value={filters.severity}
          onChange={(e) => patch({ severity: e.target.value }, { replace: false })}
        >
          <option value="">全部严重度</option>
          {SEVERITY_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </FilterSelect>
        <span className={styles.totalText}>共 {total} 条</span>
      </FilterGroup>
      {!!issues.length && (
        <div role="alert" className="mb-3 text-xs text-status-caution">
          URL BUG 筛选无法完整恢复，已使用安全默认值。
        </div>
      )}

      {/* List */}
      <div className={detailId ? styles.layoutWithDetail : styles.layout}>
        <div>
          {loading && <div className={styles.emptyState}>加载中...</div>}
          {!loading && items.length === 0 && <div className={styles.emptyState}>暂无反馈</div>}
          <table className={styles.table}>
            <thead>
              <tr className={styles.headerRow}>
                <th className={styles.th}>ID</th>
                <th className={styles.th}>标题</th>
                <th className={styles.th}>严重度</th>
                <th className={styles.th}>状态</th>
                <th className={styles.th}>时间</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr
                  key={item.id}
                  onClick={() => loadDetail(item.id)}
                  className={
                    detailId === item.id
                      ? `${styles.itemRow} ${styles.itemRowSelected}`
                      : styles.itemRow
                  }
                >
                  <td className={styles.idCell}>{item.display_id}</td>
                  <td className={styles.titleCell}>
                    {item.title.length > 40 ? item.title.slice(0, 40) + "..." : item.title}
                  </td>
                  <td className={styles.td}>
                    <span className={`${styles.severity} ${severityClass[item.severity] ?? ""}`}>
                      {item.severity}
                    </span>
                  </td>
                  <td className={styles.td}>
                    {statusLabel[item.status] ?? item.status}
                    {item.reopen_count > 0 && (
                      <span
                        title={
                          item.last_reopened_at
                            ? `最近重开：${new Date(item.last_reopened_at).toLocaleString("zh-CN")}`
                            : undefined
                        }
                        className={styles.reopenPill}
                      >
                        ↻{item.reopen_count}
                      </span>
                    )}
                  </td>
                  <td className={styles.dateCell}>
                    {new Date(item.created_at).toLocaleDateString("zh-CN")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Detail panel */}
        {detailId && detail && (
          <div className={styles.detailPanel}>
            <div className={styles.detailHeader}>
              <h2 className={styles.detailTitle}>
                {detail.display_id}: {detail.title}
              </h2>
              <button
                onClick={() => {
                  invalidateDetailScope();
                  setDetailId(null);
                  setDetail(null);
                }}
                className={styles.closeButton}
              >
                <Icon name="x" size={14} />
              </button>
            </div>

            <div className={styles.metaRow}>
              <span className={styles.metaText}>
                路由：<code className={styles.routeCode}>{detail.route}</code>
              </span>
              <span className={styles.metaText}>角色：{detail.user_role}</span>
              {detail.viewport && <span className={styles.metaText}>{detail.viewport}</span>}
              {detail.reopen_count > 0 && (
                <span
                  title={
                    detail.last_reopened_at
                      ? `最近重开：${new Date(detail.last_reopened_at).toLocaleString("zh-CN")}`
                      : undefined
                  }
                  className={styles.reopenPillLarge}
                >
                  曾重开 {detail.reopen_count} 次
                </span>
              )}
            </div>
            <div className={styles.section}>
              <MarkdownBlock>{detail.description}</MarkdownBlock>
            </div>

            {detail.attachments?.length > 0 && (
              <div className={styles.section}>
                <div className={styles.sectionTitle}>截图附件 ({detail.attachments.length})</div>
                <div className={styles.attachments}>
                  {detail.attachments.map((att) => (
                    <a
                      key={att.storageKey}
                      href={bugReportsApi.attachmentDownloadUrl(detail.id, att.storageKey)}
                      target="_blank"
                      rel="noopener noreferrer"
                      className={styles.attachmentLink}
                    >
                      <Icon name="image" size={13} />
                      <span className={styles.attachmentName}>{att.fileName}</span>
                      <span className={styles.attachmentSize}>
                        {Math.round(att.size / 1024)} KB
                      </span>
                    </a>
                  ))}
                </div>
              </div>
            )}

            {detail.resolution && (
              <div className={styles.resolution}>
                <span className={styles.mediumText}>处理结果：</span>
                {detail.resolution}
              </div>
            )}

            {/* Status actions */}
            <div className={styles.statusActions}>
              {STATUS_OPTIONS.map((s) => (
                <button
                  key={s}
                  onClick={() => updateStatus(detail.id, s)}
                  disabled={detail.status === s}
                  className={
                    detail.status === s
                      ? `${styles.statusButton} ${styles.statusButtonActive}`
                      : styles.statusButton
                  }
                >
                  {statusLabel[s]}
                </button>
              ))}
            </div>

            {/* Comments */}
            <div className={styles.commentsSection}>
              <div className={styles.sectionTitle}>评论 ({detail.comments?.length ?? 0})</div>
              {detail.comments?.map((c) => (
                <div key={c.id} className={styles.comment}>
                  <div className={styles.commentMeta}>
                    <span className={styles.commentAuthor}>{c.author_name || "未知"}</span>
                    {c.author_role && <span className={styles.rolePill}>{c.author_role}</span>}
                    <span className={styles.commentTime}>
                      {new Date(c.created_at).toLocaleString("zh-CN")}
                    </span>
                  </div>
                  <MarkdownBlock compact>{c.body}</MarkdownBlock>
                </div>
              ))}
            </div>
            <div className={styles.commentForm}>
              <div>
                <CompactMarkdownEditor
                  value={commentText}
                  onChange={setCommentText}
                  onSubmit={(next) => void addComment(next)}
                  placeholder="添加评论，支持 Markdown..."
                  documentId={`bug-comment-${detail.id}`}
                  label="反馈评论"
                  variant="compact"
                  disabled={postingComment}
                />
              </div>
              <button
                onClick={() => void addComment()}
                disabled={postingComment || !commentText.trim()}
                className={
                  commentText.trim()
                    ? styles.sendButton
                    : `${styles.sendButton} ${styles.sendButtonDisabled}`
                }
              >
                发送
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
