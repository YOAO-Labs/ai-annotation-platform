// 共享值控件，个人页保持紧凑布局，工作台窗口直接展示说明。
import { useEffect, useId, useRef, useState } from "react";
import { HighlightText } from "@/components/ui/HighlightText";
import { Icon } from "@/components/ui/Icon";
import { Switch } from "@/components/shadcn/ui/switch";
import { Badge } from "@/components/shadcn/ui/badge";
import { Tabs as TabsPrimitive } from "radix-ui";
import { Field, FieldContent, FieldDescription, FieldLabel } from "@/components/shadcn/ui/field";
import { cn } from "@/lib/utils";
import type { LabelContentByType } from "@/api/auth";
import type {
  WorkbenchSettingField,
  WorkbenchSettingValue,
} from "../state/workbenchSettingsFields";

const LOCKED_TITLE = "由项目统一配置";

// 会话和独立 ui 偏好也可复用展示，不伪装成 workbench 存储字段。
export type SettingsControlField = Pick<
  WorkbenchSettingField,
  "label" | "description" | "control"
> & { key: string };

interface SettingsFieldControlProps {
  field: SettingsControlField;
  value: WorkbenchSettingValue;
  nested?: boolean;
  disabled?: boolean;
  locked?: boolean;
  layout?: "compact" | "settings";
  previewing?: boolean;
  onPreviewChange?: (active: boolean) => void;
  /** 搜索结果高亮词;空串时不高亮(个人设置页不传)。 */
  highlightQuery?: string;
  onCommit: (value: WorkbenchSettingValue) => void;
}

export function SettingsFieldControl({
  field,
  value,
  nested = false,
  disabled = false,
  locked = false,
  layout = "compact",
  onCommit,
  previewing,
  onPreviewChange,
  highlightQuery = "",
}: SettingsFieldControlProps) {
  const { control } = field;
  const detailed = layout === "settings";
  const id = useId();
  const descriptionId = `${id}-description`;
  const [sliderLive, setSliderLive] = useState(Number(value));
  useEffect(() => {
    setSliderLive(Number(value));
  }, [value]);
  const formatted =
    control.type === "slider"
      ? control.format
        ? control.format(sliderLive)
        : String(sliderLive)
      : "";
  const isColumn = control.type === "labelContentByType";
  const blocked = disabled || locked;
  const inputProps = {
    id,
    "aria-describedby": detailed && field.description ? descriptionId : undefined,
  };
  return (
    <Field
      orientation={isColumn ? "vertical" : "horizontal"}
      className={cn(
        detailed
          ? "flex-col items-stretch py-4 md:flex-row md:items-center md:gap-8"
          : "min-h-[38px] gap-3 rounded-sm px-2.5 py-2 hover:bg-muted",
        isColumn && "flex-col items-stretch md:flex-col md:items-stretch",
        nested && "ml-4 w-auto border-l border-border pl-4",
      )}
      title={locked ? LOCKED_TITLE : detailed ? undefined : field.description}
      aria-disabled={blocked}
      data-disabled={blocked || undefined}
      data-slider-preview={previewing || undefined}
      data-testid={`setting-field-${field.key}`}
    >
      <FieldContent className="min-w-0">
        <div className="flex items-center gap-2">
          <FieldLabel
            htmlFor={isColumn ? undefined : id}
            className={cn("min-w-0", !detailed && "text-xs text-muted-foreground")}
          >
            <HighlightText text={field.label} query={highlightQuery} />
            {!detailed && control.type === "slider" ? `：${formatted}` : ""}
          </FieldLabel>
          {!detailed && field.description && !locked && (
            <span aria-label={field.description} title={field.description}>
              <Icon name="info" size={11} />
            </span>
          )}
          {locked && (
            <Badge variant="secondary" title={LOCKED_TITLE}>
              项目锁定
            </Badge>
          )}
        </div>
        {detailed && field.description && (
          <FieldDescription id={descriptionId} className="text-xs leading-relaxed">
            <HighlightText text={field.description} query={highlightQuery} />
          </FieldDescription>
        )}
      </FieldContent>
      <div
        className={cn(
          "flex shrink-0 items-center justify-end gap-2",
          detailed && "w-full md:w-60",
          isColumn && "block w-full md:w-full",
        )}
      >
        {control.type === "toggle" && (
          <>
            <Switch
              {...inputProps}
              checked={Boolean(value)}
              disabled={blocked}
              onCheckedChange={onCommit}
            />
            {!detailed && (control.onText || control.offText) && (
              <span className="whitespace-nowrap text-xs text-muted-foreground">
                {value ? control.onText : control.offText}
              </span>
            )}
          </>
        )}
        {control.type === "slider" && (
          <>
            <SliderControl
              {...inputProps}
              value={Number(value)}
              min={control.min}
              max={control.max}
              step={control.step}
              disabled={blocked}
              detailed={detailed}
              onPreviewChange={onPreviewChange}
              onLiveChange={(next) => {
                setSliderLive(next);
                if (onPreviewChange) onCommit(next);
              }}
              onCommit={onCommit}
            />
            {detailed && (
              <output htmlFor={id} className="min-w-10 text-right text-sm tabular-nums">
                {formatted}
              </output>
            )}
            {control.resetTo !== undefined && (
              <button
                type="button"
                disabled={blocked}
                onClick={() => onCommit(control.resetTo!)}
                className="shrink-0 rounded-md border border-border bg-card px-2 py-1 text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
              >
                重置
              </button>
            )}
          </>
        )}
        {control.type === "select" && (
          <select
            {...inputProps}
            value={String(value)}
            disabled={blocked}
            onChange={(e) => {
              const selected = control.options.find(
                (option) => String(option.value) === e.target.value,
              );
              if (selected) onCommit(selected.value);
            }}
            className={cn(
              "box-border w-full rounded-md border border-border bg-card px-2 py-2 text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50",
              detailed ? "text-sm" : "max-w-[130px] text-xs",
            )}
          >
            {control.options.map((option) => (
              <option key={String(option.value)} value={String(option.value)}>
                {option.label}
              </option>
            ))}
          </select>
        )}
        {control.type === "text" && (
          <TextControl
            {...inputProps}
            detailed={detailed}
            value={String(value)}
            maxLength={control.maxLength}
            placeholder={control.placeholder}
            disabled={blocked}
            onCommit={onCommit}
          />
        )}
        {control.type === "multiselect" && (
          <MultiselectControl
            value={Array.isArray(value) ? value : []}
            options={control.options}
            min={control.min ?? 0}
            disabled={blocked}
            onCommit={onCommit}
          />
        )}
        {control.type === "labelContentByType" && (
          <LabelContentByTypeControl
            value={
              value && typeof value === "object" && !Array.isArray(value)
                ? (value as LabelContentByType)
                : { single: [], track: [], ai: [] }
            }
            segments={control.segments}
            disabled={blocked}
            onCommit={onCommit}
          />
        )}
      </div>
    </Field>
  );
}

