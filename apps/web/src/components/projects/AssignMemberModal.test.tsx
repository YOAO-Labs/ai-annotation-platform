import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AssignMemberModal } from "./AssignMemberModal";
import type { ProjectMemberResponse } from "@/api/projects";

const mockMutateAsync = vi.fn();
const mockUsersList = vi.fn();
const mockPushToast = vi.fn();

vi.mock("@/hooks/useProjects", () => ({
  useAddProjectMember: () => ({
    mutateAsync: mockMutateAsync,
    isPending: false,
  }),
}));

vi.mock("@/api/users", () => ({
  usersApi: {
    list: (...args: unknown[]) => mockUsersList(...args),
  },
}));

vi.mock("@/components/ui/Toast", async () => {
  const actual =
    await vi.importActual<typeof import("@/components/ui/Toast")>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(sel: (s: { push: typeof mockPushToast }) => T) =>
      sel({ push: mockPushToast }),
  };
});

// Assignment candidates are ordinary active employees; the membership role is
// chosen with the role tabs, never derived from the employee's platform role.
const USERS = [
  { id: "u1", name: "Alice", email: "alice@example.com", role: "employee", group_name: "一组" },
  { id: "u2", name: "Bob", email: "bob@example.com", role: "employee", group_name: "一组" },
  { id: "u3", name: "Existing", email: "existing@example.com", role: "employee" },
  { id: "u4", name: "Rita", email: "rita@example.com", role: "employee", group_name: "二组" },
];

const EXISTING = [
  {
    id: "m1",
    user_id: "u3",
    user_name: "Existing",
    user_email: "existing@example.com",
    role: "annotator",
    assigned_at: "2026-05-01T00:00:00Z",
  },
] as ProjectMemberResponse[];

function renderModal() {
  const onClose = vi.fn();
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={qc}>
      <AssignMemberModal open projectId="p1" existing={EXISTING} onClose={onClose} />
    </QueryClientProvider>,
  );
  // Re-render the same mounted tree with new props (open/projectId).
  const rerenderModal = (open: boolean, projectId = "p1") =>
    view.rerender(
      <QueryClientProvider client={qc}>
        <AssignMemberModal
          open={open}
          projectId={projectId}
          existing={EXISTING}
          onClose={onClose}
        />
      </QueryClientProvider>,
    );
  return { ...view, onClose, rerenderModal };
}

function userButton(name: string) {
  return screen.getByRole("button", { name: new RegExp(name) });
}

function summary() {
  return screen.getByTestId("assign-member-summary");
}

