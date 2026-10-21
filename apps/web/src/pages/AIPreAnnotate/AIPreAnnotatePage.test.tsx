import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router-dom";
import AIPreAnnotatePage from "./AIPreAnnotatePage";

const summary = vi.hoisted(() => ({ query: vi.fn(), refetch: vi.fn() }));
vi.mock("@/hooks/useBulkPreannotateActions", () => ({ useAIPreProjectSummary: summary.query }));
vi.mock("./components/ProjectDetailPanel", () => ({
  ProjectDetailPanel: ({
    projectId,
    initialCatalogModelId,
    onBack,
  }: {
    projectId: string;
    initialCatalogModelId?: string | null;
    onBack: () => void;
  }) => (
    <section aria-label="项目配置">
      {projectId}
      <span data-testid="catalog-model">{initialCatalogModelId}</span>
      <button onClick={onBack}>返回项目列表</button>
    </section>
  ),
}));

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname + location.search}</div>;
}
function mount(entry = "/ai-pre") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <AIPreAnnotatePage />
      <LocationProbe />
    </MemoryRouter>,
  );
}

const items = [
  {
    project_id: "image",
    project_name: "图片项目",
    project_display_id: "P-IMAGE",
    data_type: "image",
    ml_backend_name: "detector",
    active_batches: 2,
    ready_batches: 0,
    recent_failures: 0,
  },
  {
    project_id: "video",
    project_name: "视频项目",
    project_display_id: "P-VIDEO",
    data_type: "video",
    ml_backend_name: "tracker",
    active_batches: 1,
    ready_batches: 0,
    recent_failures: 0,
  },
];

describe("AI preannotation project selection", () => {
  beforeEach(() => {
    summary.refetch.mockReset();
    summary.query.mockReturnValue({
      data: { items },
      isLoading: false,
      isFetching: false,
      isError: false,
      refetch: summary.refetch,
    });
  });

  it("searches names, display IDs and services, and retains the filter after returning from a project", () => {
    mount();
    const search = screen.getByRole("searchbox", { name: "搜索预标项目" });
    fireEvent.change(search, { target: { value: " P-VIDEO " } });
    expect(screen.queryByText("图片项目")).not.toBeInTheDocument();
    expect(screen.getByText("匹配 1 / 2 个项目")).toBeInTheDocument();
    fireEvent.click(screen.getByText("视频项目"));
    expect(screen.getByRole("region", { name: "项目配置" })).toHaveTextContent("video");
    fireEvent.click(screen.getByRole("button", { name: "返回项目列表" }));
    expect(screen.getByRole("searchbox")).toHaveValue(" P-VIDEO ");
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "DETECTOR" } });
    expect(screen.getByText("图片项目")).toBeVisible();
    expect(screen.queryByText("视频项目")).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "图片" } });
    expect(screen.getByText("图片项目")).toBeVisible();
  });

  it("distinguishes an empty search from missing configuration, and clears the search", () => {
    mount();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "不存在" } });
    expect(screen.getByText("没有匹配的项目")).toBeVisible();
    expect(screen.queryByText("暂无可预标的项目")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "清除搜索" }));
    expect(screen.getByText("共 2 个项目")).toBeInTheDocument();
    expect(screen.getByText("图片项目")).toBeVisible();
  });

  it("shows request failures with a retry instead of claiming no projects are configured", () => {
    summary.query.mockReturnValue({
      isError: true,
      isLoading: false,
      isFetching: false,
      refetch: summary.refetch,
    });
    mount();
    expect(screen.getByRole("alert")).toHaveTextContent("项目列表加载失败");
    expect(screen.queryByText("暂无可预标的项目")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(summary.refetch).toHaveBeenCalledOnce();
  });

  it("opens a catalog model from its URL and preserves unrelated parameters when returning", () => {
    mount("/ai-pre?project=video&catalog_model=trained-detector&source=directory");
    expect(screen.getByRole("region", { name: "项目配置" })).toHaveTextContent("video");
    expect(screen.getByTestId("catalog-model")).toHaveTextContent("trained-detector");
    fireEvent.click(screen.getByRole("button", { name: "返回项目列表" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/ai-pre?source=directory");
    fireEvent.click(screen.getByText("图片项目"));
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/ai-pre?source=directory&project=image",
    );
    expect(screen.getByTestId("catalog-model")).toBeEmptyDOMElement();
  });
});
