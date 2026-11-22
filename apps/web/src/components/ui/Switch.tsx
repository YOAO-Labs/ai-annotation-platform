import { Switch as ShadcnSwitch } from "@/components/shadcn/ui/switch";
import { cn } from "@/lib/utils";

interface SwitchProps {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
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
