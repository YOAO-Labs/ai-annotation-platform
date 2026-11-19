/**
 * 评论 hover → ImageStage 历史回看叠加。
 * 增加「点击 pin」：hover 是瞬时 peek，鼠标一移开即清空，无法定睛看画布上的批注；
 *   点击评论卡片把该评论的批注 pin 到画布，持续显示直到取消 / 切换到别条 / 切换标注。
 *
 * 渲染优先级（见 selectEffectiveShapes）：hover 优先（临时 peek 覆盖 pinned），
 * 无 hover 时回落到 pinned；都没有则画布无批注叠加。
 */
import { create } from "zustand";
import type { CommentCanvasDrawing } from "@/api/comments";

type Shapes = NonNullable<CommentCanvasDrawing["shapes"]>;

interface HoveredCommentState {
  /** hover 瞬时预览（onMouseEnter 写、onMouseLeave 清）。*/
  hoverShapes: Shapes | null;
  setHover: (shapes: Shapes | null) => void;
  /** 点击 pin 的评论 id + 其 shapes；持续显示直到取消 / 切换。*/
  pinnedId: string | null;
  pinnedShapes: Shapes | null;
  /** 点击评论：已 pin 同一条则取消，否则 pin 这一条。*/
  togglePin: (id: string, shapes: Shapes) => void;
  /** 清除 pin（切换标注 / 卸载时调用）。*/
  clearPin: () => void;
  /** 正在编辑的评论的 pending 批注（弹窗批注 save / live 完成后）；它是当前焦点，
   *  持续预览在画布上直到评论发送 / 清空 / 切换标注。*/
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

/** 画布实际叠加的 shapes：hover（临时 peek）> composing（正在编辑的评论）> pinned（点击选中）。 */
export function selectEffectiveShapes(s: HoveredCommentState): Shapes | null {
  return s.hoverShapes ?? s.composingShapes ?? s.pinnedShapes;
}
