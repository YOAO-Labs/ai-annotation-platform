import { useState } from "react";
import { createRoot } from "react-dom/client";
import { Modal } from "@/components/ui/Modal";
import { ScrollArea } from "@/components/shadcn/ui/scroll-area";
import shell from "@/App.module.css";
import history from "@/components/AnnotationHistoryTimeline.module.css";
import command from "@/components/CommandPalette.module.css";
import "@/styles/shadcn.css";

const surface = new URLSearchParams(location.search).get("surface") ?? "panels";
document.documentElement.dataset.theme = matchMedia("(prefers-color-scheme: dark)").matches
  ? "dark"
  : "light";

function Rows({ long }: { long: boolean }) {
  return (
    <div data-measure>
      {Array.from({ length: long ? 80 : 1 }, (_, i) => (
        <div key={i} className="h-8 border-b border-border px-2">
          项目 {i + 1}
        </div>
      ))}
    </div>
  );
}

function Fixture() {
  const [long, setLong] = useState(false);
  const [modal, setModal] = useState(false);
  const controls = (
    <header className="col-span-2 flex h-12 items-center gap-4 border-b border-border bg-card px-4">
      <button onClick={() => setLong(!long)} aria-pressed={long}>
        切换内容长度
      </button>
      <button onClick={() => setModal(true)}>打开弹窗</button>
    </header>
  );
  const content = (
    <>
      {surface === "shell" ? (
        <div className={shell.appShell}>
          {controls}
          <aside className="w-16 border-r border-border">导航</aside>
          <main data-testid="shell-main" className={shell.main}>
            <Rows long={long} />
          </main>
        </div>
      ) : surface === "workbench" ? (
        <div className={shell.fullScreenWorkbench}>
          {controls}
          <div className="grid h-[calc(100%-3rem)] grid-cols-[1fr_18rem] max-md:grid-cols-[1fr_9rem]">
            <div data-testid="canvas" className="overflow-hidden bg-muted">
              <canvas className="h-full w-full" />
            </div>
            <aside data-testid="workbench-panel" className="overflow-y-auto bg-card">
              <Rows long={long} />
            </aside>
          </div>
        </div>
      ) : surface === "document" ? (
        <div data-testid="document" className="mx-auto max-w-3xl">
          {controls}
          <Rows long={long} />
        </div>
      ) : (
        <div className="p-4">
          {controls}
          <div className="grid grid-cols-2 gap-4 py-4 max-md:grid-cols-1">
            {[
              ["utility-y", "h-40 overflow-y-auto"],
              ["utility-both", "h-40 overflow-auto"],
              ["module-history", `h-40 ${history.bounded}`],
              ["module-command", `h-40 ${command.results}`],
            ].map(([id, className]) => (
              <section key={id}>
                <h2>{id}</h2>
                <div data-testid={id} className={className}>
                  <Rows long={long} />
                </div>
              </section>
            ))}
            <section>
              <h2>textarea</h2>
              <textarea
                data-testid="textarea"
                aria-label="长文本"
                className="h-40 w-full resize-none"
                readOnly
                value={Array.from({ length: long ? 80 : 1 }, () => "项目说明").join("\n")}
              />
            </section>
            <section>
              <h2>overlay scroll area</h2>
              <ScrollArea data-testid="custom-scroll" className="h-40">
                <Rows long={long} />
              </ScrollArea>
            </section>
            <section>
              <h2>horizontal table</h2>
              <div data-testid="horizontal" className="overflow-x-auto">
                <div className="h-8 w-[1600px]">宽表格</div>
              </div>
            </section>
            <section>
              <h2>hidden native scrollbar</h2>
              <div
                data-testid="hidden-scroll"
                className="h-40 overflow-y-auto [scrollbar-width:none]"
              >
                <Rows long={long} />
              </div>
            </section>
          </div>
        </div>
      )}
      <Modal open={modal} onClose={() => setModal(false)} title="滚动验证">
        <button onClick={() => setLong(!long)} aria-pressed={long}>
          切换弹窗内容长度
        </button>
        <div data-testid="modal-content">
          <Rows long={long} />
        </div>
      </Modal>
    </>
  );
  return content;
}

createRoot(document.getElementById("root")!).render(<Fixture />);
