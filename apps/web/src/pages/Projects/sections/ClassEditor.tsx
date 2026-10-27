/**
 * v0.7.0 · ClassEditor
 *
 * 从 ClassesSection 抽出的受控组件：颜色 + 排序 + 删除 + 新增。
 * 由 ClassesSection（保存按钮的薄外壳）和 CreateProjectWizard（向导步骤）共用。
 */
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import { classColor } from "@/pages/Workbench/stage/colors";
import type { ClassRefLite } from "./resolveClassVisual";
import { cn } from "@/lib/utils";

// UA-safe 表单基线 + token 化(无全局 preflight)。
const CONTROL_CLASS =
  "box-border appearance-none rounded-sm border border-border bg-muted px-2 py-1.5 text-sm text-foreground outline-none [font-family:inherit]";

export interface ClassRow {
  name: string;
  color: string;
  /** v0.9.5 · 英文 alias，供 SAM 文本预标 prompt 下拉直填。ASCII-only / max 50 字符。 */
  alias?: string;
  /** v0.17.15 · alias_to 软关联: 链接到另一工具单位的类，继承其 color/alias。null/缺省 = 不继承。 */
  aliasTo?: ClassRefLite | null;
}

const ALIAS_PATTERN = /^[a-zA-Z0-9 ,_-]*$/;

/** v0.9.6 · 与后端 ClassConfigEntry._normalize_alias 等价的前端实现.
 * blur 时规范化, 让所见即所得 + DINO 召回更稳; 后端 field_validator 兜底.
 */
function normalizeAlias(raw: string): string {
  let s = raw.toLowerCase().trim();
  if (!s) return s;
  // 折叠 [空白+逗号]+ 为单 ","; "a , , b" → "a,b"
  s = s.replace(/\s*,[\s,]*/g, ",");
  // 折叠多重空格
  s = s.replace(/\s+/g, " ");
  // 去掉首尾遗留逗号
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
  /** 限定最大数量（向导限 50 防止失误）；0 = 无限制 */
  max?: number;
  emptyHint?: string;
  /** B-13 · 重命名类别 (会同步迁移已有 annotations); 不提供则名称只读. */
  onRename?: (oldName: string, newName: string) => void;
  /** 外部正在跑 rename 时禁用编辑. */
  renaming?: boolean;
  /** 删除前确认；返回 false 时取消删除。 */
  onConfirmDelete?: (row: ClassRow) => boolean | Promise<boolean>;
  /** v0.17.15 · 可链接的跨工具单位类目标 (按 unit 分组)；为空 / 省略则不显示 alias_to 链接 UI。 */
  linkTargets?: { unitId: string; unitLabel: string; classNames: string[] }[];
  /** 解析某 aliasTo 引用的继承 color/alias (供链接行只读预览)。 */
  resolveLinked?: (ref: ClassRefLite) => { color?: string; alias?: string };
  /** 设置 / 清除某行链接；ref=null 清除。 */
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
  // B-13 · 行内重命名: 记录每行的草稿名 (key 用稳定的 row.name 作 baseline; 提交时与原名比对决定是否调 onRename).
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
    // v0.9.6 · 输入时若含非 ASCII 提示一次 (沿用 pattern 拒绝, toast 友好提示).
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

  /** v0.9.6 · onBlur 触发规范化: lower / strip / 折叠空格逗号; 与后端 schema 保持一致. */
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
        // Let constrained dialogs shrink the list while keeping the add controls visible.
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
                    title="alias 继承自链接的工具单位类；清除链接后可单独编辑"
                    className={`${CONTROL_CLASS} text-xs opacity-[0.6]`}
                  />
                ) : (
                  <input
                    value={r.alias ?? ""}
                    onChange={(e) => setAlias(i, e.target.value)}
                    onBlur={() => normalizeAliasOnBlur(i)}
                    placeholder="英文 alias（SAM 提示用，可空）"
                    maxLength={50}
                    title="供 SAM 文本预标 prompt 下拉填入；ASCII 字母/数字/空格/逗号/下划线/连字符；blur 自动规范化"
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
