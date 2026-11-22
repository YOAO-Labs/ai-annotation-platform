import { create } from "zustand";
import type { ReactNode } from "react";

import type { IconName } from "@/components/ui/Icon";

const DEFAULT_CANCEL_LABEL = "取消";
const DEFAULT_ALERT_CONFIRM_LABEL = "确定";

export type DecisionTone = "default" | "danger";

export interface ConfirmDialogOptions {
  tone?: DecisionTone;
  title: string;
  description?: string;
  details?: ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  icon?: IconName;
  defaultFocus?: "confirm" | "cancel";
}

export interface ChoiceOption {
  key: string;
  label: string;
  tone?: DecisionTone;
  description?: string;
}

export interface ChoiceDialogOptions {
  title: string;
  description?: string;
  options: ChoiceOption[];
}

export interface InputDialogOptions {
  title: string;
  description?: string;
  label: string;
  placeholder?: string;
  initialValue?: string;
  required?: boolean;
  maxLength?: number;
  confirmLabel: string;
  cancelLabel?: string;
  tone?: DecisionTone;
  validate?: (value: string) => string | null;
  icon?: IconName;
  presets?: string[];
}

export interface AlertDialogOptions {
  title: string;
  description?: string;
  details?: ReactNode;
  confirmLabel?: string;
}

type DecisionDialogKind = "confirm" | "choice" | "input" | "alert";

export interface DecisionDialogRequest {
  kind: DecisionDialogKind;
  tone: DecisionTone;
  title: string;
  description?: string;
  details?: ReactNode;
  icon?: IconName;
  confirmLabel: string;
  cancelLabel: string;
  defaultFocus: "confirm" | "cancel" | "input" | "option";
  options?: ChoiceOption[];
  label?: string;
  placeholder?: string;
  initialValue?: string;
  required: boolean;
  maxLength?: number;
  presets?: string[];
  validate?: (value: string) => string | null;
  resolve: (value: boolean | string | null) => void;
}

export interface QueuedDecisionDialog {
  uid: number;
  request: DecisionDialogRequest;
  settled: boolean;
}

interface DecisionDialogStore {
  queue: QueuedDecisionDialog[];
  enqueue: (request: DecisionDialogRequest) => void;
  settle: (value: boolean | string | null) => void;
  dispose: () => void;
  cancelAll: () => void;
}

let nextUid = 0;

export const useDecisionDialogStore = create<DecisionDialogStore>((set, get) => ({
  queue: [],
  enqueue: (request) =>
    set((s) => ({ queue: [...s.queue, { uid: ++nextUid, request, settled: false }] })),
  settle: (value) => {
    const head = get().queue[0];
    if (!head || head.settled) return;
    set((s) => ({
      queue: s.queue.map((item, index) => (index === 0 ? { ...item, settled: true } : item)),
    }));
    head.request.resolve(value);
  },
  dispose: () => set((s) => (s.queue[0]?.settled ? { queue: s.queue.slice(1) } : s)),
  cancelAll: () =>
    set((s) => {
      for (const item of s.queue) {
        if (item.settled) continue;
        item.request.resolve(item.request.kind === "confirm" ? false : null);
      }
      return { queue: [] };
    }),
}));

export function confirmDialog(options: ConfirmDialogOptions): Promise<boolean> {
  const tone = options.tone ?? "default";
  return new Promise<boolean>((resolve) => {
    useDecisionDialogStore.getState().enqueue({
      kind: "confirm",
      tone,
      title: options.title,
      description: options.description,
      details: options.details,
      confirmLabel: options.confirmLabel,
      cancelLabel: options.cancelLabel ?? DEFAULT_CANCEL_LABEL,
      icon: options.icon ?? (tone === "danger" ? "warning" : undefined),
      required: false,
      defaultFocus: options.defaultFocus ?? (tone === "danger" ? "cancel" : "confirm"),
      resolve: (value) => resolve(value as boolean),
    });
  });
}

export function choiceDialog(options: ChoiceDialogOptions): Promise<string | null> {
  if (options.options.length === 0) return Promise.resolve(null);
  return new Promise<string | null>((resolve) => {
    useDecisionDialogStore.getState().enqueue({
      kind: "choice",
      tone: "default",
      title: options.title,
      description: options.description,
      confirmLabel: "",
      cancelLabel: DEFAULT_CANCEL_LABEL,
      defaultFocus: "option",
      options: options.options,
      required: false,
      resolve: (value) => resolve(value as string | null),
    });
  });
}

export function inputDialog(options: InputDialogOptions): Promise<string | null> {
  const tone = options.tone ?? "default";
  return new Promise<string | null>((resolve) => {
    useDecisionDialogStore.getState().enqueue({
      kind: "input",
      tone,
      title: options.title,
      description: options.description,
      label: options.label,
      placeholder: options.placeholder,
      initialValue: options.initialValue,
      required: options.required ?? false,
      maxLength: options.maxLength,
      presets: options.presets,
      confirmLabel: options.confirmLabel,
      cancelLabel: options.cancelLabel ?? DEFAULT_CANCEL_LABEL,
      icon: options.icon ?? (tone === "danger" ? "warning" : undefined),
      validate: options.validate,
      defaultFocus: tone === "danger" ? "cancel" : "input",
      resolve: (value) => resolve(value as string | null),
    });
  });
}

export function alertDialog(options: AlertDialogOptions): Promise<void> {
  return new Promise<void>((resolve) => {
    useDecisionDialogStore.getState().enqueue({
      kind: "alert",
      tone: "default",
      title: options.title,
      description: options.description,
      details: options.details,
      confirmLabel: options.confirmLabel ?? DEFAULT_ALERT_CONFIRM_LABEL,
      cancelLabel: DEFAULT_CANCEL_LABEL,
      icon: "warning",
      defaultFocus: "confirm",
      required: false,
      resolve: () => resolve(),
    });
  });
}
