import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const mockUseWorkbenchShellModel = vi.fn();

vi.mock("@/components/ui/Button", () => ({
  Button: ({ children, onClick }: { children: React.ReactNode; onClick?: () => void }) => (
    <button type="button" onClick={onClick}>
      {children}
    </button>
  ),
}));
vi.mock("@/components/ui/Icon", () => ({
  Icon: ({ name }: { name: string }) => <span data-testid={`icon-${name}`} />,
}));
vi.mock("../state/useWorkbenchShellModel", () => ({
  useWorkbenchShellModel: (...args: unknown[]) => mockUseWorkbenchShellModel(...args),
}));
vi.mock("./WorkbenchLayout", () => ({
  WorkbenchLayout: ({ stageOverlay }: { stageOverlay?: React.ReactNode }) => (
    <div data-testid="layout">
      {stageOverlay}
    </div>
  ),
}));
vi.mock("./WorkbenchSkeleton", () => ({
  WorkbenchSkeleton: () => <div data-testid="skeleton" />,
}));
vi.mock("./IssueCreateModal", () => ({
  IssueCreateModal: () => <div data-testid="issue-create-modal" />,
}));

import { WorkbenchShell } from "./WorkbenchShell";

describe("WorkbenchShell", () => {
  it("renders WorkbenchSkeleton while model is loading", () => {
    mockUseWorkbenchShellModel.mockReturnValue({ kind: "loading" });

    render(<WorkbenchShell />);

    expect(screen.getByTestId("skeleton")).toBeTruthy();
    expect(screen.queryByTestId("layout")).toBeNull();
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
      layout: {} as never,
      propagateDialog: {} as never,
      issueSection: {
        openIssueCount: 2,
        issuePinsComplete: true,
        stageKind: "image",
        issuePinDropArmed: true,
        onOpenList,
        onToggleIssuePinDrop,
        createModal: {} as never,
      },
    });

    render(<WorkbenchShell />);

    expect(screen.getByTestId("issue-fab")).toHaveAccessibleName("查看问题（2 个未解决）");

    fireEvent.click(screen.getByTestId("issue-fab"));
    fireEvent.click(screen.getByTestId("issue-pin-fab"));

    expect(onOpenList).toHaveBeenCalledOnce();
    expect(onToggleIssuePinDrop).toHaveBeenCalledOnce();
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
          createModal: {},
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
      createModal: {},
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
