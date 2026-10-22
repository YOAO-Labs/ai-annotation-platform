import { Link, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { PageContainer } from "@/components/layout/PageContainer";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/shadcn/ui/tabs";
import { lineageReturn } from "@/api/modelLineage";
import { ProjectModelsPanel } from "../Projects/ProjectModelsPanel";
import { ModelLineagePanel } from "./ModelLineagePage";
import { trainingListPath } from "./TrainingListControls";

export function TrainingModelPage() {
  const { id = "", modelId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const view = params.get("model_view") ?? "";
  const activeView = view === "records" || view === "notes" ? view : "details";
  const back = params.has("returnTo")
    ? lineageReturn(id, params.get("returnTo"))
    : trainingListPath(id, "models", params);
  return (
    <PageContainer>
      <Link
        to={back}
        className="mb-4 inline-flex items-center gap-1.5 rounded-sm text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <ArrowLeft aria-hidden="true" className="size-3.5" />
        {back.split("?")[0] === `/projects/${id}/learning` ? "返回模型列表" : "返回来源页面"}
      </Link>
      <header className="mb-4">
        <h1 className="text-xl font-semibold">模型详情与操作</h1>
        <p className="mt-1 text-xs text-muted-foreground">
          查看评估结果、加载试用和发布，或核对模型来源、使用记录与人工备注。
        </p>
      </header>
      <Tabs
        value={activeView}
        onValueChange={(value) =>
          setParams((previous) => {
            const next = new URLSearchParams(previous);
            if (value === "details") next.delete("model_view");
            else next.set("model_view", value);
            return next;
          })
        }
      >
        <TabsList aria-label="模型详情分区" className="max-w-full">
          <TabsTrigger className="text-xs" value="details">
            评估与发布
          </TabsTrigger>
          <TabsTrigger className="text-xs" value="records">
            来源与使用
          </TabsTrigger>
          <TabsTrigger className="text-xs" value="notes">
            标记与备注
          </TabsTrigger>
        </TabsList>
        <TabsContent value="details">
          <ProjectModelsPanel projectId={id} classes={[]} mode="detail" initialModelId={modelId} />
        </TabsContent>
        <TabsContent value="records">
          <ModelLineagePanel projectId={id} modelId={modelId} view="records" />
        </TabsContent>
        <TabsContent value="notes">
          <ModelLineagePanel projectId={id} modelId={modelId} view="notes" />
        </TabsContent>
      </Tabs>
    </PageContainer>
  );
}
