import { useEffect, useId, useState, type ReactNode } from "react";
import { clsx } from "clsx";

import {
  adminInferencePluginsApi,
  formatInferencePluginError,
  type ManagedModelCompatibility,
  type PluginInstallationResponse,
  type RegisterDirectPluginRequest,
} from "@/api/adminInferencePlugins";
import { Checkbox } from "@/components/shadcn/ui/checkbox";
import { Button } from "@/components/ui/Button";
import { CopyableError } from "@/components/ui/CopyableError";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { useToastStore } from "@/components/ui/Toast";

export interface DirectInferencePluginModalProps {
  open: boolean;
  onClose: () => void;
  onSuccess?: (created: PluginInstallationResponse) => void;
}

interface CompatibilityDraftRow {
  rowKey: number;
  framework: string;
  format: string;
  runtime_device: "cpu" | "cuda";
}

type SourceTierValue = NonNullable<RegisterDirectPluginRequest["source_tier"]>;

const LABEL_CLASS = "mb-1.5 block text-xs font-medium text-muted-foreground";
const INPUT_CLASS =
  "box-border w-full appearance-none rounded-md border border-border bg-muted px-2.5 py-2 text-sm text-foreground outline-none [font-family:inherit]";
const MONO_INPUT_CLASS = "font-mono text-xs";
const HELP_CLASS = "mt-1 text-xs leading-normal text-muted-foreground";
const SHA256_HEX_RE = /^[0-9a-f]{64}$/;

