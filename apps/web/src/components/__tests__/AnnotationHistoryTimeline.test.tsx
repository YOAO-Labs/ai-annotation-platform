/**
 * v0.8.8 · AnnotationHistoryTimeline 单测：loading / empty / audit / comment / 多种 detail 格式。
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { AnnotationHistoryTimeline } from "../AnnotationHistoryTimeline";
import type { HistoryEntry } from "@/api/annotationHistory";

const baseActor = {
  id: "u1",
  name: "Alice",
  email: "alice@local",
  role: "employee",
  avatar_initial: "A",
};

const audit = (action: string, detail: Record<string, unknown> | null = null): HistoryEntry => ({
  kind: "audit",
  timestamp: new Date().toISOString(),
  actor: baseActor,
  action,
  detail,
  comment_id: null,
  body: null,
});

describe("AnnotationHistoryTimeline", () => {
  it("loading 时渲染加载占位", () => {
    render(<AnnotationHistoryTimeline entries={[]} loading />);
    expect(screen.getByText("加载历史…")).toBeInTheDocument();
  });

  it("entries 为空且非 loading → 「暂无历史记录」", () => {
    render(<AnnotationHistoryTimeline entries={[]} />);
    expect(screen.getByText("暂无历史记录")).toBeInTheDocument();
  });

  it("audit annotation.create + class_name → 显示「类别：xxx」", () => {
    render(
      <AnnotationHistoryTimeline
        entries={[audit("annotation.create", { class_name: "person" })]}
      />,
    );
    expect(screen.getByText("创建标注")).toBeInTheDocument();
    expect(screen.getByText("类别：person")).toBeInTheDocument();
  });

  it("audit annotation.attribute_change → 显示「<field>: <before> → <after>」", () => {
    render(
      <AnnotationHistoryTimeline
        entries={[
          audit("annotation.attribute_change", {
            field_key: "color",
            before: "red",
            after: "blue",
          }),
        ]}
      />,
    );
    expect(screen.getByText("属性变更")).toBeInTheDocument();
    expect(screen.getByText('color: "red" → "blue"')).toBeInTheDocument();
  });

  it("audit task.reject + reason → 显示原因摘要", () => {
    render(<AnnotationHistoryTimeline entries={[audit("task.reject", { reason: "类别错误" })]} />);
    expect(screen.getByText("驳回")).toBeInTheDocument();
    expect(screen.getByText("类别错误")).toBeInTheDocument();
  });

  it("audit annotation.update + fields → 显示「字段：a、b」", () => {
    render(
      <AnnotationHistoryTimeline entries={[audit("annotation.update", { fields: ["x", "y"] })]} />,
    );
    expect(screen.getByText("修改标注")).toBeInTheDocument();
    expect(screen.getByText("字段：x、y")).toBeInTheDocument();
  });

  it("comment 类型渲染评论 badge + body", () => {
    render(
      <AnnotationHistoryTimeline
        entries={[
          {
            kind: "comment",
            timestamp: new Date().toISOString(),
            actor: baseActor,
            action: null,
            detail: null,
            comment_id: "c1",
            body: "请确认边界",
          },
        ]}
      />,
    );
    expect(screen.getByText("评论")).toBeInTheDocument();
    expect(screen.getByText("请确认边界")).toBeInTheDocument();
  });

  it("已撤回 comment 显示「（已撤回）」并降低 opacity", () => {
    render(
      <AnnotationHistoryTimeline
        entries={[
          {
            kind: "comment",
            timestamp: new Date().toISOString(),
            actor: baseActor,
            action: null,
            detail: { is_active: false },
            comment_id: "c2",
            body: "测试",
          },
        ]}
      />,
    );
    expect(screen.getByText("评论（已撤回）")).toBeInTheDocument();
  });

  it("未识别的 action 退化为原始 action 字符串", () => {
    render(<AnnotationHistoryTimeline entries={[audit("unknown.action")]} />);
    expect(screen.getByText("unknown.action")).toBeInTheDocument();
  });

  it("显示明确的历史范围并支持填充展示", () => {
    const { container } = render(
      <AnnotationHistoryTimeline
        entries={[audit("task.submit")]}
        presentation="fill"
        scopeLabel="本任务历史"
      />,
    );
    expect(screen.getByText("本任务历史")).toBeInTheDocument();
    expect(container.querySelector('[data-presentation="fill"]')).toBeInTheDocument();
  });

  it("错误状态不是空状态，并提供重试", () => {
    const onRetry = vi.fn();
    render(
      <AnnotationHistoryTimeline
        entries={[]}
        error={new Error("network")}
        onRetry={onRetry}
        scopeLabel="当前标注历史"
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("无法加载历史记录");
    screen.getByRole("button", { name: "重试" }).click();
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("暂无历史记录")).not.toBeInTheDocument();
  });
});
