import type { AgentSourceStatus as Status } from "@/api/generated/types.gen";

const reasons: Record<string, string> = {
  agent_offline: "Agent 已离线，原图暂不可用；连接恢复后可重试。",
  agent_revoked: "Agent 授权已撤销，请联系管理员重新授权后重试。",
  agent_unpaired: "Agent 尚未配对，请联系管理员完成连接后重试。",
  agent_unauthorized: "Agent 凭据不可用，请联系管理员更新授权后重试。",
  credential_expired: "Agent 凭据已过期，请联系管理员更新凭据后重试。",
  agent_draining: "Agent 已暂停接收新操作，恢复服务后可重试原图读取。",
  agent_root_revision_changed: "来源绑定的 Agent 目录已变化，请联系管理员核对后重试。",
  agent_unavailable: "Agent 暂不可用，请联系管理员检查连接后重试。",
};

const states: Record<Status["state"], string> = {
  online: "在线",
  offline: "离线",
  revoked: "授权已撤销",
  pending: "待配对",
  expired: "凭据已过期",
  degraded: "暂不可用",
  unavailable: "暂不可用",
};

export function agentAvailabilityReason(code: string): string | undefined {
  return reasons[code];
}

export function AgentSourceStatus({
  status,
  compact = false,
}: {
  status?: Status | null;
  compact?: boolean;
}) {
  if (!status) return null;
  return (
    <span className="inline-flex max-w-full flex-col gap-1 text-xs leading-relaxed text-muted-foreground">
      <span className={status.state === "online" ? undefined : "text-status-caution"}>
        {compact
          ? "Agent"
          : [status.site_name, status.agent_name].filter(Boolean).join(" / ") || "来源 Agent"}
        {" · "}
        {states[status.state]}
      </span>
      {!compact && (
        <>
          {status.state !== "online" && (
            <span>{reasons[status.reason_code] ?? reasons.agent_unavailable}</span>
          )}
          <span>
            最后心跳：
            {status.last_seen ? new Date(status.last_seen).toLocaleString("zh-CN") : "尚无记录"}
          </span>
          <span>连接状态不代表原图已核验；已有预览和完整缓存记录独立保留，读取时仍会复核。</span>
        </>
      )}
    </span>
  );
}
