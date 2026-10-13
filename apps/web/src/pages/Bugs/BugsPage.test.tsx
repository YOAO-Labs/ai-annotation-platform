import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import { BugsPage } from "./BugsPage";

const mocks = vi.hoisted(() => ({
  pushToast: vi.fn(),
  list: vi.fn(),
  get: vi.fn(),
  update: vi.fn(),
  addComment: vi.fn(),
  useBugReports: vi.fn(),
}));

vi.mock("@/components/ui/Toast", () => ({
  useToastStore: <T,>(selector: (s: { push: typeof mocks.pushToast }) => T) =>
    selector({ push: mocks.pushToast }),
}));

vi.mock("@/components/markdown/MarkdownEditor", () => ({
  MarkdownEditor: ({
    value,
    onChange,
    onSubmit,
    onBlur,
    onUploadImage,
    placeholder,
    documentId,
    label,
    variant,
    disabled,
  }: {
    value: string;
    onChange: (next: string) => void;
    onSubmit?: (next: string) => void;
    onBlur?: () => void;
    onUploadImage?: unknown;
    placeholder?: string;
    documentId?: string;
    label?: string;
    variant?: string;
    disabled?: boolean;
  }) => (
    <textarea
      data-testid="markdown-editor"
      data-document-id={documentId}
      data-label={label}
      data-variant={variant}
      data-has-upload={onUploadImage ? "true" : "false"}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) onSubmit?.(value);
      }}
      onBlur={onBlur}
      placeholder={placeholder}
      disabled={disabled}
    />
  ),
}));

vi.mock("@/api/bug-reports", () => ({
  bugReportsApi: {
    list: mocks.list,
    get: mocks.get,
    update: mocks.update,
    addComment: mocks.addComment,
    attachmentDownloadUrl: (_id: string, key: string) => `/download?key=${encodeURIComponent(key)}`,
  },
}));

vi.mock("@/hooks/useBugReports", () => ({
  useBugReports: (...args: unknown[]) => mocks.useBugReports(...args),
}));

const item = {
  id: "bug-1",
  display_id: "B-1",
  reporter_id: "u1",
  route: "/dashboard",
  user_role: "employee",
  project_id: null,
  task_id: null,
  title: "Markdown detail",
  description: "**粗体描述**",
  severity: "medium",
  status: "new",
  duplicate_of_id: null,
  browser_ua: null,
  viewport: "1440x900",
  recent_api_calls: null,
  recent_console_errors: null,
  screenshot_url: null,
  attachments: [
    {
      storageKey: "bug-report-attachments/u1/a.png",
      fileName: "a.png",
      mimeType: "image/png",
      size: 2048,
    },
  ],
  resolution: null,
  fixed_in_version: null,
  assigned_to_id: null,
  created_at: "2026-05-10T00:00:00Z",
  triaged_at: null,
  fixed_at: null,
  reopen_count: 0,
  last_reopened_at: null,
};

