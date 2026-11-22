import { useEffect, useRef } from "react";

import { cn } from "@/lib/utils";

interface ProgressBarProps {
  value: number;
  color?: string;
  aiValue?: number;
  inProgressValue?: number;
  style?: React.CSSProperties;
}

function clampPct(value: number): string {
  return `${Math.min(100, Math.max(0, value))}%`;
}

const UNITLESS_STYLE_KEYS = new Set([
  "flex",
  "flexGrow",
  "flexShrink",
  "fontWeight",
  "lineHeight",
  "opacity",
  "order",
  "zIndex",
]);

function toCssPropertyName(key: string): string {
  if (key.startsWith("--")) return key;
  return key.replace(/[A-Z]/g, (match) => `-${match.toLowerCase()}`);
}

function serializeStyleValue(
  key: string,
  value: React.CSSProperties[keyof React.CSSProperties],
): string {
  if (value === null || value === undefined || typeof value === "boolean") return "";
  if (typeof value === "number" && value !== 0 && !UNITLESS_STYLE_KEYS.has(key)) {
    return `${value}px`;
  }
  return String(value);
}

export function ProgressBar({
  value,
  color = "var(--sc-brand)",
  aiValue,
  inProgressValue,
  style,
}: ProgressBarProps) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const callerStyleKeysRef = useRef<Array<keyof React.CSSProperties>>([]);
  const normalizedAiValue = aiValue || 0;

  useEffect(() => {
    const element = rootRef.current;
    if (!element) return;

    for (const key of callerStyleKeysRef.current) {
      if (!style || !(key in style)) {
        element.style.removeProperty(toCssPropertyName(String(key)));
      }
    }

    if (style) {
      for (const [key, value] of Object.entries(style)) {
        element.style.setProperty(toCssPropertyName(key), serializeStyleValue(key, value));
      }
      callerStyleKeysRef.current = Object.keys(style) as Array<keyof React.CSSProperties>;
    } else {
      callerStyleKeysRef.current = [];
    }
  }, [style]);

  const segBase = "absolute inset-y-0 rounded-[inherit]";

  return (
    <div ref={rootRef} className="relative h-1.5 overflow-hidden rounded-full bg-muted">
      {inProgressValue !== undefined && inProgressValue > 0 && (
        <i
          ref={(element) => {
            if (element) element.style.width = clampPct(inProgressValue);
          }}
          className={cn(segBase, "left-0 bg-foreground/10 transition-[width] duration-300")}
        />
      )}
      {aiValue !== undefined && aiValue > 0 && (
        <i
          ref={(element) => {
            if (element) element.style.width = clampPct(aiValue);
          }}
          className={cn(segBase, "left-0 bg-violet-500")}
        />
      )}
      <i
        ref={(element) => {
          if (!element) return;
          element.style.left = normalizedAiValue ? clampPct(normalizedAiValue) : "0%";
          element.style.width = clampPct(value - normalizedAiValue);
          element.style.background = color;
        }}
        className={cn(segBase, "transition-[width] duration-300")}
      />
    </div>
  );
}
