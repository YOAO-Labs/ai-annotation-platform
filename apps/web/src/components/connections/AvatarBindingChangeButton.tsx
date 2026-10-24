import { useEffect, useId, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type {
  AvatarBindingCandidate,
  AvatarBindingPreview,
  StorageBindingOut,
} from "@/api/generated/types.gen";
import { ApiError } from "@/api/client";
import { storageApi } from "@/api/storage";
import { Button } from "@/components/ui/Button";
import { ListPagination } from "@/components/ui/ListPagination";
import { Modal } from "@/components/ui/Modal";
import { useToastStore } from "@/components/ui/Toast";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { verifiedContentError } from "@/hooks/useVerifiedImage";
import { storageAccessDenied } from "./storageQueryState";

interface Props {
  binding: StorageBindingOut;
  onApplied: () => Promise<void>;
}

function errorCode(error: unknown): string {
  return error instanceof ApiError && typeof error.detailRaw === "object" && error.detailRaw
    ? String((error.detailRaw as { code?: unknown }).code ?? "")
    : "";
}

function changeError(error: unknown): string {
  const code = errorCode(error);
  if (code === "storage_binding_preview_expired") return "预览已过期，请重新预览。";
  if (code === "storage_binding_preview_changed")
    return "用途绑定、目标配置或影响关系已变化，请刷新并重新预览。";
  if (code === "storage_binding_preview_invalid") return "确认凭证无效，请重新预览。";
  if (code === "avatar_history_unlocated")
    return "存在缺少存储定位的历史头像，请相关用户重新上传头像或改用预设头像后重新预览。";
  if (code === "avatar_binding_unchanged") return "请选择与当前绑定不同的存储位置。";
  if (code === "avatar_binding_target_missing")
    return "目标位置已不存在，请刷新候选位置后重新选择。";
  if (code === "avatar_storage_binding_unproven")
    return "用途绑定或目标配置无法确认，请刷新存储配置后重新预览。";
  if (code === "storage_binding_busy") return "其他存储配置操作正在进行，请稍后重新预览。";
  if (code === "avatar_storage_conditions_unsupported")
    return "目标不支持条件写入，请选择支持该能力的存储位置。";
  if (["avatar_storage_probe_cleanup_unproven", "storage_operation_unsettled"].includes(code))
    return "验证操作尚未确认收尾，用途未切换。请管理员检查目标位置的测试对象和未收尾访问。";
  if (code === "permission_denied")
    return "目标位置拒绝读写或删除，请检查连接凭据和访问权限后重新预览。";
  return verifiedContentError(error);
}

export function AvatarBindingChangeButton(props: Props) {
  const session = useSessionRevision();
  return (
    <ChangeOwner
      key={`${session}/${props.binding.location_id}/${props.binding.generation}/${props.binding.location_config_revision}/${props.binding.bucket}`}
      session={session}
      {...props}
    />
  );
}

function ChangeOwner({ binding, onApplied, session }: Props & { session: number }) {
  const capture = useSessionGuard();
  const pushToast = useToastStore((state) => state.push);
  const selectId = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const sequence = useRef(0);
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [selected, setSelected] = useState<AvatarBindingCandidate | null>(null);
  const [preview, setPreview] = useState<AvatarBindingPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [applying, setApplying] = useState(false);
  const [expired, setExpired] = useState(false);
  const [stale, setStale] = useState(false);
  const [denied, setDenied] = useState(false);
  const [error, setError] = useState("");
  const candidates = useQuery({
    queryKey: ["avatar-binding-candidates", session, page, pageSize],
    queryFn: ({ signal }) => storageApi.avatarBindingCandidates(page * pageSize, signal, pageSize),
    enabled: open && !denied,
    retry: false,
  });
  const accessDenied = denied || storageAccessDenied(candidates.error);
  const candidateData = accessDenied ? undefined : candidates.data;
  const options = candidateData?.items ?? [];
  const choices =
    selected && !options.some((option) => option.location_id === selected.location_id)
      ? [selected, ...options]
      : options;
  const visiblePreview = accessDenied ? null : preview;
  useEffect(() => {
    if (!candidateData || candidates.isFetching) return;
    const lastPage = Math.max(0, Math.ceil(candidateData.total / pageSize) - 1);
    if (page > lastPage) setPage(lastPage);
  }, [candidateData, candidates.isFetching, page, pageSize]);
  useEffect(() => {
    if (!storageAccessDenied(candidates.error)) return;
    if (!applying) sequence.current += 1;
    setPreview(null);
    setSelected(null);
    setBusy(false);
    setDenied(true);
  }, [candidates.error, applying]);
  useEffect(() => {
    if (applying) return;
    const updated = candidateData?.items.find(
      (option) => option.location_id === selected?.location_id,
    );
    if (!updated || updated.config_revision === selected?.config_revision) return;
    sequence.current += 1;
    setSelected(updated);
    setPreview(null);
    setBusy(false);
    setError("目标位置配置已变化，请重新预览。");
  }, [candidateData, selected, applying]);
  useEffect(() => {
    if (!preview) return;
    const timer = window.setTimeout(
      () => setExpired(true),
      Math.max(0, Date.parse(preview.expires_at) - Date.now()),
    );
    return () => window.clearTimeout(timer);
  }, [preview]);
  const close = () => {
    if (applying) return;
    sequence.current += 1;
    setOpen(false);
    setPreview(null);
    setSelected(null);
    setBusy(false);
    setError("");
  };
  const select = (id: string) => {
    sequence.current += 1;
    setSelected(choices.find((option) => option.location_id === id) ?? null);
    setPreview(null);
    setBusy(false);
    setExpired(false);
    setStale(false);
    setError("");
  };
  const fail = (failure: unknown) => {
    setError(changeError(failure));
    if (errorCode(failure) === "avatar_binding_target_missing") {
      setPreview(null);
      setSelected(null);
      return;
    }
    if (storageAccessDenied(failure)) {
      setPreview(null);
      setSelected(null);
      setDenied(true);
    }
  };
  const payload =
    selected && binding.generation != null
      ? {
          expected_generation: binding.generation,
          target_location_id: selected.location_id,
          target_config_revision: selected.config_revision,
        }
      : null;
  const load = async () => {
    if (!payload || !selected || applying || accessDenied) return;
    const current = capture();
    const ticket = ++sequence.current;
    const owns = () => current() && sequence.current === ticket;
    setBusy(true);
    setStale(true);
    setError("");
    try {
      const result = await storageApi.previewAvatarBinding(payload);
      if (!owns()) return;
      if (
        result.purpose !== "avatars" ||
        result.previous.purpose !== "avatars" ||
        result.previous.location_id !== binding.location_id ||
        result.previous.generation !== binding.generation ||
        result.previous.bucket !== binding.bucket ||
        result.target.location_id !== selected.location_id ||
        result.target.config_revision !== selected.config_revision ||
        !Number.isFinite(Date.parse(result.expires_at))
      )
        throw new Error("用途绑定或目标配置已变化，请刷新并重新预览。");
      setPreview(result);
      setExpired(Date.parse(result.expires_at) <= Date.now());
      setStale(false);
    } catch (failure) {
      if (owns()) fail(failure);
    } finally {
      if (owns()) setBusy(false);
    }
  };
  const apply = async () => {
    if (!payload || !preview || !preview.can_apply || busy || stale || accessDenied) return;
    if (expired || Date.parse(preview.expires_at) <= Date.now()) {
      setExpired(true);
      return;
    }
    const current = capture();
    const ticket = ++sequence.current;
    const owns = () => current() && sequence.current === ticket;
    setBusy(true);
    setApplying(true);
    setError("");
    let saved = false;
    try {
      await storageApi.applyAvatarBinding({ ...payload, impact_token: preview.impact_token });
      saved = true;
      if (!owns()) return;
      setOpen(false);
      setPreview(null);
      pushToast({ msg: "头像存储绑定已更新，后续新头像使用新位置", kind: "success" });
      await onApplied();
    } catch (failure) {
      if (owns()) {
        if (saved) setError(`头像存储绑定已更新，但刷新失败。${changeError(failure)}`);
        else {
          setStale(true);
          fail(failure);
        }
      }
    } finally {
      if (owns()) {
        setBusy(false);
        setApplying(false);
      }
    }
  };
  return (
    <div className="mt-2 min-w-0">
      <Button ref={trigger} size="sm" disabled={accessDenied} onClick={() => setOpen(true)}>
        切换头像位置
      </Button>
      {!open && error && (
        <p role="alert" className="mt-2 text-xs leading-relaxed text-status-danger">
          {error}
        </p>
      )}
      <Modal
        open={open}
        onClose={close}
        title="切换头像存储位置"
        width={640}
        stopEscapePropagation
        returnFocusRef={trigger}
        initialFocusRef={cancel}
        footer={
          <div className="flex flex-wrap justify-end gap-2">
            <Button ref={cancel} size="sm" disabled={applying} onClick={close}>
              取消
            </Button>
            <Button
              size="sm"
              disabled={
                !payload || busy || accessDenied || selected?.location_id === binding.location_id
              }
              onClick={() => void load()}
            >
              {busy && !applying ? "正在验证…" : preview ? "重新预览" : "验证并预览"}
            </Button>
            <Button
              variant="primary"
              size="sm"
              disabled={!visiblePreview?.can_apply || busy || expired || stale || accessDenied}
              onClick={() => void apply()}
            >
              {applying ? "正在提交…" : "确认切换"}
            </Button>
          </div>
        }
      >
        <div className="space-y-4 text-sm">
          <p className="text-xs leading-relaxed text-muted-foreground">
            仅后续新上传的头像写入新位置。已有头像继续按保存的位置读取和清理，不会随绑定迁移。
          </p>
          {accessDenied ? (
            <p role="alert" className="text-xs leading-relaxed text-status-danger">
              存储管理权限已变化，请刷新后重试。
            </p>
          ) : (
            <>
              <div>
                <div className="mb-2 flex items-center justify-between gap-2">
                  <label htmlFor={selectId} className="block text-sm font-medium">
                    目标存储位置
                  </label>
                  <Button
                    size="xs"
                    disabled={candidates.isFetching || applying}
                    onClick={() => void candidates.refetch()}
                  >
                    刷新位置
                  </Button>
                </div>
                <select
                  id={selectId}
                  aria-describedby={`${selectId}-hint`}
                  value={selected?.location_id ?? ""}
                  disabled={applying}
                  onChange={(event) => select(event.target.value)}
                  className="h-9 w-full rounded-md border border-border bg-background px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
                >
                  <option value="" disabled hidden />
                  {choices.map((option) => (
                    <option
                      key={option.location_id}
                      value={option.location_id}
                      disabled={option.location_id === binding.location_id}
                    >
                      {option.name}
                    </option>
                  ))}
                </select>
                <p
                  id={`${selectId}-hint`}
                  className="mt-2 text-xs leading-relaxed text-muted-foreground"
                >
                  选择已登记的全局 S3 位置。验证将实际写入、完整读回并删除一个测试对象。
                </p>
              </div>
              {candidates.isFetching && (
                <p role="status" className="text-xs text-muted-foreground">
                  正在读取候选位置…
                </p>
              )}
              {candidates.error && (
                <p role="alert" className="text-xs leading-relaxed text-status-danger">
                  {candidateData ? "刷新失败，候选位置可能过期。" : "无法读取候选位置。"}
                  {changeError(candidates.error)}{" "}
                  <Button
                    size="xs"
                    disabled={candidates.isFetching}
                    onClick={() => void candidates.refetch()}
                  >
                    重试
                  </Button>
                </p>
              )}
              {candidateData && (
                <>
                  {!candidateData.items.some(
                    (option) => option.location_id !== binding.location_id,
                  ) && (
                    <p className="text-xs text-muted-foreground">
                      本页没有其他可切换位置。请先登记全局 S3 连接，或查看其他页。
                    </p>
                  )}
                  <ListPagination
                    label="候选位置"
                    page={page}
                    pageSize={pageSize}
                    total={candidateData.total}
                    loading={candidates.isFetching || applying}
                    onPageChange={setPage}
                    onPageSizeChange={(size) => {
                      setPageSize(size);
                      setPage(0);
                    }}
                  />
                </>
              )}
              {visiblePreview && (
                <section
                  aria-label="头像绑定变更预览"
                  className="space-y-3 rounded-md border border-border p-3"
                >
                  <h3 className="text-md font-medium">变更预览</h3>
                  <dl className="grid gap-3 sm:grid-cols-2">
                    <div className="min-w-0">
                      <dt className="text-xs text-muted-foreground">当前位置</dt>
                      <dd className="mt-1 break-all">{visiblePreview.previous.location_id}</dd>
                      <dd className="mt-1 break-all text-xs text-muted-foreground">
                        桶 {visiblePreview.previous.bucket} · 用途代次{" "}
                        {visiblePreview.previous.generation}
                      </dd>
                    </div>
                    <div className="min-w-0">
                      <dt className="text-xs text-muted-foreground">后续新头像位置</dt>
                      <dd className="mt-1 break-words">{visiblePreview.target.name}</dd>
                      <dd className="mt-1 break-all text-xs text-muted-foreground">
                        桶 {visiblePreview.target.bucket} · 前缀{" "}
                        {visiblePreview.target.key_prefix || "根目录"}
                      </dd>
                      <dd className="mt-1 text-xs text-muted-foreground">
                        位置修订 {visiblePreview.target.config_revision} · 连接修订{" "}
                        {visiblePreview.target.connection_revision ?? "不适用"}
                      </dd>
                    </div>
                  </dl>
                  <p className="text-xs text-muted-foreground">
                    用途代次 {visiblePreview.previous.generation} →{" "}
                    {(visiblePreview.previous.generation ?? 0) + 1}
                  </p>
                  <p className="text-xs leading-relaxed text-muted-foreground">
                    已登记头像：待完成 {visiblePreview.history.pending} · 可用{" "}
                    {visiblePreview.history.ready} · 待清理 {visiblePreview.history.retired}
                    。这些对象保留原位置。
                  </p>
                  {visiblePreview.history.unlocated_users > 0 && (
                    <p role="alert" className="text-xs leading-relaxed text-status-danger">
                      {visiblePreview.history.unlocated_users}{" "}
                      位用户的历史上传头像缺少存储定位。请相关用户重新上传头像或改用预设头像后重新预览，当前不能切换。
                    </p>
                  )}
                  <p className="text-xs leading-relaxed text-muted-foreground">
                    目标实际验证：条件写入
                    {visiblePreview.capabilities.conditional_write ? "通过" : "未通过"} · 完整读回
                    {visiblePreview.capabilities.full_read ? "通过" : "未通过"} · 删除
                    {visiblePreview.capabilities.delete ? "通过" : "未通过"}
                  </p>
                  <p className="text-xs leading-relaxed text-muted-foreground">
                    验证于 {new Date(visiblePreview.measured_at).toLocaleString("zh-CN")}；有效至{" "}
                    {new Date(visiblePreview.expires_at).toLocaleString("zh-CN")}
                    。绑定或目标配置变化后需重新预览。
                  </p>
                  {(expired || stale) && (
                    <p role="status" className="text-xs leading-relaxed text-status-warning">
                      {expired ? "预览已过期，请重新预览。" : "确认已失效，请重新预览后再切换。"}
                    </p>
                  )}
                </section>
              )}
              {error && (
                <p role="alert" className="text-xs leading-relaxed text-status-danger">
                  {error}
                </p>
              )}
            </>
          )}
        </div>
      </Modal>
    </div>
  );
}
