import { create } from "zustand";
import type { ReactNode } from "react";

import type { IconName } from "@/components/ui/Icon";

/**
 * decisionDialog —— 命令式决策对话框服务(docs/plans/archive/1789527942_decision-dialog-consolidation.md Phase 0)。
 *
 * confirm/choice/input/alert 四个入口都返回 Promise,hook 与非 React 模块可一行调用;
 * 对话框本体由 App.tsx 两处挂载的 <DecisionDialogHost /> 渲染(与 ToastRack 同位,覆盖全屏工作台)。
 * 服务只承载决策结果(计划 Decision 1):confirm → boolean,input/choice → string|null,
 * 没有 async 确认回调和内置 pending 态,spinner/禁用态仍由调用方自理。
 * 文案模板一律由调用方传入;这里只内置 UI 级兜底文案(取消/确定/必填报错)。
 */
const DEFAULT_CANCEL_LABEL = "取消";
const DEFAULT_ALERT_CONFIRM_LABEL = "确定";

export type DecisionTone = "default" | "danger";

/** confirm:确认 → true;取消/Esc/点遮罩 → false。 */
export interface ConfirmDialogOptions {
  tone?: DecisionTone;
  title: string;
  /** 纯文本,自动换行 */
  description?: string;
  /** 受影响数量/名单等补充块(如「将删除 3 个数据集 · 1,204 张图片」) */
  details?: ReactNode;
  /** 动词,如「删除数据集」 */
  confirmLabel: string;
  cancelLabel?: string;
  /** 默认 warning(danger 时) */
  icon?: IconName;
  /** 危险对话框默认聚焦「取消」,普通对话框默认聚焦「确认」 */
  defaultFocus?: "confirm" | "cancel";
}

/** choice 的多选一按钮;Esc/点遮罩 → null。 */
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

/** input:确认 → 填写值(首尾去空白);取消/Esc/点遮罩 → null。 */
export interface InputDialogOptions {
  title: string;
  description?: string;
  label: string;
  placeholder?: string;
  /** 预填初始值(如退回补充说明预填「标注员跳过：…」),仍可编辑、计入必填/maxLength。 */
  initialValue?: string;
  required?: boolean;
  maxLength?: number;
  confirmLabel: string;
  cancelLabel?: string;
  tone?: DecisionTone;
  /** 返回错误文案则就地报错、不关窗;返回 null 通过 */
  validate?: (value: string) => string | null;
  /** 默认 warning(danger 时) */
  icon?: IconName;
  /** 可选预设快速标签气泡（如质检退回高频短语），点击快捷追加到输入框 */
  presets?: string[];
}

/** alert:单按钮通知,确认/Esc/点遮罩 → void。 */
export interface AlertDialogOptions {
  title: string;
  description?: string;
  details?: ReactNode;
  confirmLabel?: string;
}

type DecisionDialogKind = "confirm" | "choice" | "input" | "alert";

/** Host 渲染所需的展平请求(内部类型,勿在调用方构造)。 */
export interface DecisionDialogRequest {
  kind: DecisionDialogKind;
  tone: DecisionTone;
  title: string;
  description?: string;
  details?: ReactNode;
  icon?: IconName;
  confirmLabel: string;
  cancelLabel: string;
  /** confirm/alert → 确认键;cancel → 取消键;input → 输入框;choice → 首个选项 */
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
  /** 队列内自增 id:换头时让 Host 重建本地 state(输入值/报错) */
  uid: number;
  request: DecisionDialogRequest;
  /** 已决策:等待退场动画播完再出队,期间保持挂载播放 data-[state=closed] 动画 */
  settled: boolean;
}

interface DecisionDialogStore {
  queue: QueuedDecisionDialog[];
  enqueue: (request: DecisionDialogRequest) => void;
  /** 结算队首:立即 resolve,再走退场动画;重复结算(如 Action 点击后 Radix 又请求关闭)幂等忽略 */
  settle: (value: boolean | string | null) => void;
  /** 队首退场完毕(内容已卸载)后出队,下一条接管 */
  dispose: () => void;
  /**
   * 清空整条队列并按各 kind 的取消值结算未决请求(confirm → false,其余 → null)。
   * 认证归属变更(登出/换账号)时由 App 层调用:排队中的请求属于旧会话的调用方,
   * 不能带着新凭据继续执行其捕获的变更(Codex P1,plan 1789527942 评审意见)。
   */
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
        // confirm 取消 = false;choice/input 取消 = null;alert 的 resolve 忽略入参。
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
      // Host 按 kind 结算 boolean,窄化转换安全。
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
