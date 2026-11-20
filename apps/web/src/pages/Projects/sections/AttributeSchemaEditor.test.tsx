/**
 * AttributeSchemaEditor 单测
 *
 * 1. validateAttributeFields 纯函数：空 key / 重复 key / select 缺 options
 * 2. 组件 render：fields 数组渲染对应字段；onChange 在 add/remove 时触发
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import {
  AttributeSchemaEditor,
  validateAttributeFields,
  newAttributeField,
} from "./AttributeSchemaEditor";
import type { AttributeField } from "@/api/projects";

describe("validateAttributeFields", () => {
  it("空数组合法", () => {
    expect(validateAttributeFields([])).toBeNull();
  });

  it("空 key 不合法", () => {
    const fields: AttributeField[] = [{ key: "", label: "x", type: "text", required: false }];
    expect(validateAttributeFields(fields)).toContain("key");
  });

  it("重复 key 不合法", () => {
    const fields: AttributeField[] = [
      { key: "a", label: "A", type: "text" },
      { key: "a", label: "B", type: "text" },
    ];
    expect(validateAttributeFields(fields)).toContain("重复");
  });

  it("select 类型缺 options 不合法", () => {
    const fields: AttributeField[] = [{ key: "color", label: "颜色", type: "select" }];
    expect(validateAttributeFields(fields)).toContain("选项");
  });

  it("multiselect 缺 options 不合法", () => {
    const fields: AttributeField[] = [{ key: "tags", label: "Tags", type: "multiselect" }];
    expect(validateAttributeFields(fields)).toContain("选项");
  });

  it("选项 value 为空不合法", () => {
    const fields: AttributeField[] = [
      {
        key: "color",
        label: "颜色",
        type: "select",
        options: [{ value: "", label: "红" }],
      },
    ];
    expect(validateAttributeFields(fields)).toContain("不能为空");
  });

  it("选项 value 重复不合法", () => {
    const fields: AttributeField[] = [
      {
        key: "color",
        label: "颜色",
        type: "select",
        options: [
          { value: "r", label: "红" },
          { value: "r", label: "赤" },
        ],
      },
    ];
    expect(validateAttributeFields(fields)).toContain("重复");
  });

  it("完整字段全合法", () => {
    const fields: AttributeField[] = [
      { key: "occluded", label: "遮挡", type: "boolean" },
      {
        key: "color",
        label: "颜色",
        type: "select",
        options: [{ value: "r", label: "红" }],
      },
    ];
    expect(validateAttributeFields(fields)).toBeNull();
  });
});

describe("newAttributeField", () => {
  it("默认是 text + required:false", () => {
    const f = newAttributeField();
    expect(f.type).toBe("text");
    expect(f.required).toBe(false);
    expect(f.key).toBe("");
  });
});

describe("<AttributeSchemaEditor />", () => {
  it("空数组显示 emptyHint", () => {
    render(<AttributeSchemaEditor value={[]} onChange={() => {}} emptyHint="自定义空提示" />);
    expect(screen.getByText("自定义空提示")).toBeInTheDocument();
  });

  it("点击 '新增属性' 调 onChange 加一行", () => {
    const onChange = vi.fn();
    render(<AttributeSchemaEditor value={[]} onChange={onChange} />);
    fireEvent.click(screen.getByText("新增属性"));
    expect(onChange).toHaveBeenCalledTimes(1);
    const next = onChange.mock.calls[0][0] as AttributeField[];
    expect(next).toHaveLength(1);
    expect(next[0].type).toBe("text");
  });

  it("已有字段渲染对应 key 输入框", () => {
    const fields: AttributeField[] = [{ key: "occluded", label: "是否遮挡", type: "boolean" }];
    render(<AttributeSchemaEditor value={fields} onChange={() => {}} />);
    expect(screen.getByDisplayValue("occluded")).toBeInTheDocument();
    expect(screen.getByDisplayValue("是否遮挡")).toBeInTheDocument();
  });

  it("删除字段前等待确认回调", async () => {
    const fields: AttributeField[] = [{ key: "occluded", label: "是否遮挡", type: "boolean" }];
    const onChange = vi.fn();
    const onConfirmDelete = vi.fn().mockResolvedValue(false);
    render(
      <AttributeSchemaEditor
        value={fields}
        onChange={onChange}
        onConfirmDelete={onConfirmDelete}
      />,
    );

    fireEvent.click(screen.getByTitle("删除"));

    await waitFor(() => {
      expect(onConfirmDelete).toHaveBeenCalledWith(fields[0]);
    });
    expect(onChange).not.toHaveBeenCalled();
  });
});
