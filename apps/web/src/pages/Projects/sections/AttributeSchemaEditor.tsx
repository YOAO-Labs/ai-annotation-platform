/**
 * AttributeSchemaEditor
 *
 * 从项目设置属性编辑抽出的纯受控组件：负责字段增删改 + 校验。
 * 由 ClassesSection（类别与属性合并页）和 CreateProjectWizard（向导 step）共用。
 *
 * 不在内部触发 PATCH 请求；调用方拿到 onChange 后的 fields 自行决定保存时机。
 */
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import type { AttributeField, AttributeFieldType } from "@/api/projects";
import { AttributeOptionsEditor } from "./AttributeOptionsEditor";
import { LABEL_CLASS } from "./formClasses";

// UA-safe 表单基线 + token 化(无全局 preflight)。
const CONTROL_CLASS =
  "box-border w-full appearance-none rounded-sm border border-border bg-muted px-2.5 py-1.5 text-sm text-foreground outline-none [font-family:inherit]";

const FIELD_TYPES: { value: AttributeFieldType; label: string }[] = [
  { value: "text", label: "文本" },
  { value: "number", label: "数字" },
  { value: "boolean", label: "开关" },
  { value: "select", label: "下拉单选" },
  { value: "multiselect", label: "下拉多选" },
  { value: "range", label: "区间滑杆" },
];

export function newAttributeField(): AttributeField {
  return { key: "", label: "", type: "text", required: false };
}

interface Props {
  value: AttributeField[];
  onChange: (next: AttributeField[]) => void;
  /** 空状态提示文案 */
  emptyHint?: string;
  /** 删除前确认；返回 false 时取消删除。 */
  onConfirmDelete?: (field: AttributeField) => boolean | Promise<boolean>;
  /**
   * 推荐属性字段（已含完整 key/type/options，来自在线 backend 自报 schema，
   * 调用方已排除项目已有 key）。手工建字段时一键填入，让 key 天然对齐协议（如 OCR 的
   * language/orientation），避免取成 lang/语言 等导致落点校验漏判。空 = 不显示推荐区。
   */
  recommendedFields?: AttributeField[];
}

