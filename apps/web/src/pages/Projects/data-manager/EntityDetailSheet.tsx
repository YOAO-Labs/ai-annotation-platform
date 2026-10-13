import { Suspense, lazy } from "react";
import { useNavigate } from "react-router-dom";

import type { DataManagerEntityScope } from "@/api/taskViews";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/shadcn/ui/skeleton";
import { ScrollArea } from "@/components/shadcn/ui/scroll-area";
import { Separator } from "@/components/shadcn/ui/separator";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
} from "@/components/shadcn/ui/sheet";
import { useDataManagerObjectDetail, useDataManagerTrackDetail } from "@/hooks/useTaskViews";
import { buildWorkbenchUrl } from "@/utils/workbenchNavigation";

// Lazy: opening the Data list must not initialize a workbench canvas stack.
const DataManagerAnnotationPreview = lazy(() =>
  import("./DataManagerAnnotationPreview").then((m) => ({ default: m.default })),
);

function AttributeList({ values }: { values: Record<string, unknown> }) {
  const items = Object.entries(values);
  if (!items.length) {
    return <p className="text-xs text-muted-foreground">没有可展示的属性</p>;
  }
  return (
    <dl className="grid grid-cols-[minmax(96px,auto)_minmax(0,1fr)] gap-x-4 gap-y-2 text-xs">
      {items.map(([key, value]) => (
        <div key={key} className="contents">
          <dt className="text-muted-foreground">{key}</dt>
          <dd className="truncate text-right text-foreground" title={String(value)}>
            {String(value)}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function EntityDetailSheet({
  projectId,
  scope,
  selected,
  onOpenChange,
}: {
  projectId: string;
  scope: Exclude<DataManagerEntityScope, "tasks">;
  selected: string | null;
  onOpenChange: (open: boolean) => void;
}) {
  const navigate = useNavigate();
  const objectQ = useDataManagerObjectDetail(projectId, scope === "objects" ? selected : null);
  const trackQ = useDataManagerTrackDetail(projectId, scope === "tracks" ? selected : null);
  const isLoading = scope === "objects" ? objectQ.isLoading : trackQ.isLoading;
  const isError = scope === "objects" ? objectQ.isError : trackQ.isError;
  const object = objectQ.data?.item;
  const track = trackQ.data?.track;
  const location = object?.location ?? track?.location;

  const openWorkbench = () => {
    if (!location) return;
    navigate(
      buildWorkbenchUrl(projectId, {
        batchId: location.batch_id,
        taskId: location.task_id,
        annotationId: location.annotation_id,
        trackId: location.track_id,
        frameIndex: location.video_frame_index ?? location.scene_frame_index ?? undefined,
        returnTo: window.location.pathname + window.location.search,
      }),
    );
  };

  return (
    <Sheet open={Boolean(selected)} onOpenChange={onOpenChange}>
      <SheetContent className={scope === "tracks" ? "sm:max-w-2xl" : "sm:max-w-xl"}>
        <SheetHeader>
          <SheetTitle>
            {scope === "objects"
              ? (object?.class_name ?? "对象详情")
              : (track?.track_id ?? "轨迹详情")}
          </SheetTitle>
          <SheetDescription>
            {location
              ? `${location.task_display_id}${location.scene_name ? ` / ${location.scene_name}` : ""}`
              : "查看来源、属性、质量与定位信息"}
          </SheetDescription>
        </SheetHeader>
        <ScrollArea className="min-h-0 flex-1 px-4">
          {isLoading && (
            <div className="flex flex-col gap-3 py-2">
              <Skeleton className="h-24 w-full" />
              <Skeleton className="h-40 w-full" />
            </div>
          )}
          {isError && (
            <div className="py-12 text-center text-sm text-destructive">
              详情不可用。实体可能已删除，或不在当前可见范围内。
            </div>
          )}
          {object && (
            <div className="flex flex-col gap-4 pb-6">
              <Suspense fallback={<Skeleton className="h-[320px] w-full" />}>
                <DataManagerAnnotationPreview
                  projectId={projectId}
                  taskId={object.task_id}
                  highlightAnnotationId={object.annotation_id}
                />
              </Suspense>
              <div className="flex flex-wrap gap-2">
                <Badge variant="default">{object.source}</Badge>
                <Badge variant="outline">{object.tool_unit_id}</Badge>
                <Badge variant="outline">{object.annotation_type}</Badge>
                {object.track_id && <Badge variant="accent">{object.track_id}</Badge>}
              </div>
              <Separator />
              <AttributeList values={object.attributes} />
              <Separator />
              <dl className="grid grid-cols-2 gap-3 text-xs">
                <div>
                  <dt className="text-muted-foreground">创建者</dt>
                  <dd className="mt-1">{object.created_by_name ?? "未知"}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">未解决问题</dt>
                  <dd className="mt-1 tabular-nums">{object.unresolved_feedback_count}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">置信度</dt>
                  <dd className="mt-1 tabular-nums">
                    {object.confidence === null ? "无" : object.confidence.toFixed(3)}
                  </dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">属性来源</dt>
                  <dd className="mt-1">
                    {
                      Object.values(object.attribute_origins).filter((value) => value === "ai")
                        .length
                    }{" "}
                    个 AI
                  </dd>
                </div>
              </dl>
            </div>
          )}
          {track && (
            <div className="flex flex-col gap-4 pb-6">
              <div className="flex flex-wrap gap-2">
                <Badge variant="default">
                  {track.track_kind === "compact_video" ? "视频轨迹" : "Scene 轨迹"}
                </Badge>
                {track.class_name && <Badge variant="outline">{track.class_name}</Badge>}
                {track.quality_issues.map((issue) => (
                  <Badge key={issue} variant="warning">
                    {issue}
                  </Badge>
                ))}
              </div>
              <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
                <div>
                  <dt className="text-muted-foreground">范围</dt>
                  <dd className="mt-1 tabular-nums">
                    {track.start_frame ?? "?"} - {track.end_frame ?? "?"}
                  </dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">实例</dt>
                  <dd className="mt-1 tabular-nums">{track.occurrence_count}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">关键帧</dt>
                  <dd className="mt-1 tabular-nums">{track.keyframe_count}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">缺帧 / 重复</dt>
                  <dd className="mt-1 tabular-nums">
                    {track.missing_frame_count} / {track.duplicate_frame_count}
                  </dd>
                </div>
              </dl>
              <Separator />
              <AttributeList values={track.attributes} />
              <Separator />
              <div>
                <h3 className="mb-2 text-sm font-medium">可见成员</h3>
                <div className="flex flex-col gap-1">
                  {trackQ.data?.members.map((member, index) => (
                    <button
                      key={`${member.annotation_id}-${member.frame_index ?? index}`}
                      type="button"
                      className="flex min-h-10 items-center justify-between gap-3 rounded-sm px-2 text-left text-xs hover:bg-muted"
                      onClick={() => {
                        navigate(
                          buildWorkbenchUrl(projectId, {
                            batchId: member.location.batch_id,
                            taskId: member.task_id,
                            annotationId: member.annotation_id,
                            trackId: track.track_id,
                            frameIndex: member.frame_index ?? undefined,
                            returnTo: window.location.pathname + window.location.search,
                          }),
                        );
                      }}
                    >
                      <span className="font-mono">{member.task_display_id}</span>
                      <span className="text-muted-foreground">
                        {member.frame_index === null ? "无帧" : `F${member.frame_index}`}
                        {member.keyframe_source ? ` / ${member.keyframe_source}` : ""}
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}
        </ScrollArea>
        <SheetFooter>
          <Button variant="primary" onClick={openWorkbench} disabled={!location}>
            在工作台打开并定位
          </Button>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  );
}