describe("BugsPage", () => {
  beforeEach(() => {
    useAuthStore.getState().setAuth("bugs-test-token", {
      id: "bugs-owner",
      name: "Bugs owner",
      email: "bugs@test.local",
      role: "super_admin",
      group_name: null,
      status: "active",
      created_at: "2026-01-01T00:00:00Z",
    });
    mocks.pushToast.mockReset();
    mocks.list.mockResolvedValue({ items: [item], total: 1 });
    mocks.update.mockReset().mockResolvedValue({});
    mocks.addComment.mockReset().mockResolvedValue({});
    mocks.get.mockResolvedValue({
      ...item,
      comments: [
        {
          id: "c1",
          bug_report_id: "bug-1",
          author_id: "u2",
          author_name: "Admin",
          author_role: "super_admin",
          body: "- 已复现",
          created_at: "2026-05-10T01:00:00Z",
        },
      ],
    });
    mocks.useBugReports.mockReturnValue({
      data: { items: [item], total: 1 },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });
  });

  it("renders markdown descriptions, markdown comments, and attachment links", async () => {
    render(
      <MemoryRouter>
        <BugsPage />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByText("Markdown detail"));

    await waitFor(() => expect(mocks.get).toHaveBeenCalledWith("bug-1"));
    const editor = await screen.findByPlaceholderText("添加评论，支持 Markdown...");
    expect(editor).toHaveAttribute("data-variant", "compact");
    expect(editor).toHaveAttribute("data-document-id", "bug-comment-bug-1");
    expect(editor).toHaveAttribute("data-label", "反馈评论");
    expect(editor).toHaveAttribute("data-has-upload", "false");
    expect(screen.getByText("粗体描述").tagName).toBe("STRONG");
    expect(screen.getByText("已复现")).toBeInTheDocument();
    expect(screen.getByText("a.png").closest("a")).toHaveAttribute(
      "href",
      "/download?key=bug-report-attachments%2Fu1%2Fa.png",
    );
  });

  it("keeps comment drafts on the Unicode length boundary and supports Ctrl+Enter", async () => {
    render(
      <MemoryRouter>
        <BugsPage />
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByText("Markdown detail"));

    const editor = await screen.findByPlaceholderText("添加评论，支持 Markdown...");
    fireEvent.change(editor, { target: { value: "😀".repeat(10_001) } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    expect(mocks.addComment).not.toHaveBeenCalled();
    expect(mocks.pushToast).toHaveBeenCalledWith({
      msg: "评论不能超过 10000 个字符",
      kind: "error",
    });
    expect(editor).toHaveValue("😀".repeat(10_001));

    fireEvent.change(editor, { target: { value: "补充信息" } });
    fireEvent.keyDown(editor, { key: "Enter", ctrlKey: true });
    await waitFor(() => expect(mocks.addComment).toHaveBeenCalledWith("bug-1", "补充信息"));
  });

  it("does not clear the next report draft when a previous comment finishes late", async () => {
    const secondItem = { ...item, id: "bug-2", display_id: "B-2", title: "Second bug" };
    const firstDetail = { ...item, comments: [] };
    const secondDetail = { ...secondItem, comments: [] };
    let resolveComment: (value: unknown) => void = () => undefined;
    mocks.list.mockResolvedValue({ items: [item, secondItem], total: 2 });
    mocks.get.mockImplementation((id: string) =>
      Promise.resolve(id === "bug-1" ? firstDetail : secondDetail),
    );
    mocks.addComment.mockReturnValue(
      new Promise((resolve) => {
        resolveComment = resolve;
      }),
    );
    mocks.useBugReports.mockReturnValue({
      data: { items: [item, secondItem], total: 2 },
      isLoading: false,
      error: null,
      refetch: vi.fn(),
    });

    render(
      <MemoryRouter>
        <BugsPage />
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByText("Markdown detail"));
    const firstEditor = await screen.findByPlaceholderText("添加评论，支持 Markdown...");
    fireEvent.change(firstEditor, { target: { value: "A 的待发送评论" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(mocks.addComment).toHaveBeenCalledWith("bug-1", "A 的待发送评论"));

    fireEvent.click(screen.getByText("Second bug"));
    const secondEditor = await screen.findByPlaceholderText("添加评论，支持 Markdown...");
    fireEvent.change(secondEditor, { target: { value: "B 的新草稿" } });
    resolveComment({});

    await waitFor(() => expect(secondEditor).toHaveValue("B 的新草稿"));
    expect(mocks.get).toHaveBeenCalledWith("bug-2");
  });

  it("drops a detail result when the auth owner changes while it is pending", async () => {
    let resolveDetail: (value: unknown) => void = () => undefined;
    mocks.get.mockReturnValue(
      new Promise((resolve) => {
        resolveDetail = resolve;
      }),
    );

    render(
      <MemoryRouter>
        <BugsPage />
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByText("Markdown detail"));
    await waitFor(() => expect(mocks.get).toHaveBeenCalledWith("bug-1"));

    await act(async () => {
      useAuthStore.getState().setAuth("replacement-token", {
        id: "replacement-owner",
        name: "Replacement",
        email: "replacement@test.local",
        role: "super_admin",
        group_name: null,
        status: "active",
        created_at: "2026-01-01T00:00:00Z",
      });
      resolveDetail({ ...item, comments: [] });
      await Promise.resolve();
    });

    await waitFor(() => expect(screen.queryByText("B-1: Markdown detail")).not.toBeInTheDocument());
    expect(mocks.pushToast).not.toHaveBeenCalledWith(
      expect.objectContaining({ msg: "加载详情失败" }),
    );
  });

  it("does not publish a stale status mutation after the auth owner changes", async () => {
    let resolveUpdate: (value: unknown) => void = () => undefined;
    mocks.update.mockReturnValue(
      new Promise((resolve) => {
        resolveUpdate = resolve;
      }),
    );

    render(
      <MemoryRouter>
        <BugsPage />
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByText("Markdown detail"));
    await waitFor(() => expect(screen.getByRole("button", { name: "处理中" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "处理中" }));
    await waitFor(() =>
      expect(mocks.update).toHaveBeenCalledWith("bug-1", { status: "in_progress" }),
    );

    await act(async () => {
      useAuthStore.getState().setAuth("replacement-token", {
        id: "replacement-owner",
        name: "Replacement",
        email: "replacement@test.local",
        role: "super_admin",
        group_name: null,
        status: "active",
        created_at: "2026-01-01T00:00:00Z",
      });
      resolveUpdate({});
      await Promise.resolve();
    });
    expect(mocks.pushToast).not.toHaveBeenCalledWith(
      expect.objectContaining({ msg: "状态已更新" }),
    );
  });
});
