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

const DANGER_MEDIA_CLASS = "bg-status-danger-soft text-status-danger";
const DANGER_ACTION_CLASS =
  "border-status-danger/30 text-status-danger hover:bg-status-danger-soft focus-visible:ring-status-danger/30";

export function DecisionDialogHost() {
  const head = useDecisionDialogStore((s) => s.queue[0]);
  if (!head) return null;
  return <ActiveDecisionDialog key={head.uid} entry={head} />;
}

function ActiveDecisionDialog({ entry }: { entry: QueuedDecisionDialog }) {
  const { request, settled } = entry;
  const settle = useDecisionDialogStore((s) => s.settle);
  const dispose = useDecisionDialogStore((s) => s.dispose);
  const contentRef = useRef<HTMLElement | null>(null);
  const [value, setValue] = useState(request.initialValue ?? "");
  const [error, setError] = useState<string | null>(null);
  const inputId = useId();
  const errorId = useId();

  const isDanger = request.tone === "danger";
  const settleCancel = () => settle(request.kind === "confirm" ? false : null);

  useEffect(() => {
    if (settled) return;
    const onPointerDown = (event: PointerEvent) => {
      if (contentRef.current?.contains(event.target as Node)) return;
      settle(request.kind === "confirm" ? false : null);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [settled, request, settle]);

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

  const handleCloseAutoFocus = () => {
    dispose();
  };

  const submitInput = (event: ReactMouseEvent<HTMLButtonElement>) => {
    const text = value.trim();
    const problem =
      request.required && !text ? REQUIRED_INPUT_MESSAGE : (request.validate?.(text) ?? null);
    if (problem) {
      setError(problem);
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
                setError(null);
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
