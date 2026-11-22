import { clsx } from "clsx";
import { Icon } from "@/components/ui/Icon";
import { TOOL_UNIT_GROUPS, PROJECT_DATA_TYPES, type ToolUnitId } from "@/constants/toolUnits";
import { defaultUnitBindings, type FormState } from "../CreateProjectWizard";
import styles from "../CreateProjectWizard.module.css";

export function Step1DataTypeAndTools({
  form,
  setForm,
  nameValid,
  dueValid,
}: {
  form: FormState;
  setForm: React.Dispatch<React.SetStateAction<FormState>>;
  nameValid: boolean;
  dueValid: boolean;
}) {
  return (
    <div className={styles.formStackLarge}>
      <div>
        <label className={styles.label}>项目名称</label>
        <input
          value={form.name}
          onChange={(e) => setForm((s) => ({ ...s, name: e.target.value }))}
          placeholder="如:智能门店货架商品检测"
          maxLength={60}
          className={clsx(styles.input, !nameValid && styles.inputInvalid)}
        />
        {!nameValid && <div className={styles.fieldError}>名称需 2-60 字符</div>}
      </div>

      <div>
        <label className={styles.label}>数据类型</label>
        <div className={styles.typeGrid}>
          {PROJECT_DATA_TYPES.map((t) => {
            const active = t.id === form.dataType;
            return (
              <button
                key={t.id}
                type="button"
                onClick={() =>
                  setForm((s) => {
                    const next = defaultUnitBindings(t.legacyTypeKey);
                    for (const k of Object.keys(next) as ToolUnitId[]) {
                      const prev = s.unitBindings[k];
                      if (prev) next[k] = { ...next[k]!, ...prev };
                    }
                    const stillEnabled = (Object.keys(next) as ToolUnitId[]).find(
                      (k) => next[k]?.enabled,
                    );
                    return {
                      ...s,
                      dataType: t.id,
                      typeKey: t.legacyTypeKey,
                      unitBindings: next,
                      activeUnit: stillEnabled ?? "bbox",
                      sceneMode: t.id === "video" ? false : s.sceneMode,
                      splitStrategy:
                        t.id === "video" && s.splitStrategy === "by_scene"
                          ? "none"
                          : s.splitStrategy,
                    };
                  })
                }
                className={clsx(styles.typeButton, active && styles.typeButtonActive)}
              >
                <span className={clsx(styles.typeIcon, active && styles.typeIconActive)}>
                  <Icon name={t.icon} size={14} />
                </span>
                <span className={styles.typeBody}>
                  <span className={styles.typeLabel}>{t.label}</span>
                  <span className={styles.typeHint}>{t.hint}</span>
                </span>
              </button>
            );
          })}
        </div>
      </div>

      {form.dataType !== "video" && (
        <label className={styles.unitChip}>
          <input
            type="checkbox"
            checked={form.sceneMode}
            onChange={(e) =>
              setForm((s) => ({
                ...s,
                sceneMode: e.target.checked,
                splitStrategy: e.target.checked
                  ? "by_scene"
                  : s.splitStrategy === "by_scene"
                    ? "none"
                    : s.splitStrategy,
              }))
            }
          />
          <Icon name="layers" size={12} />
          <div className={styles.unitChipBody}>
            <span className={styles.unitChipLabel}>scene 模式</span>
            <span className={styles.unitChipHint}>时序数据按 scene 关联、分包和连续标注</span>
          </div>
        </label>
      )}

      <div>
        <label className={styles.label}>
          工具集{" "}
          <span className={styles.labelNote}>
            （勾选本项目要用的工具单位，每个单位有独立的类别与属性）
          </span>
        </label>
        <div className={styles.unitChipGrid}>
          {TOOL_UNIT_GROUPS.map((g) => {
            const binding = form.unitBindings[g.id];
            if (!binding) return null;
            const disabled = !g.available;
            return (
              <label
                key={g.id}
                className={clsx(
                  styles.unitChip,
                  binding.enabled && styles.unitChipActive,
                  disabled && styles.unitChipDisabled,
                )}
                title={disabled ? "本版未实现, 后续版本上线" : g.hint}
              >
                <input
                  type="checkbox"
                  checked={!!binding.enabled}
                  disabled={disabled}
                  onChange={(e) =>
                    setForm((s) => ({
                      ...s,
                      unitBindings: {
                        ...s.unitBindings,
                        [g.id]: {
                          enabled: e.target.checked,
                          classRows: s.unitBindings[g.id]?.classRows ?? [],
                          attributeFields: s.unitBindings[g.id]?.attributeFields ?? [],
                        },
                      },
                    }))
                  }
                />
                <Icon name={g.icon} size={12} />
                <div className={styles.unitChipBody}>
                  <span className={styles.unitChipLabel}>{g.label}</span>
                  <span className={styles.unitChipHint}>{g.hint}</span>
                </div>
              </label>
            );
          })}
        </div>
      </div>

      <div>
        <label className={styles.label}>截止日期（可空）</label>
        <input
          type="date"
          value={form.dueDate}
          onChange={(e) => setForm((s) => ({ ...s, dueDate: e.target.value }))}
          className={clsx(styles.input, !dueValid && styles.inputInvalid)}
        />
        {!dueValid && <div className={styles.fieldError}>截止日期不能早于今天</div>}
      </div>
    </div>
  );
}
