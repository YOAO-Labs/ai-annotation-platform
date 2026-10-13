import { Suspense, lazy } from "react";
import { useNavigate } from "react-router-dom";

import type { DataManagerTask } from "@/api/taskViews";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/shadcn/ui/skeleton";
import { ScrollArea } from "@/components/shadcn/ui/scroll-area";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/shadcn/ui/sheet";
import { useDataManagerMatches } from "@/hooks/useTaskViews";
import { buildWorkbenchUrl } from "@/utils/workbenchNavigation";

// Lazy: opening the Data list must not initialize a workbench canvas stack.
const DataManagerAnnotationPreview = lazy(() =>
  import("./DataManagerAnnotationPreview").then((m) => ({ default: m.default })),
);

interface TaskMatchesSheetProps {
  projectId: string;
  task: Pick<DataManagerTask, "id" | "display_id" | "file_name"> | null;
  filterJson: Record<string, unknown>;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const ENTITY_LABEL = {
  annotation: "正式标注",
  prediction_shape: "AI 检测候选",
  tracker_job: "AI 追踪候选",
} as const;

export function TaskMatchesSheet({
  projectId,
  task,
  filterJson,
  open,
  onOpenChange,
}: TaskMatchesSheetProps) {
  const navigate = useNavigate();
  const matchesQ = useDataManagerMatches(projectId, task?.id ?? null, filterJson, open);

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>{task?.display_id ?? "匹配对象"}</SheetTitle>
          <SheetDescription>
            {task?.file_name ?? ""} · {matchesQ.data?.total ?? 0} 个匹配对象或候选
          </SheetDescription>
        </SheetHeader>
        <ScrollArea className="min-h-0 flex-1 px-4">
          <div className="flex flex-col gap-3 pb-4">
            {task && (
              <Suspense fallback={<Skeleton className="h-[320px] w-full" />}>
                <DataManagerAnnotationPreview projectId={projectId} taskId={task.id} />
              </Suspense>
            )}
            {matchesQ.isLoading &&
              Array.from({ length: 4 }, (_, index) => (
                <Skeleton key={index} className="h-24 w-full" />
              ))}
            {matchesQ.isError && (
              <div className="rounded-md border border-destructive p-3 text-sm text-destructive">
                无法加载匹配对象
              </div>
            )}
            {matchesQ.data?.items.map((item) => (
              <article
                key={`${item.entity_kind}-${item.id}-${item.shape_index ?? ""}`}
                className="rounded-md border border-border bg-card p-3"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium">
                      {item.class_name ?? item.track_id ?? item.id}
                    </div>
                    <div className="mt-1 text-xs text-muted-foreground">
                      {[item.tool_unit_id, item.annotation_type, item.source]
                        .filter(Boolean)
                        .join(" · ") || "候选结果"}
                    </div>
                  </div>
                  <Badge variant={item.entity_kind === "annotation" ? "default" : "warning"}>
                    {ENTITY_LABEL[item.entity_kind]}
                  </Badge>
                </div>
                {item.track_id && (
                  <div className="mt-2 font-mono text-xs text-muted-foreground">
                    {item.track_id}
                  </div>
                )}
                {Object.keys(item.attributes).length > 0 && (
                  <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
                    {Object.entries(item.attributes)
                      .slice(0, 6)
                      .map(([key, value]) => (
                        <div key={key} className="contents">
                          <dt className="text-muted-foreground">{key}</dt>
                          <dd className="truncate text-right">{String(value)}</dd>
                        </div>
                      ))}
                  </dl>
                )}
              </article>
            ))}
            {!matchesQ.isLoading && !matchesQ.isError && !matchesQ.data?.items.length && (
              <div className="py-12 text-center text-sm text-muted-foreground">
                当前条件没有对象级明细
              </div>
            )}
          </div>
        </ScrollArea>
        <SheetFooter>
          <Button
            variant="primary"
            disabled={!task}
            onClick={() =>
              task &&
              navigate(
                buildWorkbenchUrl(projectId, {
                  taskId: task.id,
                  returnTo: window.location.pathname + window.location.search,
                }),
              )
            }
          >
            在工作台打开任务
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  );
}
