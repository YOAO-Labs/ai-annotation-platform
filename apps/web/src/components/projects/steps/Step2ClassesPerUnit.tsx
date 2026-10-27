// v0.10.18 · CreateProjectWizard 第 2 步: 类别按工具单位独立配置.
// 从 CreateProjectWizard.tsx 抽出.

import { ClassEditor, type ClassRow } from "@/pages/Projects/sections/ClassEditor";
import type { FormState } from "../CreateProjectWizard";
import { UnitTabs } from "./UnitTabs";
import styles from "../CreateProjectWizard.module.css";

export function Step2ClassesPerUnit({
  form,
  setForm,
}: {
  form: FormState;
  setForm: React.Dispatch<React.SetStateAction<FormState>>;
}) {
  const activeBinding = form.unitBindings[form.activeUnit];
  const rows = activeBinding?.classRows ?? [];
  const onChange = (next: ClassRow[]) => {
    setForm((s) => ({
      ...s,
      unitBindings: {
        ...s.unitBindings,
        [s.activeUnit]: {
          enabled: s.unitBindings[s.activeUnit]?.enabled ?? true,
          classRows: next,
          attributeFields: s.unitBindings[s.activeUnit]?.attributeFields ?? [],
        },
      },
    }));
  };
  return (
    <div className={styles.classesStep}>
      <div className={styles.sectionHint}>
        类别按工具单位独立配置。切换下方 Tab 给每个启用的工具单位单独维护类别。 强隔离:
        不同工具的同名类是独立记录, 可以同名不同色。
      </div>
      <UnitTabs form={form} setForm={setForm} />
      {!activeBinding?.enabled ? (
        <div className={styles.sectionHint}>
          当前工具单位未启用。回到第 1 步「工具集」勾选后再来配置。
        </div>
      ) : (
        <ClassEditor
          className="min-h-0"
          value={rows}
          onChange={onChange}
          max={50}
          emptyHint="暂无类别（后续可在项目设置中添加）"
        />
      )}
    </div>
  );
}
