import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import AIPreAnnotateLayout from "./AIPreAnnotateLayout";

function setup(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/ai-pre" element={<AIPreAnnotateLayout />}>
          <Route index element={<p>项目内容</p>} />
          <Route path="pipelines" element={<p>编排内容</p>} />
          <Route path="jobs" element={<p>历史内容</p>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe("AI preannotation page shell", () => {
  it.each([
    ["/ai-pre", "AI 预标", "执行预标"],
    ["/ai-pre/pipelines", "编排库", "编排库"],
    ["/ai-pre/jobs?tab=video&project_id=p1", "AI 任务历史", "完整历史"],
    ["/ai-pre/jobs/", "AI 任务历史", "完整历史"],
  ])("places the page heading before route navigation for %s", (path, title, active) => {
    setup(path);
    const heading = screen.getByRole("heading", { level: 1, name: title });
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    const nav = screen.getByRole("navigation", { name: "AI 预标视图" });
    expect(heading.compareDocumentPosition(nav) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(nav).getByRole("link", { name: active })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getAllByRole("link")).toHaveLength(3);
  });

  it("navigates through the three existing routes and updates heading and active link together", () => {
    setup("/ai-pre");
    const nav = screen.getByRole("navigation", { name: "AI 预标视图" });
    fireEvent.click(within(nav).getByRole("link", { name: "编排库" }));
    expect(screen.getByText("编排内容")).toBeVisible();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("编排库");
    fireEvent.click(within(nav).getByRole("link", { name: "完整历史" }));
    expect(screen.getByText("历史内容")).toBeVisible();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("AI 任务历史");
    expect(within(nav).getByRole("link", { name: "执行预标" })).not.toHaveAttribute("aria-current");
  });
});
