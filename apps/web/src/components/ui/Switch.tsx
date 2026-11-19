import { Switch as ShadcnSwitch } from "@/components/shadcn/ui/switch";
import { cn } from "@/lib/utils";

/**
 * Switch —— shadcn(Radix)适配层。
 * 保留原有 `{checked,onChange,disabled,label,title,data-testid}` API(调用点零改动),
 * 开关本体与键盘/role 由 Radix 兜底。
 */
interface SwitchProps {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  /** 开关右侧文案；省略则只渲染开关本体。 */
  label?: string;
  title?: string;
  "data-testid"?: string;
  "data-attribute-key"?: string;
}

export function Switch({
  checked,
  onChange,
  disabled = false,
  label,
  title,
  "data-testid": testId,
  "data-attribute-key": attributeKey,
}: SwitchProps) {
  const control = (
    <ShadcnSwitch
      checked={checked}
      onCheckedChange={onChange}
      disabled={disabled}
      title={title}
      data-testid={testId}
      data-attribute-key={attributeKey}
    />
  );
  if (label === undefined) return control;
  return (
    <label
      className={cn(
        "inline-flex select-none items-center gap-2",
        disabled && "cursor-not-allowed opacity-50",
      )}
    >
      {control}
      <span className="text-sm text-foreground">{label}</span>
    </label>
  );
}
