import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { server } from "@/mocks/server";
import { renderWithProviders } from "@/test/renderWithProviders";

const mockUseWorkbenchShellModel = vi.fn();

vi.mock("../state/useWorkbenchShellModel", () => ({
  useWorkbenchShellModel: (...args: unknown[]) => mockUseWorkbenchShellModel(...args),
}));
vi.mock("./WorkbenchLayout", () => ({
  WorkbenchLayout: ({ stageOverlay }: { stageOverlay?: React.ReactNode }) => (
    <div data-testid="layout">{stageOverlay}</div>
  ),
}));

import { WorkbenchShell } from "./WorkbenchShell";

describe("WorkbenchShell", () => {
  it("does not offer issue or navigation actions until the model is loaded", () => {
    mockUseWorkbenchShellModel.mockReturnValue({ kind: "loading" });
    render(<WorkbenchShell />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("renders empty state and delegates back action", () => {
    const onBack = vi.fn();
    mockUseWorkbenchShellModel.mockReturnValue({
      kind: "empty",
      emptyState: {
        icon: "warning",
        message: "项目不存在或无访问权限",
        onBack,
      },
    });

    render(<WorkbenchShell />);

    expect(screen.getByText("项目不存在或无访问权限")).toBeTruthy();
    fireEvent.click(screen.getByRole("button"));
    expect(onBack).toHaveBeenCalledOnce();
  });

  it("renders issue controls when ready", () => {
    const onOpenList = vi.fn();
    const onToggleIssuePinDrop = vi.fn();
    mockUseWorkbenchShellModel.mockReturnValue({
      kind: "ready",
      layout: {},
      issueSection: {
        openIssueCount: 2,
        issuePinsComplete: true,
        stageKind: "image",
        issuePinDropArmed: true,
        onOpenList,
        onToggleIssuePinDrop,
        createModal: { open: false },
      },
    });

    render(<WorkbenchShell />);

    expect(screen.getByTestId("issue-fab")).toHaveAccessibleName("查看问题（2 个未解决）");

    fireEvent.click(screen.getByTestId("issue-fab"));
    fireEvent.click(screen.getByTestId("issue-pin-fab"));

    expect(onOpenList).toHaveBeenCalledOnce();
    expect(onToggleIssuePinDrop).toHaveBeenCalledOnce();
  });

  it("preserves a rejected pixel issue and closes only after its retry succeeds", async () => {
    const onClose = vi.fn();
    const requests: unknown[] = [];
    server.use(
      http.post("*/api/v1/feedbacks", async ({ request }) => {
        requests.push(await request.json());
        if (requests.length === 1)
          return HttpResponse.json({ detail: "服务暂时不可用" }, { status: 503 });
        return HttpResponse.json(
          { id: "issue-1", project_id: "P1", task_id: "T2" },
          { status: 201 },
        );
      }),
    );
    mockUseWorkbenchShellModel.mockReturnValue({
      kind: "ready",
      layout: {},
      issueSection: {
        stageKind: "image",
        openIssueCount: 0,
        issuePinsComplete: true,
        createModal: {
          open: true,
          projectId: "P1",
          taskId: "T2",
          listParams: { project_id: "P1", task_id: "T2", kind: "issue" },
          prefilledAnchor: { x: 0.2, y: 0.6 },
          onClose,
        },
      },
    });
    renderWithProviders(<WorkbenchShell />);
    const dialog = within(screen.getByRole("dialog", { name: "标记问题" }));
    const submit = dialog.getByRole("button", { name: "提交" });
    expect(submit).toBeDisabled();
    fireEvent.change(dialog.getByPlaceholderText("描述问题位置 / 现象 / 期望行为"), {
      target: { value: " 边界偏移 " },
    });
    fireEvent.click(submit);
    expect(await dialog.findByText("服务暂时不可用")).toBeVisible();
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(submit);
    await waitFor(() => expect(onClose).toHaveBeenCalledOnce());
    const expected = {
      kind: "issue",
      anchor_type: "pixel",
      project_id: "P1",
      task_id: "T2",
      anchor_position: { x: 0.2, y: 0.6 },
      severity: "warn",
      title: null,
      body: "边界偏移",
    };
    expect(requests).toEqual([expected, expected]);
  });

  it.each(["preparing", "ready", "cancelled", "timeout", "unavailable"] as const)(
    "video issues expose %s without claiming failed navigation is ready",
    (status) => {
      const onRetryIssueNavigation = vi.fn(async () => {});
      const onToggleIssuePinDrop = vi.fn();
      mockUseWorkbenchShellModel.mockReturnValue({
        kind: "ready",
        layout: {},
        issueSection: {
          openIssueCount: 0,
          issuePinsComplete: true,
          stageKind: "video",
          issuePinDropArmed: false,
          onOpenList: vi.fn(),
          onToggleIssuePinDrop,
          issueNavigation: { status, frameIndex: 0 },
          onRetryIssueNavigation,
          createModal: { open: false },
        },
      });
      render(<WorkbenchShell />);
      fireEvent.click(screen.getByTestId("issue-pin-fab"));
      expect(onToggleIssuePinDrop).toHaveBeenCalledOnce();
      const progress = screen.getByTestId("issue-frame-navigation");
      expect(screen.getByTestId("layout")).toContainElement(progress);
      expect(progress).toHaveAttribute("data-status", status);
      expect(progress).toHaveAttribute("data-frame-index", "0");
      expect(progress.textContent?.includes("已定位")).toBe(status === "ready");
      const retry = screen.queryByRole("button", { name: "重试" });
      if (status === "preparing" || status === "ready") expect(retry).toBeNull();
      else {
        expect(retry).not.toBeNull();
        fireEvent.click(retry!);
        expect(onRetryIssueNavigation).toHaveBeenCalledOnce();
      }
    },
  );

  it("shows unknown counts and incomplete pins with a scoped retry", () => {
    const onRetryIssuePins = vi.fn(async () => {});
    const issueSection = {
      openIssueCount: null,
      openIssueCountLoading: true,
      openIssueCountError: false,
      issuePinsComplete: false,
      issuePinsLoadedCount: 200,
      issuePinsError: false,
      stageKind: "image",
      onRetryIssuePins,
      createModal: { open: false },
    };
    mockUseWorkbenchShellModel.mockReturnValue({ kind: "ready", layout: {}, issueSection });
    const view = render(<WorkbenchShell />);
    expect(screen.getByTestId("issue-fab")).toHaveAccessibleName("查看问题（数量加载中）");
    expect(screen.getByTestId("issue-pin-coverage")).toHaveTextContent("已显示 200 个");
    mockUseWorkbenchShellModel.mockReturnValue({
      kind: "ready",
      layout: {},
      issueSection: {
        ...issueSection,
        openIssueCountLoading: false,
        openIssueCountError: true,
        issuePinsError: true,
      },
    });
    view.rerender(<WorkbenchShell />);
    expect(screen.getByTestId("issue-fab")).toHaveAccessibleName("查看问题（数量暂不可用）");
    expect(screen.getByTestId("issue-pin-coverage")).toHaveAttribute("data-status", "error");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(onRetryIssuePins).toHaveBeenCalledOnce();
  });
});
