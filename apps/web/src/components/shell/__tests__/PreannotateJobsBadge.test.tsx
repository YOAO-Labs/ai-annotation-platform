/**
 * v0.9.8 · PreannotateJobsBadge 单测.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { PreannotateJobsBadge } from "../PreannotateJobsBadge";

vi.mock("@/hooks/useGlobalPreannotationJobs", () => ({
  useGlobalPreannotationJobs: vi.fn(),
}));

import { useGlobalPreannotationJobs } from "@/hooks/useGlobalPreannotationJobs";

const mockHook = useGlobalPreannotationJobs as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  mockHook.mockReset();
});

function withRouter(node: React.ReactNode) {
  return <MemoryRouter>{node}</MemoryRouter>;
}

describe("PreannotateJobsBadge", () => {
  it("0 个 job 时不渲染", () => {
    mockHook.mockReturnValue({ runningJobs: [], byProject: {}, connected: true });
    const { container } = render(withRouter(<PreannotateJobsBadge />));
    expect(container.firstChild).toBeNull();
  });

  it("有 job 时显示数字徽章", () => {
    mockHook.mockReturnValue({
      runningJobs: [
        {
          job_id: "j1",
          project_id: "p1",
          project_name: "Project Alpha",
          status: "running",
          current: 3,
          total: 10,
          receivedAt: Date.now(),
        },
        {
          job_id: "j2",
          project_id: "p2",
          project_name: "Project Beta",
          status: "running",
          current: 5,
          total: 8,
          receivedAt: Date.now() - 1000,
        },
      ],
      byProject: {},
      connected: true,
    });
    render(withRouter(<PreannotateJobsBadge />));
    expect(screen.getByRole("button", { name: "2" })).toBeInTheDocument();
  });

  it("点击展开 popover 列出每个 job", async () => {
    const user = userEvent.setup();
    mockHook.mockReturnValue({
      runningJobs: [
        {
          job_id: "j-a",
          project_id: "p-alpha",
          project_name: "Alpha",
          status: "running",
          current: 3,
          total: 10,
          receivedAt: 1000,
        },
        {
          job_id: "j-b",
          project_id: "p-beta",
          project_name: "Beta",
          status: "running",
          current: 5,
          total: 8,
          receivedAt: 2000,
        },
      ],
      byProject: {},
      connected: true,
    });
    render(withRouter(<PreannotateJobsBadge />));
    await user.click(screen.getByRole("button", { name: "2" }));
    expect(screen.getByText(/预标进行中 \(2\)/)).toBeInTheDocument();
    // 项目名按 receivedAt desc: Beta 在前
    const buttons = screen.getAllByRole("button");
    const labels = buttons.map((b) => b.textContent);
    const betaIdx = labels.findIndex((l) => l?.includes("Beta"));
    const alphaIdx = labels.findIndex((l) => l?.includes("Alpha"));
    expect(betaIdx).toBeLessThan(alphaIdx);
    expect(betaIdx).toBeGreaterThanOrEqual(0);
  });

  it("popover 内进度百分比按 current/total 计算显示", async () => {
    const user = userEvent.setup();
    mockHook.mockReturnValue({
      runningJobs: [
        {
          job_id: "j",
          project_id: "p",
          project_name: "P",
          status: "running",
          current: 25,
          total: 100,
          receivedAt: 1,
        },
      ],
      byProject: {},
      connected: true,
    });
    render(withRouter(<PreannotateJobsBadge />));
    await user.click(screen.getByRole("button", { name: "1" }));
    expect(screen.getByText(/25\/100 · 25%/)).toBeInTheDocument();
  });
});
