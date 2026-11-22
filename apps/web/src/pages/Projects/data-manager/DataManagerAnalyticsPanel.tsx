import type {
  DataManagerEntityFacets,
  DataManagerEntityScope,
  DataManagerFilterField,
  DataManagerSummary,
} from "@/api/taskViews";
import { DataManagerCharts } from "./DataManagerCharts";
import { DataManagerAnalyticsContent } from "./DataManagerOverview";

export function DataManagerAnalyticsPanel({
  scope,
  summary,
  facets,
  fields,
  isLoading,
  onSelect,
}: {
  scope: DataManagerEntityScope;
  summary?: DataManagerSummary;
  facets?: DataManagerEntityFacets;
  fields?: DataManagerFilterField[];
  isLoading: boolean;
  onSelect?: (field: string, value: string) => void;
}) {
  return (
    <section
      aria-label="当前视图统计"
      className="max-h-[42vh] shrink-0 overflow-auto rounded-md border border-border bg-card p-3"
    >
      {scope === "tasks" ? (
        <DataManagerAnalyticsContent
          summary={summary}
          isLoading={isLoading}
          fields={fields}
          onSelect={onSelect}
        />
      ) : (
        <DataManagerCharts
          scope={scope}
          facets={facets}
          isLoading={isLoading}
          onSelect={onSelect}
        />
      )}
    </section>
  );
}
