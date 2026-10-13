import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ProjectResponse } from "@/api/projects";
import { ProjectActionsMenu } from "./ProjectActionsMenu";

vi.mock("@/api/maskFormats", () => ({
  maskFormatsApi: { list: vi.fn() },
}));
vi.mock("@/components/mask-formats/MaskFormatImportWizard", () => ({
  MaskFormatImportWizard: () => <div data-testid="mask-import-wizard" />,
}));
vi.mock("@/components/predictions/PredictionImportWizard", () => ({
  PredictionImportWizard: () => null,
}));
vi.mock("@/components/predictions/PredictionPurgeModal", () => ({
  PredictionPurgeModal: () => null,
}));
vi.mock("./ExportModal", () => ({
  ExportModal: ({ open }: { open: boolean }) => (open ? <div data-testid="export-modal" /> : null),
}));

import { maskFormatsApi } from "@/api/maskFormats";

const project = {
  id: "project-1",
  display_id: "P-1",
  name: "Mask Project",
  type_key: "image-seg",
  type_label: "图像分割",
  data_type: "image",
} as ProjectResponse;

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname + location.search}</output>;
}

function renderMenu(canManage = true) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <LocationProbe />
        <ProjectActionsMenu project={project} canManage={canManage} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ProjectActionsMenu", () => {
  beforeEach(() => {
    vi.mocked(maskFormatsApi.list).mockReset();
  });

  it("opens project data directly and limits the team entry to managers", () => {
    renderMenu(false);
    fireEvent.click(screen.getByRole("button", { name: "数据管理" }));
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/projects/project-1/data-manager?section=overview",
    );
    fireEvent.click(screen.getByTitle("更多操作"));
    expect(screen.queryByRole("menuitem", { name: "成员绩效" })).not.toBeInTheDocument();
  });

  it("opens member performance in the selected project", async () => {
    vi.mocked(maskFormatsApi.list).mockResolvedValue([]);
    renderMenu();
    await act(async () => fireEvent.click(screen.getByTitle("更多操作")));
    fireEvent.click(screen.getByRole("menuitem", { name: "成员绩效" }));
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/projects/project-1/data-manager?section=members",
    );
  });

  it("菜单打开后按 registry 已验证能力暴露标注导入", async () => {
    vi.mocked(maskFormatsApi.list).mockResolvedValue([
      {
        format_id: "coco",
        label: "COCO Instance",
        adapter_version: "2.0.0",
        manifest_version: "1",
        media_types: ["image"],
        import_capability: {
          supported: true,
          verified: true,
          enabled_for_ui: true,
        },
        export_capability: {
          supported: true,
          verified: true,
          enabled_for_ui: true,
        },
        option_schema: {},
      },
    ]);
    renderMenu();
    fireEvent.click(screen.getByTitle("更多操作"));

    const importItem = await screen.findByRole("menuitem", { name: "导入标注" });
    expect(maskFormatsApi.list).toHaveBeenCalledWith("project-1");
    fireEvent.click(importItem);
    expect(screen.getByTestId("mask-import-wizard")).toBeInTheDocument();
  });

  it("只在选择导出后加载导出弹窗", async () => {
    vi.mocked(maskFormatsApi.list).mockResolvedValue([]);
    renderMenu();

    expect(screen.queryByTestId("export-modal")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTitle("更多操作"));
    fireEvent.click(screen.getByRole("menuitem", { name: "导出标注数据" }));

    expect(await screen.findByTestId("export-modal")).toBeInTheDocument();
  });

  it("未验证 adapter 不暴露标注导入", async () => {
    vi.mocked(maskFormatsApi.list).mockResolvedValue([
      {
        format_id: "candidate",
        label: "Candidate",
        adapter_version: "1.0.0",
        manifest_version: "1",
        media_types: ["image"],
        import_capability: {
          supported: true,
          verified: false,
          enabled_for_ui: false,
        },
        export_capability: {
          supported: false,
          verified: false,
          enabled_for_ui: false,
        },
        option_schema: {},
      },
    ]);
    renderMenu();
    fireEvent.click(screen.getByTitle("更多操作"));

    await waitFor(() => expect(maskFormatsApi.list).toHaveBeenCalledOnce());
    expect(screen.queryByRole("menuitem", { name: "导入标注" })).not.toBeInTheDocument();
  });
});
