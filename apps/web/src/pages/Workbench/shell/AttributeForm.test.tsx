// AttributeForm 单测：
// - mutable=true 字段在 context=video 时出现「逐帧」徽标；context=image / mutable 缺省时不出现
// - 传 dirtyTracker + annotationId 时，输入改变只标 dirty，不立即 onChange；form 失焦时 flush 触发一次 onChange
// - 不传 dirtyTracker 时使用 400ms debounce。

import { describe, expect, it, vi } from "vitest";
import { render, fireEvent, act } from "@testing-library/react";
import { renderHook } from "@testing-library/react";
import type { AttributeSchema } from "@/api/projects";
import { AttributeForm } from "./AttributeForm";
import { useDirtyTracker } from "../state/useDirtyTracker";

const schema: AttributeSchema = {
  fields: [
    { key: "color", label: "颜色", type: "text", mutable: false },
    { key: "occluded", label: "遮挡", type: "boolean", mutable: true },
  ],
};

describe("AttributeForm immediate drafts", () => {
  it("输入立即上抛，卸载和定时器不再重复提交", () => {
    vi.useFakeTimers();
    try {
      const onChange = vi.fn();
      const { getByDisplayValue, unmount } = render(
        <AttributeForm
          schema={schema}
          className="car"
          attributes={{ color: "red" }}
          onChange={onChange}
          immediate
        />,
      );
      fireEvent.change(getByDisplayValue("red"), { target: { value: "blue" } });
      expect(onChange).toHaveBeenCalledTimes(1);
      expect(onChange).toHaveBeenCalledWith({ color: "blue" });
      unmount();
      act(() => vi.runAllTimers());
      expect(onChange).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("即时草稿不登记已保存对象的 dirty tracker", () => {
    const { result: tracker } = renderHook(() => useDirtyTracker());
    const onChange = vi.fn();
    const { container, getByDisplayValue } = render(
      <AttributeForm
        schema={schema}
        className="car"
        attributes={{ color: "red" }}
        onChange={onChange}
        annotationId="anno-1"
        dirtyTracker={tracker.current}
        immediate
      />,
    );
    fireEvent.change(getByDisplayValue("red"), { target: { value: "blue" } });
    fireEvent.blur(container.firstChild as HTMLElement, { relatedTarget: null });
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith({ color: "blue" });
    expect(tracker.current.getDirtyFields("anno-1")).toEqual([]);
  });

  it("切到即时草稿时取消旧防抖，后续卸载不回写旧 owner", () => {
    vi.useFakeTimers();
    try {
      const previousOnChange = vi.fn();
      const nextOnChange = vi.fn();
      const { getByDisplayValue, rerender, unmount } = render(
        <AttributeForm
          schema={schema}
          className="car"
          attributes={{ color: "red" }}
          onChange={previousOnChange}
        />,
      );
      fireEvent.change(getByDisplayValue("red"), { target: { value: "blue" } });
      rerender(
        <AttributeForm
          schema={schema}
          className="car"
          attributes={{ color: "green" }}
          onChange={nextOnChange}
          immediate
        />,
      );
      unmount();
      act(() => vi.runAllTimers());
      expect(previousOnChange).not.toHaveBeenCalled();
      expect(nextOnChange).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("AttributeForm · mutable badge", () => {
  it("context=video + mutable=true 字段渲染「逐帧」徽标", () => {
    const { queryByTestId } = render(
      <AttributeForm
        schema={schema}
        className="car"
        attributes={{}}
        onChange={() => {}}
        context="video"
      />,
    );
    expect(queryByTestId("attr-mutable-badge-occluded")).not.toBeNull();
    expect(queryByTestId("attr-mutable-badge-color")).toBeNull();
  });

  it("context=image 下 mutable 标记被忽略（向后兼容）", () => {
    const { queryByTestId } = render(
      <AttributeForm
        schema={schema}
        className="car"
        attributes={{}}
        onChange={() => {}}
        context="image"
      />,
    );
    expect(queryByTestId("attr-mutable-badge-occluded")).toBeNull();
    expect(queryByTestId("attr-mutable-badge-color")).toBeNull();
  });
});

describe("AttributeForm · dirtyTracker 首次消费", () => {
  it("输入改变标 dirty 但不立即 onChange；blur form 时 flush 触发 onChange", () => {
    const { result: tracker } = renderHook(() => useDirtyTracker());
    const onChange = vi.fn();
    const { container, getByDisplayValue } = render(
      <AttributeForm
        schema={schema}
        className="car"
        attributes={{ color: "red" }}
        onChange={onChange}
        annotationId="anno-1"
        dirtyTracker={tracker.current}
      />,
    );

    const input = getByDisplayValue("red") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "blue" } });
    // dirty 已标，但 onChange 还没触发
    expect(tracker.current.getDirtyFields("anno-1")).toEqual(["attributes"]);
    expect(onChange).not.toHaveBeenCalled();

    // blur form 容器 → flush
    const formContainer = container.firstChild as HTMLElement;
    act(() => {
      fireEvent.blur(formContainer, { relatedTarget: null });
    });
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0]).toEqual({ color: "blue" });
    expect(tracker.current.getDirtyFields("anno-1")).toEqual([]);
  });

  it("不传 dirtyTracker 时维持 400ms debounce", () => {
    vi.useFakeTimers();
    const onChange = vi.fn();
    const { getByDisplayValue } = render(
      <AttributeForm
        schema={schema}
        className="car"
        attributes={{ color: "red" }}
        onChange={onChange}
      />,
    );
    const input = getByDisplayValue("red") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "blue" } });
    expect(onChange).not.toHaveBeenCalled();
    act(() => {
      vi.advanceTimersByTime(450);
    });
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0]).toEqual({ color: "blue" });
    vi.useRealTimers();
  });

  // 回归：debounce 未到点就卸载（弹层快速关闭）时，用最新 draft 补 flush，避免「改了没保存」
  it("debounce 路径下卸载前若有未到点提交，用最新 draft 补 flush 一次", () => {
    vi.useFakeTimers();
    const onChange = vi.fn();
    const { getByDisplayValue, unmount } = render(
      <AttributeForm
        schema={schema}
        className="car"
        attributes={{ color: "red" }}
        onChange={onChange}
      />,
    );
    const input = getByDisplayValue("red") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "blue" } });
    expect(onChange).not.toHaveBeenCalled();
    // 不等 400ms 直接卸载（模拟 <400ms 内关闭弹层）
    act(() => {
      unmount();
    });
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0]).toEqual({ color: "blue" });
    // 卸载已 clearTimeout，定时器不应再触发第二次
    act(() => {
      vi.advanceTimersByTime(450);
    });
    expect(onChange).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  // 无 pending 提交时卸载不应触发任何 onChange（杜绝空 flush / 重复提交）
  it("无未到点提交时卸载不触发 onChange", () => {
    const onChange = vi.fn();
    const { unmount } = render(
      <AttributeForm
        schema={schema}
        className="car"
        attributes={{ color: "red" }}
        onChange={onChange}
      />,
    );
    act(() => {
      unmount();
    });
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("AttributeForm · batch banner", () => {
  it("batchCount > 1 时在顶部渲染 banner 提示 N 个标注被选中", () => {
    const { queryByTestId, getByTestId } = render(
      <AttributeForm
        schema={schema}
        className="car"
        attributes={{ color: "red" }}
        onChange={() => {}}
        batchCount={3}
      />,
    );
    const banner = queryByTestId("attribute-form-batch-banner");
    expect(banner).not.toBeNull();
    expect(getByTestId("attribute-form-batch-banner").textContent).toContain("3");
  });

  it("origin=ai 的字段旁渲染 AI 溯源 chip; human 字段不渲染", () => {
    const { getByTestId, queryByTestId } = render(
      <AttributeForm
        schema={schema}
        className="car"
        attributes={{ color: "red" }}
        onChange={() => {}}
        attributesMeta={{
          color: { origin: "ai", model_ref: { model_id: "cls", backend_id: "be-1" } },
        }}
      />,
    );
    const chip = getByTestId("attr-ai-origin-color");
    expect(chip).not.toBeNull();
    expect(chip.getAttribute("title")).toContain("cls");
    // occluded 无 meta → 无 chip
    expect(queryByTestId("attr-ai-origin-occluded")).toBeNull();
  });

  it("不传 attributesMeta 时不渲染任何 AI chip", () => {
    const { queryByTestId } = render(
      <AttributeForm
        schema={schema}
        className="car"
        attributes={{ color: "red" }}
        onChange={() => {}}
      />,
    );
    expect(queryByTestId("attr-ai-origin-color")).toBeNull();
  });

  it("batchCount = 1 或未传时不渲染 banner (退化兼容单条编辑)", () => {
    const { queryByTestId, rerender } = render(
      <AttributeForm
        schema={schema}
        className="car"
        attributes={{ color: "red" }}
        onChange={() => {}}
        batchCount={1}
      />,
    );
    expect(queryByTestId("attribute-form-batch-banner")).toBeNull();
    rerender(
      <AttributeForm
        schema={schema}
        className="car"
        attributes={{ color: "red" }}
        onChange={() => {}}
      />,
    );
    expect(queryByTestId("attribute-form-batch-banner")).toBeNull();
  });
});
