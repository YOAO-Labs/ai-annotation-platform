import { useCallback, useContext, useEffect, useRef } from "react";
import { UNSAFE_NavigationContext } from "react-router-dom";
import { confirmDialog } from "@/components/ui/decisionDialog";

let activeTraversalGuard: ((event: PopStateEvent) => void) | undefined;

/** Register before BrowserRouter so a rejected pop cannot unmount the form. */
export function bindSettingsHistoryGuard() {
  const onPopState = (event: PopStateEvent) => activeTraversalGuard?.(event);
  window.addEventListener("popstate", onPopState);
  return () => window.removeEventListener("popstate", onPopState);
}

/**
 * Guard this BrowserRouter form, including browser back/forward traversal.
 *
 * react-router 的 navigator.push/replace 必须同步返回,confirmDialog 却是异步的,
 * 因此采用「延迟导航」(plan「Hard cases」):同步 override 只捕获 transition,
 * 由 confirmDialog 异步决定,确认后通过原 navigator 方法重放被拦截的导航;
 * popstate 无法异步决定,先回弹拒绝,确认后用 history.go 重放同一次遍历。
 * beforeunload 保持原生 (浏览器规范要求同步)。
 *
 * 返回共享的确认入口,SettingsPage 的分区切换等非路由离开复用同一份文案与防重入。
 */
export function useUnsavedSettingsGuard(dirty: boolean): (proceed: () => void) => void {
  const navigation = useContext(UNSAFE_NavigationContext);
  // 待决确认防重入:对话框 (Radix 模态) 打开期间新的离开请求一律忽略。
  const pendingRef = useRef(false);

  const confirmLeaveAndRun = useCallback((proceed: () => void) => {
    if (pendingRef.current) return;
    pendingRef.current = true;
    void confirmDialog({
      title: "系统设置有未保存修改，确定离开吗？",
      confirmLabel: "离开",
      cancelLabel: "留在本页",
    })
      .then((leave) => {
        if (leave) proceed();
      })
      .finally(() => {
        pendingRef.current = false;
      });
  }, []);

  useEffect(() => {
    if (!dirty || !navigation) return;
    const navigator = navigation.navigator;
    let active = true;
    const originalPush = navigator.push;
    const originalReplace = navigator.replace;
    const guardedPush: typeof navigator.push = (...args) => {
      confirmLeaveAndRun(() => {
        if (active) originalPush.apply(navigator, args);
      });
    };
    const guardedReplace: typeof navigator.replace = (...args) => {
      confirmLeaveAndRun(() => {
        if (active) originalReplace.apply(navigator, args);
      });
    };
    navigator.push = guardedPush;
    navigator.replace = guardedReplace;

    // BrowserRouter records an index for same-document history entries. The
    // bootstrap listener runs first so rejecting a traversal keeps the form
    // mounted while restoring the previously accepted URL/index.
    let currentIndex: unknown = window.history.state?.idx;
    let restoring = false;
    let replaying = false;
    const onPopState = (event: PopStateEvent) => {
      const nextIndex: unknown = event.state?.idx;
      if (restoring) {
        restoring = false;
        event.stopImmediatePropagation();
        return;
      }
      if (replaying) {
        // 确认后的重放遍历:放行给 BrowserRouter 处理。
        replaying = false;
        currentIndex = nextIndex;
        return;
      }
      if (
        typeof currentIndex === "number" &&
        typeof nextIndex === "number" &&
        nextIndex !== currentIndex
      ) {
        const rejectedFrom = currentIndex;
        const rejectedTo = nextIndex;
        // 同步阶段无法等待异步确认:先一律回弹,保持表单挂载与原 URL/index。
        event.stopImmediatePropagation();
        restoring = true;
        window.history.go(rejectedFrom - rejectedTo);
        // 异步确认;确认后重放同一次遍历。已有待决确认时这次遍历已被回弹拒绝,不再询问。
        confirmLeaveAndRun(() => {
          if (!active) return;
          replaying = true;
          window.history.go(rejectedTo - rejectedFrom);
        });
        return;
      }
      currentIndex = nextIndex;
    };
    activeTraversalGuard = onPopState;
    return () => {
      active = false;
      if (navigator.push === guardedPush) navigator.push = originalPush;
      if (navigator.replace === guardedReplace) navigator.replace = originalReplace;
      if (activeTraversalGuard === onPopState) activeTraversalGuard = undefined;
    };
  }, [confirmLeaveAndRun, dirty, navigation]);

  return confirmLeaveAndRun;
}
