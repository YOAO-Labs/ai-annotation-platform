import { useCallback, useRef, useState } from "react";

import type { FilterDraftValidityChange } from "./FilterValueEditor";

export function useFilterDraftValidity(owner: string) {
  const ownerRef = useRef(owner);
  const [draftState, setDraftState] = useState(() => ({
    owner,
    invalidEditors: new Set<string>(),
  }));
  ownerRef.current = owner;

  const onDraftValidityChange = useCallback<FilterDraftValidityChange>((valid, editorId) => {
    const currentOwner = ownerRef.current;
    if (!editorId || !editorId.startsWith(`${currentOwner}:`)) return;
    setDraftState((current) => {
      const currentEditors =
        current.owner === currentOwner ? current.invalidEditors : new Set<string>();
      const currentlyInvalid = currentEditors.has(editorId);
      if (valid === !currentlyInvalid && current.owner === currentOwner) return current;
      const next = new Set(currentEditors);
      if (valid) next.delete(editorId);
      else next.add(editorId);
      return { owner: currentOwner, invalidEditors: next };
    });
  }, []);

  return {
    hasInvalidDraft: draftState.owner === owner && draftState.invalidEditors.size > 0,
    onDraftValidityChange,
  };
}
