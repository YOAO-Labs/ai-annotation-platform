import type { ReactNode } from "react";
import type { ReviewClaimResponse } from "@/types";
import type { SkipReason } from "../shell/SkipTaskModal";

export type WorkbenchMode = "annotate" | "review";
export type DiffMode = "final" | "raw" | "diff";
export type NavigateTask = (direction: "next" | "prev") => void;
export type SmartNext = (mode: "open" | "uncertain") => void;

export interface WorkbenchToast {
  msg: string;
  sub?: string;
  kind?: "success" | "warning" | "error" | "";
}

export type PushToast = (toast: WorkbenchToast) => void;

export interface WorkbenchTopbarActions {
  canSubmit?: boolean;
  onSubmit?: () => void;
  isSubmitting?: boolean;
  onSmartNextOpen?: () => void;
  onSmartNextUncertain?: () => void;
  canApprove?: boolean;
  canReject?: boolean;
  onApprove?: () => void;
  onReject?: () => void;
  isApproving?: boolean;
  isRejecting?: boolean;
  canWithdraw?: boolean;
  canReopen?: boolean;
  isWithdrawing?: boolean;
  isReopening?: boolean;
  onWithdraw?: () => void;
  onReopen?: () => void;
  isSkipping?: boolean;
  onSkip?: (reason: SkipReason, note?: string) => void;
  reviewInfoSlot?: ReactNode;
}

export interface WorkbenchBannerActions {
  canWithdraw: boolean;
  isWithdrawing: boolean;
  isReopening: boolean;
  isAcceptingRejection: boolean;
  onWithdraw: () => void;
  onReopen: () => void;
  onAcceptRejection: () => void;
}

export interface WorkbenchModeState {
  isLocked: boolean;
  diffMode?: DiffMode;
  onSetDiffMode?: (mode: DiffMode) => void;
  banners?: ReactNode;
  claimInfo: ReviewClaimResponse | null;
  topbarActions: WorkbenchTopbarActions;
  bannerActions: WorkbenchBannerActions;
}
