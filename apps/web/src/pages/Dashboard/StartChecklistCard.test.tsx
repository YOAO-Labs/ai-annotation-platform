import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import type { ProjectResponse } from "@/api/projects";
import { useAuthStore } from "@/stores/authStore";
import { StartChecklistCard } from "./StartChecklistCard";

const mocks = vi.hoisted(() => ({
  markGuideRead: vi.fn(),
  resolveImage: vi.fn(),
  summary: { data: undefined as unknown, isError: false, refetch: vi.fn() },
}));
vi.mock("@/hooks/useDashboard", () => ({ useOnboardingProjectSummary: () => mocks.summary }));
vi.mock("@/hooks/useGuideAssets", () => ({
  useGuideAssets: () => ({
    resolveImage: mocks.resolveImage,
    imageScope: "guide-session-project-a",
  }),
}));
vi.mock("@/hooks/useOnboardingProjectState", () => ({
  useOnboardingProjectState: () => ({
    guideRead: false,
    dismissed: false,
    isSaving: false,
    markGuideRead: mocks.markGuideRead,
    dismiss: vi.fn(),
    reopen: vi.fn(),
    retry: vi.fn(),
  }),
}));
vi.mock("@/components/markdown/GuideMarkdownView", () => ({
  GuideMarkdownView: ({
    content,
    resolveImage,
    imageScope,
  }: {
    content: string;
    resolveImage?: unknown;
    imageScope?: string;
  }) => (
    <p
      data-testid="guide-view"
      data-has-resolver={resolveImage ? "true" : "false"}
      data-image-scope={imageScope}
    >
      {content}
    </p>
  ),
}));
const project = {
  id: "p1",
  name: "Example",
  annotation_guide: "Draw one box",
  owner_name: "Manager",
} as ProjectResponse;
function Location() {
  const location = useLocation();
  return (
    <output data-testid="location">
      {location.pathname}
      {location.search}
    </output>
  );
}
beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  mocks.summary.data = {
    assigned_task_count: 0,
    opened_task_count: 0,
    saved_annotation_count: 0,
    reviewed_task_count: 0,
    reviewed_task_id: null,
  };
  mocks.summary.isError = false;
  useAuthStore.setState({ user: null, token: null });
});
it("does not complete guide by opening it and disables work without assignments", () => {
  render(
    <MemoryRouter>
      <StartChecklistCard project={project} batches={[]} />
    </MemoryRouter>,
  );
  expect(screen.getByRole("button", { name: "打开任务" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "开始标注" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "查看结果" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "打开指引" }));
  expect(screen.getByText("Draw one box")).toBeVisible();
  expect(screen.getByTestId("guide-view")).toHaveAttribute("data-has-resolver", "true");
  expect(screen.getByTestId("guide-view")).toHaveAttribute(
    "data-image-scope",
    "guide-session-project-a",
  );
  expect(mocks.markGuideRead).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "确认已阅读" }));
  expect(mocks.markGuideRead).toHaveBeenCalledOnce();
});
it.each(["completed", "rejected"])(
  "shows the %s result without requiring access to the editing workbench",
  (status) => {
    mocks.summary.data = {
      assigned_task_count: 1,
      opened_task_count: 1,
      saved_annotation_count: 1,
      reviewed_task_count: 1,
      reviewed_task_id: "reviewed-task",
      reviewed_task_display_id: "T-RESULT",
      reviewed_task_status: status,
      reviewed_task_reason: status === "rejected" ? "最新退回理由" : null,
    };
    render(
      <MemoryRouter>
        <StartChecklistCard project={project} batches={[]} />
        <Location />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "再次查看结果" }));
    expect(screen.getByTestId("location").textContent).toBe("/");
    expect(screen.getByTestId("start-checklist-result")).toHaveTextContent("T-RESULT");
    expect(screen.getByTestId("start-checklist-result")).toHaveTextContent(
      status === "rejected" ? "最新退回理由" : "任务已通过审核",
    );
  },
);
it("does not treat stale successful counters as true after a failed refresh", () => {
  mocks.summary.data = {
    assigned_task_count: 1,
    opened_task_count: 1,
    saved_annotation_count: 1,
    reviewed_task_count: 1,
    reviewed_task_id: "old-task",
  };
  mocks.summary.isError = true;
  render(
    <MemoryRouter>
      <StartChecklistCard project={project} batches={[]} />
    </MemoryRouter>,
  );
  expect(screen.getByText("0 / 4")).toBeVisible();
  expect(screen.getByRole("alert")).toHaveTextContent("无法读取项目真实进度");
  expect(screen.getByRole("button", { name: "查看结果" })).toBeDisabled();
});
