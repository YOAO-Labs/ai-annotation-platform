import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { AgentSourceStatus } from "./AgentSourceStatus";
import type { AgentSourceStatus as Status } from "@/api/generated/types.gen";

afterEach(cleanup);
const offline: Status = {
  state: "offline",
  reason_code: "agent_offline",
  site_name: "上海站点",
  agent_name: "图片 Agent",
  last_seen: null,
  measured_at: "2026-10-26T01:00:00Z",
};

it("explains offline availability without conflating retained content", () => {
  render(<AgentSourceStatus status={offline} />);
  expect(screen.getByText("上海站点 / 图片 Agent · 离线")).toBeVisible();
  expect(screen.getByText(/连接恢复后可重试/)).toBeVisible();
  expect(screen.getByText("最后心跳：尚无记录")).toBeVisible();
  expect(screen.getByText(/已有预览和完整缓存记录独立保留/)).toBeVisible();
});

it.each([
  ["revoked", "agent_revoked", "重新授权"],
  ["expired", "credential_expired", "更新凭据"],
  ["degraded", "agent_draining", "恢复服务"],
] as const)("shows actionable %s status", (state, reason_code, action) => {
  render(<AgentSourceStatus status={{ ...offline, state, reason_code }} />);
  expect(screen.getByText(new RegExp(action))).toBeVisible();
});

it("keeps row status compact and omits unprovided metadata", () => {
  const { rerender } = render(<AgentSourceStatus status={offline} compact />);
  expect(screen.getByText("Agent · 离线")).toBeVisible();
  expect(screen.queryByText(/上海站点/)).not.toBeInTheDocument();
  expect(screen.queryByText(/最后心跳/)).not.toBeInTheDocument();
  rerender(<AgentSourceStatus />);
  expect(screen.queryByText(/上海站点/)).not.toBeInTheDocument();
});
