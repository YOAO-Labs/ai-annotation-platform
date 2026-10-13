import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import type { StorageConnection } from "@/api/storageConnections";
import { DecisionDialogHost } from "@/components/ui/DecisionDialogHost";

const mocks = vi.hoisted(() => ({
  role: "super_admin",
  connections: [] as StorageConnection[],
  preset: { data: { enabled: false, host: null as string | null, port: 22 } },
  pushToast: vi.fn(),
  create: {} as any,
  update: {} as any,
  remove: {} as any,
  test: {} as any,
}));

vi.mock("@/hooks/usePermissions", () => ({
  usePermissions: () => ({ role: mocks.role }),
}));

vi.mock("@/components/ui/Toast", () => ({
  useToastStore: (selector: (state: any) => unknown) => selector({ push: mocks.pushToast }),
}));

vi.mock("@/hooks/useStorageConnections", () => ({
  useStorageConnections: () => ({ data: mocks.connections, isLoading: false }),
  useDeploymentSftpPreset: () => mocks.preset,
  useCreateStorageConnection: () => mocks.create,
  useUpdateStorageConnection: () => mocks.update,
  useDeleteStorageConnection: () => mocks.remove,
  useTestStorageConnection: () => mocks.test,
}));

import { StorageConnectionsPanel } from "./StorageConnectionsPanel";

const existingConnection: StorageConnection = {
  id: "conn-1",
  name: "ext-oss",
  kind: "s3",
  config: {
    endpoint: "http://minio.example:9000",
    bucket: "datasets",
    use_ssl: false,
  },
  scope: "owner",
  project_id: null,
  secret_set: true,
  created_by: "admin-1",
  created_at: "2026-08-14T00:00:00Z",
  updated_at: "2026-08-14T00:00:00Z",
};

describe("StorageConnectionsPanel", () => {
  beforeEach(() => {
    mocks.role = "super_admin";
    mocks.connections = [];
    mocks.preset = { data: { enabled: false, host: null, port: 22 } };
    mocks.pushToast.mockReset();
    mocks.create = { mutateAsync: vi.fn().mockResolvedValue({}), isPending: false };
    mocks.update = { mutateAsync: vi.fn().mockResolvedValue({}), isPending: false };
    mocks.remove = { mutate: vi.fn(), isPending: false };
    mocks.test = { mutate: vi.fn(), isPending: false };
  });

  it("新建 S3 默认启用 HTTPS，编辑旧连接器保留关闭状态", async () => {
    const { unmount } = render(<StorageConnectionsPanel />);
    fireEvent.click(screen.getByRole("button", { name: "新建数据源" }));
    expect(within(await screen.findByRole("dialog")).getByLabelText("HTTPS")).toBeChecked();
    unmount();

    mocks.connections = [existingConnection];
    render(<StorageConnectionsPanel />);
    fireEvent.click(screen.getByRole("button", { name: /编辑/ }));
    expect(within(await screen.findByRole("dialog")).getByLabelText("HTTPS")).not.toBeChecked();
  });

  it("仅超管看到部署主机快捷项，并只预填非敏感 SFTP 字段", async () => {
    mocks.preset = { data: { enabled: true, host: "10.0.3.5", port: 22 } };
    const { unmount } = render(<StorageConnectionsPanel hideHeaderAction />);

    fireEvent.click(screen.getByRole("button", { name: "添加部署主机" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByDisplayValue("部署主机")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("类型")).toHaveValue("sftp");
    expect(within(dialog).getByDisplayValue("10.0.3.5")).toBeInTheDocument();
    expect(within(dialog).getByDisplayValue("22")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("范围")).toHaveValue("owner");
    expect(within(dialog).getByLabelText("Auth")).toHaveValue("key");
    expect(within(dialog).getByLabelText(/Username/)).toHaveValue("");
    unmount();

    mocks.role = "project_admin";
    render(<StorageConnectionsPanel hideHeaderAction />);
    expect(screen.queryByRole("button", { name: "添加部署主机" })).not.toBeInTheDocument();
  });

  it("创建与更新 4xx 错误会显示 toast 且保留表单", async () => {
    mocks.create.mutateAsync.mockRejectedValueOnce(new Error("目标不在白名单内"));
    const { unmount } = render(<StorageConnectionsPanel />);
    fireEvent.click(screen.getByRole("button", { name: "新建数据源" }));
    let dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/名称/), { target: { value: "new-oss" } });
    fireEvent.change(within(dialog).getByLabelText(/Endpoint/), {
      target: { value: "minio.example:9000" },
    });
    fireEvent.change(within(dialog).getByLabelText(/Bucket/), {
      target: { value: "datasets" },
    });
    fireEvent.change(within(dialog).getByLabelText(/Access key/), { target: { value: "AK" } });
    fireEvent.change(within(dialog).getByLabelText(/Secret key/), { target: { value: "SK" } });
    fireEvent.submit(dialog.querySelector("form")!);

    await waitFor(() =>
      expect(mocks.pushToast).toHaveBeenCalledWith({
        msg: "连接器创建失败",
        sub: "目标不在白名单内",
        kind: "warning",
      }),
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    unmount();

    mocks.connections = [existingConnection];
    mocks.update.mutateAsync.mockRejectedValueOnce(new Error("配置无效"));
    render(<StorageConnectionsPanel />);
    fireEvent.click(screen.getByRole("button", { name: /编辑/ }));
    dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/名称/), {
      target: { value: "renamed-oss" },
    });
    fireEvent.submit(dialog.querySelector("form")!);

    await waitFor(() =>
      expect(mocks.pushToast).toHaveBeenCalledWith({
        msg: "连接器更新失败",
        sub: "配置无效",
        kind: "warning",
      }),
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("测试与删除请求失败会显示后端错误", async () => {
    mocks.connections = [existingConnection];
    mocks.test.mutate.mockImplementation((_id: string, options: any) => {
      options.onError(new Error("测试被白名单拒绝"));
      options.onSettled();
    });
    mocks.remove.mutate.mockImplementation((_id: string, options: any) => {
      options.onError(new Error("删除冲突"));
    });
    render(
      <>
        <StorageConnectionsPanel />
        <DecisionDialogHost />
      </>,
    );

    fireEvent.click(screen.getByRole("button", { name: "测试" }));
    expect(mocks.pushToast).toHaveBeenCalledWith({
      msg: "连接测试失败",
      sub: "测试被白名单拒绝",
      kind: "warning",
    });

    // 删除走决策对话框:确认前不触发,点确认后才发起删除
    fireEvent.click(screen.getByRole("button", { name: /删除/ }));
    const dialog = await screen.findByRole("alertdialog");
    expect(mocks.remove.mutate).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    await waitFor(() =>
      expect(mocks.pushToast).toHaveBeenCalledWith({
        msg: "连接器删除失败",
        sub: "删除冲突",
        kind: "warning",
      }),
    );
  });
});
