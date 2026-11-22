import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getAll: vi.fn(),
  subscribe: vi.fn(() => () => undefined),
  removeDurablyById: vi.fn().mockResolvedValue(undefined),
  drain: vi.fn().mockResolvedValue({ ok: 1, failed: 0 }),
  toast: vi.fn(),
}));

vi.mock("../state/offlineQueue", () => ({
  getAll: mocks.getAll,
  subscribe: mocks.subscribe,
  removeDurablyById: mocks.removeDurablyById,
  drain: mocks.drain,
}));

vi.mock("@/components/ui/Toast", () => ({
  useToastStore: (selector: (state: { push: typeof mocks.toast }) => unknown) =>
    selector({ push: mocks.toast }),
}));

import { OfflineQueueDrawer } from "./OfflineQueueDrawer";

const operations = [
  {
    kind: "delete",
    id: "op-a",
    taskId: "task-a",
    userId: "user-1",
    annotationId: "annotation-a",
    ts: 1,
  },
  {
    kind: "delete",
    id: "op-b",
    taskId: "task-b",
    userId: "user-1",
    annotationId: "annotation-b",
    ts: 2,
  },
] as const;

function renderDrawer(currentTaskId?: string, isCurrent?: () => boolean) {
  return render(
    <OfflineQueueDrawer
      open
      onClose={vi.fn()}
      currentTaskId={currentTaskId}
      queueScope={{ userId: "user-1", ...(isCurrent ? { isCurrent } : {}) }}
      onFlushOne={vi.fn().mockResolvedValue(undefined)}
      onFlushAll={vi.fn().mockResolvedValue(undefined)}
    />,
  );
}

describe("OfflineQueueDrawer", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getAll.mockResolvedValue(operations);
  });

  it("does not widen current-task mode when the current task disappears", async () => {
    const user = userEvent.setup();
    const view = renderDrawer("task-a");
    await waitFor(() => expect(screen.getByText(/2 条/)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "当前题" }));
    expect(screen.getByText(/任务 task-a/)).toBeInTheDocument();
    expect(screen.queryByText(/任务 task-b/)).not.toBeInTheDocument();

    view.rerender(
      <OfflineQueueDrawer
        open
        onClose={vi.fn()}
        queueScope={{ userId: "user-1" }}
        onFlushOne={vi.fn().mockResolvedValue(undefined)}
        onFlushAll={vi.fn().mockResolvedValue(undefined)}
      />,
    );
    await waitFor(() => expect(screen.getByText("当前筛选无匹配项")).toBeInTheDocument());
    expect(screen.queryByText(/任务 task-a/)).not.toBeInTheDocument();
    expect(screen.queryByText(/任务 task-b/)).not.toBeInTheDocument();
  });

  it("keeps account scope on the durable queue read", async () => {
    renderDrawer("task-a");
    await waitFor(() => expect(mocks.getAll).toHaveBeenCalledWith({ userId: "user-1" }));
    expect(screen.getByText(/2 条/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "当前题" }));
    expect(mocks.drain).not.toHaveBeenCalled();
  });

  it("reports a failed discard and leaves the operation available to retry", async () => {
    const user = userEvent.setup();
    mocks.removeDurablyById.mockRejectedValueOnce(new Error("quota exceeded"));
    renderDrawer("task-a", () => true);
    const discard = (await screen.findAllByRole("button", { name: "丢弃" }))[0];
    await user.click(discard);
    await waitFor(() =>
      expect(mocks.toast).toHaveBeenCalledWith(
        expect.objectContaining({ msg: "未能删除离线操作，请重试", kind: "error" }),
      ),
    );
    expect(discard).toBeEnabled();
    await user.click(discard);
    await waitFor(() =>
      expect(mocks.toast).toHaveBeenLastCalledWith({ msg: "已从队列删除", kind: "success" }),
    );
    expect(mocks.removeDurablyById).toHaveBeenCalledTimes(2);
  });
});