export function DirectInferencePluginModal({
  open,
  onClose,
  onSuccess,
}: DirectInferencePluginModalProps): ReactNode {
  const pushToast = useToastStore((s) => s.push);
  const idPrefix = useId();

  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [authToken, setAuthToken] = useState("");
  const [sourceTier, setSourceTier] = useState<SourceTierValue>("third_party");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [displayName, setDisplayName] = useState("");
  const [description, setDescription] = useState("");
  const [homepage, setHomepage] = useState("");
  const [publisher, setPublisher] = useState("");
  const [license, setLicense] = useState("");
  const [assetManifestSha256, setAssetManifestSha256] = useState("");
  const [compatRows, setCompatRows] = useState<CompatibilityDraftRow[]>([]);
  const [nextRowKey, setNextRowKey] = useState(1);
  const [approvedManagedEvaluation, setApprovedManagedEvaluation] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const resetForm = () => {
    setName("");
    setUrl("");
    setAuthToken("");
    setSourceTier("third_party");
    setAdvancedOpen(false);
    setDisplayName("");
    setDescription("");
    setHomepage("");
    setPublisher("");
    setLicense("");
    setAssetManifestSha256("");
    setCompatRows([]);
    setNextRowKey(1);
    setApprovedManagedEvaluation(false);
    setError(null);
  };

  useEffect(() => {
    if (!open) {
      resetForm();
    }
  }, [open]);

  const hasValidCompatibility = compatRows.some((row) => row.framework.trim().length > 0);

  useEffect(() => {
    if (!hasValidCompatibility && approvedManagedEvaluation) {
      setApprovedManagedEvaluation(false);
    }
  }, [hasValidCompatibility, approvedManagedEvaluation]);

  const handleClose = () => {
    if (submitting) return;
    resetForm();
    onClose();
  };

  const addCompatRow = () => {
    setCompatRows((prev) => [
      ...prev,
      { rowKey: nextRowKey, framework: "", format: "", runtime_device: "cpu" },
    ]);
    setNextRowKey((prev) => prev + 1);
  };

  const updateCompatRow = (
    rowKey: number,
    patch: Partial<Omit<CompatibilityDraftRow, "rowKey">>,
  ) => {
    setCompatRows((prev) =>
      prev.map((row) => (row.rowKey === rowKey ? { ...row, ...patch } : row)),
    );
  };

  const removeCompatRow = (rowKey: number) => {
    setCompatRows((prev) => prev.filter((row) => row.rowKey !== rowKey));
  };

  const handleSubmit = async () => {
    setError(null);
    const trimmedName = name.trim();
    const trimmedUrl = url.trim();
    const trimmedToken = authToken.trim();

    if (!trimmedName) {
      setError("插件名称不能为空。");
      return;
    }
    if (!trimmedUrl) {
      setError("服务地址不能为空。");
      return;
    }
    if (!/^https?:\/\//i.test(trimmedUrl)) {
      setError("服务地址必须以 http:// 或 https:// 开头。");
      return;
    }
    if (trimmedToken.length < 32) {
      setError("访问令牌至少需要 32 个字符。");
      return;
    }

    const trimmedHomepage = homepage.trim();
    if (trimmedHomepage && !/^https:\/\//i.test(trimmedHomepage)) {
      setError("主页地址必须以 https:// 开头。");
      return;
    }

    const trimmedSha = assetManifestSha256.trim().toLowerCase();
    if (trimmedSha && !SHA256_HEX_RE.test(trimmedSha)) {
      setError("资产清单 SHA-256 必须是 64 位小写十六进制字符串。");
      return;
    }

    const approvedCompatibility: ManagedModelCompatibility[] = [];
    const seenCompatKeys = new Set<string>();
    for (const row of compatRows) {
      const framework = row.framework.trim();
      const format = row.format.trim();
      if (!framework) {
        setError("受控模型兼容项的框架名称不能为空，请补全或删除空白行。");
        return;
      }
      const normalizedFormat = format ? format : null;
      const dedupeKey = `${framework}\0${normalizedFormat ?? ""}\0${row.runtime_device}`;
      if (seenCompatKeys.has(dedupeKey)) {
        setError(
          `受控模型兼容项重复：${framework} / ${normalizedFormat ?? "原生权重"} / ${row.runtime_device}`,
        );
        return;
      }
      seenCompatKeys.add(dedupeKey);
      approvedCompatibility.push({
        framework,
        format: normalizedFormat,
        runtime_device: row.runtime_device,
      });
    }

    if (approvedManagedEvaluation && approvedCompatibility.length === 0) {
      setError("批准受控评估前，请至少填写一条受控模型兼容项。");
      return;
    }

    const payload: RegisterDirectPluginRequest = {
      name: trimmedName,
      url: trimmedUrl,
      auth_token: trimmedToken,
      source_tier: sourceTier,
      approved_managed_model_compatibility: approvedCompatibility,
      approved_managed_evaluation: approvedManagedEvaluation,
    };

    const trimmedDisplayName = displayName.trim();
    const trimmedDescription = description.trim();
    const trimmedPublisher = publisher.trim();
    const trimmedLicense = license.trim();
    if (trimmedDisplayName) payload.display_name = trimmedDisplayName;
    if (trimmedDescription) payload.description = trimmedDescription;
    if (trimmedHomepage) payload.homepage = trimmedHomepage;
    if (trimmedPublisher) payload.publisher = trimmedPublisher;
    if (trimmedLicense) payload.license = trimmedLicense;
    if (trimmedSha) payload.asset_manifest_sha256 = trimmedSha;

    setSubmitting(true);
    try {
      const created = await adminInferencePluginsApi.registerDirect(payload);
      resetForm();
      pushToast({
        msg: `已注册直连推理插件「${created.summary?.display_name ?? created.installation.manifest.plugin_id}」`,
        kind: "success",
      });
      onSuccess?.(created);
      onClose();
    } catch (err) {
      setError(formatInferencePluginError(err));
    } finally {
      setSubmitting(false);
    }
  };

  const nameId = `${idPrefix}-name`;
  const urlId = `${idPrefix}-url`;
  const urlHelpId = `${idPrefix}-url-help`;
  const tokenId = `${idPrefix}-token`;
  const tokenHelpId = `${idPrefix}-token-help`;
  const sourceTierId = `${idPrefix}-source-tier`;
  const sourceTierHelpId = `${idPrefix}-source-tier-help`;
  const displayNameId = `${idPrefix}-display-name`;
  const descriptionId = `${idPrefix}-description`;
  const homepageId = `${idPrefix}-homepage`;
  const homepageHelpId = `${idPrefix}-homepage-help`;
  const publisherId = `${idPrefix}-publisher`;
  const licenseId = `${idPrefix}-license`;
  const assetShaId = `${idPrefix}-asset-sha256`;
  const assetShaHelpId = `${idPrefix}-asset-sha256-help`;
  const evalCheckboxId = `${idPrefix}-managed-eval`;
  const evalHelpId = `${idPrefix}-managed-eval-help`;

  return (
    <Modal
      open={open}
      onClose={handleClose}
      title="注册直连推理插件"
      width={620}
      footer={
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={handleClose} disabled={submitting}>
            取消
          </Button>
          <Button type="button" variant="primary" onClick={handleSubmit} disabled={submitting}>
            {submitting ? (
              <>
                <Icon name="loader2" size={12} className="spin" />
                正在探活并注册…
              </>
            ) : (
              "注册直连插件"
            )}
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-3.5">
        <div>
          <label htmlFor={nameId} className={LABEL_CLASS}>
            名称
          </label>
          <input
            id={nameId}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="例如 remote-detector-direct"
            maxLength={255}
            disabled={submitting}
            className={INPUT_CLASS}
          />
        </div>

        <div>
          <label htmlFor={urlId} className={LABEL_CLASS}>
            服务地址
          </label>
          <input
            id={urlId}
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="http://127.0.0.1:8090"
            maxLength={512}
            aria-describedby={urlHelpId}
            disabled={submitting}
            className={clsx(INPUT_CLASS, MONO_INPUT_CLASS)}
          />
          <p id={urlHelpId} className={HELP_CLASS}>
            须为 <span className="font-mono">http://</span> 或{" "}
            <span className="font-mono">https://</span>{" "}
            地址。私网或本机回环地址需先在服务端配置对应白名单；注册时会实时核验{" "}
            <span className="font-mono">/health</span> 与 <span className="font-mono">/setup</span>
            。
          </p>
        </div>

        <div>
          <label htmlFor={tokenId} className={LABEL_CLASS}>
            访问令牌
          </label>
          <input
            id={tokenId}
            type="password"
            autoComplete="new-password"
            value={authToken}
            onChange={(e) => setAuthToken(e.target.value)}
            placeholder="填写至少 32 位字符的 Bearer Token"
            aria-describedby={tokenHelpId}
            disabled={submitting}
            className={clsx(INPUT_CLASS, MONO_INPUT_CLASS)}
          />
          <p id={tokenHelpId} className={HELP_CLASS}>
            长度须不少于 32 个字符，由平台加密存管；提交失败时不会回显令牌内容。
          </p>
        </div>

        <div>
          <label htmlFor={sourceTierId} className={LABEL_CLASS}>
            来源层级
          </label>
          <select
            id={sourceTierId}
            value={sourceTier}
            onChange={(e) => setSourceTier(e.target.value as SourceTierValue)}
            aria-describedby={sourceTierHelpId}
            disabled={submitting}
            className={clsx(INPUT_CLASS, "cursor-pointer")}
          >
            <option value="third_party">第三方</option>
            <option value="official">官方</option>
          </select>
          <p id={sourceTierHelpId} className={HELP_CLASS}>
            由平台超管显式指定来源层级，不采信端点自报值，且不豁免鉴权、隔离或能力审批门禁。
          </p>
        </div>

        <div className="rounded-md border border-border bg-muted/30 p-3">
          <button
            type="button"
            aria-expanded={advancedOpen}
            onClick={() => setAdvancedOpen((prev) => !prev)}
            className="inline-flex w-full cursor-pointer appearance-none items-center gap-1.5 border-0 bg-transparent p-0 text-left text-xs font-medium text-foreground [font-family:inherit]"
          >
            <Icon name={advancedOpen ? "chevDown" : "chevRight"} size={13} />
            更多元数据与高级授权
          </button>

          {advancedOpen && (
            <div className="mt-3 flex flex-col gap-3">
              <div className="grid grid-cols-2 gap-3 max-sm:grid-cols-1">
                <div>
                  <label htmlFor={displayNameId} className={LABEL_CLASS}>
                    显示名称（可选）
                  </label>
                  <input
                    id={displayNameId}
                    value={displayName}
                    onChange={(e) => setDisplayName(e.target.value)}
                    placeholder="例如 Remote YOLO v11"
                    maxLength={64}
                    disabled={submitting}
                    className={INPUT_CLASS}
                  />
                </div>
                <div>
                  <label htmlFor={homepageId} className={LABEL_CLASS}>
                    主页地址（可选）
                  </label>
                  <input
                    id={homepageId}
                    value={homepage}
                    onChange={(e) => setHomepage(e.target.value)}
                    placeholder="https://example.com/plugin"
                    maxLength={255}
                    aria-describedby={homepageHelpId}
                    disabled={submitting}
                    className={clsx(INPUT_CLASS, MONO_INPUT_CLASS)}
                  />
                  <p id={homepageHelpId} className={HELP_CLASS}>
                    仅允许公开 https:// 地址。
                  </p>
                </div>
              </div>

              <div>
                <label htmlFor={descriptionId} className={LABEL_CLASS}>
                  简介（可选）
                </label>
                <textarea
                  id={descriptionId}
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="简要描述插件用途或运行环境"
                  maxLength={280}
                  rows={2}
                  disabled={submitting}
                  className={clsx(INPUT_CLASS, "resize-y")}
                />
              </div>

              <div className="grid grid-cols-2 gap-3 max-sm:grid-cols-1">
                <div>
                  <label htmlFor={publisherId} className={LABEL_CLASS}>
                    发布方（可选）
                  </label>
                  <input
                    id={publisherId}
                    value={publisher}
                    onChange={(e) => setPublisher(e.target.value)}
                    placeholder="留空按默认规则补齐"
                    maxLength={255}
                    disabled={submitting}
                    className={INPUT_CLASS}
                  />
                </div>
                <div>
                  <label htmlFor={licenseId} className={LABEL_CLASS}>
                    许可证（可选）
                  </label>
                  <input
                    id={licenseId}
                    value={license}
                    onChange={(e) => setLicense(e.target.value)}
                    placeholder="例如 Apache-2.0"
                    maxLength={255}
                    disabled={submitting}
                    className={INPUT_CLASS}
                  />
                </div>
              </div>

              <div>
                <label htmlFor={assetShaId} className={LABEL_CLASS}>
                  资产清单 SHA-256（可选）
                </label>
                <input
                  id={assetShaId}
                  value={assetManifestSha256}
                  onChange={(e) => setAssetManifestSha256(e.target.value)}
                  placeholder="64 位小写十六进制摘要（外部管理权重时留空）"
                  maxLength={64}
                  aria-describedby={assetShaHelpId}
                  disabled={submitting}
                  className={clsx(INPUT_CLASS, MONO_INPUT_CLASS)}
                />
                <p id={assetShaHelpId} className={HELP_CLASS}>
                  仅当需要为直连插件绑定资产审批清单时填写；外部托管权重保持留空即可。
                </p>
              </div>

              <fieldset className="flex flex-col gap-2.5 rounded-md border border-border bg-card p-3">
                <legend className="px-1 text-xs font-medium text-foreground">
                  受控模型兼容性授权
                </legend>
                <p className="text-xs leading-normal text-muted-foreground">
                  发现不等于资格。仅当需要允许该直连插件承接平台受控模型部署时，在此显式批准与端点{" "}
                  <span className="font-mono">/setup</span> 声明相交的兼容条目。
                </p>

                {compatRows.length > 0 && (
                  <div className="flex flex-col gap-2">
                    {compatRows.map((row, index) => {
                      const fwId = `${idPrefix}-compat-fw-${row.rowKey}`;
                      const fmtId = `${idPrefix}-compat-fmt-${row.rowKey}`;
                      const devId = `${idPrefix}-compat-dev-${row.rowKey}`;
                      return (
                        <div
                          key={row.rowKey}
                          className="grid grid-cols-[1fr_1fr_110px_auto] items-end gap-2 max-sm:grid-cols-1"
                        >
                          <div>
                            <label htmlFor={fwId} className={LABEL_CLASS}>
                              框架 #{index + 1}
                            </label>
                            <input
                              id={fwId}
                              value={row.framework}
                              onChange={(e) =>
                                updateCompatRow(row.rowKey, { framework: e.target.value })
                              }
                              placeholder="例如 libreyolo"
                              maxLength={64}
                              disabled={submitting}
                              className={INPUT_CLASS}
                            />
                          </div>
                          <div>
                            <label htmlFor={fmtId} className={LABEL_CLASS}>
                              格式（可空）
                            </label>
                            <input
                              id={fmtId}
                              value={row.format}
                              onChange={(e) =>
                                updateCompatRow(row.rowKey, { format: e.target.value })
                              }
                              placeholder="原生权重留空，如 onnx"
                              maxLength={64}
                              disabled={submitting}
                              className={INPUT_CLASS}
                            />
                          </div>
                          <div>
                            <label htmlFor={devId} className={LABEL_CLASS}>
                              运行设备
                            </label>
                            <select
                              id={devId}
                              value={row.runtime_device}
                              onChange={(e) =>
                                updateCompatRow(row.rowKey, {
                                  runtime_device: e.target.value as "cpu" | "cuda",
                                })
                              }
                              disabled={submitting}
                              className={clsx(INPUT_CLASS, "cursor-pointer")}
                            >
                              <option value="cpu">CPU</option>
                              <option value="cuda">CUDA</option>
                            </select>
                          </div>
                          <Button
                            type="button"
                            size="xs"
                            variant="ghost"
                            aria-label={`删除兼容项 ${index + 1}`}
                            onClick={() => removeCompatRow(row.rowKey)}
                            disabled={submitting}
                            className="h-9 text-muted-foreground hover:text-status-danger"
                          >
                            <Icon name="trash" size={13} />
                          </Button>
                        </div>
                      );
                    })}
                  </div>
                )}

                <div>
                  <Button
                    type="button"
                    size="xs"
                    variant="default"
                    onClick={addCompatRow}
                    disabled={submitting}
                  >
                    <Icon name="plus" size={11} />
                    添加受控模型兼容项
                  </Button>
                </div>

                <div className="border-t border-border pt-2.5">
                  <div className="flex items-start gap-2">
                    <Checkbox
                      id={evalCheckboxId}
                      checked={approvedManagedEvaluation}
                      disabled={submitting || !hasValidCompatibility}
                      aria-describedby={evalHelpId}
                      onCheckedChange={(checked) => setApprovedManagedEvaluation(checked === true)}
                      className="mt-0.5"
                    />
                    <div className="flex flex-col">
                      <label
                        htmlFor={evalCheckboxId}
                        className={clsx(
                          "text-sm font-medium",
                          hasValidCompatibility
                            ? "cursor-pointer text-foreground"
                            : "cursor-not-allowed text-muted-foreground",
                        )}
                      >
                        批准受控评估 (managed_evaluation)
                      </label>
                      <p id={evalHelpId} className={HELP_CLASS}>
                        仅在至少填写一条受控模型兼容项且端点 /setup 声明支持受控评估时可启用。
                      </p>
                    </div>
                  </div>
                </div>
              </fieldset>
            </div>
          )}
        </div>

        {error && (
          <div
            role="alert"
            className="flex items-start gap-1.5 rounded-md border border-status-danger bg-status-danger-soft px-3 py-2 text-xs text-status-danger"
          >
            <Icon name="warning" size={13} className="mt-0.5 shrink-0" />
            <span className="min-w-0 flex-1">
              <CopyableError message={error} />
            </span>
          </div>
        )}
      </div>
    </Modal>
  );
}
