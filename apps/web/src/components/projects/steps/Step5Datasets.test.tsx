import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";

const mockUseDatasets = vi.fn();

vi.mock("@/hooks/useDatasets", () => ({
  useDatasets: (...args: unknown[]) => mockUseDatasets(...args),
}));

vi.mock("@/hooks/useBatches", () => ({
  useSplitBatches: () => ({ mutateAsync: vi.fn() }),
}));

vi.mock("@/components/ui/Toast", async () => {
  const actual =
    await vi.importActual<typeof import("@/components/ui/Toast")>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(sel: (s: { push: (...args: unknown[]) => void }) => T) =>
      sel({ push: vi.fn() }),
  };
});

import { Step5Datasets } from "./Step5Datasets";
import type { FormState } from "../CreateProjectWizard";
import type { ProjectResponse } from "@/api/projects";

const baseForm: FormState = {
  name: "p",
  dataType: "lidar",
  typeKey: "lidar",
  dueDate: "",
  unitBindings: {},
  activeUnit: "lidar_box_3d",
  aiEnabled: false,
  mlBackendSourceId: "",
  sceneMode: true,
  datasetIds: [],
  splitStrategy: "by_scene",
  splitNBatches: 0,
  members: [],
  copyAnnotationGuide: true,
};

function Harness() {
  const [form, setForm] = useState<FormState>(baseForm);
  return (
    <Step5Datasets
      project={
        {
          id: "p1",
          display_id: "P-1",
          name: "project",
          type_key: "lidar",
          type_label: "点云",
          data_type: "lidar",
          scene_mode: true,
        } as unknown as ProjectResponse
      }
      form={form}
      setForm={setForm}
      onNext={vi.fn()}
    />
  );
}

describe("Step5Datasets", () => {
  beforeEach(() => {
    mockUseDatasets.mockReset();
  });

  it("scene lidar 项目按 lidar(media kind)+ has_scenes=true 过滤数据集", () => {
    mockUseDatasets.mockReturnValue({
      data: {
        items: [
          {
            id: "d1",
            display_id: "D-1",
            name: "nuScenes",
            data_type: "point_cloud",
            file_count: 3,
            has_scenes: true,
          },
        ],
      },
      isLoading: false,
    });

    render(<Harness />);

    expect(mockUseDatasets).toHaveBeenCalledWith({
      data_type: "lidar",
      has_scenes: true,
    });
    expect(screen.getByText("nuScenes")).toBeInTheDocument();
    expect(screen.getByText(/时序/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /nuScenes/ }));
    expect(screen.getByLabelText("按 scene 分包")).toBeChecked();
  });

  it("无匹配数据集时显示 kind 空态", () => {
    mockUseDatasets.mockReturnValue({ data: { items: [] }, isLoading: false });

    render(<Harness />);

    expect(screen.getByText(/没有符合该项目类型且包含时序/)).toBeInTheDocument();
  });
});
