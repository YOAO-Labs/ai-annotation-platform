import { useEffect, useId, useRef, useState, type RefObject } from "react";
import { useQuery } from "@tanstack/react-query";
import type {
  AvatarBindingCandidate,
  AvatarBindingPreview,
  BugAttachmentBindingPreview,
  AuditArchiveBindingPreview,
  ExportBindingPreview,
  ImportBindingPreview,
  DatasetBindingPreview,
  MediaCacheBindingPreview,
  AnnotationsBindingPreview,
  TrainingBindingPreview,
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
  purpose:
    | "avatars"
    | "bug-reports"
    | "audit-archive"
    | "export"
    | "import"
    | "datasets"
    | "media-cache"
    | "annotations"
    | "training";
  binding: StorageBindingOut;
  onApplied: () => Promise<void>;
}

function errorCode(error: unknown): string {
  return error instanceof ApiError && typeof error.detailRaw === "object" && error.detailRaw
    ? String((error.detailRaw as { code?: unknown }).code ?? "")
    : "";
}

function changeError(error: unknown, _purpose: Props["purpose"]): string {
  const code = errorCode(error);
  if (code === "storage_binding_preview_expired") return "预览已过期，请重新预览。";
  if (code === "storage_binding_preview_changed")
    return "用途绑定、目标配置或影响关系已变化，请刷新并重新预览。";
  if (code === "storage_binding_preview_invalid") return "确认凭证无效，请重新预览。";
  if (code === "avatar_history_unlocated")
    return "存在缺少存储定位的历史头像，请相关用户重新上传头像或改用预设头像后重新预览。";
  if (
    [
      "avatar_binding_unchanged",
      "bug_attachment_binding_unchanged",
      "audit_archive_binding_unchanged",
      "export_binding_unchanged",
      "import_binding_unchanged",
      "dataset_binding_unchanged",
      "media_cache_binding_unchanged",
      "annotations_binding_unchanged",
      "training_binding_unchanged",
    ].includes(code)
  )
    return "请选择与当前绑定不同的存储位置。";
  if (
    [
      "avatar_binding_target_missing",
      "bug_attachment_binding_target_missing",
      "audit_archive_binding_target_missing",
      "export_binding_target_missing",
      "import_binding_target_missing",
      "dataset_binding_target_missing",
      "media_cache_binding_target_missing",
      "annotations_binding_target_missing",
      "training_binding_target_missing",
    ].includes(code)
  )
    return "目标位置已不存在，请刷新候选位置后重新选择。";
  if (
    [
      "avatar_storage_binding_unproven",
      "bug_attachment_storage_binding_unproven",
      "audit_archive_storage_binding_unproven",
      "export_storage_binding_unproven",
      "import_storage_binding_unproven",
      "dataset_storage_binding_unproven",
      "media_cache_storage_binding_unproven",
      "annotations_storage_binding_unproven",
      "training_storage_binding_unproven",
    ].includes(code)
  )
    return "用途绑定或目标配置无法确认，请刷新存储配置后重新预览。";
  if (code === "storage_binding_busy") return "其他存储配置操作正在进行，请稍后重新预览。";
  if (
    [
      "avatar_storage_conditions_unsupported",
      "bug_attachment_storage_conditions_unsupported",
      "audit_archive_storage_conditions_unsupported",
      "export_storage_conditions_unsupported",
      "import_storage_conditions_unsupported",
      "dataset_storage_conditions_unsupported",
      "media_cache_storage_conditions_unsupported",
      "annotations_storage_conditions_unsupported",
    ].includes(code)
  )
    return "目标不支持条件写入，请选择支持该能力的存储位置。";
  if (
    [
      "avatar_storage_probe_cleanup_unproven",
      "bug_attachment_storage_probe_cleanup_unproven",
      "audit_archive_storage_probe_cleanup_unproven",
      "export_storage_probe_cleanup_unproven",
      "import_storage_probe_cleanup_unproven",
      "dataset_storage_probe_cleanup_unproven",
      "media_cache_storage_probe_cleanup_unproven",
      "annotations_storage_probe_cleanup_unproven",
      "training_storage_probe_cleanup_unproven",
      "storage_operation_unsettled",
    ].includes(code)
  )
    return "验证操作尚未确认收尾，用途未切换。请管理员检查目标位置的测试对象和未收尾访问。";
  if (code === "import_storage_versioning_required")
    return "临时输入需要版本化存储，请启用目标桶版本控制并允许版本读取、删除和列举后重新预览。";
  if (code === "training_storage_unavailable")
    return "目标训练存储验证失败，请检查连接、普通写入、分段上传、读取、前缀列举及批量删除权限后重新预览。";
  if (code === "media_cache_storage_unavailable")
    return "目标缓存存储验证失败，请检查连接、条件写入、读取、删除和前缀列举权限后重新预览。";
  if (code === "dataset_storage_unavailable" || code === "annotations_storage_unavailable")
    return "目标存储验证失败，请检查连接、读写权限及分段上传能力后重新预览。";
  if (code === "permission_denied")
    return "目标位置拒绝读写或删除，请检查连接凭据和访问权限后重新预览。";
  return verifiedContentError(error);
}

