import { lazy, Suspense, useState, useEffect, useRef, type ClipboardEvent } from "react";
import { Icon } from "@/components/ui/Icon";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { useToastStore } from "@/components/ui/Toast";
import {
  bugReportsApi,
  uploadBugAttachment,
  type BugAttachment,
  type BugReportResponse,
  type BugReportDetail,
} from "@/api/bug-reports";
import {
  getRecentApiCalls,
  getRecentConsoleErrors,
  sanitizeApiCalls,
  captureScreenshot,
} from "@/utils/bugReportCapture";
import {
  appendVideoWorkbenchDiagnostics,
  getVideoWorkbenchDiagnosticsSnapshot,
  taskIdFromVideoWorkbenchDiagnostics,
  videoWorkbenchDiagnosticsConsoleEntry,
} from "@/utils/videoWorkbenchDiagnostics";
import {
  appendRasterMaskComputeDiagnostics,
  getRasterMaskComputeDiagnosticsSnapshot,
  rasterMaskComputeDiagnosticsConsoleEntry,
} from "@/utils/rasterMaskComputeDiagnostics";
import {
  appendImageTileDiagnostics,
  getImageTileDiagnosticsSnapshot,
  imageTileDiagnosticsConsoleEntry,
} from "@/utils/imageTileDiagnostics";
import {
  appendRasterResourceDiagnostics,
  getRasterResourceDiagnosticsSnapshot,
  rasterResourceDiagnosticsConsoleEntry,
} from "@/utils/rasterResourceDiagnostics";
import { readWorkbenchPerfSnapshot } from "@/pages/Workbench/stage/shared/useWorkbenchPerf";
import { ScreenshotEditor } from "./ScreenshotEditor";
import { MarkdownBlock } from "./MarkdownBlock";
import type { MarkdownEditorProps } from "@/components/markdown/MarkdownEditor";
import styles from "./BugReportDrawer.module.css";

interface Props {
  open: boolean;
  onClose: () => void;
  focusBugId?: string | null;
}

type ViewState = "list" | "create" | "detail" | "edit";

const MAX_ATTACHMENTS = 5;
const MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024;
const ALLOWED_ATTACHMENT_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);
const MAX_DESCRIPTION_LENGTH = 20_000;
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

// BUG 表单使用紧凑编辑器，保持 MDXEditor 不进入抽屉首屏 bundle。
const MarkdownEditor = lazy(() =>
  import("@/components/markdown/MarkdownEditor").then((m) => ({
    default: m.MarkdownEditor,
  })),
);

const codePointLength = (value: string) => Array.from(value).length;

function CompactMarkdownEditor(props: CompactMarkdownEditorProps) {
  return (
    <Suspense fallback={<div className={styles.field}>编辑器加载中…</div>}>
      <MarkdownEditor {...props} />
    </Suspense>
  );
}

const cx = (...classNames: Array<string | false | null | undefined>) =>
  classNames.filter(Boolean).join(" ");

const severityClassName: Record<string, string> = {
  low: styles.severityLow,
  medium: styles.severityMedium,
  high: styles.severityHigh,
  critical: styles.severityCritical,
};

interface PendingAttachment {
  id: string;
  blob: Blob;
  fileName: string;
  mimeType: string;
}

