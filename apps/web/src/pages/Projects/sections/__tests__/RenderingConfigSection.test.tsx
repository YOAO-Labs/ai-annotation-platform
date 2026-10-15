import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ProjectResponse } from "@/api/projects";
import { RenderingConfigSection } from "../RenderingConfigSection";

const mocks = vi.hoisted(() => ({ mutate: vi.fn(), canManage: true, pending: false }));
vi.mock("@/hooks/useProjects", () => ({
  useUpdateProject: () => ({ mutate: mocks.mutate, isPending: mocks.pending }),
}));
vi.mock("@/hooks/useProjectAccess", () => ({
  useProjectAccess: () => ({ hasCapability: () => mocks.canManage }),
}));
vi.mock("../RenderingConfigEditor", () => ({
  RenderingConfigEditor: () => <div>渲染设置</div>,
}));
vi.mock("../PointCloudPreparation", () => ({
  PointCloudPreparation: () => <div>准备场景</div>,
}));

function show(dataType: ProjectResponse["data_type"]) {
  render(
    <RenderingConfigSection project={{ id: "project", data_type: dataType } as ProjectResponse} />,
  );
}

beforeEach(() => {
  mocks.mutate.mockReset();
  mocks.canManage = true;
  mocks.pending = false;
});

describe("RenderingConfigSection", () => {
  it.each(["image", "video"] as const)("%s 项目隐藏点云实验与准备入口", (type) => {
    show(type);
    expect(screen.getByText("渲染设置")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "AI 点云补强实验" })).toBeNull();
    expect(screen.queryByText("准备场景")).toBeNull();
  });

  it("LiDAR 管理员可开启实验", () => {
    show("lidar");
    expect(screen.getByText("准备场景")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("switch", { name: "允许成员使用实验" }));
    expect(mocks.mutate).toHaveBeenCalledWith(
      { point_cloud_enhancement_config: expect.objectContaining({ enabled: true }) },
      expect.any(Object),
    );
  });

  it.each(["readonly", "saving"])("%s 时不能修改实验设置", (state) => {
    mocks.canManage = state !== "readonly";
    mocks.pending = state === "saving";
    show("lidar");
    const toggle = screen.getByRole("switch", { name: "允许成员使用实验" });
    expect(toggle).toBeDisabled();
    fireEvent.click(toggle);
    expect(mocks.mutate).not.toHaveBeenCalled();
  });
});
