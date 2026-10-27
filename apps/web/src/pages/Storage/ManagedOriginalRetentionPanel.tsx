import { useEffect, useId, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  ManagedOriginalRetentionChange,
  ManagedOriginalRetentionHold,
  ManagedOriginalRetentionPreview,
  ManagedOriginalRetentionState,
  ManagedOriginalRetentionSummary,
} from "@/api/generated/types.gen";
import { ApiError } from "@/api/client";
import { managedOriginalRetentionApi } from "@/api/managedOriginalRetention";
import { storageAccessDenied } from "@/components/connections/storageQueryState";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { ListPagination } from "@/components/ui/ListPagination";
import { useToastStore } from "@/components/ui/Toast";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { useAuthStore } from "@/stores/authStore";

const field =
  "w-full min-w-0 rounded-md border border-input bg-background px-2 py-1.5 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";
const hint = "text-xs leading-relaxed text-muted-foreground";
const kinds = { backup: "备份保留", migration: "迁移观察" };
const date = (value: string) => new Date(value).toLocaleString("zh-CN");
const deadlineLabel = (value: string | null) => (value ? date(value) : "无限期保护");

function validState(data: ManagedOriginalRetentionState, id: string) {
  return (
    data.retirement_id === id &&
    data.coverage === "declared_requirements_only" &&
    data.execution_permission === false &&
    Number.isSafeInteger(data.policy_revision) &&
    data.policy_revision > 0 &&
    Number.isFinite(Date.parse(data.measured_at)) &&
    Array.isArray(data.holds)
  );
}

function errorMessage(failure: unknown) {
  if (storageAccessDenied(failure)) return "当前权限或记录已失效，已隐藏保留设置。";
  if (failure instanceof ApiError && failure.status === 409)
    return "记录、保护要求或确认已变化，请刷新后重新预览。";
  if (failure instanceof ApiError && failure.status === 422)
    return "输入不符合要求；请检查截止时间、引用编号和原因后重新预览。";
  return "操作未完成，请刷新状态后重试。";
}

interface Props {
  retirementId: string;
  onChanged: () => Promise<unknown>;
  onDenied: () => void;
}

export function ManagedOriginalRetentionPanel(props: Props) {
  const revision = useSessionRevision();
  return <RetentionDisclosure key={`${revision}/${props.retirementId}`} {...props} />;
}

function RetentionDisclosure(props: Props) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="rounded-md border border-border"
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary className="cursor-pointer px-3 py-2 text-sm font-medium">保留设置与要求</summary>
      {open && <RetentionNavigation {...props} />}
    </details>
  );
}

function RetentionNavigation(props: Props) {
  const [id, setId] = useState(props.retirementId);
  return (
    <div className="min-w-0 border-t border-border p-3">
      {id !== props.retirementId && (
        <Button size="sm" onClick={() => setId(props.retirementId)}>
          返回原记录的设置
        </Button>
      )}
      <RetentionOwner key={id} {...props} retirementId={id} onNavigate={setId} />
    </div>
  );
}

