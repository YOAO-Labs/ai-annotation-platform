import { lazy, Suspense, useEffect, useState, type ComponentProps } from "react";

import { FilterTrigger } from "@/components/filters/FilterTrigger";
import type { ProjectFilterPanel as PanelComponent } from "./ProjectFilterPanel";

const ProjectFilterPanel = lazy(() =>
  import("./ProjectFilterPanel").then((module) => ({ default: module.ProjectFilterPanel })),
);

type Props = ComponentProps<typeof PanelComponent>;

export function ProjectFilterControl(props: Props) {
  const { open, onOpenChange, count } = props;
  const [requested, setRequested] = useState(open);
  useEffect(() => {
    if (open) setRequested(true);
  }, [open]);

  const loading = requested || open;
  const trigger = (
    <FilterTrigger
      size="md"
      count={count}
      countLabel="附加筛选条件（不含状态标签）"
      aria-haspopup="dialog"
      aria-expanded={open}
      aria-busy={loading || undefined}
      disabled={loading}
      onClick={() => {
        setRequested(true);
        onOpenChange(true);
      }}
    />
  );

  if (!loading) return trigger;
  return (
    <Suspense fallback={trigger}>
      <ProjectFilterPanel {...props} />
    </Suspense>
  );
}
