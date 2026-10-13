import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ApiError } from "@/api/client";
import type { ProjectMemberResponse } from "@/api/projects";

const previewApi = vi.hoisted(() => ({ mutateAsync: vi.fn(), reset: vi.fn() }));
const changeApi = vi.hoisted(() => ({
  mutate: vi.fn(),
  reset: vi.fn(),
  isPending: false,
  isError: false,
  error: null as unknown,
}));
vi.mock("@/hooks/useProjects", () => ({
  usePreviewProjectMemberRole: () => previewApi,
  useChangeProjectMemberRole: () => changeApi,
}));

import { MemberRoleChangeModal } from "./MemberRoleChangeModal";

const member: ProjectMemberResponse = {
  id: "m1",
  user_id: "u1",
  user_name: "Alice",
  user_email: "alice@example.com",
  role: "annotator",
  platform_role: "employee",
  version: 2,
  assigned_at: "2026-01-01T00:00:00Z",
};
const otherAnnotator: ProjectMemberResponse = {
  ...member,
  id: "m2",
  user_id: "u2",
  user_name: "Bob",
};

const basePreview = {
  member_id: "m1",
  user_id: "u1",
  current_role: "annotator" as const,
  current_version: 3,
  target_role: "annotator" as const,
  requires_handoff: false,
  blockers: [] as string[],
  blocker_details: [],
  resource_snapshot: {},
  preview_token: "token-1",
};

function dynamicPreview() {
  previewApi.mutateAsync.mockImplementation(
    async (vars: { payload: { project_role: string } }) => ({
      ...basePreview,
      target_role: vars.payload.project_role,
    }),
  );
}

function renderModal() {
  return render(
    <MemberRoleChangeModal
      open
      projectId="p1"
      member={member}
      members={[member, otherAnnotator]}
      onClose={vi.fn()}
    />,
  );
}

describe("MemberRoleChangeModal", () => {
  beforeEach(() => {
    previewApi.mutateAsync.mockReset();
    previewApi.reset.mockReset();
    changeApi.mutate.mockReset();
    changeApi.reset.mockReset();
    changeApi.isPending = false;
    changeApi.isError = false;
    changeApi.error = null;
  });

  it("保存时回传预览版本与 token", async () => {
    dynamicPreview();
    renderModal();
    await screen.findByText(/当前职责/);
    fireEvent.change(screen.getByLabelText("目标项目职责"), { target: { value: "reviewer" } });
    await screen.findByText(/目标职责 质检员/);
    fireEvent.change(screen.getByLabelText("变更原因"), { target: { value: "分工调整" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "保存职责" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "保存职责" }));
    expect(changeApi.mutate).toHaveBeenCalledWith(
      {
        memberId: "m1",
        payload: {
          project_role: "reviewer",
          expected_version: 3,
          preview_token: "token-1",
          reason: "分工调整",
        },
      },
      expect.any(Object),
    );
  });

  it("存在阻塞项时禁止保存", async () => {
    previewApi.mutateAsync.mockResolvedValue({
      ...basePreview,
      blockers: ["仍有未完成审核"],
    });
    renderModal();
    await screen.findByText(/存在阻塞项/);
    fireEvent.change(screen.getByLabelText("变更原因"), { target: { value: "调整" } });
    expect(screen.getByRole("button", { name: "保存职责" })).toBeDisabled();
  });

  it("交接对象变化后必须重新预览才可保存", async () => {
    let resolveSecond!: (value: unknown) => void;
    previewApi.mutateAsync
      .mockResolvedValueOnce({ ...basePreview, requires_handoff: true })
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveSecond = resolve;
          }),
      );
    renderModal();
    await screen.findByText(/当前职责/);
    fireEvent.change(screen.getByLabelText("变更原因"), { target: { value: "调整" } });
    fireEvent.change(screen.getByLabelText("标注工作交接给"), { target: { value: "u2" } });
    expect(screen.getByRole("button", { name: "保存职责" })).toBeDisabled();
    await act(async () => {
      resolveSecond({ ...basePreview, requires_handoff: true, current_version: 5 });
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "保存职责" })).toBeEnabled());
  });

  it("丢弃较早输入的迟到预览响应", async () => {
    let resolveFirst!: (value: unknown) => void;
    previewApi.mutateAsync
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveFirst = resolve;
          }),
      )
      .mockResolvedValueOnce({ ...basePreview, target_role: "reviewer" });
    renderModal();
    fireEvent.change(screen.getByLabelText("目标项目职责"), { target: { value: "reviewer" } });
    await screen.findByText(/目标职责 质检员/);
    await act(async () => {
      resolveFirst({ ...basePreview, target_role: "annotator", current_version: 99 });
    });
    expect(screen.getByText(/目标职责 质检员/)).toBeInTheDocument();
  });

  it("过期预览冲突提示重新预览且不静默成功", async () => {
    previewApi.mutateAsync.mockResolvedValue(basePreview);
    changeApi.mutate.mockImplementation((_vars, opts) => {
      opts?.onError?.(new ApiError(409, "stale"));
    });
    renderModal();
    await screen.findByText(/当前职责/);
    fireEvent.change(screen.getByLabelText("变更原因"), { target: { value: "调整" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "保存职责" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "保存职责" }));
    expect(screen.getByRole("alert")).toHaveTextContent("预览已过期");
    expect(screen.getByRole("button", { name: "保存职责" })).toBeDisabled();
  });
});

describe("MemberRoleChangeModal context binding", () => {
  beforeEach(() => {
    previewApi.mutateAsync.mockReset();
    previewApi.reset.mockReset();
    changeApi.mutate.mockReset();
    changeApi.reset.mockReset();
    changeApi.isPending = false;
  });

  it("invalidates a previous preview when the member version changes", async () => {
    let resolveSecond!: (value: unknown) => void;
    previewApi.mutateAsync.mockResolvedValueOnce(basePreview).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveSecond = resolve;
        }),
    );
    const { rerender } = renderModal();
    await screen.findByText(/当前职责/);
    fireEvent.change(screen.getByLabelText("变更原因"), { target: { value: "调整" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "保存职责" })).toBeEnabled());

    rerender(
      <MemberRoleChangeModal
        open
        projectId="p1"
        member={{ ...member, version: 4 }}
        members={[member, otherAnnotator]}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "保存职责" })).toBeDisabled();
    await act(async () => {
      resolveSecond({ ...basePreview, current_version: 4 });
    });
    await waitFor(() => expect(screen.getByRole("button", { name: "保存职责" })).toBeEnabled());
  });

  it("ignores an old success after close and reopen of the same member", async () => {
    previewApi.mutateAsync.mockResolvedValue(basePreview);
    const onClose = vi.fn();
    const view = render(
      <MemberRoleChangeModal
        open
        projectId="p1"
        member={member}
        members={[member, otherAnnotator]}
        onClose={onClose}
      />,
    );
    await screen.findByText(/当前职责/);
    fireEvent.change(screen.getByLabelText("变更原因"), { target: { value: "调整" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "保存职责" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "保存职责" }));
    const options = changeApi.mutate.mock.calls[0][1];

    view.rerender(
      <MemberRoleChangeModal
        open={false}
        projectId="p1"
        member={member}
        members={[member, otherAnnotator]}
        onClose={onClose}
      />,
    );
    view.rerender(
      <MemberRoleChangeModal
        open
        projectId="p1"
        member={member}
        members={[member, otherAnnotator]}
        onClose={onClose}
      />,
    );
    await act(async () => {
      options.onSuccess();
    });
    expect(onClose).not.toHaveBeenCalled();
  });
});