export function AttributeSchemaEditor({
  value,
  onChange,
  emptyHint = "尚未配置任何属性",
  onConfirmDelete,
  recommendedFields = [],
}: Props) {
  const setField = (i: number, patch: Partial<AttributeField>) =>
    onChange(value.map((f, idx) => (idx === i ? { ...f, ...patch } : f)));

  const moveField = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= value.length) return;
    const out = value.slice();
    [out[i], out[j]] = [out[j], out[i]];
    onChange(out);
  };

  const removeField = async (i: number) => {
    const field = value[i];
    if (!field) return;
    if (onConfirmDelete) {
      const ok = await onConfirmDelete(field);
      if (!ok) return;
    }
    onChange(value.filter((_, idx) => idx !== i));
  };
  const addField = () => onChange([...value, newAttributeField()]);

  return (
    <div className="flex flex-col gap-2.5">
      {value.length === 0 && (
        <div className="rounded-md border border-dashed border-border p-6 text-center text-xs text-muted-foreground">
          {emptyHint}
        </div>
      )}

      {value.map((f, i) => (
        <div key={i} className="flex flex-col gap-2 rounded-md border border-border bg-card p-3">
          <div className="grid grid-cols-1 gap-2 md:grid-cols-[repeat(3,minmax(0,1fr))_auto]">
            <div>
              <label className={LABEL_CLASS}>key</label>
              <input
                value={f.key}
                onChange={(e) => setField(i, { key: e.target.value })}
                className={CONTROL_CLASS}
                placeholder="occluded"
              />
            </div>
            <div>
              <label className={LABEL_CLASS}>显示名</label>
              <input
                value={f.label}
                onChange={(e) => setField(i, { label: e.target.value })}
                className={CONTROL_CLASS}
                placeholder="是否遮挡"
              />
            </div>
            <div>
              <label className={LABEL_CLASS}>类型</label>
              <select
                value={f.type}
                onChange={(e) => {
                  const next = e.target.value as AttributeFieldType;
                  // style_occluded 仅在 boolean 字段上有效；切换到非 boolean 时同步清理，
                  // 否则字段会保留 style_occluded:true 且 UI 入口消失 → 提交时被后端校验拒绝。
                  setField(
                    i,
                    next !== "boolean" ? { type: next, style_occluded: undefined } : { type: next },
                  );
                }}
                className={`${CONTROL_CLASS} cursor-pointer`}
              >
                {FIELD_TYPES.map((t) => (
                  <option key={t.value} value={t.value}>
                    {t.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex items-center gap-1 md:items-end">
              <Button
                size="sm"
                variant="ghost"
                onClick={() => moveField(i, -1)}
                disabled={i === 0}
                title="上移"
              >
                <Icon name="chevUp" size={11} />
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => moveField(i, 1)}
                disabled={i === value.length - 1}
                title="下移"
              >
                <Icon name="chevDown" size={11} />
              </Button>
              <Button size="sm" variant="danger" onClick={() => removeField(i)} title="删除">
                <Icon name="trash" size={11} />
              </Button>
            </div>
          </div>

          <label className="flex items-center gap-1.5 text-xs">
            <input
              type="checkbox"
              checked={!!f.required}
              onChange={(e) => setField(i, { required: e.target.checked })}
            />
            必填（提交质检前必须填写）
          </label>

          {f.type === "boolean" && (
            <label className="flex items-center gap-1.5 text-xs">
              <input
                type="checkbox"
                checked={!!f.style_occluded}
                onChange={(e) => setField(i, { style_occluded: e.target.checked })}
              />
              遮挡样式（该属性为真时，画布框渲染为虚线+半透）
            </label>
          )}

          {(f.type === "select" || f.type === "multiselect") && (
            <AttributeOptionsEditor
              value={f.options ?? []}
              onChange={(options) => setField(i, { options })}
            />
          )}

          {(f.type === "number" || f.type === "range") && (
            <div className="grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-2">
              <div>
                <label className={LABEL_CLASS}>min</label>
                <input
                  type="number"
                  value={f.min ?? ""}
                  onChange={(e) =>
                    setField(i, { min: e.target.value === "" ? undefined : Number(e.target.value) })
                  }
                  className={CONTROL_CLASS}
                />
              </div>
              <div>
                <label className={LABEL_CLASS}>max</label>
                <input
                  type="number"
                  value={f.max ?? ""}
                  onChange={(e) =>
                    setField(i, { max: e.target.value === "" ? undefined : Number(e.target.value) })
                  }
                  className={CONTROL_CLASS}
                />
              </div>
            </div>
          )}

          <div>
            <label className={LABEL_CLASS}>仅对类别（applies_to）</label>
            <input
              value={Array.isArray(f.applies_to) ? f.applies_to.join(", ") : ""}
              onChange={(e) => {
                const v = e.target.value.trim();
                if (!v) setField(i, { applies_to: undefined });
                else
                  setField(i, {
                    applies_to: v
                      .split(",")
                      .map((s) => s.trim())
                      .filter(Boolean),
                  });
              }}
              placeholder="留空 = 全局；如 car, truck"
              className={CONTROL_CLASS}
            />
          </div>
        </div>
      ))}

      <div className="flex flex-wrap items-center gap-2">
        <Button variant="ghost" size="sm" onClick={addField}>
          <Icon name="plus" size={12} />
          新增属性
        </Button>
        {recommendedFields.length > 0 && (
          <>
            <span className="text-2xs text-muted-foreground">推荐（来自模型服务）：</span>
            {recommendedFields.map((rf) => (
              <button
                key={rf.key}
                type="button"
                onClick={() => onChange([...value, rf])}
                title={`一键添加属性「${rf.label || rf.key}」(key=${rf.key})`}
                className="inline-flex h-8 items-center gap-1 rounded-md border border-dashed border-border px-2.5 text-xs text-foreground hover:bg-muted"
              >
                <Icon name="plus" size={10} />
                {rf.label || rf.key}
                {/* 等宽拉丁 key 字体上沉重(无下行字 + cap 偏上), items-center 下墨迹中心比同排 CJK
                    标签实测高 2px; 下移 2px 让两者墨迹中线对齐 (canvas 字体度量实测, 非拍脑袋)。 */}
                <code className="translate-y-0.5 text-2xs text-muted-foreground">{rf.key}</code>
              </button>
            ))}
          </>
        )}
      </div>
    </div>
  );
}

/** 共享校验逻辑，调用方在保存 / 进入下一步前调一次。返回错误描述或 null。*/
export function validateAttributeFields(fields: AttributeField[]): string | null {
  const seen = new Set<string>();
  for (const f of fields) {
    if (!f.key.trim()) return "属性 key 不能为空";
    if (seen.has(f.key)) return `属性 key 重复: ${f.key}`;
    seen.add(f.key);
    if (f.type === "select" || f.type === "multiselect") {
      const opts = f.options ?? [];
      if (opts.length === 0) return `${f.label || f.key} 需要至少 1 个选项`;
      // 空 value 会渲染成 <option value="">（选了等于没选）；重复 value 让两个选项无法区分。
      // 后端 AttributeFieldOption 两者都不拦，只能在此把关。
      const seenValues = new Set<string>();
      for (const o of opts) {
        if (!o.value.trim()) return `${f.label || f.key}：选项 value 不能为空`;
        if (seenValues.has(o.value)) return `${f.label || f.key}：选项 value 重复 ${o.value}`;
        seenValues.add(o.value);
      }
    }
    if (f.style_occluded && f.type !== "boolean") {
      return `${f.label || f.key}：遮挡样式仅支持开关（boolean）字段`;
    }
  }
  return null;
}