export function BugReportDrawer({ open, onClose, focusBugId = null }: Props) {
  const [view, setView] = useState<ViewState>("list");
  const [reports, setReports] = useState<BugReportResponse[]>([]);
  const [detail, setDetail] = useState<BugReportDetail | null>(null);
  const [loading, setLoading] = useState(false);

  // create/edit form
  const [title, setTitle] = useState("");
  const [desc, setDesc] = useState("");
  const [severity, setSeverity] = useState<string>("medium");
  const [submitting, setSubmitting] = useState(false);
  const [editId, setEditId] = useState<string | null>(null);

  // detail-view comment composer
  const [commentBody, setCommentBody] = useState("");
  const [postingComment, setPostingComment] = useState(false);
  const detailRequestRef = useRef(0);
  const activeDetailIdRef = useRef<string | null>(null);
  const commentRequestRef = useRef(0);

  // v0.6.6 · 截图状态
  const [screenshotBlob, setScreenshotBlob] = useState<Blob | null>(null);
  const [screenshotEditing, setScreenshotEditing] = useState(false);
  const [pendingAttachments, setPendingAttachments] = useState<PendingAttachment[]>([]);
  // v0.7.0：上传失败 retry 状态
  const [screenshotUploadFail, setScreenshotUploadFail] = useState<string | null>(null);

  const pushToast = useToastStore((s) => s.push);

  useEffect(() => {
    if (open && view === "list") {
      loadMine();
    }
  }, [open, view]);

  // v0.7.0：从通知中心点击「我的反馈」类通知跳转时，自动定位到该条详情
  useEffect(() => {
    if (open && focusBugId) {
      loadDetail(focusBugId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, focusBugId]);

  const loadMine = async () => {
    setLoading(true);
    try {
      const data = await bugReportsApi.listMine(20);
      setReports(data.items);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  };

  const invalidateDetailScope = () => {
    detailRequestRef.current += 1;
    commentRequestRef.current += 1;
    activeDetailIdRef.current = null;
    setPostingComment(false);
  };

  const loadDetail = async (id: string) => {
    const requestId = ++detailRequestRef.current;
    commentRequestRef.current += 1;
    if (activeDetailIdRef.current !== id) setCommentBody("");
    activeDetailIdRef.current = id;
    setPostingComment(false);
    setLoading(true);
    setDetail(null);
    try {
      const data = await bugReportsApi.get(id);
      if (requestId !== detailRequestRef.current || activeDetailIdRef.current !== id) return;
      setDetail(data);
      setView("detail");
    } catch {
      if (requestId === detailRequestRef.current && activeDetailIdRef.current === id) {
        pushToast({ msg: "加载失败", kind: "error" });
      }
    } finally {
      if (requestId === detailRequestRef.current && activeDetailIdRef.current === id) {
        setLoading(false);
      }
    }
  };

  const addPendingAttachment = (blob: Blob, fileName: string) => {
    const mimeType = blob.type || "image/png";
    if (!ALLOWED_ATTACHMENT_TYPES.has(mimeType)) {
      pushToast({ msg: "仅支持 PNG / JPEG / WebP 截图", kind: "error" });
      return false;
    }
    if (blob.size > MAX_ATTACHMENT_SIZE) {
      pushToast({ msg: "截图超过 10MB", kind: "error" });
      return false;
    }
    if (pendingAttachments.length >= MAX_ATTACHMENTS) {
      pushToast({ msg: `最多上传 ${MAX_ATTACHMENTS} 张截图`, kind: "error" });
      return false;
    }
    setPendingAttachments((items) => [
      ...items,
      {
        id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
        blob,
        fileName,
        mimeType,
      },
    ]);
    setScreenshotUploadFail(null);
    return true;
  };

  const handlePasteImage = (e: ClipboardEvent) => {
    if (view !== "create") return;
    const files = Array.from(e.clipboardData.files).filter((file) =>
      file.type.startsWith("image/"),
    );
    if (files.length === 0) return;
    e.preventDefault();
    e.stopPropagation();
    const nextAttachments: PendingAttachment[] = [];
    let nextCount = pendingAttachments.length;
    for (const file of files) {
      const mimeType = file.type || "image/png";
      if (!ALLOWED_ATTACHMENT_TYPES.has(mimeType)) {
        pushToast({ msg: "仅支持 PNG / JPEG / WebP 截图", kind: "error" });
        continue;
      }
      if (file.size > MAX_ATTACHMENT_SIZE) {
        pushToast({ msg: "截图超过 10MB", kind: "error" });
        continue;
      }
      if (nextCount >= MAX_ATTACHMENTS) {
        pushToast({ msg: `最多上传 ${MAX_ATTACHMENTS} 张截图`, kind: "error" });
        break;
      }
      const ext = file.type === "image/jpeg" ? "jpg" : file.type === "image/webp" ? "webp" : "png";
      nextAttachments.push({
        id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
        blob: file,
        fileName: file.name || `clipboard-${Date.now()}-${nextAttachments.length + 1}.${ext}`,
        mimeType,
      });
      nextCount += 1;
    }
    if (nextAttachments.length > 0) {
      setPendingAttachments((items) => [...items, ...nextAttachments]);
      setScreenshotUploadFail(null);
      pushToast({ msg: `已添加 ${nextAttachments.length} 张截图`, kind: "success" });
    }
  };

  const handleSubmit = async (skipScreenshot = false) => {
    const trimmedTitle = title.trim();
    const trimmedDescription = desc.trim();
    if (!trimmedTitle || !trimmedDescription) return;

    const videoDiagnostics = getVideoWorkbenchDiagnosticsSnapshot();
    const videoDiagnosticsEntry = videoWorkbenchDiagnosticsConsoleEntry(videoDiagnostics);
    const rasterMaskDiagnostics = getRasterMaskComputeDiagnosticsSnapshot();
    const rasterMaskDiagnosticsEntry =
      rasterMaskComputeDiagnosticsConsoleEntry(rasterMaskDiagnostics);
    const imageTileDiagnostics = getImageTileDiagnosticsSnapshot();
    const imageTileDiagnosticsEntry = imageTileDiagnosticsConsoleEntry(imageTileDiagnostics);
    const rasterResourceDiagnostics = getRasterResourceDiagnosticsSnapshot();
    const rasterResourceDiagnosticsEntry =
      rasterResourceDiagnosticsConsoleEntry(rasterResourceDiagnostics);
    const description = appendRasterResourceDiagnostics(
      appendImageTileDiagnostics(
        appendRasterMaskComputeDiagnostics(
          appendVideoWorkbenchDiagnostics(trimmedDescription, videoDiagnostics),
          rasterMaskDiagnostics,
        ),
        imageTileDiagnostics,
      ),
      rasterResourceDiagnostics,
    );
    if (codePointLength(description) > MAX_DESCRIPTION_LENGTH) {
      pushToast({
        msg: `描述（含自动诊断）不能超过 ${MAX_DESCRIPTION_LENGTH} 个字符`,
        kind: "error",
      });
      return;
    }

    setSubmitting(true);
    try {
      let uploadedAttachments: BugAttachment[] = [];
      if (!skipScreenshot && pendingAttachments.length > 0) {
        try {
          uploadedAttachments = await Promise.all(
            pendingAttachments.map((item) => uploadBugAttachment(item.blob, item.fileName)),
          );
          setScreenshotUploadFail(null);
        } catch (e) {
          // v0.7.0：失败不再静默降级，停在表单让用户选 retry / skip / cancel
          console.error("Bug screenshot upload failed", e);
          setScreenshotUploadFail("请稍后重试，或跳过截图继续提交。");
          setSubmitting(false);
          return;
        }
      }
      const recentConsoleErrors = getRecentConsoleErrors().map((e) => ({
        msg: e.msg,
        stack: e.stack || "",
      }));
      if (videoDiagnosticsEntry) recentConsoleErrors.unshift(videoDiagnosticsEntry);
      if (rasterMaskDiagnosticsEntry) recentConsoleErrors.unshift(rasterMaskDiagnosticsEntry);
      if (imageTileDiagnosticsEntry) recentConsoleErrors.unshift(imageTileDiagnosticsEntry);
      if (rasterResourceDiagnosticsEntry) {
        recentConsoleErrors.unshift(rasterResourceDiagnosticsEntry);
      }
      // v0.9.41: 附 workbench longtask 快照，便于 BUG 排查时定位卡顿点。
      const perf = readWorkbenchPerfSnapshot();
      if (perf.longTaskCount > 0) {
        recentConsoleErrors.unshift({
          msg: "[workbench-perf]",
          stack: JSON.stringify(perf, null, 2),
        });
      }
      await bugReportsApi.create({
        title: trimmedTitle,
        description,
        severity: severity as "low" | "medium" | "high" | "critical",
        route: location.pathname + location.search,
        browser_ua: navigator.userAgent.slice(0, 200),
        viewport: `${window.innerWidth}x${window.innerHeight}`,
        task_id: taskIdFromVideoWorkbenchDiagnostics(videoDiagnostics),
        recent_api_calls: sanitizeApiCalls(getRecentApiCalls()),
        recent_console_errors: recentConsoleErrors,
        screenshot_url: uploadedAttachments[0]?.storageKey ?? null,
        attachments: uploadedAttachments,
      });
      pushToast({ msg: "反馈已提交", kind: "success" });
      setTitle("");
      setDesc("");
      setSeverity("medium");
      setScreenshotBlob(null);
      setPendingAttachments([]);
      setView("list");
    } catch {
      pushToast({ msg: "提交失败，请稍后重试", kind: "error" });
    } finally {
      setSubmitting(false);
    }
  };

  const handleCaptureScreenshot = async () => {
    pushToast({ msg: "正在截图…" });
    try {
      // 关闭 drawer 短暂时间让 html2canvas 截到完整页面（可选简化：用 ignoreElements）
      const blob = await captureScreenshot();
      setScreenshotBlob(blob);
      setScreenshotEditing(true);
    } catch (e) {
      pushToast({
        msg: "截图失败",
        sub: e instanceof Error ? e.message : String(e),
        kind: "error",
      });
    }
  };

  const startEdit = (r: BugReportDetail) => {
    invalidateDetailScope();
    setEditId(r.id);
    setTitle(r.title);
    setDesc(r.description);
    setSeverity(r.severity);
    setView("edit");
  };

  const handleUpdate = async () => {
    const trimmedTitle = title.trim();
    const description = desc.trim();
    if (!editId || !trimmedTitle || !description) return;
    if (codePointLength(description) > MAX_DESCRIPTION_LENGTH) {
      pushToast({ msg: `描述不能超过 ${MAX_DESCRIPTION_LENGTH} 个字符`, kind: "error" });
      return;
    }
    setSubmitting(true);
    try {
      await bugReportsApi.update(editId, {
        title: trimmedTitle,
        description,
        severity,
      });
      pushToast({ msg: "反馈已更新", kind: "success" });
      setTitle("");
      setDesc("");
      setSeverity("medium");
      setEditId(null);
      setView("list");
    } catch {
      pushToast({ msg: "更新失败", kind: "error" });
    } finally {
      setSubmitting(false);
    }
  };

  const handlePostComment = async (submittedBody = commentBody) => {
    if (!detail || !submittedBody.trim() || postingComment) return;
    const reportId = detail.id;
    const detailScope = detailRequestRef.current;
    const requestId = ++commentRequestRef.current;
    const body = submittedBody.trim();
    if (codePointLength(body) > MAX_COMMENT_LENGTH) {
      pushToast({ msg: `评论不能超过 ${MAX_COMMENT_LENGTH} 个字符`, kind: "error" });
      return;
    }
    const willReopen = ["fixed", "wont_fix", "duplicate"].includes(detail.status);
    setPostingComment(true);
    try {
      await bugReportsApi.addComment(reportId, body);
      if (
        requestId !== commentRequestRef.current ||
        detailScope !== detailRequestRef.current ||
        activeDetailIdRef.current !== reportId
      ) {
        return;
      }
      setCommentBody("");
      pushToast({
        msg: willReopen ? "评论已发送，反馈已重新打开" : "评论已发送",
        kind: "success",
      });
      const fresh = await bugReportsApi.get(reportId);
      if (
        requestId !== commentRequestRef.current ||
        detailScope !== detailRequestRef.current ||
        activeDetailIdRef.current !== reportId
      ) {
        return;
      }
      setDetail(fresh);
    } catch {
      if (
        requestId === commentRequestRef.current &&
        detailScope === detailRequestRef.current &&
        activeDetailIdRef.current === reportId
      ) {
        pushToast({ msg: "评论发送失败", kind: "error" });
      }
    } finally {
      if (requestId === commentRequestRef.current) setPostingComment(false);
    }
  };

  const handleDelete = async (id: string) => {
    if (
      !(await confirmDialog({
        tone: "danger",
        title: "删除反馈",
        description: "反馈删除后不可恢复。",
        confirmLabel: "删除",
      }))
    )
      return;
    try {
      await bugReportsApi.delete(id);
      pushToast({ msg: "反馈已删除", kind: "success" });
      setDetail(null);
      setView("list");
    } catch {
      pushToast({ msg: "删除失败", kind: "error" });
    }
  };

  const statusLabel: Record<string, string> = {
    new: "新提交",
    triaged: "已确认",
    in_progress: "处理中",
    fixed: "已修复",
    wont_fix: "不修复",
    duplicate: "重复",
  };

  if (!open) return null;

  const handleClose = () => {
    invalidateDetailScope();
    onClose();
  };

  return (
    <>
      <div data-bug-drawer className={styles.overlay} onClick={handleClose} />
      <div data-bug-drawer className={styles.drawer}>
        {/* Header */}
        <div className={styles.header}>
          <span className={styles.title}>
            {view === "list"
              ? "我的反馈"
              : view === "create"
                ? "提交反馈"
                : view === "edit"
                  ? "编辑反馈"
                  : (detail?.display_id ?? "详情")}
          </span>
          <div className={styles.headerActions}>
            {view !== "list" && (
              <button
                onClick={() => {
                  invalidateDetailScope();
                  setView("list");
                  setDetail(null);
                }}
                className={cx(styles.button, styles.secondaryButton)}
              >
                返回
              </button>
            )}
            <button onClick={handleClose} className={styles.closeButton}>
              <Icon name="x" size={16} />
            </button>
          </div>
        </div>

        {/* Content */}
        <div className={styles.content}>
          {view === "list" && (
            <div>
              <button
                onClick={() => {
                  setTitle("");
                  setDesc("");
                  setSeverity("medium");
                  setPendingAttachments([]);
                  setScreenshotBlob(null);
                  setScreenshotEditing(false);
                  setScreenshotUploadFail(null);
                  setView("create");
                }}
                className={styles.primaryButton}
              >
                <Icon name="plus" size={13} className={styles.plusIcon} />
                提交新反馈
              </button>
              {loading && <div className={styles.loadingState}>加载中...</div>}
              {!loading && reports.length === 0 && (
                <div className={styles.emptyState}>暂无反馈</div>
              )}
              {reports.map((r) => (
                <div key={r.id} onClick={() => loadDetail(r.id)} className={styles.reportRow}>
                  <div className={styles.reportTitle}>
                    {r.display_id}: {r.title}
                  </div>
                  <div className={styles.reportMeta}>
                    <span className={cx(styles.severity, severityClassName[r.severity])}>
                      {r.severity}
                    </span>
                    {" · "}
                    <span>{statusLabel[r.status] ?? r.status}</span>
                    {" · "}
                    <span>{new Date(r.created_at).toLocaleDateString("zh-CN")}</span>
                  </div>
                </div>
              ))}
            </div>
          )}

          {view === "create" && (
            <form
              onPasteCapture={handlePasteImage}
              onSubmit={(e) => {
                e.preventDefault();
                handleSubmit();
              }}
            >
              <label className={styles.label}>标题 *</label>
              <input
                required
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                maxLength={500}
                placeholder="发生了什么问题？"
                className={styles.field}
              />

              <label className={styles.label}>描述 *</label>
              <CompactMarkdownEditor
                value={desc}
                onChange={setDesc}
                placeholder="详细描述问题..."
                documentId="bug-create"
                label="反馈描述"
                variant="compact"
                disabled={submitting}
              />

              <label className={styles.label}>严重程度</label>
              <select
                value={severity}
                onChange={(e) => setSeverity(e.target.value)}
                className={styles.field}
              >
                <option value="low">低 - 小建议</option>
                <option value="medium">中 - 影响体验</option>
                <option value="high">高 - 影响功能</option>
                <option value="critical">严重 - 系统不可用</option>
              </select>

              <div className={styles.screenshotSection}>
                <label className={styles.strongLabel}>截图（可选）</label>
                {screenshotEditing && screenshotBlob ? (
                  <ScreenshotEditor
                    imageBlob={screenshotBlob}
                    onConfirm={(blob) => {
                      addPendingAttachment(blob, `screenshot-${Date.now()}.png`);
                      setScreenshotBlob(null);
                      setScreenshotEditing(false);
                    }}
                    onCancel={() => {
                      setScreenshotBlob(null);
                      setScreenshotEditing(false);
                    }}
                  />
                ) : (
                  <>
                    <div className={styles.captureRow}>
                      <button
                        type="button"
                        onClick={handleCaptureScreenshot}
                        disabled={pendingAttachments.length >= MAX_ATTACHMENTS}
                        className={styles.captureButton}
                      >
                        <Icon name="image" size={12} /> 截取当前画面
                      </button>
                      <span className={styles.hint}>
                        可直接粘贴剪贴板截图，最多 {MAX_ATTACHMENTS} 张
                      </span>
                    </div>
                    {pendingAttachments.length > 0 && (
                      <div className={styles.attachmentList}>
                        {pendingAttachments.map((att, index) => (
                          <div
                            key={att.id}
                            className={cx(styles.attachmentItem, styles.pendingAttachmentItem)}
                          >
                            <Icon name="image" size={12} />
                            <span className={styles.truncate}>
                              图 {index + 1} · {att.fileName} · {Math.round(att.blob.size / 1024)}{" "}
                              KB
                            </span>
                            <button
                              type="button"
                              onClick={() =>
                                setPendingAttachments((items) =>
                                  items.filter((item) => item.id !== att.id),
                                )
                              }
                              className={styles.removeButton}
                            >
                              移除
                            </button>
                          </div>
                        ))}
                      </div>
                    )}
                  </>
                )}
              </div>

              {screenshotUploadFail && (
                <div className={styles.uploadError}>
                  <div className={styles.uploadErrorText}>截图上传失败：{screenshotUploadFail}</div>
                  <div className={styles.inlineActions}>
                    <button
                      type="button"
                      onClick={() => {
                        setScreenshotUploadFail(null);
                        handleSubmit(false);
                      }}
                      className={styles.smallPrimaryButton}
                    >
                      重试上传
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setScreenshotUploadFail(null);
                        handleSubmit(true);
                      }}
                      className={styles.smallGhostButton}
                    >
                      跳过截图提交
                    </button>
                  </div>
                </div>
              )}

              <button
                type="submit"
                disabled={submitting || !title.trim() || !desc.trim()}
                className={cx(styles.primaryButton, styles.submitButton)}
              >
                {submitting ? "提交中..." : "提交反馈"}
              </button>
            </form>
          )}

          {view === "edit" && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                handleUpdate();
              }}
            >
              <label className={styles.label}>标题 *</label>
              <input
                required
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                maxLength={500}
                className={styles.field}
              />
              <label className={styles.label}>描述 *</label>
              <CompactMarkdownEditor
                value={desc}
                onChange={setDesc}
                documentId={editId ? `bug-edit-${editId}` : "bug-edit"}
                label="反馈描述"
                variant="compact"
                disabled={submitting}
              />
              <label className={styles.label}>严重程度</label>
              <select
                value={severity}
                onChange={(e) => setSeverity(e.target.value)}
                className={styles.field}
              >
                <option value="low">低 - 小建议</option>
                <option value="medium">中 - 影响体验</option>
                <option value="high">高 - 影响功能</option>
                <option value="critical">严重 - 系统不可用</option>
              </select>
              <button
                type="submit"
                disabled={submitting || !title.trim() || !desc.trim()}
                className={cx(styles.primaryButton, styles.submitButton)}
              >
                {submitting ? "保存中..." : "保存修改"}
              </button>
            </form>
          )}

          {view === "detail" && detail && (
            <div className={styles.detail}>
              <div className={styles.detailHeader}>
                <span className={styles.detailTitle}>{detail.title}</span>
                <div className={styles.detailActions}>
                  <button onClick={() => startEdit(detail)} className={styles.detailButton}>
                    编辑
                  </button>
                  <button
                    onClick={() => handleDelete(detail.id)}
                    className={cx(styles.detailButton, styles.dangerButton)}
                  >
                    删除
                  </button>
                </div>
              </div>
              <div className={styles.detailMeta}>
                <span className={cx(styles.severity, severityClassName[detail.severity])}>
                  {detail.severity}
                </span>
                <span className={styles.badge}>{statusLabel[detail.status] ?? detail.status}</span>
                {detail.reopen_count > 0 && (
                  <span
                    title={
                      detail.last_reopened_at
                        ? `最近重开：${new Date(detail.last_reopened_at).toLocaleString("zh-CN")}`
                        : undefined
                    }
                    className={cx(styles.badge, styles.reopenBadge)}
                  >
                    曾重开 {detail.reopen_count} 次
                  </span>
                )}
                <span className={styles.muted}>
                  {new Date(detail.created_at).toLocaleString("zh-CN")}
                </span>
              </div>
              <div className={styles.routeLine}>
                路由：<code className={styles.routeCode}>{detail.route}</code>
              </div>
              <div className={styles.sectionBlock}>
                <MarkdownBlock compact>{detail.description}</MarkdownBlock>
              </div>
              {detail.attachments?.length > 0 && (
                <div className={styles.sectionBlock}>
                  <div className={styles.sectionTitle}>截图附件 ({detail.attachments.length})</div>
                  <div className={styles.attachmentGrid}>
                    {detail.attachments.map((att) => (
                      <a
                        key={att.storageKey}
                        href={bugReportsApi.attachmentDownloadUrl(detail.id, att.storageKey)}
                        target="_blank"
                        rel="noopener noreferrer"
                        className={styles.attachmentItem}
                      >
                        <Icon name="image" size={12} />
                        <span className={styles.truncate}>{att.fileName}</span>
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
                  <span className={styles.resolutionLabel}>处理结果：</span>
                  {detail.resolution}
                </div>
              )}
              <div className={styles.comments}>
                <div className={styles.sectionTitle}>评论 ({detail.comments.length})</div>
                {detail.comments.map((c) => (
                  <div key={c.id} className={styles.comment}>
                    <div className={styles.commentMeta}>
                      <span className={styles.commentAuthor}>{c.author_name || "未知"}</span>
                      {c.author_role && <span className={styles.roleBadge}>{c.author_role}</span>}
                      <span className={styles.commentTime}>
                        {new Date(c.created_at).toLocaleString("zh-CN")}
                      </span>
                    </div>
                    <MarkdownBlock compact>{c.body}</MarkdownBlock>
                  </div>
                ))}
                {detail.comments.length === 0 && (
                  <div className={styles.emptyComments}>暂无评论</div>
                )}

                {/* 评论输入框 */}
                <div className={styles.commentComposer}>
                  {["fixed", "wont_fix", "duplicate"].includes(detail.status) && (
                    <div className={styles.reopenNotice}>
                      ⚠ 当前状态为「{statusLabel[detail.status] ?? detail.status}
                      」，发送评论将自动重新打开此反馈
                    </div>
                  )}
                  <div>
                    <CompactMarkdownEditor
                      value={commentBody}
                      onChange={setCommentBody}
                      onSubmit={(next) => void handlePostComment(next)}
                      placeholder="写下你的回复 / 补充信息..."
                      documentId={`bug-comment-${detail.id}`}
                      label="反馈评论"
                      variant="compact"
                      disabled={postingComment}
                    />
                  </div>
                  <button
                    type="button"
                    onClick={() => void handlePostComment()}
                    disabled={postingComment || !commentBody.trim()}
                    className={styles.sendButton}
                  >
                    {postingComment ? "发送中..." : "发送"}
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
