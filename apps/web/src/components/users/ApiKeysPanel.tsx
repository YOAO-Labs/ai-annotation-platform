import { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Badge } from "@/components/ui/Badge";
import { Tooltip } from "@/components/ui/Tooltip";
import { Modal } from "@/components/ui/Modal";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { useToastStore } from "@/components/ui/Toast";
import {
  useApiKeys,
  useCreateApiKey,
  useUpdateApiKey,
  useRotateApiKey,
  useRevokeApiKey,
} from "@/hooks/useApiKeys";
import type { ApiKey, ApiKeyCreated, ApiKeyUpdatePayload } from "@/api/apiKeys";
import styles from "./ApiKeysModal.module.css";

const FULL_ACCESS = "*";

const SCOPE_OPTIONS: { id: string; label: string }[] = [
  { id: "annotations:read", label: "标注 - 读" },
  { id: "annotations:write", label: "标注 - 写" },
  { id: "predictions:read", label: "预测 - 读" },
  { id: "datasets:read", label: "数据集 - 读" },
];

type ExpiryMode = "keep" | "never" | "30" | "90" | "365" | "custom";

const EXPIRY_PRESETS: { id: ExpiryMode; label: string }[] = [
  { id: "30", label: "30 天后过期" },
  { id: "90", label: "90 天后过期" },
  { id: "365", label: "1 年后过期" },
  { id: "never", label: "永不过期" },
  { id: "custom", label: "自定义天数…" },
];

function formatDate(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("zh-CN", { hour12: false });
}

function isExpired(iso: string | null) {
  return !!iso && new Date(iso).getTime() < Date.now();
}

/** 把表单的有效期模式换算为提交给后端的 expires_in_days（undefined = 不发送该字段）。 */
function expiryToDays(mode: ExpiryMode, customDays: number): number | null | undefined {
  switch (mode) {
    case "keep":
      return undefined; // PATCH 时不改有效期
    case "never":
      return null; // 显式改回永不过期
    case "custom":
      return customDays > 0 ? customDays : undefined;
    default:
      return Number(mode);
  }
}

/**
 * API 密钥管理主体：新建 / 编辑 / 轮换（一次性明文）/ 列表 / 吊销。
 * 同时被 ApiKeysModal（用户页弹窗）与个人设置页「API 密钥」分区复用。
 * active 控制列表查询的 enabled 与表单状态重置（弹窗关闭 / 切走分区时归位）。
 */
