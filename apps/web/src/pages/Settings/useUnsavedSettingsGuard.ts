import { useCallback, useContext, useEffect, useRef } from "react";
import { UNSAFE_NavigationContext } from "react-router-dom";
import { confirmDialog } from "@/components/ui/decisionDialog";

let activeTraversalGuard: ((event: PopStateEvent) => void) | undefined;

export function bindSettingsHistoryGuard() {
  const onPopState = (event: PopStateEvent) => activeTraversalGuard?.(event);
  window.addEventListener("popstate", onPopState);
  return () => window.removeEventListener("popstate", onPopState);
}

export function useUnsavedSettingsGuard(dirty: boolean): (proceed: () => void) => void {
  const navigation = useContext(UNSAFE_NavigationContext);
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
        event.stopImmediatePropagation();
        restoring = true;
        window.history.go(rejectedFrom - rejectedTo);
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
