import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { BatchResponse } from "@/api/batches";

const logs = Array.from({ length: 45 }, (_, index) => ({
  id: index,
  created_at: "2026-10-21T00:00:00Z",
  actor_email: `actor-${index}@example.test`,
  actor_role: "annotator",
  action: "batch.created",
  detail: {},
}));
const useLogs = vi.hoisted(() => vi.fn());
vi.mock("@/hooks/useBatches", () => ({
  useBatchAuditLogs: (...args: unknown[]) => useLogs(...args),
}));
import { BatchAuditLogDrawer } from "./BatchAuditLogDrawer";

describe("batch audit history pagination", () => {
  it("queries offsets with one lookahead row, hides that row, and resets on a row-count change", () => {
    useLogs.mockImplementation((_project, _batch, _enabled, pagination) => ({
      data: logs.slice(pagination.offset, pagination.offset + pagination.limit),
      isLoading: false,
      isFetching: false,
    }));
    render(
      <BatchAuditLogDrawer
        projectId="p1"
        batch={{ id: "b1", display_id: "B-1", name: "批次一" } as BatchResponse}
        onClose={() => undefined}
      />,
    );
    expect(screen.getByText("actor-0@example.test")).toBeInTheDocument();
    expect(screen.queryByText("actor-20@example.test")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(screen.getByText("actor-20@example.test")).toBeInTheDocument();
    expect(screen.queryByText("actor-0@example.test")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(useLogs).toHaveBeenLastCalledWith("p1", "b1", true, { limit: 21, offset: 40 });
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("每页操作记录数"), { target: { value: "10" } });
    expect(useLogs).toHaveBeenLastCalledWith("p1", "b1", true, { limit: 11, offset: 0 });
    expect(screen.getByText("actor-0@example.test")).toBeInTheDocument();
    expect(screen.queryByText("actor-10@example.test")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
  });
});
