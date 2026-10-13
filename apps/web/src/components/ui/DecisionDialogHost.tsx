import { useEffect, useId, useRef, useState, type MouseEvent as ReactMouseEvent } from "react";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogMedia,
  AlertDialogTitle,
} from "@/components/shadcn/ui/alert-dialog";
import { Label } from "@/components/shadcn/ui/label";
import { Textarea } from "@/components/shadcn/ui/textarea";
import { Icon } from "@/components/ui/Icon";
import { cn } from "@/lib/utils";

import { useDecisionDialogStore, type QueuedDecisionDialog } from "./decisionDialog";

const REQUIRED_INPUT_MESSAGE = "请填写此项";

// 克制的危险态(设计规范):语义 status token 着色图标与确认按钮,
// 柔和底色只给图标容器,不做整块红。
const DANGER_MEDIA_CLASS = "bg-status-danger-soft text-status-danger";
const DANGER_ACTION_CLASS =
  "border-status-danger/30 text-status-danger hover:bg-status-danger-soft focus-visible:ring-status-danger/30";

/**
 * <DecisionDialogHost /> —— decisionDialog 服务的渲染端,一次一条地消费队列。
 * App.tsx 在两个 ToastRack 旁各挂一份(AppShell + FullScreenWorkbench,全屏工作台不渲染 AppShell)。
 */
export function DecisionDialogHost() {
  const head = useDecisionDialogStore((s) => s.queue[0]);
  if (!head) return null;
  // key=uid:换头时重建本地 state(输入值/报错);已结算条目在退场动画期间保持挂载。
  return <ActiveDecisionDialog key={head.uid} entry={head} />;
}

