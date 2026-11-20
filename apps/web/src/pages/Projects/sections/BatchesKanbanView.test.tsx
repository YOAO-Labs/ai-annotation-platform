/**
 * BatchesKanbanView 单测：核心是验证拖拽迁移合法性 dryrun。
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { BatchesKanbanView } from "./BatchesKanbanView";
import type { BatchResponse } from "@/api/batches";

function batch(over: Partial<BatchResponse> = {}): BatchResponse {
  return {
    id: over.id ?? "b1",
    project_id: "p1",
    dataset_id: null,
    display_id: over.display_id ?? "B-1",
    name: over.name ?? "batch one",
    description: "",
    status: over.status ?? "active",
    priority: 50,
    deadline: null,
    assigned_user_ids: [],
    annotator_id: null,
    reviewer_id: null,
    annotator: null,
    reviewer: null,
    total_tasks: 10,
    completed_tasks: 5,
    review_tasks: 0,
    approved_tasks: 0,
    rejected_tasks: 0,
    progress_pct: 50,
    created_by: null,
    created_at: "",
    updated_at: null,
    review_feedback: null,
    reviewed_at: null,
    reviewed_by: null,
    admin_locked: false,
    admin_lock_reason: null,
    admin_locked_at: null,
    admin_locked_by: null,
    ...over,
  };
}

describe("<BatchesKanbanView />", () => {
  it("渲染 7 个状态列", () => {
    render(<BatchesKanbanView batches={[]} isOwner={true} onTransition={() => {}} />);
    ["草稿", "激活", "标注中", "审核中", "已通过", "已退回", "已归档"].forEach((label) => {
      expect(screen.getByText(label)).toBeInTheDocument();
    });
  });

  it("批次按 status 落到对应列", () => {
    const batches = [
      batch({ id: "1", status: "active", name: "act-batch" }),
      batch({ id: "2", status: "approved", name: "ok-batch" }),
    ];
    render(<BatchesKanbanView batches={batches} isOwner={true} onTransition={() => {}} />);
    expect(screen.getByText("act-batch")).toBeInTheDocument();
    expect(screen.getByText("ok-batch")).toBeInTheDocument();
  });

  it("非 owner 时卡片不可拖拽", () => {
    const batches = [batch({ id: "1", status: "active", name: "x" })];
    const { container } = render(
      <BatchesKanbanView batches={batches} isOwner={false} onTransition={() => {}} />,
    );
    const card = container.querySelector('[draggable="false"]');
    expect(card).not.toBeNull();
  });

  it("非法 transition（active → reviewing）不会调 onTransition", () => {
    const onTransition = vi.fn();
    const batches = [batch({ id: "1", status: "active", name: "x" })];
    const { container } = render(
      <BatchesKanbanView batches={batches} isOwner={true} onTransition={onTransition} />,
    );

    // 模拟 dragStart on card
    const card = container.querySelector('[draggable="true"]')!;
    fireEvent.dragStart(card);

    // 找到 "审核中" 列容器并触发 drop
    const reviewing = screen.getByText("审核中").closest("div")?.parentElement;
    expect(reviewing).toBeTruthy();
    fireEvent.dragOver(reviewing!);
    fireEvent.drop(reviewing!);

    expect(onTransition).not.toHaveBeenCalled();
  });

  it("合法 transition（active → annotating）调 onTransition", () => {
    const onTransition = vi.fn();
    const batches = [batch({ id: "1", status: "active", name: "x" })];
    const { container } = render(
      <BatchesKanbanView batches={batches} isOwner={true} onTransition={onTransition} />,
    );
    const card = container.querySelector('[draggable="true"]')!;
    fireEvent.dragStart(card);
    const annotating = screen.getByText("标注中").closest("div")?.parentElement;
    fireEvent.dragOver(annotating!);
    fireEvent.drop(annotating!);
    expect(onTransition).toHaveBeenCalledTimes(1);
    expect(onTransition.mock.calls[0][1]).toBe("annotating");
  });
});