function confirmButton() {
  return screen.getByRole("button", { name: /确认指派|指派中/ });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("AssignMemberModal", () => {
  beforeEach(() => {
    mockMutateAsync.mockReset();
    mockMutateAsync.mockResolvedValue({});
    mockUsersList.mockReset();
    mockUsersList.mockImplementation(() => Promise.resolve(USERS));
    mockPushToast.mockReset();
  });

  it("queries employee candidates and excludes existing members", async () => {
    renderModal();

    expect(await screen.findByText("Alice")).toBeInTheDocument();
    expect(mockUsersList).toHaveBeenCalledWith({ role: "employee" });
    expect(screen.getByText("Bob")).toBeInTheDocument();
    expect(screen.getByText("Rita")).toBeInTheDocument();
    expect(screen.queryByText("Existing")).not.toBeInTheDocument();
  });

  it("submits exact mixed-role payloads from one confirmation", async () => {
    const { onClose } = renderModal();

    expect(await screen.findByText("Alice")).toBeInTheDocument();
    fireEvent.click(userButton("Alice"));
    fireEvent.click(userButton("Bob"));

    fireEvent.click(screen.getByRole("button", { name: "审核员" }));
    fireEvent.click(userButton("Rita"));

    expect(summary()).toHaveTextContent("标注员 2 名");
    expect(summary()).toHaveTextContent("审核员 1 名");
    expect(summary()).toHaveTextContent("共 3 人");

    fireEvent.click(screen.getByRole("button", { name: /确认指派 3 人/ }));

    await waitFor(() => expect(mockMutateAsync).toHaveBeenCalledTimes(3));
    expect(mockMutateAsync).toHaveBeenNthCalledWith(1, { user_id: "u1", role: "annotator" });
    expect(mockMutateAsync).toHaveBeenNthCalledWith(2, { user_id: "u2", role: "annotator" });
    expect(mockMutateAsync).toHaveBeenNthCalledWith(3, { user_id: "u4", role: "reviewer" });
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it("preserves mixed selections across tab round trips and search", async () => {
    renderModal();

    expect(await screen.findByText("Alice")).toBeInTheDocument();
    fireEvent.click(userButton("Alice"));
    fireEvent.click(userButton("Bob"));

    // Selections survive a switch to the other role tab.
    fireEvent.click(screen.getByRole("button", { name: "审核员" }));
    expect(summary()).toHaveTextContent("标注员 2 名");
    expect(summary()).toHaveTextContent("共 2 人");

    // Searching narrows the list without dropping stored selections.
    fireEvent.change(screen.getByPlaceholderText("按姓名、邮箱、分组搜索"), {
      target: { value: "rita" },
    });
    expect(screen.queryByText("Alice")).not.toBeInTheDocument();
    fireEvent.click(userButton("Rita"));

    fireEvent.change(screen.getByPlaceholderText("按姓名、邮箱、分组搜索"), {
      target: { value: "" },
    });
    fireEvent.click(screen.getByRole("button", { name: "标注员" }));

    expect(userButton("Alice")).toHaveAttribute("aria-pressed", "true");
    expect(userButton("Bob")).toHaveAttribute("aria-pressed", "true");
    expect(summary()).toHaveTextContent("标注员 2 名");
    expect(summary()).toHaveTextContent("审核员 1 名");
    expect(summary()).toHaveTextContent("共 3 人");
  });

  it("disables a candidate already selected for the opposite role with a hint", async () => {
    renderModal();

    expect(await screen.findByText("Alice")).toBeInTheDocument();
    fireEvent.click(userButton("Alice"));

    fireEvent.click(screen.getByRole("button", { name: "审核员" }));

    const alice = userButton("Alice");
    expect(alice).toBeDisabled();
    expect(within(alice).getByText("已选为标注员")).toBeInTheDocument();

    // Clicking the disabled row must not move or duplicate the selection.
    fireEvent.click(alice);
    expect(summary()).toHaveTextContent("标注员 1 名");
    expect(summary()).toHaveTextContent("审核员 0 名");

    // Unselecting on the owning tab re-enables the candidate.
    fireEvent.click(screen.getByRole("button", { name: "标注员" }));
    fireEvent.click(userButton("Alice"));
    fireEvent.click(screen.getByRole("button", { name: "审核员" }));
    expect(userButton("Alice")).not.toBeDisabled();
  });

  it("keeps the single-role reviewer path unchanged", async () => {
    const { onClose } = renderModal();

    fireEvent.click(screen.getByRole("button", { name: "审核员" }));
    expect(await screen.findByText("Rita")).toBeInTheDocument();
    fireEvent.click(userButton("Rita"));

    expect(summary()).toHaveTextContent("标注员 0 名");
    expect(summary()).toHaveTextContent("审核员 1 名");

    fireEvent.click(screen.getByRole("button", { name: /确认指派 1 人/ }));

    await waitFor(() => expect(mockMutateAsync).toHaveBeenCalledTimes(1));
    expect(mockMutateAsync).toHaveBeenCalledWith({ user_id: "u4", role: "reviewer" });
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it("on partial failure keeps only the failed selection for retry", async () => {
    mockMutateAsync
      .mockImplementationOnce(() => Promise.resolve({}))
      .mockImplementationOnce(() => Promise.reject(new Error("boom")));
    const { onClose } = renderModal();

    expect(await screen.findByText("Alice")).toBeInTheDocument();
    fireEvent.click(userButton("Alice"));
    fireEvent.click(screen.getByRole("button", { name: "审核员" }));
    fireEvent.click(userButton("Rita"));

    fireEvent.click(screen.getByRole("button", { name: /确认指派 2 人/ }));

    await waitFor(() => expect(mockPushToast).toHaveBeenCalled());
    expect(onClose).not.toHaveBeenCalled();
    expect(mockPushToast).toHaveBeenCalledWith(expect.objectContaining({ kind: "error" }));

    // Successful annotator selection is gone; the failed reviewer selection
    // stays on its original role for retry.
    expect(summary()).toHaveTextContent("标注员 0 名");
    expect(summary()).toHaveTextContent("审核员 1 名");
    expect(screen.getByRole("button", { name: "审核员" })).toHaveAttribute("aria-pressed", "true");

    mockMutateAsync.mockResolvedValue({});
    fireEvent.click(screen.getByRole("button", { name: /确认指派 1 人/ }));

    await waitFor(() => expect(mockMutateAsync).toHaveBeenCalledTimes(3));
    expect(mockMutateAsync).toHaveBeenNthCalledWith(3, { user_id: "u4", role: "reviewer" });
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it("locks the whole submit until every request settles and blocks duplicate submits", async () => {
    const first = deferred<unknown>();
    const second = deferred<unknown>();
    mockMutateAsync
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);
    const { onClose } = renderModal();

    expect(await screen.findByText("Alice")).toBeInTheDocument();
    fireEvent.click(userButton("Alice"));
    fireEvent.click(screen.getByRole("button", { name: "审核员" }));
    fireEvent.click(userButton("Rita"));

    fireEvent.click(screen.getByRole("button", { name: /确认指派 2 人/ }));
    await waitFor(() => expect(mockMutateAsync).toHaveBeenCalledTimes(2));

    // While sending: no duplicate submit, no tab change.
    const pending = screen.getByRole("button", { name: "指派中..." });
    expect(pending).toBeDisabled();
    fireEvent.click(pending);
    fireEvent.click(screen.getByRole("button", { name: "标注员" }));
    expect(screen.getByRole("button", { name: "标注员" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "审核员" })).toHaveAttribute("aria-pressed", "true");
    expect(mockMutateAsync).toHaveBeenCalledTimes(2);

    // A single settle must not unlock the submit while a request is in flight.
    await act(async () => {
      second.resolve({});
      await second.promise;
    });
    expect(screen.getByRole("button", { name: "指派中..." })).toBeDisabled();
    expect(onClose).not.toHaveBeenCalled();

    await act(async () => {
      first.resolve({});
      await first.promise;
    });
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(mockMutateAsync).toHaveBeenCalledTimes(2);
  });

  it("resets selections when the modal is closed and reopened", async () => {
    const { rerenderModal } = renderModal();

    expect(await screen.findByText("Alice")).toBeInTheDocument();
    fireEvent.click(userButton("Alice"));
    expect(summary()).toHaveTextContent("共 1 人");

    rerenderModal(false);
    rerenderModal(true);

    expect(await screen.findByText("Alice")).toBeInTheDocument();
    expect(summary()).toHaveTextContent("共 0 人");
    expect(userButton("Alice")).toHaveAttribute("aria-pressed", "false");
  });

  it("ignores a late completion from a previous context", async () => {
    const pending = deferred<unknown>();
    mockMutateAsync.mockImplementationOnce(() => pending.promise);
    const { onClose, rerenderModal } = renderModal();

    expect(await screen.findByText("Alice")).toBeInTheDocument();
    fireEvent.click(userButton("Alice"));
    fireEvent.click(screen.getByRole("button", { name: /确认指派 1 人/ }));
    await waitFor(() => expect(mockMutateAsync).toHaveBeenCalledTimes(1));

    rerenderModal(false);
    rerenderModal(true);

    expect(await screen.findByText("Alice")).toBeInTheDocument();
    fireEvent.click(userButton("Bob"));
    expect(summary()).toHaveTextContent("共 1 人");

    await act(async () => {
      pending.resolve({});
      await pending.promise;
    });

    // The stale success must not close the modal or clear the new selection.
    expect(onClose).not.toHaveBeenCalled();
    expect(userButton("Bob")).toHaveAttribute("aria-pressed", "true");
    expect(summary()).toHaveTextContent("共 1 人");
  });

  it("resets state when the project changes while a request is pending", async () => {
    const pending = deferred<unknown>();
    mockMutateAsync.mockImplementationOnce(() => pending.promise);
    const { onClose, rerenderModal } = renderModal();

    expect(await screen.findByText("Alice")).toBeInTheDocument();
    fireEvent.click(userButton("Alice"));
    fireEvent.click(screen.getByRole("button", { name: /确认指派 1 人/ }));
    await waitFor(() => expect(mockMutateAsync).toHaveBeenCalledTimes(1));

    // Switch project while the previous project's request is still in flight.
    rerenderModal(true, "p2");

    expect(await screen.findByText("Alice")).toBeInTheDocument();
    await waitFor(() => expect(summary()).toHaveTextContent("共 0 人"));
    fireEvent.click(userButton("Bob"));
    expect(summary()).toHaveTextContent("共 1 人");

    await act(async () => {
      pending.resolve({});
      await pending.promise;
    });

    // The stale success must not close the modal, toast, or clear p2's selection.
    expect(onClose).not.toHaveBeenCalled();
    expect(mockPushToast).not.toHaveBeenCalled();
    expect(userButton("Bob")).toHaveAttribute("aria-pressed", "true");
    expect(summary()).toHaveTextContent("共 1 人");
  });
});
