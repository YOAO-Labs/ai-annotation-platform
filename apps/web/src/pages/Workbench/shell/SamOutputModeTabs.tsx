// 共享「输出形态三选一」选择器 (box / mask / both).
// 由 SAM text-prompt 与 exemplar 两个子工具复用, 避免标签映射 / TabRow 接线复制粘贴.

import { TabRow } from "@/components/ui/TabRow";
import type { TextOutputMode } from "../state/useInteractiveAI";

// 中文标签 ↔ TextOutputMode 双向映射 (TabRow 直接显示标签字符串).
const OUTPUT_MODE_LABELS: Record<TextOutputMode, string> = {
  box: "□ 框",
  mask: "○ 掩膜",
  both: "⊕ 全部",
};
const OUTPUT_MODE_BY_LABEL: Record<string, TextOutputMode> = {
  "□ 框": "box",
  "○ 掩膜": "mask",
  "⊕ 全部": "both",
};
const OUTPUT_MODE_TABS = Object.values(OUTPUT_MODE_LABELS);

export function SamOutputModeTabs({
  value,
  onChange,
}: {
  value: TextOutputMode;
  onChange: (mode: TextOutputMode) => void;
}) {
  return (
    <TabRow
      tabs={OUTPUT_MODE_TABS}
      active={OUTPUT_MODE_LABELS[value]}
      onChange={(label) => {
        const mode = OUTPUT_MODE_BY_LABEL[label];
        if (mode) onChange(mode);
      }}
    />
  );
}