export function ApiKeysPanel({ active }: { active: boolean }) {
  const { data: keys = [], isLoading } = useApiKeys(active);
  const createKey = useCreateApiKey();
  const updateKey = useUpdateApiKey();
  const rotateKey = useRotateApiKey();
  const revokeKey = useRevokeApiKey();
  const pushToast = useToastStore((s) => s.push);

  // editing: null=未开表单; {id:null}=新建; {id}=编辑既有 key
  const [editing, setEditing] = useState<{ id: string | null } | null>(null);
  const [name, setName] = useState("");
  const [fullAccess, setFullAccess] = useState(false);
  const [scopes, setScopes] = useState<string[]>(["annotations:read"]);
  const [expiryMode, setExpiryMode] = useState<ExpiryMode>("never");
  const [customDays, setCustomDays] = useState(90);
  const [secret, setSecret] = useState<ApiKeyCreated | null>(null);

  const resetForm = () => {
    setEditing(null);
    setName("");
    setFullAccess(false);
    setScopes(["annotations:read"]);
    setExpiryMode("never");
    setCustomDays(90);
  };

  useEffect(() => {
    if (!active) {
      resetForm();
      setSecret(null);
      createKey.reset();
      updateKey.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  const openCreate = () => {
    resetForm();
    setEditing({ id: null });
  };

  const openEdit = (k: ApiKey) => {
    const full = k.scopes.includes(FULL_ACCESS);
    setName(k.name);
    setFullAccess(full);
    setScopes(full ? [] : k.scopes);
    setExpiryMode("keep");
    setCustomDays(90);
    setEditing({ id: k.id });
  };

  const pending = createKey.isPending || updateKey.isPending;
  const formError = createKey.isError
    ? (createKey.error as Error).message
    : updateKey.isError
      ? (updateKey.error as Error).message
      : null;

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !editing) return;
    const effectiveScopes = fullAccess ? [FULL_ACCESS] : scopes;

    if (editing.id === null) {
      const days = expiryToDays(expiryMode, customDays);
      createKey.mutate(
        {
          name: name.trim(),
          scopes: effectiveScopes,
          expires_in_days: days === undefined ? undefined : days,
        },
        {
          onSuccess: (data) => {
            setSecret(data);
            resetForm();
          },
        },
      );
    } else {
      const payload: ApiKeyUpdatePayload = {
        name: name.trim(),
        scopes: effectiveScopes,
      };
      const days = expiryToDays(expiryMode, customDays);
      if (days !== undefined) payload.expires_in_days = days; // keep 时不带该字段
      updateKey.mutate(
        { id: editing.id, payload },
        {
          onSuccess: () => {
            pushToast({ msg: "已更新", kind: "success" });
            resetForm();
          },
        },
      );
    }
  };

  const onRotate = async (key: ApiKey) => {
    if (key.revoked_at) return;
    if (
      !(await confirmDialog({
        tone: "danger",
        title: "轮换 API 密钥",
        description: `轮换后「${key.name}」的旧密钥将立即失效，需替换所有使用方。`,
        confirmLabel: "轮换",
      }))
    )
      return;
    rotateKey.mutate(key.id, {
      onSuccess: (data) => setSecret(data),
      onError: (err) =>
        pushToast({
          msg: "轮换失败",
          sub: err instanceof Error ? err.message : String(err),
          kind: "error",
        }),
    });
  };

  const onRevoke = async (key: ApiKey) => {
    if (key.revoked_at) return;
    if (
      !(await confirmDialog({
        tone: "danger",
        title: "吊销 API 密钥",
        description: `吊销后「${key.name}」将立即失效，此操作不可恢复。`,
        confirmLabel: "吊销",
      }))
    )
      return;
    revokeKey.mutate(key.id, {
      onSuccess: () => pushToast({ msg: "已吊销", kind: "success" }),
      onError: (err) =>
        pushToast({
          msg: "吊销失败",
          sub: err instanceof Error ? err.message : String(err),
          kind: "error",
        }),
    });
  };

  const onCopySecret = async () => {
    if (!secret) return;
    try {
      await navigator.clipboard.writeText(secret.plaintext);
      pushToast({ msg: "已复制到剪贴板", kind: "success" });
    } catch {
      pushToast({ msg: "复制失败，请手动选择文本", kind: "warning" });
    }
  };

  return (
    <div className={styles.root}>
      <div className={styles.panelHeader}>
        <div className={styles.muted}>
          密钥用于程序化访问 API（CI / 脚本 /
          SDK）；创建后请立即复制保存，离开本页后将无法再次查看明文。
        </div>
        <Button variant="primary" onClick={openCreate} className={styles.newKeyBtn}>
          <Icon name="plus" size={12} /> 新建密钥
        </Button>
      </div>

      <Modal
        open={!!editing || !!secret}
        onClose={() => (secret ? setSecret(null) : resetForm())}
        title={secret ? "密钥已创建" : editing?.id === null ? "新建密钥" : "编辑密钥"}
        width={520}
      >
        {secret ? (
          <SecretReveal data={secret} onAck={() => setSecret(null)} onCopy={onCopySecret} />
        ) : editing ? (
          <form onSubmit={submit} className={styles.modalForm}>
            <Field label="名称">
              <input
                type="text"
                autoFocus
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={60}
                placeholder="如 ci-bot / 数据导出脚本"
                className={styles.input}
              />
            </Field>

            <Field label="权限范围（scope）">
              <div className={styles.scopeList}>
                <label className={`${styles.scopeOption} ${styles.fullAccessOption}`}>
                  <input
                    type="checkbox"
                    checked={fullAccess}
                    onChange={(e) => setFullAccess(e.target.checked)}
                  />
                  <code className={styles.scopeCode}>{FULL_ACCESS}</code>
                  <span className={styles.muted}>完全访问（full-access，等同所有权限）</span>
                </label>
                {SCOPE_OPTIONS.map((opt) => (
                  <label
                    key={opt.id}
                    className={`${styles.scopeOption} ${fullAccess ? styles.scopeDisabled : ""}`}
                  >
                    <input
                      type="checkbox"
                      disabled={fullAccess}
                      checked={!fullAccess && scopes.includes(opt.id)}
                      onChange={(e) => {
                        setScopes((prev) =>
                          e.target.checked ? [...prev, opt.id] : prev.filter((s) => s !== opt.id),
                        );
                      }}
                    />
                    <code className={styles.scopeCode}>{opt.id}</code>
                    <span className={styles.muted}>{opt.label}</span>
                  </label>
                ))}
              </div>
            </Field>

            <Field label="有效期">
              <select
                className={styles.input}
                value={expiryMode}
                onChange={(e) => setExpiryMode(e.target.value as ExpiryMode)}
              >
                {editing.id !== null && <option value="keep">保持不变</option>}
                {EXPIRY_PRESETS.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.label}
                  </option>
                ))}
              </select>
              {expiryMode === "custom" && (
                <input
                  type="number"
                  min={1}
                  max={3650}
                  value={customDays}
                  onChange={(e) => setCustomDays(Number(e.target.value))}
                  className={styles.input}
                  placeholder="天数（1–3650）"
                />
              )}
            </Field>

            <div className={styles.note}>
              scope 目前仅对标注 / 数据集读 /
              预测读等部分端点在路由层强制；未覆盖的端点（含多数写操作）仍遵从你的账号角色——所选
              scope 不等于只读隔离。需要真正受限的程序化访问，请改用低权限账号创建 key。
            </div>
            {formError && <div className={styles.errorText}>{formError ?? "提交失败"}</div>}
            <div className={styles.actions}>
              <Button type="button" onClick={resetForm} disabled={pending}>
                取消
              </Button>
              <Button type="submit" variant="primary" disabled={!name.trim() || pending}>
                {pending
                  ? editing.id === null
                    ? "创建中..."
                    : "保存中..."
                  : editing.id === null
                    ? "创建"
                    : "保存"}
              </Button>
            </div>
          </form>
        ) : null}
      </Modal>

      <div className={styles.tableShell}>
        <table className={styles.table}>
          <thead>
            <tr>
              {["名称", "前缀", "权限", "有效期", "最后使用", "创建", ""].map((h, i) => (
                <th key={i} className={styles.th}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {isLoading && (
              <tr>
                <td colSpan={7} className={styles.emptyCell}>
                  加载中…
                </td>
              </tr>
            )}
            {!isLoading && keys.length === 0 && (
              <tr>
                <td colSpan={7} className={styles.emptyCell}>
                  尚未创建任何密钥
                </td>
              </tr>
            )}
            {keys.map((k) => {
              const revoked = !!k.revoked_at;
              const expired = !revoked && isExpired(k.expires_at);
              return (
                <tr key={k.id} className={revoked ? styles.revokedRow : undefined}>
                  <td className={`${styles.cell} ${styles.nameCell}`} title={k.name}>
                    {k.name}
                    {revoked && (
                      <span className={styles.revokedBadge}>
                        <Badge variant="outline">已吊销</Badge>
                      </span>
                    )}
                  </td>
                  <td className={`${styles.cell} mono ${styles.keyPrefix}`}>{k.key_prefix}…</td>
                  <td className={styles.cell}>
                    {k.scopes.length === 0 ? (
                      <span className={styles.subtle}>—</span>
                    ) : k.scopes.includes(FULL_ACCESS) ? (
                      <Badge variant="outline">完全访问</Badge>
                    ) : (
                      <Tooltip
                        side="top"
                        name="权限范围"
                        desc={
                          <div className={styles.scopeTipList}>
                            {k.scopes.map((s) => (
                              <code key={s}>{s}</code>
                            ))}
                          </div>
                        }
                      >
                        <span className={styles.scopeSummary} tabIndex={0}>
                          {k.scopes.length} 项权限
                        </span>
                      </Tooltip>
                    )}
                  </td>
                  <td className={`${styles.cell} ${styles.dateCell}`}>
                    {k.expires_at === null ? (
                      <span className={styles.subtle}>永不</span>
                    ) : expired ? (
                      <Badge variant="outline">已过期</Badge>
                    ) : (
                      formatDate(k.expires_at)
                    )}
                  </td>
                  <td className={`${styles.cell} ${styles.dateCell}`}>
                    {formatDate(k.last_used_at)}
                  </td>
                  <td className={`${styles.cell} ${styles.dateCell}`}>
                    {formatDate(k.created_at)}
                  </td>
                  <td className={`${styles.cell} ${styles.actionsCell}`}>
                    {!revoked && (
                      <>
                        <Button variant="ghost" size="sm" onClick={() => openEdit(k)} title="编辑">
                          <Icon name="edit" size={11} />
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => onRotate(k)}
                          disabled={rotateKey.isPending}
                          title="轮换（生成新密钥，旧的失效）"
                        >
                          <Icon name="refresh" size={11} />
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => onRevoke(k)}
                          disabled={revokeKey.isPending}
                          title="吊销密钥"
                        >
                          <Icon name="trash" size={11} className={styles.dangerIcon} />
                        </Button>
                      </>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className={styles.field}>
      <span className={styles.fieldLabel}>{label}</span>
      {children}
    </label>
  );
}

function SecretReveal({
  data,
  onAck,
  onCopy,
}: {
  data: ApiKeyCreated;
  onAck: () => void;
  onCopy: () => void | Promise<void>;
}) {
  return (
    <div className={styles.secretRoot}>
      <div className={styles.muted}>
        密钥 <strong>{data.name}</strong> 已生成。请立即复制保存，关闭后无法再次查看。
      </div>
      <div className={styles.secretBox}>{data.plaintext}</div>
      <div className={styles.actions}>
        <Button onClick={() => onCopy()}>复制</Button>
        <Button variant="primary" onClick={onAck}>
          我已记下
        </Button>
      </div>
    </div>
  );
}
