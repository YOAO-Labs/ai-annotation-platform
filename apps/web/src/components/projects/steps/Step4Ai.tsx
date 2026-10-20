// v0.10.18 · CreateProjectWizard 第 4 步: AI 接入 (启用开关 + backend 复用).
// 从 CreateProjectWizard.tsx 抽出. 含本步专用的 BackendSourceSelect 子组件.

import { clsx } from "clsx";
import { useQuery } from "@tanstack/react-query";
import { Icon } from "@/components/ui/Icon";
import { adminMlIntegrationsApi } from "@/api/adminMlIntegrations";
import type { FormState } from "../CreateProjectWizard";
import styles from "../CreateProjectWizard.module.css";

export function Step4Ai({
  form,
  setForm,
}: {
  form: FormState;
  setForm: React.Dispatch<React.SetStateAction<FormState>>;
}) {
  return (
    <div className={styles.formStackAi}>
      <label className={clsx(styles.aiToggle, form.aiEnabled && styles.aiToggleEnabled)}>
        <input
          type="checkbox"
          checked={form.aiEnabled}
          onChange={(e) => setForm((s) => ({ ...s, aiEnabled: e.target.checked }))}
          className={styles.aiCheckbox}
        />
        <Icon name="sparkles" size={14} className={styles.aiIcon} />
        <div className={styles.aiToggleText}>
          <span className={styles.aiToggleTitle}>启用 AI 预标注</span>
          <span className={styles.aiToggleHint}>创建后可在项目内挂接真实 ML Backend 推理服务</span>
        </div>
      </label>

      {form.aiEnabled && (
        <>
          {/* v0.9.7 · 复用现有 backend dropdown — 让新项目立即可用 AI */}
          {/* v0.10.37 · 按项目 data_type 标注 backend 模态匹配 (epic 阶段 1) */}
          <BackendSourceSelect
            value={form.mlBackendSourceId}
            dataType={form.dataType}
            onChange={(v) => setForm((s) => ({ ...s, mlBackendSourceId: v }))}
          />

          <div className={styles.aiHelpBox}>
            选「复用 backend」后, 项目创建时会自动复制 backend 配置到新项目, 无需再回设置页注册.
          </div>
        </>
      )}
    </div>
  );
}

/** v0.9.7 · Wizard step 4 复用 backend 下拉. 拉 /admin/ml-integrations/all */
function BackendSourceSelect({
  value,
  dataType,
  onChange,
}: {
  value: string;
  // v0.10.37 · 项目媒体维度, 用于标注 backend 模态匹配
  dataType: string;
  onChange: (v: string) => void;
}) {
  const q = useQuery({
    queryKey: ["admin", "ml-integrations", "all"],
    queryFn: () => adminMlIntegrationsApi.listAll(),
    staleTime: 1000 * 60 * 5,
  });
  const items = q.data?.items ?? [];
  const selected = items.find((b) => b.id === value);

  // v0.10.37 · 由 backend 能力快照派生模态; 未健康检查过 (modalities 空) 视为「未知」, 不标不匹配。
  const modalityHint = (b: (typeof items)[number]): string => {
    const mods = b.health_meta?.capabilities?.modalities;
    if (!mods || mods.length === 0) return "模态未知";
    if (dataType !== "image" && dataType !== "video") return "";
    return mods.includes(dataType) ? "" : "⚠ 不支持本项目模态";
  };
  const selectedHint = selected ? modalityHint(selected) : "";

  return (
    <div>
      <label className={styles.label}>
        ML Backend <span className={styles.labelNote}>（可选, 复用其它项目已注册的 backend）</span>
      </label>
      {q.isLoading ? (
        <div className={clsx(styles.input, styles.readonlyInput)}>加载中…</div>
      ) : items.length === 0 ? (
        <div className={clsx(styles.input, styles.readonlyInput)}>
          系统内尚无已注册 backend; 项目创建后到设置页注册一个.
        </div>
      ) : (
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className={clsx(styles.input, styles.selectInput)}
        >
          <option value="">暂不绑定</option>
          {items.map((b) => {
            const hint = modalityHint(b);
            return (
              <option key={b.id} value={b.id}>
                {b.name} ({b.url}) · {b.state}
                {hint ? ` · ${hint}` : ""} · 来源: {b.source_project_name}
              </option>
            );
          })}
        </select>
      )}
      {!selected && items.length > 0 && (
        <p className={styles.helpText}>创建项目后，可到项目设置注册 ML Backend。</p>
      )}
      {selected && (
        <div className={styles.helpText}>
          将复制 {selected.name} ({selected.url}) 到新项目, 含 auth 配置, state 重置为 disconnected.
          {selectedHint ? ` ${selectedHint}（仍可选, 绑定时后端会按模态二次校验）。` : ""}
        </div>
      )}
    </div>
  );
}
