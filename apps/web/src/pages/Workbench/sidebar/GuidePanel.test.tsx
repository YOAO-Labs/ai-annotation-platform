import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  markGuideRead: vi.fn(),
  retry: vi.fn(),
  state: {
    guideRead: false,
    isSaving: false,
    saveError: null as string | null,
  },
}));

vi.mock("@/hooks/useGuideAssets", () => ({
  useGuideAssets: () => ({ resolveImage: vi.fn() }),
}));

vi.mock("@/hooks/useOnboardingProjectState", () => ({
  useOnboardingProjectState: () => ({
    ...mocks.state,
    markGuideRead: mocks.markGuideRead,
    retry: mocks.retry,
  }),
}));

import { GuidePanel } from "./GuidePanel";

function renderGuide(content = "# 当前指南") {
  return render(
    <GuidePanel projectId="project-a" userId="user-a" projectName="项目 A" content={content} />,
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("GuidePanel", () => {
  beforeEach(() => {
    localStorage.clear();
    mocks.markGuideRead.mockReset().mockResolvedValue(true);
    mocks.retry.mockReset().mockResolvedValue(true);
    mocks.state.guideRead = false;
    mocks.state.isSaving = false;
    mocks.state.saveError = null;
  });

  it("keeps the guide closed until the trigger is activated and does not confirm on close", () => {
    renderGuide();

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "标注指引" }));
    const dialog = screen.getByRole("dialog", { name: "标注指引" });
    expect(dialog).toBeInTheDocument();
    expect(dialog).toHaveFocus();
    const overlay = screen.getByTestId("wb-guide-overlay");
    expect(overlay).toHaveClass("backdrop-blur-overlay");
    fireEvent.pointerDown(overlay);
    fireEvent.click(overlay);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "标注指引" })).toHaveFocus();
    expect(mocks.markGuideRead).not.toHaveBeenCalled();
  });

  it("confirms only from the fixed footer and removes the unread marker after success", async () => {
    renderGuide();
    fireEvent.click(screen.getByRole("button", { name: "标注指引" }));

    expect(screen.getByTestId("wb-guide-unread")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("wb-guide-confirm"));
    await waitFor(() => expect(mocks.markGuideRead).toHaveBeenCalledOnce());
    expect(screen.queryByTestId("wb-guide-unread")).not.toBeInTheDocument();
    expect(screen.getByTestId("wb-guide-confirm")).toHaveTextContent("已确认阅读");
    expect(screen.getByText("项目：项目 A")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toHaveClass("md:w-[min(1120px,calc(100vw-64px))]");
  });

  it("offers retry after a failed confirmation", async () => {
    mocks.markGuideRead.mockResolvedValueOnce(false);
    mocks.state.saveError = "保存失败";
    renderGuide();
    fireEvent.click(screen.getByRole("button", { name: "标注指引" }));
    fireEvent.click(screen.getByTestId("wb-guide-confirm"));
    await waitFor(() => expect(mocks.markGuideRead).toHaveBeenCalledOnce());

    fireEvent.click(screen.getByTestId("wb-guide-retry"));
    await waitFor(() => expect(mocks.retry).toHaveBeenCalledOnce());
    expect(screen.getByTestId("wb-guide-confirm")).toHaveTextContent("已确认阅读");
  });

  it("ignores a late confirmation after the guide scope changes", async () => {
    const pending = deferred<boolean>();
    mocks.markGuideRead.mockReturnValueOnce(pending.promise);
    const view = renderGuide();
    fireEvent.click(screen.getByRole("button", { name: "标注指引" }));
    fireEvent.click(screen.getByTestId("wb-guide-confirm"));

    view.rerender(
      <GuidePanel projectId="project-b" userId="user-a" projectName="项目 B" content="# B" />,
    );
    pending.resolve(true);
    await waitFor(() => expect(screen.getByTestId("wb-guide-unread")).toBeInTheDocument());
    expect(localStorage.length).toBe(0);
  });

  it("drops the overlay blur on a live 3D canvas while keeping the dim", () => {
    render(
      <GuidePanel
        projectId="project-a"
        userId="user-a"
        projectName="项目 A"
        content="# 当前指南"
        backdropBlur={false}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "标注指引" }));
    const overlay = screen.getByTestId("wb-guide-overlay");
    expect(overlay).not.toHaveClass("backdrop-blur-overlay");
    expect(overlay).toHaveClass("bg-black/25");
  });

  it("does not render an entry for an empty guide", () => {
    renderGuide("  \n");
    expect(screen.queryByRole("button", { name: "标注指引" })).not.toBeInTheDocument();
  });
});
