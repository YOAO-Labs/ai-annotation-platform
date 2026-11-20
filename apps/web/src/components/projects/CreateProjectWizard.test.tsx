import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";
import type { ProjectCreatePayload } from "@/api/projects";
import { projectTemplatesApi } from "@/api/projectTemplates";
import { server } from "@/mocks/server";
import * as stageColors from "@/pages/Workbench/stage/colors";
import { expectNoUnexpectedApiRequests, resetUnexpectedApiRequests } from "@/test/apiRequestGuard";
import { createTestUser, resetAuthUser, seedAuthUser } from "@/test/auth";
import { renderWithProviders } from "@/test/renderWithProviders";
import { CreateProjectWizard } from "./CreateProjectWizard";

function renderUI(props: Partial<ComponentProps<typeof CreateProjectWizard>> = {}) {
  return renderWithProviders(<CreateProjectWizard open onClose={vi.fn()} {...props} />);
}

describe("CreateProjectWizard", () => {
  const creates: ProjectCreatePayload[] = [];

  beforeEach(() => {
    creates.length = 0;
    resetUnexpectedApiRequests();
    seedAuthUser(createTestUser({ role: "project_admin" }));
    server.use(
      http.post("*/api/v1/projects", async ({ request }) => {
        const body = (await request.json()) as ProjectCreatePayload;
        creates.push(body);
        return HttpResponse.json({ ...body, id: "p-new", display_id: "P-1" }, { status: 201 });
      }),
      http.get("*/api/v1/datasets", () => HttpResponse.json({ items: [], total: 0 })),
    );
  });

  afterEach(() => {
    cleanup();
    resetAuthUser();
    vi.restoreAllMocks();
    expectNoUnexpectedApiRequests();
  });

  it("名称边界：空白和一个字符不能继续，两个非空字符可以无来源创建", async () => {
    renderUI();
    expect(screen.getByRole("dialog", { name: "新建项目" })).toBeVisible();
    const input = screen.getByPlaceholderText(/智能门店货架/);
    const next = screen.getByRole("button", { name: /下一步/ });
    expect(next).toBeDisabled();
    fireEvent.change(input, { target: { value: " A " } });
    expect(next).toBeDisabled();
    fireEvent.change(input, { target: { value: " AB " } });
    expect(next).toBeEnabled();
    fireEvent.click(next);
    expect(screen.getByRole("button", { name: /上一步/ })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    fireEvent.click(screen.getByRole("button", { name: /^创建$/ }));
    await waitFor(() =>
      expect(creates).toMatchObject([{ name: "AB", template_id: null, source_project_id: null }]),
    );
  });

  it.each([
    { mode: "source", legacy: false },
    { mode: "source", legacy: true },
    { mode: "template", legacy: false },
    { mode: "template", legacy: true },
  ])("$mode legacy=$legacy 保留显式颜色并用类别默认色预填及提交", async ({ mode, legacy }) => {
    // Hex fixtures exercise the real editor without relying on jsdom's canvas parser.
    vi.spyOn(stageColors, "classColor").mockImplementation((name) =>
      name === "person" ? "#123456" : "#abcdef",
    );
    const classes = [
      { name: "car", color: "#ff0000", order: 2 },
      { name: "bicycle", order: 1 },
      { name: "person", order: 0, alias: "pedestrian" },
      { name: "motorcycle", color: "#888888", order: 3 },
    ];
    const source = {
      name: "Source",
      type_key: "image-det",
      data_type: "image",
      ai_enabled: false,
      ...(legacy
        ? {
            classes: classes.map((c) => c.name),
            classes_config: Object.fromEntries(
              classes.map(({ name, ...config }) => [name, config]),
            ),
          }
        : { tool_bindings: { bbox: { enabled: true, classes } } }),
    };
    const route = mode === "source" ? "*/api/v1/projects/src1" : "*/api/v1/project-templates/t1";
    server.use(http.get(route, () => HttpResponse.json(source)));
    renderUI(mode === "source" ? { sourceProjectId: "src1" } : { templateId: "t1" });
    expect(
      screen.getByRole("dialog", { name: mode === "source" ? "复制项目配置" : "从模板创建项目" }),
    ).toBeVisible();
    await screen.findByText(mode === "source" ? /已用源项目配置预填表单/ : /已用模板字段预填表单/);
    fireEvent.change(screen.getByPlaceholderText(/智能门店货架/), {
      target: { value: "颜色复制测试" },
    });
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));

    const colorInputs = screen
      .getByRole("dialog")
      .querySelectorAll<HTMLInputElement>("input[type=color]");
    expect(Array.from(colorInputs, (input) => input.value)).toEqual([
      "#123456",
      "#abcdef",
      "#ff0000",
      "#888888",
    ]);
    expect(screen.getByDisplayValue("pedestrian")).toBeInTheDocument();
    fireEvent.change(colorInputs[1], { target: { value: "#654321" } });
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    fireEvent.click(screen.getByRole("button", { name: /下一步/ }));
    fireEvent.click(screen.getByRole("button", { name: /^创建$/ }));
    await waitFor(() => expect(creates).toHaveLength(1));
    const [payload] = creates;
    expect(payload).toMatchObject({
      source_project_id: mode === "source" ? "src1" : null,
      template_id: mode === "template" ? "t1" : null,
    });
    expect(payload).toHaveProperty("tool_bindings.bbox.classes", [
      { name: "person", color: "#123456", order: 0, alias: "pedestrian" },
      { name: "bicycle", color: "#654321", order: 1 },
      { name: "car", color: "#ff0000", order: 2 },
      { name: "motorcycle", color: "#888888", order: 3 },
    ]);
    expect(payload).toHaveProperty("classes_config.person", {
      color: "#123456",
      order: 0,
      alias: "pedestrian",
    });
    expect(payload).toHaveProperty("classes_config.bicycle", { color: "#654321", order: 1 });
  });

  it("模板向导关闭时既不展示表单，也不读取模板详情", () => {
    const readTemplate = vi.spyOn(projectTemplatesApi, "get");
    renderUI({ open: false, templateId: "t1" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(readTemplate).not.toHaveBeenCalled();
  });
});
