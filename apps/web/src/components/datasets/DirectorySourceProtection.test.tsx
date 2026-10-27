import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { DatasetSourceOut } from "@/api/generated/types.gen";
import { ApiError } from "@/api/client";
import { DirectorySourceProtection, ExternalProtectionSummary } from "./DirectorySourceProtection";

const api = vi.hoisted(() => ({ updateProtection: vi.fn(), source: vi.fn() }));
vi.mock("@/api/datasetSources", () => ({ datasetSourcesApi: api }));
const source: DatasetSourceOut = {
  id: "source",
  dataset_id: "dataset",
  location_id: "location",
  name: "车辆目录",
  config_revision: 2,
  enabled: true,
  mode: "inventory",
  relative_path: "traffic",
  node_id: "worker",
  recursive: true,
  include_globs: ["*"],
  exclude_globs: [],
  ownership_mode: "reference",
  cache_policy: "none",
  effective_cache_policy: "none",
  location_cache_policy: "preview",
  policy_revision: 1,
  unregistered_at: null,
};
const record = {
  revision: 3,
  responsible_party: "数据组",
  recovery_method: "unrecoverable" as const,
  recovery_reference: null,
  recorded_at: "2026-10-25T00:00:00Z",
  recorded_by_id: "actor",
};
beforeEach(() => {
  vi.clearAllMocks();
  api.updateProtection.mockResolvedValue({});
});
function fill() {
  fireEvent.change(screen.getByRole("textbox", { name: "责任方" }), {
    target: { value: " 运维团队 " },
  });
  fireEvent.change(screen.getByRole("combobox", { name: "恢复方式" }), {
    target: { value: "external_backup" },
  });
}
it("keeps unrecorded distinct and uses a centered form with hints below the select", async () => {
  render(<DirectorySourceProtection source={source} onSaved={async () => {}} />);
  expect(screen.getByText("尚未登记责任方和恢复方式。")).toBeInTheDocument();
  const trigger = screen.getByRole("button", { name: "登记保护责任" });
  trigger.focus();
  fireEvent.click(trigger);
  expect(screen.getByRole("dialog", { name: "来源原件保护责任" })).toBeInTheDocument();
  const select = screen.getByRole("combobox", { name: "恢复方式" });
  expect(select).toHaveValue("");
  expect(select).toHaveAccessibleDescription(
    "选择责任方当前声明的恢复方式；这不是平台的备份验证结果。",
  );
  expect(screen.getByRole("option", { name: "外部备份恢复" })).toBeInTheDocument();
  fireEvent.keyDown(select, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  await waitFor(() => expect(trigger).toHaveFocus());
  expect(api.updateProtection).not.toHaveBeenCalled();
});
it("saves a declaration without changing the source content revision", async () => {
  const saved = vi.fn().mockResolvedValue(undefined);
  render(<DirectorySourceProtection source={source} onSaved={saved} />);
  fireEvent.click(screen.getByRole("button", { name: "登记保护责任" }));
  fill();
  fireEvent.click(screen.getByRole("button", { name: "保存责任声明" }));
  await waitFor(() => expect(saved).toHaveBeenCalledOnce());
  expect(api.updateProtection).toHaveBeenCalledWith("source", {
    expected_revision: 0,
    responsible_party: "运维团队",
    recovery_method: "external_backup",
    recovery_reference: null,
  });
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
});
it("cancels a filled form without submitting", () => {
  render(<DirectorySourceProtection source={source} onSaved={async () => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "登记保护责任" }));
  fill();
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(api.updateProtection).not.toHaveBeenCalled();
});
it("shows retained responsibility on an unregistered source without claiming a verified backup", () => {
  render(
    <DirectorySourceProtection
      source={{
        ...source,
        enabled: false,
        unregistered_at: "2026-10-25T00:00:00Z",
        external_protection: record,
      }}
      onSaved={async () => {}}
    />,
  );
  expect(screen.getByText(/责任方：数据组/)).toHaveTextContent("无恢复方案（已声明）");
  expect(screen.getByRole("button", { name: "编辑保护责任" })).toBeEnabled();
  expect(screen.queryByText("备份验证成功")).not.toBeInTheDocument();
});
it("preserves edits on conflict and explicitly reloads the latest declaration", async () => {
  api.updateProtection
    .mockRejectedValueOnce(new ApiError(409, "conflict"))
    .mockResolvedValueOnce({});
  api.source.mockResolvedValue({ ...source, external_protection: { ...record, revision: 4 } });
  render(<DirectorySourceProtection source={source} onSaved={async () => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "登记保护责任" }));
  fill();
  fireEvent.click(screen.getByRole("button", { name: "保存责任声明" }));
  await screen.findByRole("alert");
  expect(screen.getByRole("textbox", { name: "责任方" })).toHaveValue(" 运维团队 ");
  expect(screen.getByRole("button", { name: "保存责任声明" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "载入最新记录" }));
  await waitFor(() =>
    expect(screen.getByRole("textbox", { name: "责任方" })).toHaveValue("数据组"),
  );
  expect(api.updateProtection).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "保存责任声明" }));
  await waitFor(() => expect(api.updateProtection).toHaveBeenCalledTimes(2));
  expect(api.updateProtection.mock.calls[1][1]).toMatchObject({
    expected_revision: 4,
    responsible_party: "数据组",
    recovery_method: "unrecoverable",
  });
});
it("does not publish a late error into another source", async () => {
  let reject!: (error: Error) => void;
  api.updateProtection.mockReturnValue(
    new Promise((_resolve, fail) => {
      reject = fail;
    }),
  );
  const view = render(
    <DirectorySourceProtection key="a" source={source} onSaved={async () => {}} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "登记保护责任" }));
  fill();
  fireEvent.click(screen.getByRole("button", { name: "保存责任声明" }));
  view.rerender(
    <DirectorySourceProtection
      key="b"
      source={{ ...source, id: "other" }}
      onSaved={async () => {}}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "登记保护责任" }));
  await act(async () => {
    reject(new ApiError(409, "old conflict"));
  });
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "责任方" })).toHaveValue("");
});
it("keeps unknown recovery separate from no recovery in read-only history", () => {
  render(<ExternalProtectionSummary value={{ ...record, recovery_method: "unknown" }} />);
  expect(screen.getByText("尚未确认")).toBeInTheDocument();
  expect(screen.queryByText("无恢复方案")).not.toBeInTheDocument();
});
