import { useId, useState } from "react";
import styles from "./ProjectDetailPanel.module.css";

export function ChipMultiSelect({
  options,
  selected,
  onChange,
  conflictKeys,
  emptyHint,
  allowFreeText = false,
  freeTextPlaceholder = "输入内容添加",
}: {
  options: Array<{ value: string; label: string }>;
  selected: Set<string>;
  onChange: (next: Set<string>) => void;
  conflictKeys?: Set<string>;
  emptyHint?: string;
  allowFreeText?: boolean;
  freeTextPlaceholder?: string;
}) {
  const listId = useId();
  const [draft, setDraft] = useState("");
  if (options.length === 0 && !allowFreeText) {
    return <span className={styles.mutedText}>{emptyHint ?? "无可选项"}</span>;
  }
  const toggle = (v: string) => {
    const next = new Set(selected);
    if (next.has(v)) next.delete(v);
    else next.add(v);
    onChange(next);
  };
  const add = (raw: string) => {
    const v = raw.trim();
    if (!v) return;
    const next = new Set(selected);
    next.add(v);
    onChange(next);
    setDraft("");
  };
  const optionValues = new Set(options.map((o) => o.value));
  const extraSelected = allowFreeText
    ? Array.from(selected)
        .filter((v) => !optionValues.has(v))
        .map((v) => ({ value: v, label: v }))
    : [];
  const allChips = [...options, ...extraSelected];
  return (
    <>
      {allowFreeText && (
        <div className={styles.presetRow}>
          <input
            className={styles.textInput}
            type="text"
            list={listId}
            value={draft}
            placeholder={freeTextPlaceholder}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                add(draft);
              }
            }}
          />
          <datalist id={listId}>
            {options.map((o) => (
              <option key={o.value} value={o.value} />
            ))}
          </datalist>
          <button
            type="button"
            className={styles.presetButton}
            disabled={!draft.trim()}
            onClick={() => add(draft)}
            title="添加"
          >
            添加
          </button>
        </div>
      )}
      <div className={styles.aliasList}>
        {allChips.map((o) => {
          const active = selected.has(o.value);
          const conflict = active && conflictKeys?.has(o.value);
          const cls = [
            styles.aliasChip,
            active && styles.aliasChipActive,
            conflict && styles.aliasChipConflict,
          ]
            .filter(Boolean)
            .join(" ");
          return (
            <button
              key={o.value}
              type="button"
              onClick={() => toggle(o.value)}
              className={cls}
              title={conflict ? `属性键 ${o.value} 与其它并行阶段冲突` : o.label}
            >
              <span>
                {active ? "✓ " : ""}
                {o.label}
              </span>
            </button>
          );
        })}
        {selected.size > 0 && (
          <button
            type="button"
            onClick={() => onChange(new Set())}
            className={styles.refillButton}
            title="清空选择"
          >
            清空
          </button>
        )}
      </div>
    </>
  );
}
