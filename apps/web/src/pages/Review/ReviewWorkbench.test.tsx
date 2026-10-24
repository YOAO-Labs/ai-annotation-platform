import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const state = vi.hoisted(() => ({
  blocked: "正在核验并读取原图，请稍候" as string | null,
  status: "loading",
  retry: vi.fn(),
}));
vi.mock("@/hooks/useTasks", () => ({
  useTask: () => ({
    data: { id: "task", project_id: "project", file_type: "image", status: "completed", tags: [] },
  }),
  useAnnotations: () => ({ data: [] }),
  useReviewClaim: () => ({ mutate: vi.fn() }),
}));
vi.mock("@/hooks/useProjects", () => ({
  useProject: () => ({ data: { id: "project", tool_bindings: {} } }),
}));
vi.mock("@/hooks/usePredictions", () => ({ usePredictions: () => ({ data: { pages: [] } }) }));
vi.mock("@/pages/Workbench/state/useSessionStats", () => ({ useSessionStats: vi.fn() }));
vi.mock("@/pages/Workbench/stage/ImageStage", () => ({ ImageStage: () => <div /> }));
vi.mock("@/pages/Workbench/shell/CommentsPanel", () => ({ CommentsPanel: () => null }));
vi.mock("./ReviewerMiniPanel", () => ({ ReviewerMiniPanel: () => null }));
vi.mock("@/pages/Workbench/stage/useWorkbenchImageSource", () => ({
  useWorkbenchImageSource: () => ({
    source: state.blocked ? null : { kind: "single", url: "blob:owned" },
    contentBlockedReason: state.blocked,
    refresh: state.retry,
  }),
  workbenchImagePreviewUrl: (source: { url?: string } | null) => source?.url ?? null,
}));
vi.mock("@/pages/Workbench/stage/useAbortableImage", () => ({
  useAbortableImage: () => [undefined, state.status],
}));
import { ReviewWorkbench } from "./ReviewWorkbench";
beforeEach(() => {
  state.blocked = "正在核验并读取原图，请稍候";
  state.status = "loading";
  state.retry.mockReset();
});
afterEach(cleanup);
describe("review image authority gate", () => {
  it("blocks review decisions until the current picture has loaded", () => {
    const approve = vi.fn(),
      reject = vi.fn();
    const { rerender } = render(
      <ReviewWorkbench taskId="task" onApprove={approve} onReject={reject} />,
    );
    expect(screen.getByTestId("review-approve")).toBeDisabled();
    expect(screen.getByTestId("review-reject")).toBeDisabled();
    fireEvent.click(screen.getByTestId("review-approve"));
    expect(approve).not.toHaveBeenCalled();
    state.blocked = null;
    state.status = "loaded";
    rerender(<ReviewWorkbench taskId="task" onApprove={approve} onReject={reject} />);
    fireEvent.click(screen.getByTestId("review-approve"));
    expect(approve).toHaveBeenCalledTimes(1);
  });
  it("shows a scoped failure and actionable retry while keeping decisions blocked", () => {
    state.blocked = "来源图片已变化";
    state.status = "failed";
    render(<ReviewWorkbench taskId="task" onApprove={vi.fn()} onReject={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent("来源图片已变化");
    fireEvent.click(screen.getByRole("button", { name: "重试图片读取" }));
    expect(state.retry).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("review-approve")).toBeDisabled();
  });
});
