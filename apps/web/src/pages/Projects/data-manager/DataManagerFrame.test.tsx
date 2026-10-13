import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { DataManagerFrame } from "./DataManagerFrame";

describe("DataManagerFrame", () => {
  it("exposes the three project sections and keeps the active section accessible", async () => {
    const user = userEvent.setup();
    const onSectionChange = vi.fn();

    render(
      <MemoryRouter>
        <DataManagerFrame
          projectId="p1"
          projectName="Inspection"
          projectDisplayId="P-1"
          section="data"
          canViewMembers
          onSectionChange={onSectionChange}
        >
          <div>content</div>
        </DataManagerFrame>
      </MemoryRouter>,
    );

    expect(screen.getByRole("heading", { name: "Inspection" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "返回项目设置" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /项目概览/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /数据浏览/ })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("button", { name: /成员绩效/ })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /成员绩效/ }));
    expect(onSectionChange).toHaveBeenCalledWith("members");
  });

  it("hides the team section when the caller cannot view project members", () => {
    render(
      <MemoryRouter>
        <DataManagerFrame
          projectId="p1"
          projectName="Inspection"
          projectDisplayId="P-1"
          section="data"
          onSectionChange={vi.fn()}
        >
          <div>content</div>
        </DataManagerFrame>
      </MemoryRouter>,
    );

    expect(screen.queryByRole("button", { name: /成员绩效/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "返回项目设置" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "返回项目总览" })).toBeInTheDocument();
  });
});
