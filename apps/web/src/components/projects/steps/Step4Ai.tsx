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
          <span className={styles.aiToggleHint}>创建后可在项目内接入模型服务</span>
        </div>
      </label>

      {form.aiEnabled && (
        <>
          <BackendSourceSelect
            value={form.mlBackendSourceId}
            dataType={form.dataType}
            onChange={(v) => setForm((s) => ({ ...s, mlBackendSourceId: v }))}
          />

          <div className={styles.aiHelpBox}>
            选择已有模型服务后，新项目会启用同一服务并复用其服务池绑定，不会复制服务配置。
          </div>
        </>
      )}
    </div>
  );
}

function BackendSourceSelect({
  value,
  dataType,
  onChange,
}: {
  value: string;
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
        模型服务 <span className={styles.labelNote}>（可选，复用其他项目已注册的服务）</span>
      </label>
      {q.isLoading ? (
        <div className={clsx(styles.input, styles.readonlyInput)}>加载中…</div>
      ) : items.length === 0 ? (
        <div className={clsx(styles.input, styles.readonlyInput)}>
          系统内尚无已注册的模型服务；项目创建后可在设置页接入。
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
        <p className={styles.helpText}>创建项目后，可到项目设置启用模型服务。</p>
      )}
      {selected && (
        <div className={styles.helpText}>
          新项目将启用 {selected.name} ({selected.url})，与原项目复用同一服务及服务池绑定。
          {selectedHint ? ` ${selectedHint}（仍可选，绑定时会再次校验模态）。` : ""}
        </div>
      )}
    </div>
  );
}
