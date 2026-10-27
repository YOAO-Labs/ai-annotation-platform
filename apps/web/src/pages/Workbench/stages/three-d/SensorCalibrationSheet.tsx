import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useMemo, useRef, useState } from "react";
import { HistoryIcon, PencilIcon, RotateCcwIcon, SaveIcon, TriangleAlertIcon } from "lucide-react";

import type { SensorCalibrationRevisionOut } from "@/api/generated";
import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { useToastStore } from "@/components/ui/Toast";
import { Alert, AlertDescription, AlertTitle } from "@/components/shadcn/ui/alert";
import { Badge } from "@/components/shadcn/ui/badge";
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/shadcn/ui/field";
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
import { Skeleton } from "@/components/shadcn/ui/skeleton";
import { Spinner } from "@/components/shadcn/ui/spinner";
import { Table, TableBody, TableCell, TableRow } from "@/components/shadcn/ui/table";
import { Textarea } from "@/components/shadcn/ui/textarea";
import { useSensorCalibration } from "@/hooks/useSensorCalibration";
import type { SensorCalibration } from "@/types";
import {
  changedCalibrationParts,
  formatCalibrationDraft,
  parseCalibrationDraft,
} from "./calibrationDraft";

const PART_LABELS = {
  intrinsic: "内参 intrinsic",
  extrinsic: "外参 extrinsic",
  rect: "矫正矩阵 rect",
} as const;