function RetentionOwner({
  retirementId,
  onChanged,
  onDenied,
  onNavigate,
}: Props & { onNavigate: (id: string) => void }) {
  const session = useAuthStore((s) => s.sessionId);
  const revision = useSessionRevision();
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const query = useQuery({
    queryKey: ["managed-original-retention", session, revision, retirementId, pageSize, page],
    queryFn: ({ signal }) =>
      managedOriginalRetentionApi.state(retirementId, pageSize, page * pageSize, signal),
    retry: false,
    staleTime: 0,
    refetchOnWindowFocus: false,
  });
  const denied = storageAccessDenied(query.error);
  const data =
    !denied && query.data && validState(query.data, retirementId) ? query.data : undefined;
  useEffect(() => {
    if (denied) onDenied();
  }, [denied, onDenied]);
  useEffect(() => {
    if (!data || query.isFetching) return;
    const last = Math.max(0, Math.ceil(data.total_holds / pageSize) - 1);
    if (page > last) setPage(last);
  }, [data, query.isFetching, page, pageSize]);
  return (
    <div className="space-y-3 text-sm">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className={hint}>
          设置记录 {retirementId.slice(0, 8)}
          。这里只管理已登记原件的声明要求，不能证明备份可恢复或约束其他清理器。
        </p>
        <Button size="sm" disabled={query.isFetching} onClick={() => void query.refetch()}>
          刷新保留设置
        </Button>
      </div>
      {query.isPending && (
        <p role="status" className={hint}>
          正在读取保留设置…
        </p>
      )}
      {(query.error || (query.data && !data)) && (
        <p role="alert" className="text-xs leading-relaxed text-status-danger">
          {denied
            ? errorMessage(query.error)
            : data
              ? "刷新失败，以下为上次观测；刷新成功前不能修改。"
              : "无法读取当前记录的保留设置。"}
        </p>
      )}
      {data && (
        <>
          <p className={hint}>
            观测于 {date(data.measured_at)} · 策略修订 {data.policy_revision}
          </p>
          <ProtectionSummary data={data.summary} />
          <RetentionControls
            key={`${data.policy_revision}/${JSON.stringify(data.summary)}/${data.holds.map((h) => `${h.id}/${h.revision}`).join(",")}`}
            data={data}
            disabled={query.isFetching || !!query.error}
            onDenied={onDenied}
            onNavigate={onNavigate}
            onChanged={async () => {
              await Promise.all([query.refetch(), onChanged()]);
            }}
          />
          <p className={hint}>
            相关记录按同桶同键保守归组，可能包含不同端点；一条记录的无限期、较晚期限或有效要求都会继续保护整组。
          </p>
          <ListPagination
            label="原件保留要求历史"
            page={page}
            pageSize={pageSize}
            total={data.total_holds}
            hasNext={data.has_more}
            loading={query.isFetching}
            onPageChange={setPage}
            onPageSizeChange={(size) => {
              setPage(0);
              setPageSize(size);
            }}
          />
        </>
      )}
    </div>
  );
}

function ProtectionSummary({ data }: { data: ManagedOriginalRetentionSummary }) {
  return (
    <dl className="grid gap-3 sm:grid-cols-2">
      <div>
        <dt className={hint}>本条保留期限</dt>
        <dd className="mt-1">{deadlineLabel(data.retain_until)}</dd>
      </div>
      <div>
        <dt className={hint}>相关记录共同保护至</dt>
        <dd className="mt-1">
          {data.indefinite_retention ? "无限期保护" : deadlineLabel(data.effective_retain_until)}
        </dd>
      </div>
      <div className="sm:col-span-2">
        <dt className={hint}>已登记保护要求</dt>
        <dd className="mt-1">
          相关记录 {data.peer_assertions} 条 · 有效备份保留 {data.backup_holds} 条 · 有效迁移观察{" "}
          {data.migration_holds} 条
        </dd>
      </div>
    </dl>
  );
}

