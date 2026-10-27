/**
 * v0.14.16 · 推理参数命名预设行.
 *
 * 下拉选已存预设一键套用 + "存为预设"(内联输入名) + 删除当前选中预设.
 * 预设的存取由 useAiParamPresets (localStorage, 按 backend×task 分桶) 负责; 本组件只管 UI.
 */
import { useState } from "react";
import { Icon } from "@/components/ui/Icon";
import type { AiParamPreset } from "../utils/useAiParamPresets";
import styles from "./ProjectDetailPanel.module.css";

interface Props {
  presets: AiParamPreset[];
  /** 套用某预设 (写回面板 paramsValue). */
  onApply: (preset: AiParamPreset) => void;
  /** 存当前 (variant + params) 为命名预设; 同名覆盖. */
  onSave: (name: string) => void;
  onRemove: (id: string) => void;
  /** 无 backend / 能力未就位时禁用. */
  disabled?: boolean;
}

export function PresetRow({ presets, onApply, onSave, onRemove, disabled }: Props) {
  const [selectedId, setSelectedId] = useState<string>("");
  const [naming, setNaming] = useState(false);
  const [name, setName] = useState("");

  const selected = presets.find((p) => p.id === selectedId) ?? null;

  const commitSave = () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    onSave(trimmed);
    setName("");
    setNaming(false);
  };

  return (
    <div className={styles.field}>
      <span className={styles.fieldLabel}>参数预设（variant + 参数，按 backend / 任务记忆）</span>
      <div className={styles.presetRow}>
        <select
          className={styles.presetSelect}
          value={selected?.id ?? ""}
          disabled={disabled || presets.length === 0}
          onChange={(e) => {
            const id = e.target.value;
            setSelectedId(id);
            const p = presets.find((x) => x.id === id);
            if (p) onApply(p);
          }}
        >
          <option value="" disabled hidden>
            {presets.length === 0 ? "暂无预设" : "选择预设…"}
          </option>
          {presets.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>

        {naming ? (
          <>
            <input
              className={styles.presetNameInput}
              value={name}
              autoFocus
              placeholder="预设名"
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitSave();
                if (e.key === "Escape") {
                  setNaming(false);
                  setName("");
                }
              }}
            />
            <button
              type="button"
              className={styles.presetButton}
              disabled={!name.trim()}
              onClick={commitSave}
            >
              保存
            </button>
            <button
              type="button"
              className={styles.presetGhostButton}
              onClick={() => {
                setNaming(false);
                setName("");
              }}
            >
              取消
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              className={styles.presetButton}
              disabled={disabled}
              onClick={() => setNaming(true)}
              title="把当前 variant + 参数存为命名预设"
            >
              <Icon name="plus" size={11} /> 存为预设
            </button>
            {selected && (
              <button
                type="button"
                className={styles.presetGhostButton}
                onClick={() => {
                  onRemove(selected.id);
                  setSelectedId("");
                }}
                title={`删除预设「${selected.name}」`}
              >
                <Icon name="trash" size={11} /> 删除
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}
