import { useState, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import type { AttributeFieldOption } from "@/api/projects";
import { LABEL_CLASS } from "./formClasses";

const CONTROL_CLASS =
  "box-border w-full appearance-none rounded-sm border border-border bg-muted px-2.5 py-1.5 text-sm text-foreground outline-none [font-family:inherit]";

export function parseOptionToken(raw: string): AttributeFieldOption | null {
  const s = raw.trim();
  if (!s) return null;
  const at = s.indexOf(":");
  if (at < 0) return { value: s, label: s };
  const value = s.slice(0, at).trim();
  const label = s.slice(at + 1).trim();
  if (!value) return null;
  return { value, label: label || value };
}

const isOption = (o: AttributeFieldOption | null): o is AttributeFieldOption => o !== null;

function dedupeByValue(options: AttributeFieldOption[]): AttributeFieldOption[] {
  const seen = new Set<string>();
  return options.filter((o) => {
    if (seen.has(o.value)) return false;
    seen.add(o.value);
    return true;
  });
}

export function parseOptionLines(text: string): AttributeFieldOption[] {
  return dedupeByValue(text.split("\n").map(parseOptionToken).filter(isOption));
}

export function parseOptionTokens(text: string): AttributeFieldOption[] {
  return dedupeByValue(text.split(/[\n,]/).map(parseOptionToken).filter(isOption));
}

export function serializeOptionLines(options: AttributeFieldOption[]): string {
  return options.map((o) => (o.value === o.label ? o.value : `${o.value}:${o.label}`)).join("\n");
}

function duplicateValues(options: AttributeFieldOption[]): Set<string> {
  const seen = new Set<string>();
  const dup = new Set<string>();
  for (const o of options) {
    if (seen.has(o.value)) dup.add(o.value);
    seen.add(o.value);
  }
  return dup;
}

interface Props {
  value: AttributeFieldOption[];
  onChange: (next: AttributeFieldOption[]) => void;
}

export function AttributeOptionsEditor({ value, onChange }: Props) {
  const [bulkText, setBulkText] = useState<string | null>(null);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [addText, setAddText] = useState("");

  const dup = duplicateValues(value);

  const removeAt = (i: number) => {
    setEditingIndex(null);
    onChange(value.filter((_, idx) => idx !== i));
  };

  const commitAdd = () => {
    const parsed = parseOptionTokens(addText);
    if (parsed.length === 0) return;
    const existing = new Set(value.map((o) => o.value));
    const fresh = parsed.filter((o) => !existing.has(o.value));
    if (fresh.length > 0) onChange([...value, ...fresh]);
    setAddText("");
  };

  if (bulkText !== null) {
    const parsed = parseOptionLines(bulkText);
    const nonEmpty = bulkText.split("\n").filter((l) => l.trim());
    const invalid = nonEmpty.filter((l) => !parseOptionToken(l)).length;
    const dropped = nonEmpty.length - invalid - parsed.length;

    return (
      <div>
        <div className="mb-1.5 flex items-center justify-between">
          <label className={`${LABEL_CLASS} mb-0`}>选项（每行一个，格式 value:label）</label>
          <Button size="xs" variant="ghost" onClick={() => setBulkText(null)}>
            <Icon name="tag" size={11} />
            返回 chip
          </Button>
        </div>
        <textarea
          value={bulkText}
          onChange={(e) => {
            setBulkText(e.target.value);
            onChange(parseOptionLines(e.target.value));
          }}
          rows={Math.min(Math.max(parsed.length + 1, 3), 14)}
          placeholder={"car:小车\ntruck:卡车"}
          aria-label="批量编辑选项"
          className={`${CONTROL_CLASS} resize-y font-mono text-xs leading-relaxed`}
        />
        <div className="mt-1 flex flex-wrap gap-x-3 text-2xs text-muted-foreground">
          <span>{parsed.length} 个选项</span>
          {invalid > 0 && (
            <span className="text-status-danger">{invalid} 行缺少 value，已忽略</span>
          )}
          {dropped > 0 && (
            <span className="text-status-danger">{dropped} 行 value 重复，已忽略</span>
          )}
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between">
        <label className={`${LABEL_CLASS} mb-0`}>选项</label>
        <Button
          size="xs"
          variant="ghost"
          onClick={() => {
            setEditingIndex(null);
            setBulkText(serializeOptionLines(value));
          }}
        >
          <Icon name="list" size={11} />
          批量编辑
        </Button>
      </div>

      <div className="rounded-md border border-border bg-muted/40 p-2">
        {value.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {value.map((o, i) =>
              editingIndex === i ? (
                <InlineOptionEdit
                  key={i}
                  option={o}
                  onCancel={() => setEditingIndex(null)}
                  onCommit={(next) => {
                    setEditingIndex(null);
                    onChange(value.map((x, idx) => (idx === i ? next : x)));
                  }}
                />
              ) : (
                <OptionChip
                  key={i}
                  option={o}
                  duplicate={dup.has(o.value)}
                  onEdit={() => setEditingIndex(i)}
                  onRemove={() => removeAt(i)}
                />
              ),
            )}
          </div>
        )}

        <input
          value={addText}
          onChange={(e) => setAddText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commitAdd();
            } else if (e.key === "Backspace" && addText === "" && value.length > 0) {
              e.preventDefault();
              removeAt(value.length - 1);
            }
          }}
          onBlur={commitAdd}
          placeholder="添加选项，回车确认（car:小车），可粘贴逗号分隔的多个"
          aria-label="添加选项"
          className={`${CONTROL_CLASS} border-dashed bg-transparent`}
        />
      </div>

      {dup.size > 0 && (
        <div className="mt-1 text-2xs text-status-danger">value 重复：{[...dup].join("、")}</div>
      )}
    </div>
  );
}