function HoldHistory({
  data,
  disabled,
  onNavigate,
  onRelease,
}: {
  data: ManagedOriginalRetentionState;
  disabled: boolean;
  onNavigate: (id: string) => void;
  onRelease: (hold: ManagedOriginalRetentionHold) => void;
}) {
  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium">相关记录的保留要求</h3>
      {data.holds.length === 0 && (
        <p className={hint}>暂无已登记要求；未登记备份或迁移的覆盖仍未知。</p>
      )}
      <ul className="divide-y divide-border">
        {data.holds.map((hold) => (
          <li key={hold.id} className="space-y-1 py-3">
            <p className="break-words font-medium">
              {kinds[hold.kind]} · {hold.reference_code}
            </p>
            <p className={hint}>
              {hold.released_at ? `已解除 · ${date(hold.released_at)}` : "有效，需显式解除"} ·
              登记记录 {hold.retirement_id.slice(0, 8)}
            </p>
            <p className={`${hint} break-words`}>{hold.reason}</p>
            {hold.release_reason && (
              <p className={`${hint} break-words`}>解除原因：{hold.release_reason}</p>
            )}
            {hold.retirement_id !== data.retirement_id && (
              <Button size="sm" disabled={disabled} onClick={() => onNavigate(hold.retirement_id)}>
                打开登记记录
              </Button>
            )}
            {hold.retirement_id === data.retirement_id && !hold.released_at && (
              <Button size="sm" disabled={disabled} onClick={() => onRelease(hold)}>
                解除 {hold.reference_code}
              </Button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function localDate(value: string | null) {
  if (!value) return "";
  const stamp = new Date(value);
  return new Date(stamp.getTime() - stamp.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

function RetentionControls({
  data,
  disabled,
  onChanged,
  onDenied,
  onNavigate,
}: {
  data: ManagedOriginalRetentionState;
  disabled: boolean;
  onChanged: () => Promise<unknown>;
  onDenied: () => void;
  onNavigate: (id: string) => void;
}) {
  const capture = useSessionGuard();
  const client = useQueryClient();
  const pushToast = useToastStore((s) => s.push);
  const formId = useId();
  const [editor, setEditor] = useState<"policy" | "hold" | ManagedOriginalRetentionHold | null>(
    null,
  );
  const [mode, setMode] = useState(data.summary.retain_until ? "deadline" : "indefinite");
  const [deadline, setDeadline] = useState(localDate(data.summary.retain_until));
  const [kind, setKind] = useState<"backup" | "migration">("backup");
  const [reference, setReference] = useState("");
  const [reason, setReason] = useState("");
  const [holdId, setHoldId] = useState("");
  const [busy, setBusy] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState("");
  const [denied, setDenied] = useState(false);
  const [review, setReview] = useState<{
    receipt: ManagedOriginalRetentionPreview;
    payload: ManagedOriginalRetentionChange;
  } | null>(null);
  const [expired, setExpired] = useState(false);
  const previewButton = useRef<HTMLButtonElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const sequence = useRef(0);
  useEffect(() => {
    setReview(null);
  }, [mode, deadline, kind, reference, reason]);
  useEffect(() => {
    if (!review) return;
    const timer = window.setTimeout(
      () => setExpired(true),
      Math.max(0, Date.parse(review.receipt.expires_at) - Date.now()),
    );
    return () => window.clearTimeout(timer);
  }, [review]);
  const choose = (next: typeof editor) => {
    sequence.current += 1;
    setEditor(next);
    setReason("");
    setError("");
    setReview(null);
    if (next === "hold") setHoldId(crypto.randomUUID());
  };
  const close = () => {
    if (!applying) {
      sequence.current += 1;
      setReview(null);
      setBusy(false);
    }
  };
  const fail = (failure: unknown) => {
    setReview(null);
    setError(errorMessage(failure));
    if (storageAccessDenied(failure)) {
      setDenied(true);
      onDenied();
    }
  };
  const finite = mode === "deadline" ? Date.parse(deadline) : null;
  const canPreview =
    !!editor &&
    !!reason.trim() &&
    !disabled &&
    !busy &&
    !denied &&
    (editor !== "policy" ||
      mode === "indefinite" ||
      (finite !== null && Number.isFinite(finite) && finite > Date.now())) &&
    (editor !== "hold" || /^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$/.test(reference));
  const load = async () => {
    if (!canPreview) return;
    const payload: ManagedOriginalRetentionChange =
      editor === "policy"
        ? {
            action: "set_retention",
            expected_revision: data.policy_revision,
            reason: reason.trim(),
            retain_until: mode === "indefinite" ? null : new Date(deadline).toISOString(),
          }
        : editor === "hold"
          ? {
              action: "add_hold",
              expected_revision: data.policy_revision,
              reason: reason.trim(),
              hold_id: holdId,
              hold_kind: kind,
              reference_code: reference,
            }
          : {
              action: "release_hold",
              expected_revision: data.policy_revision,
              reason: reason.trim(),
              hold_id: (editor as ManagedOriginalRetentionHold).id,
              expected_hold_revision: (editor as ManagedOriginalRetentionHold).revision,
            };
    const current = capture();
    const ticket = ++sequence.current;
    const owns = () => current() && ticket === sequence.current;
    setBusy(true);
    setError("");
    try {
      const receipt = await managedOriginalRetentionApi.preview(data.retirement_id, payload);
      if (!owns()) return;
      if (
        receipt.retirement_id !== data.retirement_id ||
        receipt.policy_revision !== data.policy_revision ||
        receipt.action !== payload.action ||
        receipt.execution_permission !== false ||
        receipt.coverage !== "declared_requirements_only" ||
        !Number.isFinite(Date.parse(receipt.expires_at))
      )
        throw new Error("Preview ownership mismatch");
      setReview({ receipt, payload });
      setExpired(Date.parse(receipt.expires_at) <= Date.now());
    } catch (failure) {
      if (owns()) fail(failure);
    } finally {
      if (owns()) setBusy(false);
    }
  };
  const apply = async () => {
    if (
      !review ||
      busy ||
      disabled ||
      expired ||
      Date.parse(review.receipt.expires_at) <= Date.now()
    ) {
      setExpired(true);
      return;
    }
    const current = capture();
    setBusy(true);
    setApplying(true);
    setError("");
    let saved = false;
    try {
      await managedOriginalRetentionApi.apply(data.retirement_id, {
        ...review.payload,
        impact_token: review.receipt.impact_token,
      });
      saved = true;
      if (!current()) return;
      setReview(null);
      pushToast({ msg: "原件保留要求已更新", kind: "success" });
      await Promise.all([
        client.invalidateQueries({ queryKey: ["storage-managed-original-reclamation"] }),
        onChanged(),
      ]);
    } catch (failure) {
      if (current()) {
        if (saved) setError("保留要求已更新，但刷新失败，请刷新保留设置。");
        else fail(failure);
      }
    } finally {
      if (current()) {
        setBusy(false);
        setApplying(false);
      }
    }
  };
  const lock = disabled || busy || denied;
  const release = editor && typeof editor === "object" ? editor : null;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        <Button size="sm" disabled={lock} onClick={() => choose("policy")}>
          调整本条期限
        </Button>
        <Button size="sm" disabled={lock} onClick={() => choose("hold")}>
          登记保留要求
        </Button>
      </div>
      {editor && !denied && (
        <div className="space-y-3 rounded-md border border-border p-3">
          <h3 className="text-sm font-medium">
            {editor === "policy"
              ? "调整本条保留期限"
              : editor === "hold"
                ? "登记备份或迁移保留要求"
                : `解除 ${release?.reference_code}`}
          </h3>
          {editor === "policy" && (
            <>
              <label className="grid gap-1 text-sm">
                保留方式
                <select
                  className={field}
                  value={mode}
                  disabled={lock}
                  aria-describedby={`${formId}-policy`}
                  onChange={(e) => setMode(e.target.value)}
                >
                  <option value="indefinite">无限期保护</option>
                  <option value="deadline">指定截止时间</option>
                </select>
                <span id={`${formId}-policy`} className={hint}>
                  只调整本条记录；其他相关记录和有效要求仍独立保护整组。
                </span>
              </label>
              {mode === "deadline" && (
                <label className="grid gap-1 text-sm">
                  保留截止时间
                  <input
                    type="datetime-local"
                    className={field}
                    disabled={lock}
                    value={deadline}
                    onChange={(e) => setDeadline(e.target.value)}
                    aria-describedby={`${formId}-deadline`}
                  />
                  <span id={`${formId}-deadline`} className={hint}>
                    按浏览器时区 {Intl.DateTimeFormat().resolvedOptions().timeZone}{" "}
                    解释，须晚于当前时间；提交时再次核验。
                  </span>
                </label>
              )}
            </>
          )}
          {editor === "hold" && (
            <>
              <label className="grid gap-1 text-sm">
                要求类型
                <select
                  className={field}
                  value={kind}
                  disabled={lock}
                  onChange={(e) => setKind(e.target.value as typeof kind)}
                  aria-describedby={`${formId}-kind`}
                >
                  <option value="backup">备份保留</option>
                  <option value="migration">迁移观察</option>
                </select>
                <span id={`${formId}-kind`} className={hint}>
                  登记后持续有效，需经预览确认显式解除；不自动到期。
                </span>
              </label>
              <label className="grid gap-1 text-sm">
                引用编号
                <input
                  className={field}
                  maxLength={100}
                  value={reference}
                  disabled={lock}
                  onChange={(e) => setReference(e.target.value)}
                  aria-describedby={`${formId}-reference`}
                />
                <span id={`${formId}-reference`} className={hint}>
                  使用字母、数字或 . _ : -
                  标记内部备份或迁移记录；不要填写路径、链接或凭据。此编号不是恢复验证结果。
                </span>
              </label>
            </>
          )}
          {release && (
            <p className={hint}>
              只解除选中的{kinds[release.kind]}
              要求，保留登记与解除历史；不会调整期限或解除其他要求。
            </p>
          )}
          <label className="grid gap-1 text-sm">
            {release ? "解除原因" : "变更原因"}
            <textarea
              className={field}
              rows={2}
              maxLength={500}
              value={reason}
              disabled={lock}
              onChange={(e) => setReason(e.target.value)}
              aria-describedby={`${formId}-reason`}
            />
            <span id={`${formId}-reason`} className={hint}>
              填写可审计的原因，不要包含密码或访问令牌。
            </span>
          </label>
          <div className="flex flex-wrap gap-2">
            <Button
              ref={previewButton}
              size="sm"
              disabled={!canPreview}
              onClick={() => void load()}
            >
              {busy ? "正在预览…" : "预览保留变更"}
            </Button>
            <Button size="sm" disabled={busy} onClick={() => choose(null)}>
              取消修改
            </Button>
          </div>
        </div>
      )}
      {error && (
        <p role="alert" className="text-xs leading-relaxed text-status-danger">
          {error}
        </p>
      )}
      <HoldHistory data={data} disabled={lock} onNavigate={onNavigate} onRelease={choose} />
      <Modal
        open={!!review}
        onClose={close}
        title="确认原件保留变更"
        width={600}
        stopEscapePropagation
        returnFocusRef={previewButton}
        initialFocusRef={cancelButton}
        footer={
          <div className="flex flex-wrap justify-end gap-2">
            <Button ref={cancelButton} size="sm" disabled={applying} onClick={close}>
              返回修改
            </Button>
            <Button
              variant="primary"
              size="sm"
              disabled={busy || disabled || expired || denied}
              onClick={() => void apply()}
            >
              {applying ? "正在提交…" : "确认更新保留要求"}
            </Button>
          </div>
        }
      >
        {review && (
          <div className="space-y-4 text-sm">
            <p>
              记录 {data.retirement_id.slice(0, 8)} · 策略修订 {review.receipt.policy_revision} →{" "}
              {review.receipt.policy_revision + 1}
            </p>
            {review.receipt.shortens_retention && (
              <p role="status" className="text-xs leading-relaxed text-status-warning">
                本次将缩短本条保留期限，请核对原因。其他相关记录和有效要求仍会保护整组。
              </p>
            )}
            {review.receipt.action === "add_hold" && (
              <p>
                登记{review.receipt.hold_kind ? kinds[review.receipt.hold_kind] : "保留"}要求：
                {review.receipt.reference_code}
              </p>
            )}
            {review.receipt.action === "release_hold" && (
              <p>解除要求：{review.receipt.reference_code}。其他要求和期限保持各自保护。</p>
            )}
            <div>
              <h3 className="mb-2 text-sm font-medium">变更前</h3>
              <ProtectionSummary data={review.receipt.before} />
            </div>
            <div>
              <h3 className="mb-2 text-sm font-medium">变更后</h3>
              <ProtectionSummary data={review.receipt.after} />
            </div>
            <p className={`${hint} break-words`}>原因：{review.payload.reason}</p>
            <p className={hint}>
              观测于 {date(review.receipt.measured_at)}，有效至 {date(review.receipt.expires_at)}
              。确认会重新核对修订和已登记要求。
            </p>
            <p className={hint}>
              本次只更新保留设置，不提供物理删除许可，也不证明真实备份、迁移或最小副本已通过验证。
            </p>
            {expired && (
              <p role="status" className="text-xs text-status-warning">
                预览已过期，请返回重新预览。
              </p>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}
