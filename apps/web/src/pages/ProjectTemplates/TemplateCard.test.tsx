/**
 * TemplateCard 单测.
 * 操作按钮可见性 / usage_count 渲染 / scope chip.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import type { ProjectTemplateOut } from "@/api/projectTemplates";

import { TemplateCard } from "./TemplateCard";

function makeTemplate(over: Partial<ProjectTemplateOut> = {}): ProjectTemplateOut {
  return {
    id: "t-1",
    display_id: "PT-1",
    name: "模板",
    description: null,
    type_label: "图像-检测",
    type_key: "image-det",
    classes: [],
    classes_config: {},
    attribute_schema: { fields: [] },
    label_config: {},
    ai_enabled: false,
    sampling: "sequence",
    maximum_annotations: 1,
    show_overlap_first: false,
    iou_dedup_threshold: 0.7,
    box_threshold: 0.35,
    text_threshold: 0.25,
    rendering_config: {},
    annotation_guide: null,
    scope: "private",
    organization_id: null,
    created_by: "u-1",
    created_by_name: "creator",
    source_project_id: null,
    usage_count: 0,
    created_at: "",
    updated_at: "",
    ...over,
  };
}

describe("TemplateCard", () => {
  it("canEdit=false → 不显示编辑/删除按钮", () => {
    render(
      <TemplateCard
        template={makeTemplate({ id: "t-a" })}
        canEdit={false}
        onApply={() => {}}
        onDuplicate={() => {}}
        onEdit={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(screen.queryByTestId("template-delete-t-a")).not.toBeInTheDocument();
    expect(screen.getByTestId("template-apply-t-a")).toBeInTheDocument();
  });

  it("usage_count + 含指引 → 在 cardBody 渲染对应文案", () => {
    render(
      <TemplateCard
        template={makeTemplate({
          id: "t-b",
          usage_count: 7,
          annotation_guide: "# 指引",
          classes: ["car", "person"],
        })}
        canEdit={true}
        onApply={() => {}}
        onDuplicate={() => {}}
        onEdit={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(screen.getByText(/使用 7 次/)).toBeInTheDocument();
    expect(screen.getByText(/2 个类别/)).toBeInTheDocument();
    expect(screen.getByText(/含指引/)).toBeInTheDocument();
  });

  it("scope chip 根据 scope 切换文案与样式 class", () => {
    const { rerender } = render(
      <TemplateCard
        template={makeTemplate({ id: "t-c", scope: "private" })}
        canEdit={true}
        onApply={() => {}}
        onDuplicate={() => {}}
        onEdit={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(screen.getByTestId("template-scope-t-c").textContent).toBe("私有");

    rerender(
      <TemplateCard
        template={makeTemplate({ id: "t-c", scope: "public" })}
        canEdit={true}
        onApply={() => {}}
        onDuplicate={() => {}}
        onEdit={() => {}}
        onDelete={() => {}}
      />,
    );
    expect(screen.getByTestId("template-scope-t-c").textContent).toBe("公共");
  });

  it("点击应用 → 调用 onApply", () => {
    const onApply = vi.fn();
    render(
      <TemplateCard
        template={makeTemplate({ id: "t-d" })}
        canEdit={true}
        onApply={onApply}
        onDuplicate={() => {}}
        onEdit={() => {}}
        onDelete={() => {}}
      />,
    );
    fireEvent.click(screen.getByTestId("template-apply-t-d"));
    expect(onApply).toHaveBeenCalledOnce();
  });
});
