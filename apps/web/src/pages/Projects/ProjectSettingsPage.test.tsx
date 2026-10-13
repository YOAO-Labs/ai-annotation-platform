import { fireEvent, screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ProjectResponse } from "@/api/projects";
import { server } from "@/mocks/server";
import { expectNoUnexpectedApiRequests, resetUnexpectedApiRequests } from "@/test/apiRequestGuard";
import { createTestUser, resetAuthUser, seedAuthUser } from "@/test/auth";
import { renderWithProviders } from "@/test/renderWithProviders";

// Heavy section bodies are stand-ins on purpose: this suite protects the
// settings page's routing, deep-link and project-role assembly contract, not
// each section's own business flow. Those sections keep their own suites.
vi.mock("./sections/GeneralSection", () => ({
  GeneralSection: () => <div>general-section</div>,
}));
vi.mock("./sections/MembersSection", () => ({
  MembersSection: () => <div>members-section</div>,
}));
vi.mock("./sections/OwnerSection", () => ({
  OwnerSection: () => <div>owner-section</div>,
}));
vi.mock("./sections/DangerSection", () => ({
  DangerSection: () => <div>danger-section</div>,
}));
vi.mock("./sections/BatchesSection", () => ({
  BatchesSection: () => <div>batches-section</div>,
}));
vi.mock("./sections/ClassesSection", () => ({
  ClassesSection: () => <div>classes-section</div>,
}));
vi.mock("./sections/DatasetsSection", () => ({
  DatasetsSection: () => <div>datasets-section</div>,
}));
vi.mock("./sections/MlBackendsSection", () => ({
  MlBackendsSection: () => <div>ml-backends-section</div>,
}));
vi.mock("./sections/RenderingConfigSection", () => ({
  RenderingConfigSection: () => <div>rendering-section</div>,
}));
vi.mock("./sections/VideoSamplingSection", () => ({
  VideoSamplingSection: () => <div>video-sampling-section</div>,
}));
vi.mock("./sections/AnnotationGuideSection", () => ({
  AnnotationGuideSection: () => <div>annotation-guide-section</div>,
}));
vi.mock("./sections/ProjectReadinessSection", () => ({
  ProjectReadinessSection: () => <div>project-readiness-section</div>,
}));

import { ProjectSettingsPage } from "./ProjectSettingsPage";

const OWNER_ID = "owner-1";

function projectResponse(overrides: Partial<ProjectResponse>): ProjectResponse {
  return {
    id: "p-image",
    name: "Image Project",
    display_id: "P-IMAGE",
    owner_id: OWNER_ID,
    type_label: "图像检测",
    type_key: "image-det",
    data_type: "image",
    status: "in_progress",
    ...overrides,
  } as unknown as ProjectResponse;
}

function installProjectApi(project: ProjectResponse) {
  server.use(http.get("*/api/v1/projects/:projectId", () => HttpResponse.json(project)));
}

function LocationProbe() {
  const location = useLocation();
  return (
    <div data-testid="location">
      {location.pathname}
      {location.search}
    </div>
  );
}

function renderSettingsPage(path: string, probes?: ReactNode) {
  return renderWithProviders(
    <Routes>
      <Route path="/projects/:id/settings" element={<ProjectSettingsPage />} />
      <Route
        path="/projects/:id/annotate"
        element={
          <>
            <div>workbench-target</div>
            <LocationProbe />
          </>
        }
      />
      <Route path="/unauthorized" element={<div>unauthorized</div>} />
    </Routes>,
    { initialEntries: [path], children: probes },
  );
}

beforeEach(() => {
  resetUnexpectedApiRequests();
  seedAuthUser(createTestUser({ id: OWNER_ID, role: "project_admin" }));
});

afterEach(() => {
  expectNoUnexpectedApiRequests();
  resetAuthUser();
});

describe("ProjectSettingsPage", () => {
  it("shows the workbench entry for video projects", async () => {
    installProjectApi(
      projectResponse({
        id: "p-video",
        name: "Video Project",
        display_id: "P-VIDEO",
        type_label: "视频项目",
        type_key: "video-track",
        data_type: "video",
      }),
    );

    renderSettingsPage("/projects/p-video/settings");

    fireEvent.click(await screen.findByRole("button", { name: /打开工作台/ }));

    expect(screen.getByText("workbench-target")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/projects/p-video/annotate?returnTo=%2Fprojects%2Fp-video%2Fsettings",
    );
  });

  it("shows the workbench entry for point cloud projects", async () => {
    installProjectApi(
      projectResponse({
        id: "p-lidar",
        name: "Point Cloud Project",
        display_id: "P-LIDAR",
        type_label: "3D 点云",
        type_key: "lidar",
        data_type: "lidar",
      }),
    );

    renderSettingsPage("/projects/p-lidar/settings");

    fireEvent.click(await screen.findByRole("button", { name: /打开工作台/ }));

    expect(screen.getByText("workbench-target")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/projects/p-lidar/annotate?returnTo=%2Fprojects%2Fp-lidar%2Fsettings",
    );
  });

  it("uses one combined classes and attributes settings tab", async () => {
    installProjectApi(projectResponse({ id: "p-image", display_id: "P-IMAGE" }));

    renderSettingsPage("/projects/p-image/settings");

    expect(await screen.findByTestId("settings-tab-classes")).toHaveTextContent("类别与属性");
    expect(screen.queryByTestId("settings-tab-attributes")).toBeNull();
  });

  it("maps old section=attributes links to the combined tab", async () => {
    installProjectApi(projectResponse({ id: "p-image", display_id: "P-IMAGE" }));

    renderSettingsPage("/projects/p-image/settings?section=attributes");

    expect(await screen.findByText("classes-section")).toBeInTheDocument();
  });

  it("keeps a non-owner employee out of project settings", async () => {
    installProjectApi(projectResponse({ id: "p-image", display_id: "P-IMAGE" }));
    seedAuthUser(createTestUser({ id: "employee-2", role: "employee" }));

    renderSettingsPage("/projects/p-image/settings");

    expect(await screen.findByText("unauthorized")).toBeInTheDocument();
  });
});