/** v0.16.7 · 标签内容按类型分段:顶部段切换(单帧/轨迹/AI),下方该段字段 Switch 列;类别名恒显标必选。 */
function LabelContentByTypeControl({
  value,
  segments,
  disabled,
  onCommit,
}: {
  value: LabelContentByType;
  segments: Array<{
    key: "single" | "track" | "ai";
    label: string;
    options: Array<{ value: string; label: string }>;
  }>;
  disabled: boolean;
  onCommit: (value: LabelContentByType) => void;
}) {
  const [active, setActive] = useState<"single" | "track" | "ai">(segments[0]?.key ?? "single");
  const activeSeg = segments.find((s) => s.key === active) ?? segments[0];
  const selected: string[] = value[active] ?? [];
  const toggle = (optValue: string) => {
    const has = selected.includes(optValue);
    // 按 options 顺序重建当前段,其余段原样保留(提交整个对象)。
    const next = activeSeg.options
      .map((o) => o.value)
      .filter((v) => (v === optValue ? !has : selected.includes(v)));
    onCommit({ ...value, [active]: next } as LabelContentByType);
  };
  return (
    // 保留键盘语义，避免外层分类 Tabs 的纵向 group 样式穿透嵌套列表。
    <TabsPrimitive.Root
      value={active}
      onValueChange={(next) => setActive(next as typeof active)}
      className="flex flex-col gap-2"
    >
      <TabsPrimitive.List aria-label="标签类型" className="flex gap-1 rounded-md bg-muted p-1">
        {segments.map((s) => (
          <TabsPrimitive.Trigger
            key={s.key}
            value={s.key}
            className="flex-1 rounded-sm px-2 py-1 text-xs text-muted-foreground data-[state=active]:bg-card data-[state=active]:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
            disabled={disabled}
          >
            {s.label}
          </TabsPrimitive.Trigger>
        ))}
      </TabsPrimitive.List>
      <TabsPrimitive.Content value={active} className="flex flex-col gap-0.5">
        <div className="flex items-center justify-between gap-3 min-h-[30px] px-1 py-0.5 text-foreground text-xs cursor-default text-muted-foreground">
          <span>类别名</span>
          <span className="px-1.5 py-px rounded-full bg-muted text-muted-foreground text-2xs">
            必选
          </span>
        </div>
        {activeSeg.options.map((opt) => (
          <label
            key={opt.value}
            className="flex items-center justify-between gap-3 min-h-[30px] px-1 py-0.5 text-foreground text-xs cursor-pointer"
          >
            <span>{opt.label}</span>
            <Switch
              checked={selected.includes(opt.value)}
              disabled={disabled}
              onCheckedChange={() => toggle(opt.value)}
            />
          </label>
        ))}
      </TabsPrimitive.Content>
    </TabsPrimitive.Root>
  );
}

