import type { UrlStateCodec, UrlStateIssue } from "@/hooks/useUrlFilterState";

export type AuditScope = "business" | "all";

export interface AuditUrlState {
  action: string;
  targetType: string;
  targetId: string;
  actorId: string;
  detailKey: string;
  detailValue: string;
  scope: AuditScope;
  page: number;
}

export const AUDIT_URL_DEFAULTS: AuditUrlState = {
  action: "",
  targetType: "",
  targetId: "",
  actorId: "",
  detailKey: "",
  detailValue: "",
  scope: "business",
  page: 1,
};

export const AUDIT_URL_KEYS = [
  "action",
  "target_type",
  "target_id",
  "actor_id",
  "detail_key",
  "detail_value",
  "scope",
  "page",
] as const;

function parsePage(params: URLSearchParams, issues: UrlStateIssue[]) {
  const raw = params.get("page");
  if (raw === null || raw === "") return 1;
  const value = Number(raw);
  if (Number.isInteger(value) && value >= 1) return value;
  issues.push({ key: "page", message: "页码无效，已使用第 1 页" });
  return 1;
}

export function parseAuditUrl(search: string | URLSearchParams) {
  const params = new URLSearchParams(search);
  const issues: UrlStateIssue[] = [];
  const rawScope = params.get("scope");
  const scope: AuditScope = rawScope === "all" || rawScope === "business" ? rawScope : "business";
  if (rawScope && rawScope !== "all" && rawScope !== "business") {
    issues.push({ key: "scope", message: "未知的审计范围" });
  }
  return {
    state: {
      action: params.get("action")?.trim() ?? "",
      targetType: params.get("target_type")?.trim() ?? "",
      targetId: params.get("target_id")?.trim() ?? "",
      actorId: params.get("actor_id")?.trim() ?? "",
      detailKey: params.get("detail_key")?.trim() ?? "",
      detailValue: params.get("detail_value") ?? "",
      scope,
      page: parsePage(params, issues),
    },
    issues,
  };
}

export const auditUrlCodec: UrlStateCodec<AuditUrlState> = {
  parse: parseAuditUrl,
  encode: (current, state) => {
    const params = new URLSearchParams(current);
    const textKeys: Array<
      ["action" | "targetType" | "targetId" | "actorId" | "detailKey", string]
    > = [
      ["action", "action"],
      ["targetType", "target_type"],
      ["targetId", "target_id"],
      ["actorId", "actor_id"],
      ["detailKey", "detail_key"],
    ];
    for (const [stateKey, urlKey] of textKeys) {
      const value = state[stateKey].trim();
      if (value) params.set(urlKey, value);
      else params.delete(urlKey);
    }
    if (state.detailKey.trim()) params.set("detail_value", state.detailValue);
    else params.delete("detail_value");
    if (state.scope === "business") params.delete("scope");
    else params.set("scope", state.scope);
    if (state.page === 1) params.delete("page");
    else params.set("page", String(state.page));
    return params;
  },
};
