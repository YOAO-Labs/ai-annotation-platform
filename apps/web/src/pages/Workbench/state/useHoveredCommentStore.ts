import { create } from "zustand";
import type { CommentCanvasDrawing } from "@/api/comments";

type Shapes = NonNullable<CommentCanvasDrawing["shapes"]>;

interface HoveredCommentState {
  hoverShapes: Shapes | null;
  setHover: (shapes: Shapes | null) => void;
  pinnedId: string | null;
  pinnedShapes: Shapes | null;
  togglePin: (id: string, shapes: Shapes) => void;
  clearPin: () => void;
  composingShapes: Shapes | null;
  setComposing: (shapes: Shapes | null) => void;
}

export const useHoveredCommentStore = create<HoveredCommentState>((set) => ({
  hoverShapes: null,
  setHover: (shapes) => set({ hoverShapes: shapes }),
  pinnedId: null,
  pinnedShapes: null,
  togglePin: (id, shapes) =>
    set((s) =>
      s.pinnedId === id
        ? { pinnedId: null, pinnedShapes: null }
        : { pinnedId: id, pinnedShapes: shapes },
    ),
  clearPin: () => set({ pinnedId: null, pinnedShapes: null }),
  composingShapes: null,
  setComposing: (shapes) => set({ composingShapes: shapes }),
}));

export function selectEffectiveShapes(s: HoveredCommentState): Shapes | null {
  return s.hoverShapes ?? s.composingShapes ?? s.pinnedShapes;
}
