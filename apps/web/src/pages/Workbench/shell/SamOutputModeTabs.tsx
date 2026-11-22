import { TabRow } from "@/components/ui/TabRow";
import type { TextOutputMode } from "../state/useInteractiveAI";

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
