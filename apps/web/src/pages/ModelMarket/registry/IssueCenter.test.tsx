import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { IssueCenter } from "./IssueCenter";
import type { RegistryScope } from "./registryTypes";
import { REGISTRY_URL_DEFAULTS, type RegistryUrlState } from "../marketUrlState";

const noopPatch = vi.fn();

function urlState(overrides: Partial<RegistryUrlState> = {}): RegistryUrlState {
  return { ...REGISTRY_URL_DEFAULTS, ...overrides };
}

describe("IssueCenter filter summaries", () => {
  it("完成关闭筛选面板并保留已选条件", async () => {
    const user = (await import("@testing-library/user-event")).default.setup();
    const patchUrl = vi.fn();
    render(
      <IssueCenter
        scope={{ diagnostics: [] } as unknown as RegistryScope}
        url={urlState({ issueSeverity: "critical", issueCode: "pool_offline" })}
        patchUrl={patchUrl}
        urlIssues={[]}
      />,
    );
    await user.click(screen.getByRole("button", { name: "筛选" }));
    expect(screen.getByText("筛选诊断")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "完成" }));
    expect(patchUrl).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByText("筛选诊断")).not.toBeInTheDocument());
    expect(screen.getByLabelText("按严重度筛选")).toHaveValue("critical");
    expect(screen.getByLabelText("已应用的诊断筛选")).toHaveTextContent("pool_offline");
  });
  it("does not reserve a summary row when no conditions are applied", () => {
    // The empty branch reads only diagnostics; no topology rows are rendered.
    const scope = { diagnostics: [] } as unknown as RegistryScope;
    render(<IssueCenter scope={scope} url={urlState()} patchUrl={noopPatch} urlIssues={[]} />);
    expect(screen.getByText("当前没有诊断告警")).toBeInTheDocument();
    expect(screen.queryByLabelText("已应用的诊断筛选")).not.toBeInTheDocument();
  });

  it("clear-conditions entry keeps the code text search (plan §5)", async () => {
    const user = (await import("@testing-library/user-event")).default.setup();
    const scope = { diagnostics: [] } as unknown as RegistryScope;
    render(
      <IssueCenter
        scope={scope}
        url={urlState({ issueQ: "circuit_open", issueSeverity: "warning" })}
        patchUrl={noopPatch}
        urlIssues={[]}
      />,
    );
    await user.click(screen.getByRole("button", { name: /清除条件（保留搜索）/ }));
    expect(noopPatch).toHaveBeenCalledWith({
      issueSeverity: "all",
      issuePool: "",
      issueInstance: "",
      issueGpu: "",
      issueCode: "",
    });
    // 文本搜索键不在清除范围内。
    expect(screen.getByLabelText("按 code 搜索")).toHaveValue("circuit_open");
  });
});