export function StorageBindingChangeButton(props: Props) {
  const session = useSessionRevision();
  const trigger = useRef<HTMLButtonElement>(null);
  return (
    <ChangeOwner
      key={`${props.purpose}/${session}/${props.binding.location_id}/${props.binding.generation}/${props.binding.location_config_revision}/${props.binding.bucket}`}
      session={session}
      trigger={trigger}
      {...props}
    />
  );
}

function ChangeOwner({
  binding,
  purpose,
  onApplied,
  session,
  trigger,
}: Props & {
  session: number;
  trigger: RefObject<HTMLButtonElement>;
}) {
  const noun = {
    avatars: "头像",
    "bug-reports": "报告附件",
    "audit-archive": "审计归档",
    export: "导出",
    import: "导入暂存",
    datasets: "数据集原件",
    annotations: "标注文件",
    training: "训练文件",
    "media-cache": "媒体缓存",
  }[purpose];
  const capture = useSessionGuard();
  const pushToast = useToastStore((state) => state.push);
  const selectId = useId();
  const cancel = useRef<HTMLButtonElement>(null);
  const sequence = useRef(0);
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [selected, setSelected] = useState<AvatarBindingCandidate | null>(null);
  const [preview, setPreview] = useState<
    | AvatarBindingPreview
    | BugAttachmentBindingPreview
    | AuditArchiveBindingPreview
    | ExportBindingPreview
    | ImportBindingPreview
    | DatasetBindingPreview
    | TrainingBindingPreview
    | AnnotationsBindingPreview
    | MediaCacheBindingPreview
    | null
  >(null);
  const [busy, setBusy] = useState(false);
  const [applying, setApplying] = useState(false);
  const [expired, setExpired] = useState(false);
  const [stale, setStale] = useState(false);
  const [denied, setDenied] = useState(false);
  const [error, setError] = useState("");
  const candidates = useQuery({
    queryKey: [
      {
        avatars: "avatar-binding-candidates",
        "bug-reports": "bug-attachment-binding-candidates",
        "audit-archive": "audit-archive-binding-candidates",
        export: "export-binding-candidates",
        import: "import-binding-candidates",
        datasets: "dataset-binding-candidates",
        annotations: "annotations-binding-candidates",
        training: "training-binding-candidates",
        "media-cache": "media-cache-binding-candidates",
      }[purpose],
      session,
      page,
      pageSize,
    ],
    queryFn: ({ signal }) =>
      purpose === "avatars"
        ? storageApi.avatarBindingCandidates(page * pageSize, signal, pageSize)
        : storageApi.bindingCandidates(purpose, page * pageSize, signal, pageSize),
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
    setError(changeError(failure, purpose));
    if (
      [
        "avatar_binding_target_missing",
        "bug_attachment_binding_target_missing",
        "audit_archive_binding_target_missing",
        "export_binding_target_missing",
        "import_binding_target_missing",
        "dataset_binding_target_missing",
        "media_cache_binding_target_missing",
        "annotations_binding_target_missing",
        "training_binding_target_missing",
      ].includes(errorCode(failure))
    ) {
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
      const result = await (purpose === "avatars"
        ? storageApi.previewAvatarBinding(payload)
        : storageApi.previewBinding(purpose, payload));
      if (!owns()) return;
      if (
        result.purpose !== purpose ||
        result.previous.purpose !== purpose ||
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
      await (purpose === "avatars"
        ? storageApi.applyAvatarBinding({ ...payload, impact_token: preview.impact_token })
        : storageApi.applyBinding(purpose, { ...payload, impact_token: preview.impact_token }));
      saved = true;
      if (!owns()) return;
      setOpen(false);
      setPreview(null);
      pushToast({
        msg:
          purpose === "media-cache"
            ? "媒体缓存绑定已更新，后续新分配使用新位置"
            : purpose === "import"
              ? "导入暂存绑定已更新，后续新分配使用新位置"
              : purpose === "training"
                ? "训练文件绑定已更新，后续新分配使用新位置"
                : purpose === "annotations"
                  ? "标注文件绑定已更新，后续新分配使用新位置"
                  : purpose === "datasets"
                    ? "数据集原件绑定已更新，后续新分配使用新位置"
                    : `${noun}存储绑定已更新，后续新${noun}使用新位置`,
        kind: "success",
      });
      await onApplied();
    } catch (failure) {
      if (owns()) {
        if (saved) setError(`${noun}存储绑定已更新，但刷新失败。${changeError(failure, purpose)}`);
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
        切换{noun}位置
      </Button>
      {!open && error && (
        <p role="alert" className="mt-2 text-xs leading-relaxed text-status-danger">
          {error}
        </p>
      )}
      <Modal
        open={open}
        onClose={close}
        title={purpose === "import" ? "切换导入暂存位置" : `切换${noun}存储位置`}
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
            {purpose === "media-cache" ? (
              <>
                仅后续新分配的原图缓存、目录预览、缩略图、图片金字塔和视频派生输出使用新位置。已有及在途输出保留原位置，切换不会搬迁或清空缓存。
              </>
            ) : purpose === "training" ? (
              <>
                仅后续新分配的训练版本、实验、预览、逐样本评估和主动学习文件使用新位置。已有及在途文件保留原位置，重试、续训及清理沿保存地址，内容和首次期限不变。
              </>
            ) : purpose === "annotations" ? (
              <>
                仅后续新分配的独立任务原件、RLE
                对象、评论附件和指南图片写入新位置。已有及在途对象保留原位置，逻辑键、发布状态与期限不变。
              </>
            ) : purpose === "datasets" ? (
              <>
                仅后续新分配的数据集原件及辅助原件写入新位置。已有原件和在途上传保留原位置，数据集改名不改变文件路径。
              </>
            ) : purpose === "import" ? (
              <>
                仅后续新分配的导入暂存写入新位置。已有及在途上传、预标输入和格式导入继续使用原位置，最终原件位置不变。
              </>
            ) : purpose === "export" ? (
              <>
                仅后续新分配的导出写入新位置。已有及已分配的 ZIP
                和独立媒体保留原位置，下载期限不变。
              </>
            ) : purpose === "audit-archive" ? (
              <>仅后续新分配的审计归档写入新位置。已有及已分配归档继续使用保存的位置，永久保留。</>
            ) : (
              <>
                仅后续新上传的{noun}写入新位置。已有{noun}
                继续按保存的位置读取和清理，不会随绑定迁移。
              </>
            )}
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
                  {purpose === "training"
                    ? "选择已登记的全局 S3 位置。验证将实际执行普通写入、非空分段上传、完整读回、精确前缀列举和批量删除。"
                    : purpose === "media-cache"
                      ? "选择已登记的全局 S3 位置。验证将检查条件写入、完整读回、准确删除和前缀列举。"
                      : purpose === "datasets" || purpose === "annotations"
                        ? "选择已登记的全局 S3 位置。验证将检查空文件和分段上传的条件写入，完整读回并清理测试对象。"
                        : "选择已登记的全局 S3 位置。验证将实际写入、完整读回并删除一个测试对象。"}
                  {purpose === "export" &&
                    "同时验证对象和版本列举权限；外部端点须供浏览器及下载脚本访问。"}
                  {purpose === "import" &&
                    "同时验证版本读取、删除与列举能力；目标桶须启用版本控制，外部端点须供浏览器与模型服务访问。"}
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
                  {changeError(candidates.error, purpose)}{" "}
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
                  aria-label={`${noun}绑定变更预览`}
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
                      <dt className="text-xs text-muted-foreground">
                        {purpose === "import" ||
                        purpose === "datasets" ||
                        purpose === "media-cache" ||
                        purpose === "annotations" ||
                        purpose === "training"
                          ? "后续分配位置"
                          : `后续新${noun}位置`}
                      </dt>
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
                  {"training_versions" in visiblePreview.history ? (
                    <div className="space-y-1 text-xs leading-relaxed text-muted-foreground">
                      <p>
                        训练版本 {visiblePreview.history.training_versions} · 训练实验{" "}
                        {visiblePreview.history.training_runs}
                      </p>
                      <p>
                        训练预览 {visiblePreview.history.training_previews} · 逐样本评估{" "}
                        {visiblePreview.history.sample_evaluations}
                      </p>
                      <p>
                        主动学习轮次 {visiblePreview.history.active_learning_rounds} · 删除意图{" "}
                        {visiblePreview.history.deletion_intents}
                      </p>
                      <p>
                        以上为不同类型的责任记录，包含所有保存状态，不相加作为对象总数。未固定的历史对象仍须显式核验，不要求先全库接管。
                      </p>
                    </div>
                  ) : "task_originals" in visiblePreview.history ? (
                    <div className="space-y-1 text-xs leading-relaxed text-muted-foreground">
                      <p>
                        任务原件 {visiblePreview.history.task_originals} · 任务写入分配{" "}
                        {visiblePreview.history.creation_allocations}
                      </p>
                      <p>
                        RLE 对象 {visiblePreview.history.raster_mask_objects} · 评论附件{" "}
                        {visiblePreview.history.comment_attachments} · 指南图片{" "}
                        {visiblePreview.history.guide_assets}
                      </p>
                      <p>
                        以上为不同类型的责任记录，包含所有保存状态，不相加作为对象总数。已有及在途对象保留原位置。
                      </p>
                    </div>
                  ) : "directory_previews" in visiblePreview.history ? (
                    <div className="space-y-1 text-xs leading-relaxed text-muted-foreground">
                      <p>
                        目录预览 {visiblePreview.history.directory_previews} · 原图缓存{" "}
                        {visiblePreview.history.original_cache_entries}
                      </p>
                      <p>
                        图片金字塔 {visiblePreview.history.image_pyramid_outputs} · 视频派生{" "}
                        {visiblePreview.history.video_derivative_outputs}
                      </p>
                      <p>
                        缩略图输出 {visiblePreview.history.thumbnail_outputs} · 旧缩略图{" "}
                        {visiblePreview.history.legacy_thumbnails}
                      </p>
                      <p>
                        以上为不同类型的责任记录，不相加作为对象总数。已有和在途输出保留原位置。
                      </p>
                    </div>
                  ) : "asset_locations" in visiblePreview.history ? (
                    <div className="space-y-1 text-xs leading-relaxed text-muted-foreground">
                      <p>
                        原件位置记录 {visiblePreview.history.asset_locations} · 写入分配{" "}
                        {visiblePreview.history.creation_allocations}
                      </p>
                      <p>
                        辅助原件 {visiblePreview.history.auxiliary_originals} · 退役记录{" "}
                        {visiblePreview.history.retirements}
                      </p>
                      <p>这些记录保留原位置；切换不会搬迁文件。</p>
                    </div>
                  ) : "lease_active" in visiblePreview.history ? (
                    <div className="space-y-1 text-xs leading-relaxed text-muted-foreground">
                      <p>
                        预标临时输入：活动 {visiblePreview.history.lease_active} · 待清理{" "}
                        {visiblePreview.history.lease_cleanup_pending} · 已清理{" "}
                        {visiblePreview.history.lease_deleted}
                      </p>
                      <p>直传暂存：已分配 {visiblePreview.history.creation_staging}</p>
                      <p>
                        格式导入：已分配上传 {visiblePreview.history.mask_uploads} · 批次{" "}
                        {visiblePreview.history.mask_batches}
                      </p>
                      <p>
                        这些输入保留原位置。预标输入沿用已有清理；直传和格式导入暂存不自动删除。
                      </p>
                    </div>
                  ) : "zip_pending" in visiblePreview.history ? (
                    <div className="space-y-1 text-xs leading-relaxed text-muted-foreground">
                      <p>
                        ZIP：待完成 {visiblePreview.history.zip_pending} · 可用{" "}
                        {visiblePreview.history.zip_ready} · 待清理{" "}
                        {visiblePreview.history.zip_retired}
                      </p>
                      <p>
                        独立媒体包：生成中 {visiblePreview.history.media_building} · 可用{" "}
                        {visiblePreview.history.media_ready} · 清理中{" "}
                        {visiblePreview.history.media_retiring} · 已清理{" "}
                        {visiblePreview.history.media_deleted}
                      </p>
                      <p>已有及已分配对象保留原位置，配套 ZIP 使用媒体包分配时的位置。</p>
                    </div>
                  ) : (
                    <p className="text-xs leading-relaxed text-muted-foreground">
                      已登记{noun}：待完成 {visiblePreview.history.pending} · 可用{" "}
                      {visiblePreview.history.ready}
                      {"retired" in visiblePreview.history && (
                        <> · 待清理 {visiblePreview.history.retired}</>
                      )}
                      。这些对象保留原位置。
                      {purpose === "audit-archive" && "归档永久保留，不执行物理清理。"}
                    </p>
                  )}
                  {"unlocated_users" in visiblePreview.history &&
                    visiblePreview.history.unlocated_users > 0 && (
                      <p role="alert" className="text-xs leading-relaxed text-status-danger">
                        {visiblePreview.history.unlocated_users}{" "}
                        位用户的历史上传头像缺少存储定位。请相关用户重新上传头像或改用预设头像后重新预览，当前不能切换。
                      </p>
                    )}
                  <p className="text-xs leading-relaxed text-muted-foreground">
                    目标实际验证：{purpose === "training" ? "普通写入" : "条件写入"}
                    {(
                      purpose === "training"
                        ? visiblePreview.capabilities.write
                        : visiblePreview.capabilities.conditional_write
                    )
                      ? "通过"
                      : "未通过"}{" "}
                    · 完整读回
                    {visiblePreview.capabilities.full_read ? "通过" : "未通过"} · 删除
                    {visiblePreview.capabilities.delete ? "通过" : "未通过"}
                    {(purpose === "datasets" || purpose === "annotations") && (
                      <>
                        {" "}
                        · 空文件{visiblePreview.capabilities.empty_put ? "通过" : "未通过"} ·
                        分段上传{visiblePreview.capabilities.multipart ? "通过" : "未通过"}
                      </>
                    )}
                    {purpose === "training" && (
                      <>
                        {" "}
                        · 分段上传{visiblePreview.capabilities.multipart ? "通过" : "未通过"} ·
                        前缀列举{visiblePreview.capabilities.list ? "通过" : "未通过"} · 批量删除
                        {visiblePreview.capabilities.delete ? "通过" : "未通过"}
                      </>
                    )}
                    {purpose === "media-cache" && (
                      <> · 前缀列举{visiblePreview.capabilities.list ? "通过" : "未通过"}</>
                    )}
                    {(purpose === "export" || purpose === "import") && (
                      <>
                        {" "}
                        · 对象列举{visiblePreview.capabilities.list ? "通过" : "未通过"} · 版本列举
                        {visiblePreview.capabilities.list_versions ? "通过" : "未通过"}
                      </>
                    )}
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
