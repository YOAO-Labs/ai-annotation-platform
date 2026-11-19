/**
 * SectionDivider —— 分区标题分隔。
 */
interface SectionDividerProps {
  label: string;
  hint?: string;
}

export function SectionDivider({ label, hint }: SectionDividerProps) {
  return (
    <div className="mb-2 mt-5 flex items-center gap-2.5">
      <span className="text-xs font-semibold uppercase tracking-[0.06em] text-muted-foreground">
        {label}
      </span>
      {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
      <span className="h-px flex-1 bg-border" />
    </div>
  );
}
