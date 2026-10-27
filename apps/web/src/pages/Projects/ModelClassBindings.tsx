import { useId, useState } from "react";
import { Button } from "@/components/ui/Button";

const PAGE_SIZE = 10;
const controlClass =
  "min-w-0 rounded-md border border-border bg-background px-2 py-1.5 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

export function ModelClassBindings({
  nativeClasses,
  projectClasses,
  bindings,
  disabled,
  onChange,
}: {
  nativeClasses: string[];
  projectClasses: string[];
  bindings: Record<number, string>;
  disabled: boolean;
  onChange: (bindings: Record<number, string>) => void;
}) {
  const id = useId();
  const [search, setSearch] = useState("");
  const [onlyUnbound, setOnlyUnbound] = useState(false);
  const [page, setPage] = useState(0);
  const targets = new Set(projectClasses);
  const isBound = (index: number) => targets.has(bindings[index]);
  const boundCount = nativeClasses.filter((_, index) => isBound(index)).length;
  const query = search.trim().toLocaleLowerCase();
  const rows = nativeClasses
    .map((name, index) => ({ name, index }))
    .filter(
      ({ name, index }) =>
        (!onlyUnbound || !isBound(index)) &&
        (!query ||
          [name, String(index), bindings[index] ?? ""].some((value) =>
            value.toLocaleLowerCase().includes(query),
          )),
    );
  const lastPage = Math.max(0, Math.ceil(rows.length / PAGE_SIZE) - 1);
  const currentPage = Math.min(page, lastPage);
  const visible = rows.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);
  const canMatch = nativeClasses.some((name, index) => !isBound(index) && targets.has(name));

  return (
    <section aria-label="类别绑定" className="min-w-0 space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
        <h3 className="font-medium">类别绑定</h3>
        <span aria-live="polite" className="tabular-nums text-muted-foreground">
          已绑定 {boundCount} / {nativeClasses.length}
        </span>
      </div>
      <div className="flex items-center gap-2">
        <input
          type="search"
          aria-label="搜索类别绑定"
          placeholder="搜索类别或序号"
          className={`${controlClass} w-0 flex-1`}
          value={search}
          onChange={(event) => {
            setSearch(event.target.value);
            setPage(0);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter") event.preventDefault();
          }}
        />
        <Button
          type="button"
          size="xs"
          disabled={disabled || !canMatch}
          title="填充所有未绑定的同名类别，保留已有选择"
          onClick={() => {
            const next = { ...bindings };
            nativeClasses.forEach((name, index) => {
              if (!isBound(index) && targets.has(name)) next[index] = name;
            });
            onChange(next);
          }}
        >
          按同名绑定
        </Button>
      </div>
      <label className="flex w-fit items-center gap-2 text-xs text-muted-foreground">
        <input
          type="checkbox"
          checked={onlyUnbound}
          onChange={(event) => {
            setOnlyUnbound(event.target.checked);
            setPage(0);
          }}
        />
        仅未绑定（{nativeClasses.length - boundCount}）
      </label>
      <div className="overflow-hidden rounded-md border border-border">
        <div className="grid grid-cols-2 gap-2 border-b border-border bg-muted px-2 py-1.5 text-2xs text-foreground">
          <span>原生输出类别</span>
          <span>项目类别</span>
        </div>
        <div
          role="group"
          aria-label="类别绑定列表"
          tabIndex={0}
          className="max-h-56 overflow-y-auto overscroll-contain focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
        >
          {visible.map(({ name, index }) => (
            <div
              key={index}
              className="grid grid-cols-2 items-center gap-2 border-b border-border px-2 py-1.5 last:border-b-0"
            >
              <label
                htmlFor={`${id}-${index}`}
                title={`原生类别 ${index} · ${name}`}
                className="flex min-w-0 items-center gap-1.5 text-xs"
              >
                <span className="sr-only">原生类别 </span>
                <span className="shrink-0 tabular-nums text-muted-foreground">{index}</span>
                <span className="truncate"> · {name}</span>
              </label>
              <div className="flex min-w-0 items-center gap-1">
                <select
                  id={`${id}-${index}`}
                  className={`${controlClass} w-0 flex-1`}
                  value={isBound(index) ? bindings[index] : ""}
                  disabled={disabled || projectClasses.length === 0}
                  onChange={(event) => onChange({ ...bindings, [index]: event.target.value })}
                >
                  <option value="" disabled hidden>
                    选择项目类别
                  </option>
                  {projectClasses.map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
                <Button
                  type="button"
                  size="xs"
                  variant="ghost"
                  aria-label={`清除原生类别 ${index} · ${name} 的绑定`}
                  disabled={disabled || !bindings[index]}
                  onClick={() => onChange({ ...bindings, [index]: "" })}
                >
                  清除
                </Button>
              </div>
            </div>
          ))}
          {visible.length === 0 && (
            <p className="px-3 py-5 text-center text-xs text-muted-foreground">
              {onlyUnbound && boundCount === nativeClasses.length
                ? "全部类别已绑定"
                : "没有匹配的类别，可调整搜索或取消筛选"}
            </p>
          )}
        </div>
      </div>
      {rows.length > PAGE_SIZE && (
        <div className="flex items-center justify-between gap-2 text-xs">
          <span className="tabular-nums text-muted-foreground">
            {currentPage * PAGE_SIZE + 1}–{Math.min((currentPage + 1) * PAGE_SIZE, rows.length)}
            {" / "}
            {rows.length}
          </span>
          <div className="flex gap-1">
            <Button
              type="button"
              size="xs"
              disabled={currentPage === 0}
              onClick={() => setPage(currentPage - 1)}
            >
              上一页类别
            </Button>
            <Button
              type="button"
              size="xs"
              disabled={currentPage === lastPage}
              onClick={() => setPage(currentPage + 1)}
            >
              下一页类别
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}
