import { create } from "zustand";
import type { MouseEvent } from "react";
import { toast } from "sonner";

import { Toaster } from "@/components/shadcn/ui/sonner";
import { copyToClipboard } from "@/lib/clipboard";

type ToastKind = "success" | "warning" | "error" | "";

interface ToastData {
  msg: string;
  sub?: string;
  kind?: ToastKind;
  retry?: () => boolean;
}

interface ToastStore {
  push: (toast: ToastData) => void;
}

export const useToastStore = create<ToastStore>(() => ({
  push: ({ msg, sub, kind, retry }) => {
    const duration = retry ? Infinity : kind === "error" ? 6000 : kind === "warning" ? 4500 : 3500;
    const diagnostic = kind === "error" || (kind !== "success" && !!sub);
    const opts = {
      description: sub,
      duration,
      closeButton: !!retry,
      ...(diagnostic && {
        classNames: {
          content: "min-w-0",
          title: "max-h-40 overflow-auto whitespace-pre-wrap break-words",
          description: "max-h-40 overflow-auto whitespace-pre-wrap break-words",
        },
        action: {
          label: "复制详情",
          onClick: async (event: MouseEvent<HTMLButtonElement>) => {
            event.preventDefault();
            const copied = await copyToClipboard(sub ? `${msg}\n${sub}` : msg);
            const feedback = copied ? "已复制完整错误信息" : "复制失败，请检查浏览器剪贴板权限";
            if (copied) toast.success(feedback);
            else toast.error(feedback);
          },
        },
      }),
    };
    if (retry) {
      toast.error(msg, {
        ...opts,
        action: {
          label: "重试",
          onClick: (event) => {
            if (!retry()) event.preventDefault();
          },
        },
      });
      return;
    }
    if (kind === "success") toast.success(msg, opts);
    else if (kind === "warning") toast.warning(msg, opts);
    else if (kind === "error") toast.error(msg, opts);
    else toast(msg, opts);
  },
}));

export function ToastRack() {
  return <Toaster position="top-right" richColors />;
}
