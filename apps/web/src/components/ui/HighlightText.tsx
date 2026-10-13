import { Fragment } from "react";

import { cn } from "@/lib/utils";

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

interface HighlightTextProps {
  text: string;
  /** 当前搜索词;空串或未命中时原样返回。大小写不敏感,按子串匹配。 */
  query: string;
  className?: string;
}

/**
 * 在文本中高亮命中的搜索词(`<mark>` + 语义色)。
 * 供设置 / 快捷键面板的搜索结果使用,避免各面板各写一份正则。
 */
export function HighlightText({ text, query, className }: HighlightTextProps) {
  const term = query.trim();
  if (!term) return <>{text}</>;
  const parts = text.split(new RegExp(`(${escapeRegExp(term)})`, "gi"));
  if (parts.length === 1) return <>{text}</>;
  const needle = term.toLowerCase();
  return (
    <>
      {parts.map((part, index) =>
        part.toLowerCase() === needle ? (
          <mark
            key={index}
            className={cn(
              "rounded-[2px] bg-mark px-0.5 font-medium text-mark-foreground",
              className,
            )}
          >
            {part}
          </mark>
        ) : (
          <Fragment key={index}>{part}</Fragment>
        ),
      )}
    </>
  );
}
