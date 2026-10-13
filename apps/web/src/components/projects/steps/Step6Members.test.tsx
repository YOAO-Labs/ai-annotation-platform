import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";

const mockUseUsers = vi.fn();

vi.mock("@/hooks/useUsers", () => ({
  useUsers: (...args: unknown[]) => mockUseUsers(...args),
}));

vi.mock("@/hooks/useProjects", () => ({
  useAddProjectMember: () => ({ mutateAsync: vi.fn(), isPending: false }),
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

import { Step6Members } from "./Step6Members";
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

const user = (id: string, name: string) => ({
  id,
  name,
  email: `${id}@example.com`,
  role: "employee",
  is_active: true,
  status: "offline",
  group_id: null,
  group_name: null,
  created_at: `2026-01-0${id.slice(-1)}T00:00:00Z`,
});

function Harness() {
  const [form, setForm] = useState<FormState>(baseForm);
  return (
    <Step6Members
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

describe("Step6Members", () => {
  beforeEach(() => {
    mockUseUsers.mockReset();
  });

  it("只按平台员工取一次候选人", () => {
    mockUseUsers.mockReturnValue({
      data: [user("a1", "Free Employee")],
      isLoading: false,
    });
    render(<Harness />);

    expect(mockUseUsers).toHaveBeenCalledWith({ role: "employee" });
    expect(screen.getByText("Free Employee")).toBeInTheDocument();
  });

  it("无候选人时显示空态提示", () => {
    mockUseUsers.mockReturnValue({ data: [], isLoading: false });
    render(<Harness />);
    expect(screen.getByText(/暂无员工账号/)).toBeInTheDocument();
  });

  it("点击候选人写入表单成员并默认标注员，可切换为质检员", () => {
    mockUseUsers.mockReturnValue({ data: [user("a1", "Free Employee")], isLoading: false });
    render(<Harness />);
    fireEvent.click(screen.getByText("Free Employee"));
    expect(screen.getByText(/添加 1 位并完成/)).toBeInTheDocument();
    const select = screen.getByLabelText("项目职责 a1@example.com") as HTMLSelectElement;
    expect(select.value).toBe("annotator");
    fireEvent.change(select, { target: { value: "reviewer" } });
    expect(select.value).toBe("reviewer");
  });
});
