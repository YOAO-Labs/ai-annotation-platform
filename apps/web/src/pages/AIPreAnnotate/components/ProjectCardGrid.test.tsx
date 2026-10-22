/**
 * v0.9.12 · ProjectCardGrid 单测 (BUG B-17 项目卡片视图).
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { ProjectCardGrid } from "./ProjectCardGrid";
import type { PreannotateProjectSummary } from "@/api/adminPreannotate";

function makeItem(over: Partial<PreannotateProjectSummary> = {}): PreannotateProjectSummary {
  return {
    project_id: "p1",
    project_name: "项目甲",
    project_display_id: "P-1",
    type_key: "image-det",
    data_type: "image",
    ml_backend_id: "bk1",
    ml_backend_name: "grounded-sam2",
    ml_backend_state: "ready",
    ml_backend_max_concurrency: 4,
    ready_batches: 2,
    active_batches: 5,
    last_job_at: new Date().toISOString(),
    recent_failures: 0,
    ...over,
  };
}

describe("ProjectCardGrid", () => {
  it("空态时提示在项目中接入推理服务", () => {
    render(<ProjectCardGrid items={[]} isLoading={false} onSelect={() => {}} />);
    expect(screen.getByText(/暂无可预标的项目/)).toBeInTheDocument();
  });

  it("加载态显示 loading 文字", () => {
    render(<ProjectCardGrid items={[]} isLoading={true} onSelect={() => {}} />);
    expect(screen.getByText(/加载项目列表/)).toBeInTheDocument();
  });

  it("渲染项目名 + ml_backend 名 + 三个数字徽章", () => {
    render(
      <ProjectCardGrid
        items={[
          makeItem({
            project_name: "Demo Project",
            active_batches: 7,
            ml_backend_state: "connected",
            ready_batches: 3,
            recent_failures: 1,
          }),
        ]}
        isLoading={false}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByText("Demo Project")).toBeInTheDocument();
    expect(screen.getByText(/grounded-sam2/)).toBeInTheDocument();
    expect(screen.getByText(/已连接/)).toBeInTheDocument();
    expect(screen.getByText(/P-1 · 图片/)).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();
  });

  it("点击卡片触发 onSelect(project_id)", () => {
    const onSelect = vi.fn();
    render(
      <ProjectCardGrid
        items={[makeItem({ project_id: "abc-123", project_name: "目标项目" })]}
        isLoading={false}
        onSelect={onSelect}
      />,
    );
    fireEvent.click(screen.getByText("目标项目"));
    expect(onSelect).toHaveBeenCalledWith("abc-123");
  });
});
