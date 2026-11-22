import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import { classColor } from "@/pages/Workbench/stage/colors";
import type { ClassRefLite } from "./resolveClassVisual";
import { cn } from "@/lib/utils";

const CONTROL_CLASS =
  "box-border appearance-none rounded-sm border border-border bg-muted px-2 py-1.5 text-sm text-foreground outline-none [font-family:inherit]";

export interface ClassRow {
  name: string;
  color: string;
  alias?: string;
  aliasTo?: ClassRefLite | null;
}

const ALIAS_PATTERN = /^[a-zA-Z0-9 ,_-]*$/;

function normalizeAlias(raw: string): string {
  let s = raw.toLowerCase().trim();
  if (!s) return s;
  s = s.replace(/\s*,[\s,]*/g, ",");
  s = s.replace(/\s+/g, " ");
  s = s.replace(/^,+|,+$/g, "").trim();
  return s;
}

const ALIAS_NORM_HINTED_KEY = "cfg:aliasNormHinted";

function rgbToHex(rgb: string): string {
  if (rgb.startsWith("#") && rgb.length === 7) return rgb;
  try {
    const cvs = document.createElement("canvas");
    cvs.width = cvs.height = 1;
    const ctx = cvs.getContext("2d")!;
    ctx.fillStyle = rgb;
    ctx.fillRect(0, 0, 1, 1);
    const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
    return `#${r.toString(16).padStart(2, "0")}${g.toString(16).padStart(2, "0")}${b.toString(16).padStart(2, "0")}`;
  } catch {
    return "#888888";
  }
}

export function defaultColorFor(name: string): string {
  return rgbToHex(classColor(name));
}

interface Props {
  className?: string;
  value: ClassRow[];
  onChange: (next: ClassRow[]) => void;
  max?: number;
  emptyHint?: string;
  onRename?: (oldName: string, newName: string) => void;
  renaming?: boolean;
  onConfirmDelete?: (row: ClassRow) => boolean | Promise<boolean>;
  linkTargets?: { unitId: string; unitLabel: string; classNames: string[] }[];
  resolveLinked?: (ref: ClassRefLite) => { color?: string; alias?: string };
  onLink?: (rowName: string, ref: ClassRefLite | null) => void;
}