function formatTimestamp(value?: string | null): string {
  if (!value) return "导入基线";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function formatMatrixValue(value: number): string {
  return String(value);
}

function CalibrationMatrix({
  label,
  values,
  columns,
}: {
  label: string;
  values: number[] | null | undefined;
  columns: number;
}) {
  if (!values) {
    return (
      <section className="flex flex-col gap-2">
        <h3 className="text-sm font-medium text-foreground">{label}</h3>
        <p className="text-sm text-muted-foreground">未提供</p>
      </section>
    );
  }
  const rows = Array.from({ length: values.length / columns }, (_, row) =>
    values.slice(row * columns, (row + 1) * columns),
  );
  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-sm font-medium text-foreground">{label}</h3>
      <Table aria-label={label}>
        <TableBody>
          {rows.map((row, rowIndex) => (
            <TableRow key={`${label}-${rowIndex}`}>
              {row.map((value, columnIndex) => (
                <TableCell
                  key={`${label}-${rowIndex}-${columnIndex}`}
                  className="text-right tabular-nums"
                >
                  {formatMatrixValue(value)}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </section>
  );
}

function RevisionRow({
  revision,
  current,
  canManage,
  onLoad,
}: {
  revision: SensorCalibrationRevisionOut;
  current: boolean;
  canManage: boolean;
  onLoad: () => void;
}) {
  return (
    <div className="flex min-h-14 items-center justify-between gap-3 py-2">
      <div className="flex min-w-0 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="inline-flex items-center gap-1">
            <span className="text-sm font-medium tabular-nums">Revision {revision.revision}</span>
            <CopyValueButton
              value={revision.digest}
              label={`复制 Revision ${revision.revision} 标定哈希`}
            />
          </span>
          {current && <Badge variant="secondary">当前</Badge>}
          {!revision.created_at && <Badge variant="outline">虚拟基线</Badge>}
        </div>
        <span className="text-xs text-muted-foreground">
          {formatTimestamp(revision.created_at)}
        </span>
      </div>
      {canManage && !current && (
        <Button type="button" size="xs" variant="ghost" onClick={onLoad}>
          <RotateCcwIcon data-icon="inline-start" />
          加载为草稿
        </Button>
      )}
    </div>
  );
}

export function SensorCalibrationSheet({
  open,
  onOpenChange,
  taskId,
  projectId,
  cameraName,
  cameraRole,
  calibration,
  revision,
  digest,
  canManage,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  taskId: string | null;
  projectId: string | null;
  cameraName: string;
  cameraRole: string;
  calibration: SensorCalibration;
  revision: number;
  digest: string;
  canManage: boolean;
}) {
  const pushToast = useToastStore((state) => state.push);
  const sensorCalibration = useSensorCalibration({
    taskId,
    cameraRole,
    projectId,
    enabled: open,
  });
  const latest = sensorCalibration.query.data?.items[0];
  const currentCalibration = latest?.calibration ?? calibration;
  const currentRevision = sensorCalibration.query.data?.current_revision ?? revision;
  const currentDigest = sensorCalibration.query.data?.current_digest ?? digest;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(() => formatCalibrationDraft(calibration));
  const [baseRevision, setBaseRevision] = useState(revision);
  const [baseDigest, setBaseDigest] = useState(digest);
  const [submitIssue, setSubmitIssue] = useState<string | null>(null);
  const [hasConflict, setHasConflict] = useState(false);
  const pendingIntent = useRef<{ payload: string; idempotencyKey: string } | null>(null);
  const previousScope = useRef<{
    taskId: string | null;
    cameraRole: string | null;
    open: boolean;
  } | null>(null);
  const parsedDraft = useMemo(() => parseCalibrationDraft(draft), [draft]);
  const changedParts = useMemo(
    () => (parsedDraft.ok ? changedCalibrationParts(currentCalibration, parsedDraft.value) : []),
    [currentCalibration, parsedDraft],
  );
  const remoteChanged =
    editing && (currentRevision !== baseRevision || currentDigest !== baseDigest);

  useEffect(() => {
    const previous = previousScope.current;
    const shouldReset =
      previous === null ||
      previous.taskId !== taskId ||
      previous.cameraRole !== cameraRole ||
      (open && !previous.open);
    previousScope.current = { taskId, cameraRole, open };
    if (!shouldReset) return;
    setEditing(false);
    setDraft(formatCalibrationDraft(calibration));
    setBaseRevision(revision);
    setBaseDigest(digest);
    setSubmitIssue(null);
    setHasConflict(false);
    pendingIntent.current = null;
  }, [cameraRole, taskId, open, calibration, digest, revision]);

  useEffect(() => {
    if (!open || editing) return;
    setDraft(formatCalibrationDraft(currentCalibration));
    setBaseRevision(currentRevision);
    setBaseDigest(currentDigest);
  }, [currentCalibration, currentDigest, currentRevision, editing, open]);

  const startEditing = (source = currentCalibration) => {
    setDraft(formatCalibrationDraft(source));
    setBaseRevision(currentRevision);
    setBaseDigest(currentDigest);
    setSubmitIssue(null);
    setHasConflict(false);
    pendingIntent.current = null;
    setEditing(true);
  };

  const submit = async () => {
    if (!parsedDraft.ok || changedParts.length === 0) return;
    setSubmitIssue(null);
    const payload = {
      calibration: parsedDraft.value,
      expected_revision: baseRevision,
      expected_digest: baseDigest,
    };
    const identity = JSON.stringify(payload);
    let intent = pendingIntent.current;
    if (!intent || intent.payload !== identity) {
      intent = { payload: identity, idempotencyKey: crypto.randomUUID() };
      pendingIntent.current = intent;
    }
    try {
      const updated = await sensorCalibration.update.mutateAsync({
        payload,
        idempotencyKey: intent.idempotencyKey,
      });
      pendingIntent.current = null;
      setEditing(false);
      setDraft(formatCalibrationDraft(updated.calibration));
      setBaseRevision(updated.revision);
      setBaseDigest(updated.digest);
      setHasConflict(false);
      const refreshed = await sensorCalibration.refreshRelated();
      pushToast({
        msg: refreshed
          ? `标定已追加为 revision ${updated.revision}`
          : `标定已追加为 revision ${updated.revision}，部分视图刷新失败`,
        sub: `${cameraName} · ${cameraRole}`,
        kind: refreshed ? "success" : "warning",
      });
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        pendingIntent.current = null;
        setHasConflict(true);
        const refreshed = await sensorCalibration.refreshRelated();
        if (!refreshed) {
          setSubmitIssue("标定已被其他操作更新，且部分关联视图刷新失败。请稍后重试刷新。");
        }
        return;
      }
      const refreshed = await sensorCalibration.refreshRelated();
      const message = error instanceof Error ? error.message : "无法确认标定保存结果";
      setSubmitIssue(
        refreshed
          ? `${message}。已重新读取当前标定，请核对 revision 后再操作。`
          : `${message}，且关联视图刷新失败。请稍后重试刷新。`,
      );
    }
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        className="sm:max-w-2xl"
        closeButtonClassName="top-2 right-2 flex size-10 items-center justify-center"
        closeButtonLabel="关闭"
        data-testid="sensor-calibration-sheet"
      >
        <SheetHeader>
          <div className="flex flex-wrap items-start justify-between gap-3 pr-12">
            <div className="flex min-w-0 flex-col gap-1">
              <SheetTitle>相机标定</SheetTitle>
              <SheetDescription>
                {cameraName} · {cameraRole}
              </SheetDescription>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="secondary">Revision {currentRevision}</Badge>
              <CopyValueButton value={currentDigest} label="复制当前标定哈希" />
            </div>
          </div>
        </SheetHeader>

        <ScrollArea className="min-h-0 flex-1 px-4">
          <div className="flex flex-col gap-5 pb-6">
            {!canManage && (
              <Alert>
                <HistoryIcon />
                <AlertTitle>只读标定</AlertTitle>
                <AlertDescription>
                  只有项目所有者或超级管理员可以追加 revision。你仍可检查当前矩阵和历史。
                </AlertDescription>
              </Alert>
            )}

            {submitIssue && (
              <Alert variant="warning">
                <TriangleAlertIcon />
                <AlertTitle>提交未完成</AlertTitle>
                <AlertDescription>{submitIssue}</AlertDescription>
              </Alert>
            )}

            {(hasConflict || remoteChanged) && (
              <Alert variant="warning">
                <TriangleAlertIcon />
                <AlertTitle>检测到新的 revision</AlertTitle>
                <AlertDescription className="flex flex-col items-start gap-3">
                  <span>当前草稿已保留，但不会自动获得新 revision 的写入凭据。</span>
                  {remoteChanged && (
                    <Button type="button" variant="default" onClick={() => startEditing()}>
                      基于最新 revision 重新编辑
                    </Button>
                  )}
                </AlertDescription>
              </Alert>
            )}

            {editing ? (
              <FieldGroup>
                <Field data-invalid={!parsedDraft.ok}>
                  <FieldLabel htmlFor="sensor-calibration-json">标定 JSON</FieldLabel>
                  <Textarea
                    id="sensor-calibration-json"
                    value={draft}
                    onChange={(event) => {
                      setDraft(event.target.value);
                      setSubmitIssue(null);
                    }}
                    aria-invalid={!parsedDraft.ok}
                    spellCheck={false}
                    className="min-h-[340px] resize-y"
                  />
                  <FieldDescription>
                    intrinsic 需要 9 个数字，extrinsic 需要 16 个数字，rect 可省略或提供 16 个数字。
                  </FieldDescription>
                  {!parsedDraft.ok && <FieldError>{parsedDraft.error}</FieldError>}
                </Field>

                {parsedDraft.ok && (
                  <Alert variant={changedParts.length > 0 ? "warning" : "default"}>
                    <TriangleAlertIcon />
                    <AlertTitle>
                      {changedParts.length > 0 ? "将追加新 revision" : "当前草稿没有变化"}
                    </AlertTitle>
                    <AlertDescription>
                      {changedParts.length > 0
                        ? `基于 revision ${baseRevision}，变更：${changedParts
                            .map((part) => PART_LABELS[part])
                            .join("、")}。已有人工 2D 成员不会改坐标，但会进入待复核状态。`
                        : "未改变任何矩阵，不会发送保存请求。"}
                    </AlertDescription>
                  </Alert>
                )}
              </FieldGroup>
            ) : (
              <div className="flex flex-col gap-5">
                <CalibrationMatrix
                  label={PART_LABELS.intrinsic}
                  values={currentCalibration.intrinsic}
                  columns={3}
                />
                <CalibrationMatrix
                  label={PART_LABELS.extrinsic}
                  values={currentCalibration.extrinsic}
                  columns={4}
                />
                <CalibrationMatrix
                  label={PART_LABELS.rect}
                  values={currentCalibration.rect}
                  columns={4}
                />
              </div>
            )}

            <Separator />

            <section className="flex flex-col gap-2" aria-labelledby="calibration-history-title">
              <div className="flex items-center justify-between gap-3">
                <h3 id="calibration-history-title" className="text-sm font-medium text-foreground">
                  Revision 历史
                </h3>
                {sensorCalibration.query.isFetching && !sensorCalibration.query.isLoading && (
                  <Spinner />
                )}
              </div>
              {sensorCalibration.query.isLoading && (
                <div className="flex flex-col gap-2">
                  <Skeleton className="h-14 w-full" />
                  <Skeleton className="h-14 w-full" />
                </div>
              )}
              {sensorCalibration.query.isError && (
                <Alert variant="destructive">
                  <TriangleAlertIcon />
                  <AlertTitle>无法读取标定历史</AlertTitle>
                  <AlertDescription>
                    <CopyableError
                      message={
                        sensorCalibration.query.error instanceof Error
                          ? sensorCalibration.query.error.message
                          : "请稍后重试"
                      }
                    />
                  </AlertDescription>
                </Alert>
              )}
              {sensorCalibration.query.data && (
                <ul className="m-0 flex list-none flex-col p-0">
                  {sensorCalibration.query.data.items.map((item, index) => (
                    <li key={item.revision}>
                      {index > 0 && <Separator />}
                      <RevisionRow
                        revision={item}
                        current={index === 0}
                        canManage={canManage}
                        onLoad={() => startEditing(item.calibration)}
                      />
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        </ScrollArea>

        {canManage && (
          <SheetFooter className="border-t border-border">
            {editing ? (
              <div className="flex flex-wrap justify-end gap-2">
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => {
                    setEditing(false);
                    setSubmitIssue(null);
                    setHasConflict(false);
                  }}
                  disabled={sensorCalibration.update.isPending}
                >
                  取消编辑
                </Button>
                <Button
                  type="button"
                  variant="primary"
                  disabled={
                    !parsedDraft.ok ||
                    changedParts.length === 0 ||
                    hasConflict ||
                    remoteChanged ||
                    sensorCalibration.update.isPending
                  }
                  onClick={() => void submit()}
                >
                  {sensorCalibration.update.isPending ? (
                    <Spinner data-icon="inline-start" />
                  ) : (
                    <SaveIcon data-icon="inline-start" />
                  )}
                  追加 revision
                </Button>
              </div>
            ) : (
              <Button type="button" variant="primary" onClick={() => startEditing()}>
                <PencilIcon data-icon="inline-start" />
                编辑当前标定
              </Button>
            )}
          </SheetFooter>
        )}
      </SheetContent>
    </Sheet>
  );
}
