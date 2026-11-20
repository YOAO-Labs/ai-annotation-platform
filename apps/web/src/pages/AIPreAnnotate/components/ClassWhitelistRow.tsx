/**
 * YOLO 类别白名单勾选 ([index]类名).
 *
 * 闭集检测器 (YOLO) 暴露模型原生类别表 (model.names) 后, 让用户勾选只检出哪些类 (留空=全部).
 * 平台不做"模型类→项目标签"映射 (NG6): 预标结果仍渲染模型原生类名, 采纳时由人选项目标签.
 * 类别表改由 backend 静态自报 (COCO80/DOTA15/person, 见各 backend), 免预热、切模型即在;
 *   再补一个文本输入框 (datalist 自动补全) 按类名快速勾选 —— 类别多时不用在长 chip 列里找。
 *   仍 index 制 (闭集只能选模型认识的类), 输入命中类名才落选。
 */
import { useMemo, useState } from "react";

import styles from "./ProjectDetailPanel.module.css";

const MAX_SUGGESTIONS = 8;

function cx(...names: Array<string | false | null | undefined>) {
  return names.filter(Boolean).join(" ");
}

interface Props {
  /** 模型原生类别表; undefined/空 = 尚未就位 (模型未加载过). */
  classes: { index: number; name: string }[] | undefined;
  selected: Set<number>;
  onChange: (next: Set<number>) => void;
  /** 手动预热该 task 以加载类别表 (model.names); 想按类筛选才需要, 默认全标无需预热. */
  onWarm?: () => void;
  warming?: boolean;
}

export function ClassWhitelistRow({ classes, selected, onChange, onWarm, warming }: Props) {
  const [draft, setDraft] = useState("");
  const [open, setOpen] = useState(false);

  // 输入子串 (大小写不敏感) 过滤候选; 空草稿不展开 (避免 80 类全列出来「夸张」)。截断 MAX。
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
  // Enter: 选中首个匹配 (子串命中即可, 不再要求精确等值)。
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
      {/* 文本输入: 按类名快速勾选, 自定义下拉 (原生 datalist 弹层字号控不住、80 类时过大)。 */}
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
            // 延迟关闭, 让下拉项的 mousedown/click 先落地。
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
