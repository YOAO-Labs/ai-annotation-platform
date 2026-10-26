import { ApiError, apiClient } from "./client";
import type {
  AgentCreate,
  AgentLimits,
  AgentList,
  AgentLocationCreate,
  AgentOut,
  AgentPairingOut,
  SiteCreate,
  SiteList,
  SiteOut,
  StorageLocationList,
  StorageLocationOut,
} from "./generated/types.gen";

export type { AgentLimits };

export const siteAgentsApi = {
  sites: (offset: number, signal?: AbortSignal) =>
    apiClient.get<SiteList>(`/site-agents/sites?limit=200&offset=${offset}`, { signal }),
  createSite: (payload: SiteCreate) => apiClient.post<SiteOut>("/site-agents/sites", payload),
  agents: (offset: number, limit: number, siteId: string, signal?: AbortSignal) =>
    apiClient.get<AgentList>(
      `/site-agents/agents?${new URLSearchParams({ limit: String(limit), offset: String(offset), ...(siteId ? { site_id: siteId } : {}) })}`,
      { signal },
    ),
  agent: async (id: string, signal?: AbortSignal) => {
    const result = await apiClient.get<AgentList>(
      `/site-agents/agents?${new URLSearchParams({ agent_id: id, limit: "1" })}`,
      { signal },
    );
    const agent = result.items.find((item) => item.id === id);
    if (!agent) throw new ApiError(404, "Agent 不存在或无权查看");
    return agent;
  },
  createAgent: (siteId: string, payload: AgentCreate) =>
    apiClient.post<AgentPairingOut>(`/site-agents/sites/${siteId}/agents`, payload),
  drain: (id: string, revision: number, draining: boolean) =>
    apiClient.post<AgentOut>(`/site-agents/agents/${id}/drain`, {
      expected_revision: revision,
      draining,
    }),
  rotate: (id: string, revision: number) =>
    apiClient.post<AgentPairingOut>(`/site-agents/agents/${id}/rotate`, {
      expected_revision: revision,
    }),
  revoke: (id: string, revision: number) =>
    apiClient.post<AgentOut>(`/site-agents/agents/${id}/revoke`, {
      expected_revision: revision,
    }),
  locations: (agentId: string, offset: number, limit: number, signal?: AbortSignal) =>
    apiClient.get<StorageLocationList>(
      `/storage-locations?${new URLSearchParams({ agent_id: agentId, offset: String(offset), limit: String(limit) })}`,
      { signal },
    ),
  registerLocation: (payload: AgentLocationCreate) =>
    apiClient.post<StorageLocationOut>("/storage-locations/agent", payload),
};
