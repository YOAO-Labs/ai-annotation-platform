import { useState } from "react";
import { createRoot } from "react-dom/client";
import { FilterSelect } from "@/components/filters/FilterControls";
import { Button } from "@/components/ui/Button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/shadcn/ui/select";
import { ModelClassBindings } from "@/pages/Projects/ModelClassBindings";
import { trainingInputClass } from "@/pages/Training/TrainingParameterField";
import importStyles from "@/components/datasets/ImportDatasetWizard.module.css";
import "@/styles/shadcn.css";

document.documentElement.dataset.theme = matchMedia("(prefers-color-scheme: dark)").matches
  ? "dark"
  : "light";

function Fixture() {
  const [model, setModel] = useState("");
  const [filter, setFilter] = useState("");
  const [radix, setRadix] = useState("");
  const [bindings, setBindings] = useState<Record<number, string>>({ 0: "车辆" });
  const [submits, setSubmits] = useState(0);
  return (
    <main className="mx-auto max-w-3xl space-y-5 p-5">
      <h1 className="text-lg font-semibold">下拉菜单提示与选项</h1>
      <p className="text-xs">
        <span data-testid="foreground" className="text-foreground">
          字段值
        </span>{" "}
        /{" "}
        <span data-testid="muted" className="text-muted-foreground">
          提示文字
        </span>
      </p>
      <label className="block space-y-1 text-xs">
        候选模型
        <select
          className={trainingInputClass}
          value={model}
          onChange={(e) => setModel(e.target.value)}
        >
          <option value="" disabled hidden>
            选择候选模型
          </option>
          <option value="candidate-a">候选 A</option>
          <option value="candidate-b">候选 B</option>
          <option value="unavailable" disabled>
            暂不可用的候选
          </option>
        </select>
      </label>
      <Button type="button" size="sm" onClick={() => setModel("")}>
        重置模型
      </Button>
      <label className="block space-y-1 text-xs">
        保留的执行记录
        <select className={importStyles.input} defaultValue="attempt-pending">
          <option value="attempt-pending" disabled hidden>
            请选择成功的执行记录
          </option>
          <option value="attempt-success">成功执行</option>
        </select>
      </label>
      <label className="block space-y-1 text-xs">
        当前无可用部署
        <select className={trainingInputClass} value="" disabled>
          <option value="" disabled hidden>
            暂无就绪部署
          </option>
        </select>
      </label>
      <label className="flex flex-col items-start gap-1 text-xs">
        状态筛选
        <FilterSelect value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="">全部状态</option>
          <option value="ready">已就绪</option>
        </FilterSelect>
      </label>
      <section className="space-y-1 text-xs">
        <label htmlFor="radix-model">模型配置</label>
        <Select value={radix} onValueChange={setRadix}>
          <SelectTrigger id="radix-model" className="w-full">
            <SelectValue placeholder="选择模型配置" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="model-a">配置 A</SelectItem>
            <SelectItem value="model-b">配置 B</SelectItem>
          </SelectContent>
        </Select>
      </section>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setSubmits(submits + 1);
        }}
      >
        <ModelClassBindings
          nativeClasses={["car", "person"]}
          projectClasses={["车辆", "行人"]}
          bindings={bindings}
          disabled={false}
          onChange={setBindings}
        />
        <output aria-label="绑定结果">{JSON.stringify(bindings)}</output>
        <output aria-label="提交次数">{submits}</output>
      </form>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<Fixture />);
