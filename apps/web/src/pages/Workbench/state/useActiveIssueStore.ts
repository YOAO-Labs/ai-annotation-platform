/**
 * DiscussionPanel issues tab ↔ IssueLayer 图钉双向联动的轻量共享 store。
 *
 * - highlightId: 当前高亮的 issue feedback id。列表单击 / 图钉单击都写它，
 *   IssueLayer (pin 圆环加亮) 与 issues tab 列表行 (描边) 同时读它。
 * - focusTick: 列表 → 画布定位的请求计数。issues tab 单击列表项时 bump，
 *   model 监听后把视口平移到对应图钉。
 * - tabRequestTick: 画布 → tab 切换的请求计数。单击/hover 图钉、或工作台 issue FAB 时 bump，
 *   DiscussionPanel 监听后切到 issues tab。
 * - detailRequestTick/detailTarget: Issue pin/title → detail 视图的独立请求。
 *   它和 focusTick 分开，因此打开详情不会改变画布；同一图钉再次点击也会
 *   产生新的激活请求。
 */
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
  /** Explicit pin requests are distinct from a generic Issues-tab/FAB request. */
  pinRequestTick: number;
  pinTarget: AnnotationFeedback | null;
  /** Issue detail activation is independent from canvas focus/navigation. */
  detailRequestTick: number;
  /** Complete detail snapshot when the activator already has the record. */
  detailTarget: AnnotationFeedback | null;
  /** Root id is retained for direct activations that do not have a snapshot. */
  detailTargetId: string | null;
  /** Auth owner captured when the request was emitted, for stale-session guards. */
  detailOwnerId: string | null;
  /** Optional scope binding for direct/string activations from later deep links. */
  detailScope: IssueDetailScope | null;
  /** 列表单击：高亮 + 请求画布定位到该图钉。 */
  focusIssue: (target: string | AnnotationFeedback) => void;
  /** 图钉单击/hover：高亮 + 请求切到 issues tab。 */
  highlightFromPin: (target: string | AnnotationFeedback) => void;
  /** 仅请求切到 issues tab (工作台 issue FAB)，不改高亮。 */
  requestIssuesTab: () => void;
  /** Open an Issue detail without focusing or moving the canvas. */
  openIssueDetail: (target: string | AnnotationFeedback, scope?: IssueDetailScope) => void;
  /** Return to the list without changing the current highlight. */
  closeIssueDetail: () => void;
  /** 仅设置高亮 (hover 等不触发 tab 切换的场景)。 */
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
      // A generic FAB request is not a replay of the last pin/detail request.
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

// The pin/detail store is intentionally lightweight and cannot own the
// authenticated discussion lease. It still retires its activation snapshots
// synchronously when the auth identity changes, while preserving ordinary
// same-user token renewal. This prevents a logout/login remount from replaying
// an old user's pin into the new Workbench.
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
