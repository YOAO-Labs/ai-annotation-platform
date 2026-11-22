import { create } from "zustand";
import type { AnnotationFeedback } from "@/api/feedbacks";
import { useAuthStore } from "@/stores/authStore";

export interface IssueDetailScope {
  projectId: string;
  taskId: string | null;
}

interface ActiveIssueState {
  highlightId: string | null;
  focusTick: number;
  focusTarget: AnnotationFeedback | null;
  tabRequestTick: number;
  pinRequestTick: number;
  pinTarget: AnnotationFeedback | null;
  detailRequestTick: number;
  detailTarget: AnnotationFeedback | null;
  detailTargetId: string | null;
  detailOwnerId: string | null;
  detailScope: IssueDetailScope | null;
  focusIssue: (target: string | AnnotationFeedback) => void;
  highlightFromPin: (target: string | AnnotationFeedback) => void;
  requestIssuesTab: () => void;
  openIssueDetail: (target: string | AnnotationFeedback, scope?: IssueDetailScope) => void;
  closeIssueDetail: () => void;
  setHighlightId: (id: string | null) => void;
}

export const useActiveIssueStore = create<ActiveIssueState>((set) => ({
  highlightId: null,
  focusTick: 0,
  focusTarget: null,
  tabRequestTick: 0,
  pinRequestTick: 0,
  pinTarget: null,
  detailRequestTick: 0,
  detailTarget: null,
  detailTargetId: null,
  detailOwnerId: null,
  detailScope: null,
  focusIssue: (target) =>
    set((s) => ({
      highlightId: typeof target === "string" ? target : target.id,
      focusTarget: typeof target === "string" ? null : structuredClone(target),
      focusTick: s.focusTick + 1,
    })),
  highlightFromPin: (target) =>
    set((s) => ({
      highlightId: typeof target === "string" ? target : target.id,
      pinTarget: typeof target === "string" ? null : structuredClone(target),
      pinRequestTick: s.pinRequestTick + 1,
      tabRequestTick: s.tabRequestTick + 1,
      detailRequestTick: s.detailRequestTick + 1,
      detailTarget: typeof target === "string" ? null : structuredClone(target),
      detailTargetId: typeof target === "string" ? target : target.id,
      detailOwnerId: useAuthStore.getState().user?.id ?? null,
      detailScope:
        typeof target === "string"
          ? null
          : { projectId: target.project_id, taskId: target.task_id },
    })),
  requestIssuesTab: () =>
    set((s) => ({
      tabRequestTick: s.tabRequestTick + 1,
      pinTarget: null,
      detailTarget: null,
      detailTargetId: null,
      detailOwnerId: null,
      detailScope: null,
    })),
  openIssueDetail: (target, scope) =>
    set((s) => ({
      detailRequestTick: s.detailRequestTick + 1,
      detailTarget: typeof target === "string" ? null : structuredClone(target),
      detailTargetId: typeof target === "string" ? target : target.id,
      detailOwnerId: useAuthStore.getState().user?.id ?? null,
      detailScope:
        scope ??
        (typeof target === "string"
          ? null
          : { projectId: target.project_id, taskId: target.task_id }),
    })),
  closeIssueDetail: () =>
    set({ detailTarget: null, detailTargetId: null, detailOwnerId: null, detailScope: null }),
  setHighlightId: (highlightId) => set({ highlightId }),
}));

type AuthIdentity = ReturnType<typeof useAuthStore.getState>;
const authIdentity = (state: AuthIdentity) => (state.token ? (state.user?.id ?? null) : null);
useAuthStore.subscribe((next, previous) => {
  if (authIdentity(next) === authIdentity(previous)) return;
  useActiveIssueStore.setState((state) => ({
    highlightId: null,
    focusTarget: null,
    pinTarget: null,
    detailTarget: null,
    detailTargetId: null,
    detailOwnerId: null,
    detailScope: null,
    detailRequestTick: state.detailRequestTick + 1,
    pinRequestTick: state.pinRequestTick + 1,
  }));
});
