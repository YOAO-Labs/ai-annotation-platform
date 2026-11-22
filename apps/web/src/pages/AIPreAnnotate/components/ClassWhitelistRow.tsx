import { useMemo, useState } from "react";

import styles from "./ProjectDetailPanel.module.css";

const MAX_SUGGESTIONS = 8;

function cx(...names: Array<string | false | null | undefined>) {
  return names.filter(Boolean).join(" ");
}

interface Props {
  classes: { index: number; name: string }[] | undefined;
  selected: Set<number>;
  onChange: (next: Set<number>) => void;
  onWarm?: () => void;
  warming?: boolean;
}

export function ClassWhitelistRow({ classes, selected, onChange, onWarm, warming }: Props) {
  const [draft, setDraft] = useState("");
  const [open, setOpen] = useState(false);

  const suggestions = useMemo(() => {
    const q = draft.trim().toLowerCase();
    if (!q) return [];
    return (classes ?? [])
      .filter((c) => c.name.toLowerCase().includes(q))
      .slice(0, MAX_SUGGESTIONS);
  }, [classes, draft]);

  const pick = (idx: number) => {
    const next = new Set(selected);
    next.add(idx);
    onChange(next);
    setDraft("");
    setOpen(false);
  };
  const addByName = () => {
    if (suggestions.length > 0) pick(suggestions[0].index);
  };

  if (!classes || classes.length === 0) {
    return (
      <div className={styles.field}>
        <span className={styles.fieldLabel}>类别筛选</span>
        <div className={styles.presetRow}>
          <span className={styles.mutedText}>
            当前将检出全部类别。如需只标部分类别，先预热加载类别表。
          </span>
          {onWarm && (
            <button
              type="button"
              className={styles.presetButton}
              disabled={warming}
              onClick={onWarm}
              title="加载该模型的类别表 (model.names), 之后可勾选类别白名单"
            >
              {warming ? "预热中…（首次约 5-15s）" : "预热以加载类别"}
            </button>
          )}
        </div>
      </div>
    );
  }

  const toggle = (idx: number) => {
    const next = new Set(selected);
    if (next.has(idx)) next.delete(idx);
    else next.add(idx);
    onChange(next);
  };

  return (
    <div className={styles.field}>
      <span className={styles.fieldLabel}>
        类别筛选（可选，留空=检出全部 {classes.length} 类
        {selected.size > 0 ? `；已选 ${selected.size}` : ""}）
      </span>
      <div className={styles.presetRow}>
        <div className={styles.combo}>
          <input
            className={styles.textInput}
            type="text"
            value={draft}
            placeholder="输入类名快速勾选，如 person"
            onChange={(e) => {
              setDraft(e.target.value);
              setOpen(true);
            }}
            onFocus={() => setOpen(true)}
            onBlur={() => setTimeout(() => setOpen(false), 120)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                addByName();
              } else if (e.key === "Escape") {
                setOpen(false);
              }
            }}
          />
          {open && suggestions.length > 0 && (
            <div className={styles.comboList}>
              {suggestions.map((c) => (
                <button
                  key={c.index}
                  type="button"
                  className={styles.comboOption}
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => pick(c.index)}
                  title={`勾选 [${c.index}] ${c.name}`}
                >
                  <span>
                    {selected.has(c.index) ? "✓ " : ""}
                    {c.name}
                  </span>
                  <span className={styles.comboOptionIdx}>[{c.index}]</span>
                </button>
              ))}
            </div>
          )}
        </div>
        <button
          type="button"
          className={styles.presetButton}
          disabled={suggestions.length === 0}
          onClick={addByName}
          title="按类名勾选"
        >
          添加
        </button>
      </div>
      <div className={styles.aliasList}>
        {classes.map((c) => {
          const active = selected.has(c.index);
          return (
            <button
              key={c.index}
              type="button"
              onClick={() => toggle(c.index)}
              className={cx(styles.aliasChip, active && styles.aliasChipActive)}
              title={`类别 [${c.index}] ${c.name}`}
            >
              <span>
                {active ? "✓ " : ""}
                {c.name}
              </span>
              <span className={styles.aliasName}>[{c.index}]</span>
            </button>
          );
        })}
        {selected.size > 0 && (
          <button
            type="button"
            onClick={() => onChange(new Set())}
            className={styles.refillButton}
            title="清空选择 (恢复检出全部类别)"
          >
            清空
          </button>
        )}
      </div>
    </div>
  );
}