function OptionChip({
  option,
  duplicate,
  onEdit,
  onRemove,
}: {
  option: AttributeFieldOption;
  duplicate: boolean;
  onEdit: () => void;
  onRemove: () => void;
}) {
  return (
    <span
      className={`inline-flex h-7 items-center gap-1.5 rounded-md border px-2 text-xs ${
        duplicate
          ? "border-rose-500/60 bg-status-danger-soft text-status-danger"
          : "border-border bg-card text-foreground"
      }`}
    >
      <button
        type="button"
        onClick={onEdit}
        title={`编辑「${option.label}」(value=${option.value})`}
        className="inline-flex items-center gap-1.5"
      >
        {option.label}
        {option.value !== option.label && (
          <code className="translate-y-0.5 text-2xs text-muted-foreground">{option.value}</code>
        )}
      </button>
      <button
        type="button"
        onClick={onRemove}
        title={`删除「${option.label}」`}
        className="text-muted-foreground hover:text-foreground"
      >
        <Icon name="x" size={10} />
      </button>
    </span>
  );
}

function InlineOptionEdit({
  option,
  onCommit,
  onCancel,
}: {
  option: AttributeFieldOption;
  onCommit: (next: AttributeFieldOption) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState(option);
  const valid = draft.value.trim().length > 0;

  const commit = () => {
    if (!valid) return onCancel();
    onCommit({ value: draft.value.trim(), label: draft.label.trim() || draft.value.trim() });
  };

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Enter") commit();
    if (e.key === "Escape") onCancel();
  };

  return (
    <span className="inline-flex items-center gap-1 rounded-md border border-primary/60 bg-card p-1">
      <input
        autoFocus
        value={draft.value}
        onChange={(e) => setDraft({ ...draft, value: e.target.value })}
        onKeyDown={onKeyDown}
        placeholder="value"
        aria-label="选项 value"
        className="w-24 rounded-sm border border-border bg-muted px-1.5 py-0.5 font-mono text-2xs text-foreground outline-none"
      />
      <input
        value={draft.label}
        onChange={(e) => setDraft({ ...draft, label: e.target.value })}
        onKeyDown={onKeyDown}
        placeholder="显示名"
        aria-label="选项显示名"
        className="w-24 rounded-sm border border-border bg-muted px-1.5 py-0.5 text-2xs text-foreground outline-none"
      />
      <button
        type="button"
        onClick={commit}
        disabled={!valid}
        title="确认"
        className="text-muted-foreground hover:text-foreground disabled:opacity-40"
      >
        <Icon name="check" size={11} />
      </button>
    </span>
  );
}