export function ClassEditor({
  className,
  value,
  onChange,
  max = 0,
  emptyHint = "尚未配置任何类别",
  onRename,
  renaming = false,
  onConfirmDelete,
  linkTargets,
  resolveLinked,
  onLink,
}: Props) {
  const [classInput, setClassInput] = useState("");
  const [renameDrafts, setRenameDrafts] = useState<Record<string, string>>({});
  const pushToast = useToastStore((s) => s.push);

  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= value.length) return;
    const out = value.slice();
    [out[i], out[j]] = [out[j], out[i]];
    onChange(out);
  };

  const setColor = (i: number, color: string) =>
    onChange(value.map((r, idx) => (idx === i ? { ...r, color } : r)));
  const setAlias = (i: number, raw: string) => {
    if (!ALIAS_PATTERN.test(raw)) {
      try {
        if (!sessionStorage.getItem("cfg:aliasAsciiHinted")) {
          sessionStorage.setItem("cfg:aliasAsciiHinted", "1");
          pushToast({
            msg: "alias 仅支持 ASCII",
            sub: "DINO 文本召回仅认英文 / 数字 / 空格 / , _ -",
            kind: "warning",
          });
        }
      } catch {
        // sessionStorage 不可用时静默
      }
      return;
    }
    const alias = raw.trim() === "" ? undefined : raw;
    onChange(value.map((r, idx) => (idx === i ? { ...r, alias } : r)));
  };

  const normalizeAliasOnBlur = (i: number) => {
    const cur = value[i]?.alias ?? "";
    if (!cur) return;
    const next = normalizeAlias(cur);
    if (next === cur) return;
    onChange(value.map((r, idx) => (idx === i ? { ...r, alias: next || undefined } : r)));
    try {
      if (!sessionStorage.getItem(ALIAS_NORM_HINTED_KEY)) {
        sessionStorage.setItem(ALIAS_NORM_HINTED_KEY, "1");
        pushToast({
          msg: "alias 已自动规范化",
          sub: "DINO 推荐全小写英文; 重复空格 / 逗号已折叠",
          kind: "",
        });
      }
    } catch {
      // sessionStorage 不可用时静默
    }
  };
  const remove = async (i: number) => {
    const row = value[i];
    if (!row) return;
    if (onConfirmDelete) {
      const ok = await onConfirmDelete(row);
      if (!ok) return;
    }
    onChange(value.filter((_, idx) => idx !== i));
  };

  const add = () => {
    const v = classInput.trim();
    if (!v || value.some((r) => r.name === v)) {
      setClassInput("");
      return;
    }
    if (max > 0 && value.length >= max) {
      setClassInput("");
      return;
    }
    onChange([...value, { name: v, color: defaultColorFor(v) }]);
    setClassInput("");
  };

  return (
    <div className={cn("flex flex-col gap-2.5", className)}>
      {value.length === 0 && (
        <div className="rounded-md border border-dashed border-border p-6 text-center text-xs text-muted-foreground">
          {emptyHint}
        </div>
      )}

      {value.length > 0 && (
        <div className="flex min-h-0 max-h-[1150px] flex-col gap-2.5 overflow-y-auto pr-0.5">
          {value.map((r, i) => {
            const linked = !!r.aliasTo;
            const linkedVisual = linked && resolveLinked ? resolveLinked(r.aliasTo!) : undefined;
            const swatchColor = linked ? (linkedVisual?.color ?? defaultColorFor(r.name)) : r.color;
            return (
              <div
                key={r.name}
                className="grid shrink-0 grid-cols-[auto_24px_minmax(0,1.4fr)_minmax(0,1.2fr)_70px_auto] items-center gap-2 rounded-md border border-border bg-card px-2.5 py-1.5"
              >
                <span className="w-5 text-right text-xs text-muted-foreground">{i + 1}</span>
                <svg
                  className="inline-block size-[18px] rounded border border-border"
                  viewBox="0 0 18 18"
                  aria-hidden="true"
                >
                  <rect width="18" height="18" rx="4" ry="4" fill={swatchColor} />
                </svg>
                {onRename ? (
                  <input
                    value={renameDrafts[r.name] ?? r.name}
                    onChange={(e) =>
                      setRenameDrafts((prev) => ({ ...prev, [r.name]: e.target.value }))
                    }
                    onBlur={() => {
                      const draft = (renameDrafts[r.name] ?? r.name).trim();
                      if (!draft || draft === r.name) {
                        setRenameDrafts((prev) => {
                          const { [r.name]: _, ...rest } = prev;
                          return rest;
                        });
                        return;
                      }
                      if (value.some((row, idx) => idx !== i && row.name === draft)) {
                        pushToast({
                          msg: "类别名重复",
                          sub: `「${draft}」已存在`,
                          kind: "error",
                        });
                        setRenameDrafts((prev) => {
                          const { [r.name]: _, ...rest } = prev;
                          return rest;
                        });
                        return;
                      }
                      onRename(r.name, draft);
                      setRenameDrafts((prev) => {
                        const { [r.name]: _, ...rest } = prev;
                        return rest;
                      });
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                      if (e.key === "Escape") {
                        setRenameDrafts((prev) => {
                          const { [r.name]: _, ...rest } = prev;
                          return rest;
                        });
                        (e.target as HTMLInputElement).blur();
                      }
                    }}
                    disabled={renaming}
                    maxLength={30}
                    title="重命名 (回车提交 / Esc 取消) — 会同步迁移已有标注"
                    className={CONTROL_CLASS}
                  />
                ) : (
                  <span className="text-sm text-foreground">{r.name}</span>
                )}
                {linked ? (
                  <input
                    value={linkedVisual?.alias ?? ""}
                    disabled
                    placeholder="继承自链接"
                    title="英文别名继承自链接的工具单位类别；清除链接后可单独编辑"
                    className={`${CONTROL_CLASS} text-xs opacity-[0.6]`}
                  />
                ) : (
                  <input
                    value={r.alias ?? ""}
                    onChange={(e) => setAlias(i, e.target.value)}
                    onBlur={() => normalizeAliasOnBlur(i)}
                    placeholder="英文别名（SAM 提示用，可空）"
                    maxLength={50}
                    title="供 SAM 文本预标的提示词选择使用；支持 ASCII 字母、数字、空格、逗号、下划线和连字符；离开输入框时自动规范化"
                    className={`${CONTROL_CLASS} text-xs`}
                  />
                )}
                {linked ? (
                  <span
                    className="inline-flex h-6 w-[60px] items-center justify-center rounded-[3px] border border-dashed border-border text-2xs text-muted-foreground"
                    title="颜色继承自链接的工具单位类"
                  >
                    继承
                  </span>
                ) : (
                  <input
                    type="color"
                    value={r.color}
                    onChange={(e) => setColor(i, e.target.value)}
                    className="h-6 w-[60px] cursor-pointer rounded-[3px] border border-border bg-transparent p-0"
                  />
                )}
                <div className="flex items-center gap-1">
                  {linkTargets && linkTargets.length > 0 && onLink && (
                    <select
                      value={r.aliasTo ? `${r.aliasTo.tool_unit_id}␟${r.aliasTo.class_name}` : ""}
                      onChange={(e) => {
                        const v = e.target.value;
                        if (!v) {
                          onLink(r.name, null);
                          return;
                        }
                        const [tool_unit_id, class_name] = v.split("␟");
                        onLink(r.name, { tool_unit_id, class_name });
                      }}
                      title="继承另一工具单位同类的颜色 / alias（alias_to 软关联）"
                      className={`${CONTROL_CLASS} max-w-[120px] text-xs`}
                    >
                      <option value="">不继承</option>
                      {linkTargets.map((t) => (
                        <optgroup key={t.unitId} label={t.unitLabel}>
                          {t.classNames.map((cn) => (
                            <option key={cn} value={`${t.unitId}␟${cn}`}>
                              {cn}
                            </option>
                          ))}
                        </optgroup>
                      ))}
                    </select>
                  )}
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => move(i, -1)}
                    disabled={i === 0}
                    title="上移"
                  >
                    <Icon name="chevUp" size={11} />
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => move(i, 1)}
                    disabled={i === value.length - 1}
                    title="下移"
                  >
                    <Icon name="chevDown" size={11} />
                  </Button>
                  <Button size="sm" variant="danger" onClick={() => remove(i)} title="删除">
                    <Icon name="trash" size={11} />
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <div className="mt-1 flex shrink-0 gap-1.5">
        <input
          value={classInput}
          onChange={(e) => setClassInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add();
            }
          }}
          placeholder={max > 0 && value.length >= max ? `最多 ${max} 个类别` : "新增类别名（回车）"}
          maxLength={30}
          disabled={max > 0 && value.length >= max}
          className={`${CONTROL_CLASS} flex-1`}
        />
        <Button onClick={add} disabled={!classInput.trim() || (max > 0 && value.length >= max)}>
          <Icon name="plus" size={12} />
          添加
        </Button>
      </div>
    </div>
  );
}
