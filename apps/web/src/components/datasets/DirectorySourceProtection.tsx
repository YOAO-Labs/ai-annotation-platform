import { useId, useRef, useState } from "react";
import type {
  DatasetSourceOut,
  SourceExternalProtectionOut,
  SourceExternalProtectionUpdate,
} from "@/api/generated/types.gen";
import { datasetSourcesApi as api } from "@/api/datasetSources";
import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useSessionGuard } from "@/hooks/useSessionGuard";
import { verifiedContentError } from "@/hooks/useVerifiedImage";

const methods = {
  external_backup: "外部备份恢复",
  reacquire: "上游重新获取",
  unrecoverable: "无恢复方案",
  unknown: "尚未确认",
};
const field =
  "w-full min-w-0 rounded-md border border-input bg-background px-2 py-1.5 text-sm text-foreground";

export function ExternalProtectionSummary({
  value,
}: {
  value?: SourceExternalProtectionOut | null;
}) {
  if (!value) {
    return <p className="text-xs text-muted-foreground">尚未登记责任方和恢复方式。</p>;
  }
  return (
    <div className="min-w-0 space-y-1 text-sm">
      <p className="break-words">
        责任方：{value.responsible_party} ·{" "}
        <span className={value.recovery_method === "unrecoverable" ? "text-status-caution" : ""}>
          {methods[value.recovery_method]}
        </span>
        （已声明）
      </p>
      <p className="break-words text-xs text-muted-foreground">
        内部编号：{value.recovery_reference ?? "未填写"} · 更新于{" "}
        {new Date(value.recorded_at).toLocaleString()}
      </p>
    </div>
  );
}

export function DirectorySourceProtection({
  source,
  onSaved,
}: {
  source: DatasetSourceOut;
  onSaved: () => Promise<unknown>;
}) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  return (
    <section aria-label="来源原件保护责任" className="rounded-lg border border-border bg-card p-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-md font-semibold">来源原件保护责任</h3>
        <Button ref={trigger} size="sm" onClick={() => setOpen(true)}>
          {source.external_protection ? "编辑保护责任" : "登记保护责任"}
        </Button>
      </div>
      <ExternalProtectionSummary value={source.external_protection} />
      <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
        由责任方保护来源原件。引用索引、缓存和责任声明都不能证明外部原件已备份或可恢复。
      </p>
      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="来源原件保护责任"
        returnFocusRef={trigger}
      >
        {open && (
          <ProtectionEditor source={source} onSaved={onSaved} onClose={() => setOpen(false)} />
        )}
      </Modal>
    </section>
  );
}

function ProtectionEditor({
  source,
  onSaved,
  onClose,
}: {
  source: DatasetSourceOut;
  onSaved: () => Promise<unknown>;
  onClose: () => void;
}) {
  const hint = useId();
  const capture = useSessionGuard();
  const [revision, setRevision] = useState(source.external_protection?.revision ?? 0);
  const [party, setParty] = useState(source.external_protection?.responsible_party ?? "");
  const [method, setMethod] = useState<SourceExternalProtectionUpdate["recovery_method"] | "">(
    source.external_protection?.recovery_method ?? "",
  );
  const [reference, setReference] = useState(source.external_protection?.recovery_reference ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const reload = async () => {
    const current = capture();
    setBusy(true);
    try {
      const fresh = await api.source(source.id);
      if (!current()) return;
      setRevision(fresh.external_protection?.revision ?? 0);
      setParty(fresh.external_protection?.responsible_party ?? "");
      setMethod(fresh.external_protection?.recovery_method ?? "");
      setReference(fresh.external_protection?.recovery_reference ?? "");
      setConflict(false);
      setError("");
    } catch (failure) {
      if (current()) setError(verifiedContentError(failure));
    } finally {
      if (current()) setBusy(false);
    }
  };
  return (
    <form
      className="space-y-4 text-sm"
      onSubmit={(event) => {
        event.preventDefault();
        if (!party.trim() || !method || busy || conflict) return;
        const current = capture();
        setBusy(true);
        setError("");
        void api
          .updateProtection(source.id, {
            expected_revision: revision,
            responsible_party: party.trim(),
            recovery_method: method,
            recovery_reference: reference.trim() || null,
          })
          .then(async () => {
            // The callback only invalidates queries belonging to this source owner.
            await onSaved();
            if (current()) onClose();
          })
          .catch((failure: unknown) => {
            if (!current()) return;
            if (failure instanceof ApiError && failure.status === 409) {
              setConflict(true);
              setError("保护责任已更新，请载入最新记录后重新核对。");
            } else setError(verifiedContentError(failure));
          })
          .finally(() => {
            if (current()) setBusy(false);
          });
      }}
    >
      <p className="text-xs leading-relaxed text-muted-foreground">
        为“{source.name}”登记责任。记录不会修改源文件、扫描规则或已有任务。
      </p>
      <label className="grid gap-1">
        责任方
        <input
          className={field}
          required
          maxLength={100}
          value={party}
          disabled={busy}
          onChange={(event) => setParty(event.target.value)}
        />
      </label>
      <div>
        <label className="grid gap-1">
          恢复方式
          <select
            className={field}
            required
            aria-describedby={`${hint}-method`}
            value={method}
            disabled={busy}
            onChange={(event) => setMethod(event.target.value as typeof method)}
          >
            <option value="" disabled hidden />
            {Object.entries(methods).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <p id={`${hint}-method`} className="mt-2 text-xs leading-relaxed text-muted-foreground">
          选择责任方当前声明的恢复方式；这不是平台的备份验证结果。
        </p>
      </div>
      <div>
        <label className="grid gap-1">
          内部参考编号（可选）
          <input
            className={field}
            maxLength={100}
            pattern="[A-Za-z0-9_.:\-]+"
            value={reference}
            disabled={busy}
            aria-describedby={`${hint}-reference`}
            onChange={(event) => setReference(event.target.value)}
          />
        </label>
        <p id={`${hint}-reference`} className="mt-2 text-xs leading-relaxed text-muted-foreground">
          填写内部恢复说明或工单编号，仅用字母、数字及 . _ : -，不要填写路径、链接或凭据。
        </p>
      </div>
      {error && (
        <p role="alert" className="text-sm text-status-danger">
          {error}
        </p>
      )}
      {conflict && (
        <Button type="button" disabled={busy} onClick={() => void reload()}>
          载入最新记录
        </Button>
      )}
      <div className="flex flex-wrap justify-end gap-2">
        <Button type="button" onClick={onClose}>
          取消
        </Button>
        <Button
          type="submit"
          variant="primary"
          disabled={busy || conflict || !party.trim() || !method}
        >
          {busy ? "正在保存…" : "保存责任声明"}
        </Button>
      </div>
    </form>
  );
}
