import { cn } from "@/lib/utils";

interface TabRowProps {
  tabs: string[];
  active: string;
  onChange: (tab: string) => void;
}

export function TabRow({ tabs, active, onChange }: TabRowProps) {
  return (
    <div className="inline-flex items-center gap-1">
      {tabs.map((t) => (
        <button
          key={t}
          type="button"
          onClick={() => onChange(t)}
          className={cn(
            "appearance-none border-0 bg-transparent rounded-md px-3 py-1.5 text-sm font-medium transition-[background-color,color,transform] duration-200 active:scale-[0.98]",
            active === t
              ? "bg-muted text-foreground"
              : "text-muted-foreground hover:bg-accent hover:text-foreground",
          )}
        >
          {t}
        </button>
      ))}
    </div>
  );
}
