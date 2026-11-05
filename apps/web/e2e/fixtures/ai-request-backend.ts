import type { APIRequestContext } from "@playwright/test";
import type { SeedAPI, SeedData } from "./seed";

type JsonObject = Record<string, unknown>;

export interface AiRequestRecord {
  tasks: Array<{ id: string }>;
  context: JsonObject;
  receivedAt: number;
  responseStatus?: number;
}

/** Controls the same managed provider that receives actual inference requests. */
export function startAiRequestBackend(seed: SeedAPI, data: SeedData) {
  const control = (action: Parameters<SeedAPI["providerControl"]>[2], value?: boolean | number) =>
    seed.providerControl(data.project_id, data.ml_backend_id, action, value);
  let attached = false;
  return {
    async requests(): Promise<AiRequestRecord[]> {
      return (await control("requests")).requests;
    },
    async hold() {
      await control("hold");
    },
    async release() {
      await control("release");
    },
    async failNext(count = 1) {
      await control("fail_next", count);
    },
    async failAll(enabled: boolean) {
      await control("fail_all", enabled);
    },
    async attach(
      request: APIRequestContext,
      config: { apiBase: string; projectId: string; token: string; defaultBackendId: string },
    ) {
      if (
        attached ||
        config.projectId !== data.project_id ||
        config.defaultBackendId !== data.ml_backend_id
      ) {
        throw new Error("Fixture provider does not match the owned project");
      }
      await control("attach");
      attached = true;
      const endpoint = `${config.apiBase.replace(/\/$/, "")}/api/v1/projects/${config.projectId}/ml-backends/${config.defaultBackendId}/health`;
      const refresh = async () => {
        const response = await request.post(endpoint, {
          headers: { Authorization: `Bearer ${config.token}` },
        });
        if (!response.ok() || (await response.json()).status !== "ok") {
          throw new Error(`Fixture provider health observation failed: ${response.status()}`);
        }
      };
      const detach = async () => {
        if (!attached) return;
        await control("detach");
        attached = false;
        await refresh();
      };
      try {
        await refresh();
        return detach;
      } catch (error) {
        try {
          await detach();
        } catch (cleanupError) {
          throw new AggregateError([error, cleanupError], "Fixture attach and restoration failed");
        }
        throw error;
      }
    },
  };
}
