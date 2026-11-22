import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router-dom";
import { clsx } from "clsx";
import { Avatar } from "@/components/ui/Avatar";
import { Badge } from "@/components/ui/Badge";
import { useGlobalSearch } from "@/hooks/useGlobalSearch";
import styles from "./CommandPalette.module.css";

interface Props {
  open: boolean;
  onClose: () => void;
}

type FlatItem =
  | { kind: "project"; id: string; label: string; sublabel: string; href: string }
  | { kind: "task"; id: string; label: string; sublabel: string; href: string }
  | { kind: "dataset"; id: string; label: string; sublabel: string; href: string }
  | { kind: "member"; id: string; label: string; sublabel: string; href: string };

const KIND_LABEL: Record<FlatItem["kind"], string> = {
  project: "项目",
  task: "任务",
  dataset: "数据集",
  member: "成员",
};

export function CommandPalette({ open, onClose }: Props) {
  const navigate = useNavigate();
  const [input, setInput] = useState("");
  const [activeIdx, setActiveIdx] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const { data, isLoading, debounced } = useGlobalSearch(input);

  useEffect(() => {
    if (open) {
      setInput("");
      setActiveIdx(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  const items: FlatItem[] = useMemo(() => {
    const out: FlatItem[] = [];
    for (const p of data.projects) {
      out.push({
        kind: "project",
        id: p.id,
        label: p.name,
        sublabel: `${p.display_id} · ${p.type_label}`,
        href: `/projects/${p.id}`,
      });
    }
    for (const t of data.tasks) {
      out.push({
        kind: "task",
        id: t.id,
        label: t.file_name,
        sublabel: `${t.display_id} · ${t.project_name}`,
        href: `/projects/${t.project_id}/annotate?task=${t.id}`,
      });
    }
    for (const d of data.datasets) {
      out.push({
        kind: "dataset",
        id: d.id,
        label: d.name,
        sublabel: d.data_type,
        href: `/datasets/${d.id}`,
      });
    }
    for (const m of data.members) {
      out.push({
        kind: "member",
        id: m.id,
        label: m.name,
        sublabel: `${m.role} · ${m.email}`,
        href: `/users?focus=${m.id}`,
      });
    }
    return out;
  }, [data]);

  useEffect(() => {
    if (activeIdx >= items.length) setActiveIdx(0);
  }, [items.length, activeIdx]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        setActiveIdx((i) => Math.min(items.length - 1, i + 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActiveIdx((i) => Math.max(0, i - 1));
      } else if (e.key === "Enter") {
        e.preventDefault();
        const target = items[activeIdx];
        if (target) {
          navigate(target.href);
          onClose();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, items, activeIdx, navigate, onClose]);

  if (!open) return null;

  const grouped: { kind: FlatItem["kind"]; rows: { item: FlatItem; flatIdx: number }[] }[] = [];
  let flatIdx = 0;
  for (const kind of ["project", "task", "dataset", "member"] as const) {
    const rows: { item: FlatItem; flatIdx: number }[] = [];
    for (const it of items) {
      if (it.kind === kind) {
        rows.push({ item: it, flatIdx });
        flatIdx += 1;
      }
    }
    if (rows.length > 0) grouped.push({ kind, rows });
  }

  return createPortal(
    <div onClick={onClose} className={styles.backdrop}>
      <div
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        className={styles.dialog}
      >
        <input
          ref={inputRef}
          type="text"
          value={input}
          onChange={(e) => {
            setInput(e.target.value);
            setActiveIdx(0);
          }}
          placeholder="搜索项目、任务、数据集、成员..."
          className={styles.input}
        />

        <div className={styles.results}>
          {!debounced && <div className={styles.empty}>开始输入以搜索</div>}
          {debounced && isLoading && items.length === 0 && (
            <div className={styles.empty}>搜索中…</div>
          )}
          {debounced && !isLoading && items.length === 0 && (
            <div className={styles.empty}>没有找到结果</div>
          )}

          {grouped.map((g) => (
            <div key={g.kind}>
              <div className={styles.groupLabel}>
                {KIND_LABEL[g.kind]} ({g.rows.length})
              </div>
              {g.rows.map(({ item, flatIdx: idx }) => {
                const active = idx === activeIdx;
                return (
                  <button
                    key={`${item.kind}-${item.id}`}
                    type="button"
                    onMouseEnter={() => setActiveIdx(idx)}
                    onClick={() => {
                      navigate(item.href);
                      onClose();
                    }}
                    className={clsx(styles.resultButton, active && styles.resultButtonActive)}
                  >
                    {item.kind === "member" ? (
                      <Avatar size="sm" initial={(item.label || "?").slice(0, 1).toUpperCase()} />
                    ) : (
                      <span className={styles.kindBadge}>
                        <Badge variant="outline">{KIND_LABEL[item.kind]}</Badge>
                      </span>
                    )}
                    <span className={styles.itemText}>
                      <span className={styles.itemLabel}>{item.label}</span>
                      <span className={styles.itemSublabel}>{item.sublabel}</span>
                    </span>
                  </button>
                );
              })}
            </div>
          ))}
        </div>

        <div className={styles.footer}>
          <span>↑↓ 选择 · ↵ 跳转</span>
          <span>Esc 关闭</span>
        </div>
      </div>
    </div>,
    document.body,
  );
}
