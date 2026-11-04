// v0.10.18 · CreateProjectWizard 第 3 步: 属性 schema 按工具单位独立.
// 从 CreateProjectWizard.tsx 抽出.

import { AttributeSchemaEditor } from "@/pages/Projects/sections/AttributeSchemaEditor";
import type { AttributeField } from "@/api/projects";
import type { FormState } from "../CreateProjectWizard";
import { UnitTabs } from "./UnitTabs";
import styles from "../CreateProjectWizard.module.css";

export function Step3AttributesPerUnit({
  form,
  setForm,
  error,
}: {
  form: FormState;
  setForm: React.Dispatch<React.SetStateAction<FormState>>;
  error: string | null;
}) {
  const activeBinding = form.unitBindings[form.activeUnit];
  const fields = activeBinding?.attributeFields ?? [];
  const onChange = (next: AttributeField[]) => {
    setForm((s) => ({
      ...s,
      unitBindings: {
        ...s.unitBindings,
        [s.activeUnit]: {
          enabled: s.unitBindings[s.activeUnit]?.enabled ?? true,
          classRows: s.unitBindings[s.activeUnit]?.classRows ?? [],
          attributeFields: next,
        },
      },
    }));
  };
  return (
    <div className={styles.formStack}>
      <div className={styles.sectionHintTall}>
        属性按工具单位独立配置。例如矩形框可设置「朝向 / 遮挡」，区域可设置「面积估值」， 互不影响。
      </div>
      <UnitTabs form={form} setForm={setForm} />
      {!activeBinding?.enabled ? (
        <div className={styles.sectionHint}>
          当前工具单位未启用。回到第 1 步「工具集」勾选后再来配置。
        </div>
      ) : (
        <AttributeSchemaEditor
          value={fields}
          onChange={onChange}
          emptyHint="暂无属性（可跳过，后续在项目设置中添加）"
        />
      )}
      {error && <div className={styles.schemaError}>{error}</div>}
    </div>
  );
}