function ActiveDecisionDialog({ entry }: { entry: QueuedDecisionDialog }) {
  const { request, settled } = entry;
  const settle = useDecisionDialogStore((s) => s.settle);
  const dispose = useDecisionDialogStore((s) => s.dispose);
  const contentRef = useRef<HTMLElement | null>(null);
  // input 预填(如退回补充说明预填「标注员跳过：…」);key=uid 换头时随本地 state 一并重建。
  const [value, setValue] = useState(request.initialValue ?? "");
  const [error, setError] = useState<string | null>(null);
  const inputId = useId();
  const errorId = useId();

  const isDanger = request.tone === "danger";
  // confirm 取消 = false;choice/input 取消 = null;alert 的 resolve 忽略入参。
  const settleCancel = () => settle(request.kind === "confirm" ? false : null);

  // AlertDialog 原语把 onPointerDownOutside/onInteractOutside 固定成仅 preventDefault
  // (@radix-ui/react-alert-dialog 1.1.17 不透传遮罩点击),这里自挂 document 监听补齐
  // 「点遮罩取消」:pointerdown 落点不在当前 alertdialog 内即视为遮罩点击。
  useEffect(() => {
    if (settled) return;
    const onPointerDown = (event: PointerEvent) => {
      if (contentRef.current?.contains(event.target as Node)) return;
      settle(request.kind === "confirm" ? false : null);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [settled, request, settle]);

  // 默认聚焦:危险 → 取消;普通 confirm/alert → 确认;input 普通态 → 输入框;choice → 首个选项。
  // preventDefault 接管 Radix 默认(其内部默认也是聚焦取消键)。
  const handleOpenAutoFocus = (event: Event) => {
    event.preventDefault();
    const content = event.currentTarget;
    if (!(content instanceof HTMLElement)) return;
    contentRef.current = content;
    const selector =
      request.defaultFocus === "cancel"
        ? '[data-slot="alert-dialog-cancel"]'
        : request.defaultFocus === "input"
          ? "[data-decision-dialog-input]"
          : request.defaultFocus === "option"
            ? "[data-decision-dialog-option]"
            : '[data-slot="alert-dialog-action"]';
    content.querySelector<HTMLElement>(selector)?.focus();
  };

  // 退场动画播完、Radix Presence 卸载内容(FocusScope unmount)时出队,队列下一条接管。
  const handleCloseAutoFocus = () => {
    dispose();
  };

  const submitInput = (event: ReactMouseEvent<HTMLButtonElement>) => {
    const text = value.trim();
    const problem =
      request.required && !text ? REQUIRED_INPUT_MESSAGE : (request.validate?.(text) ?? null);
    if (problem) {
      setError(problem);
      // 阻止 AlertDialogAction(内部是 DialogPrimitive.Close)自动关窗,留在原地改错。
      event.preventDefault();
      return;
    }
    settle(text);
  };

  return (
    <AlertDialog
      open={!settled}
      onOpenChange={(open) => {
        if (!open) settleCancel();
      }}
    >
      <AlertDialogContent
        size={request.kind === "input" ? "default" : "sm"}
        className="border-border bg-card"
        // 决策对话框是模态:挂上 isWorkbenchInteractionBlocked 识别的 [data-modal] 标记,
        // 否则 Workbench 的窗口级快捷键(如 review 流的 A/R)会穿透对话框误触后台操作。
        data-modal=""
        onOpenAutoFocus={handleOpenAutoFocus}
        onCloseAutoFocus={handleCloseAutoFocus}
      >
        <AlertDialogHeader>
          {request.icon ? (
            <AlertDialogMedia className={isDanger ? DANGER_MEDIA_CLASS : "text-muted-foreground"}>
              <Icon name={request.icon} />
            </AlertDialogMedia>
          ) : null}
          <AlertDialogTitle>{request.title}</AlertDialogTitle>
          {request.description ? (
            <AlertDialogDescription>{request.description}</AlertDialogDescription>
          ) : null}
        </AlertDialogHeader>
        {request.details ? (
          <div className="rounded-md border border-border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
            {request.details}
          </div>
        ) : null}
        {request.kind === "input" ? (
          <div className="grid gap-2">
            <Label htmlFor={inputId}>
              {request.label}
              {request.required ? <span className="text-status-danger">*</span> : null}
            </Label>
            <Textarea
              id={inputId}
              data-decision-dialog-input=""
              value={value}
              placeholder={request.placeholder}
              maxLength={request.maxLength}
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? errorId : undefined}
              onChange={(event) => {
                setValue(event.target.value);
                setError(null); // 输入即清除上次提交的报错
              }}
            />
            <div className="flex items-start justify-between gap-3">
              {error ? (
                <p id={errorId} role="alert" className="text-xs font-medium text-status-danger">
                  {error}
                </p>
              ) : (
                <span className="flex-1" />
              )}
              {request.maxLength !== undefined ? (
                <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                  {value.length}/{request.maxLength}
                </span>
              ) : null}
            </div>
            {request.presets && request.presets.length > 0 ? (
              <div className="flex flex-col gap-1.5 pt-0.5" data-testid="decision-dialog-presets">
                <span className="text-2xs font-medium text-muted-foreground">快捷预设标签：</span>
                <div className="flex flex-wrap gap-1.5">
                  {request.presets.map((preset) => (
                    <button
                      key={preset}
                      type="button"
                      className="cursor-pointer appearance-none rounded-md border border-border bg-muted/50 px-2 py-0.5 text-2xs text-muted-foreground transition-colors hover:border-brand/40 hover:bg-brand/10 hover:text-brand focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring active:scale-95"
                      onClick={() => {
                        setValue((prev) => {
                          const trimmed = prev.trim();
                          if (!trimmed) return preset;
                          if (trimmed.includes(preset)) return prev;
                          return `${trimmed}；${preset}`;
                        });
                        setError(null);
                      }}
                    >
                      + {preset}
                    </button>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        ) : null}
        <AlertDialogFooter>
          {request.kind === "choice" ? (
            request.options?.map((option) => (
              <AlertDialogAction
                key={option.key}
                data-decision-dialog-option=""
                variant="outline"
                className={cn(option.tone === "danger" && DANGER_ACTION_CLASS)}
                onClick={() => settle(option.key)}
              >
                {option.label}
              </AlertDialogAction>
            ))
          ) : request.kind === "alert" ? (
            <AlertDialogAction onClick={() => settle(null)}>
              {request.confirmLabel}
            </AlertDialogAction>
          ) : (
            <>
              <AlertDialogCancel>{request.cancelLabel}</AlertDialogCancel>
              <AlertDialogAction
                variant={isDanger ? "outline" : "default"}
                className={isDanger ? DANGER_ACTION_CLASS : undefined}
                onClick={request.kind === "input" ? submitInput : () => settle(true)}
              >
                {request.confirmLabel}
              </AlertDialogAction>
            </>
          )}
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