/** v0.15.27 · 多选 chips:点击切换;受 min 约束时已是最后一项的取消被拒绝(保序提交)。 */
function MultiselectControl({
  value,
  options,
  min,
  disabled,
  onCommit,
}: {
  value: string[];
  options: Array<{ value: string; label: string }>;
  min: number;
  disabled: boolean;
  onCommit: (value: string[]) => void;
}) {
  const selected = new Set(value);
  const toggle = (optValue: string) => {
    const isOn = selected.has(optValue);
    if (isOn && value.length <= min) return; // min 兜底:不允许低于下限
    // 按 options 顺序重建,保证提交值稳定有序。
    const next = options
      .map((o) => o.value)
      .filter((v) => (v === optValue ? !isOn : selected.has(v)));
    onCommit(next);
  };
  return (
    <span className="inline-flex shrink-0 flex-wrap justify-end gap-1.5 max-w-[200px]" role="group">
      {options.map((opt) => {
        const on = selected.has(opt.value);
        const atFloor = on && value.length <= min;
        return (
          <button
            key={opt.value}
            type="button"
            className={`px-2.5 py-1 appearance-none border rounded-full text-xs cursor-pointer transition-[border-color,background,color] duration-150 hover:border-brand hover:text-foreground disabled:cursor-not-allowed ${on ? "border-brand bg-brand/10 text-brand font-medium disabled:opacity-70" : "border-border bg-card text-muted-foreground"}`}
            // chips 嵌在字段 <label> 内,显式 aria-label 兜底可达名,避免名被父 label 文本污染。
            aria-label={opt.label}
            aria-pressed={on}
            disabled={disabled || atFloor}
            onClick={(e) => {
              e.preventDefault();
              toggle(opt.value);
            }}
          >
            {opt.label}
          </button>
        );
      })}
    </span>
  );
}

function SliderControl({
  id,
  "aria-describedby": describedBy,
  detailed,
  value,
  min,
  max,
  step,
  disabled,
  onLiveChange,
  onPreviewChange,
  onCommit,
}: {
  id: string;
  "aria-describedby"?: string;
  detailed: boolean;
  value: number;
  min: number;
  max: number;
  step: number;
  disabled: boolean;
  /** 拖动过程中每帧上报实时值(供父组件实时显示数字);不触发 commit。 */
  onLiveChange: (value: number) => void;
  onPreviewChange?: (active: boolean) => void;
  onCommit: (value: number) => void;
}) {
  const [local, setLocal] = useState(value);
  const localRef = useRef(value);
  const committedRef = useRef(value);
  const [dragging, setDragging] = useState(false);

  useEffect(() => {
    if (!dragging) {
      setLocal(value);
      localRef.current = value;
      committedRef.current = value;
    }
  }, [dragging, value]);

  const commit = () => {
    setDragging(false);
    onPreviewChange?.(false);
    if (localRef.current !== committedRef.current) {
      committedRef.current = localRef.current;
      onCommit(localRef.current);
    }
  };

  return (
    <input
      id={id}
      aria-describedby={describedBy}
      type="range"
      min={min}
      max={max}
      step={step}
      value={local}
      disabled={disabled}
      onPointerDown={(e) => {
        e.currentTarget.setPointerCapture?.(e.pointerId);
        setDragging(true);
        onPreviewChange?.(true);
      }}
      onChange={(e) => {
        const next = Number(e.target.value);
        localRef.current = next;
        setLocal(next);
        onLiveChange(next);
        if (onPreviewChange) committedRef.current = next;
      }}
      onPointerUp={commit}
      onPointerCancel={commit}
      onLostPointerCapture={commit}
      onBlur={commit}
      onKeyUp={(e) => {
        if (
          e.key === "ArrowLeft" ||
          e.key === "ArrowRight" ||
          e.key === "ArrowUp" ||
          e.key === "ArrowDown" ||
          e.key === "PageUp" ||
          e.key === "PageDown" ||
          e.key === "Home" ||
          e.key === "End"
        ) {
          commit();
        }
      }}
      className={cn(
        "h-5 min-w-0 cursor-pointer accent-brand focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-40",
        detailed ? "w-full flex-1" : "w-[110px]",
      )}
    />
  );
}

function TextControl({
  id,
  "aria-describedby": describedBy,
  value,
  maxLength,
  placeholder,
  disabled,
  detailed,
  onCommit,
}: {
  id: string;
  "aria-describedby"?: string;
  value: string;
  maxLength: number;
  placeholder?: string;
  disabled: boolean;
  detailed: boolean;
  onCommit: (value: string) => void;
}) {
  const [local, setLocal] = useState(value);
  const committedRef = useRef(value);
  useEffect(() => {
    setLocal(value);
    committedRef.current = value;
  }, [value]);
  const commit = () => {
    const next = local.trim();
    setLocal(next);
    if (next !== committedRef.current) {
      committedRef.current = next;
      onCommit(next);
    }
  };
  return (
    <input
      id={id}
      aria-describedby={describedBy}
      value={local}
      disabled={disabled}
      maxLength={maxLength}
      onChange={(e) => setLocal(e.target.value.slice(0, maxLength))}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter" && !e.nativeEvent.isComposing) {
          e.preventDefault();
          commit();
        }
      }}
      placeholder={placeholder}
      className={cn(
        "box-border w-full rounded-md border border-border bg-card px-2 py-2 text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50",
        detailed ? "text-sm" : "max-w-[180px] text-xs",
      )}
    />
  );
}
