import { CopyButton } from "@/components/ui/CopyButton";
import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { trainingObservabilityApi, type LogPage } from "@/api/trainingObservability";
import { Button } from "@/components/ui/Button";
import { mergeObservationPage, type ObservationWindow } from "./useTrainingObservability";
import type { AssertTrainingOwner } from "./trainingViewOwner";

export function TrainingLogViewer({
  projectId,
  runId,
  attemptId,
  data,
  pending,
  error,
  assertOwner,
}: {
  projectId: string;
  runId: string;
  attemptId: string;
  data?: ObservationWindow<LogPage>;
  pending: boolean;
  error?: string;
  assertOwner: AssertTrainingOwner;
}) {
  const [window, setWindow] = useState(data);
  const [following, setFollowing] = useState(true);
  const [earlierError, setEarlierError] = useState("");
  const box = useRef<HTMLPreElement>(null);
  const alive = useRef(true);
  const request = useRef<AbortController>();
  const anchor = useRef<{ sequence?: string; offset: number; top: number; height: number }>();
  const stream = useRef(data?.stream_id);
  stream.current = data?.stream_id;
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    setEarlierError("");
    setFollowing(true);
    anchor.current = undefined;
    return () => request.current?.abort();
  }, [data?.stream_id]);
  useEffect(() => {
    if (!data) return;
    setWindow((previous) =>
      following || previous?.stream_id !== data.stream_id
        ? data
        : {
            ...data,
            items: previous.items.filter(
              (item) => data.earliest_cursor == null || item.sequence >= data.earliest_cursor,
            ),
            gaps: data.gaps,
          },
    );
  }, [data, following]);
  useLayoutEffect(() => {
    const element = box.current;
    if (!element) return;
    const previous = anchor.current;
    if (previous) {
      const chunk = previous.sequence
        ? element.querySelector<HTMLElement>(`[data-sequence="${previous.sequence}"]`)
        : null;
      element.scrollTop = chunk
        ? element.scrollTop +
          chunk.getBoundingClientRect().top -
          element.getBoundingClientRect().top -
          previous.offset
        : previous.top + element.scrollHeight - previous.height;
      anchor.current = undefined;
    } else if (following) element.scrollTop = element.scrollHeight;
  }, [window, following]);
  const earlier = useMutation({
    mutationFn: async ({
      expectedStream,
      before,
      controller,
    }: {
      expectedStream: string | null | undefined;
      before: number | undefined;
      controller: AbortController;
    }) => {
      assertOwner();
      if (!before) return;
      request.current?.abort();
      request.current = controller;
      const page = await trainingObservabilityApi.logs(
        projectId,
        runId,
        attemptId,
        { before },
        { signal: controller.signal },
      );
      assertOwner(controller.signal);
      if (!alive.current || stream.current !== expectedStream || request.current !== controller)
        return;
      if (page.attempt_id !== attemptId || page.stream_id !== expectedStream)
        throw new Error("日志归属已变化，请重新加载");
      setFollowing(false);
      const element = box.current;
      if (element) {
        const top = element.getBoundingClientRect().top;
        const visible = [...element.querySelectorAll<HTMLElement>("[data-sequence]")].find(
          (chunk) => chunk.getBoundingClientRect().bottom > top,
        );
        anchor.current = {
          sequence: visible?.dataset.sequence,
          offset: visible ? visible.getBoundingClientRect().top - top : 0,
          top: element.scrollTop,
          height: element.scrollHeight,
        };
      }
      setWindow((previous) => {
        if (!previous || previous.stream_id !== expectedStream) return previous;
        const merged = mergeObservationPage(previous, page, 1024);
        return {
          ...previous,
          items: merged.items.slice(0, 512),
          gaps: merged.gaps,
          earliest_cursor: page.earliest_cursor,
        };
      });
      setEarlierError("");
    },
    onError: (reason: Error, { expectedStream, controller }) => {
      if (
        alive.current &&
        !controller.signal.aborted &&
        request.current === controller &&
        stream.current === expectedStream
      ) {
        try {
          assertOwner();
          setEarlierError(reason.message);
        } catch {
          /* Superseded owner. */
        }
      }
    },
  });
  const first = window?.items[0]?.sequence;
  const canEarlier =
    first != null && window?.earliest_cursor != null && first > window.earliest_cursor;
  const text = window?.items.map((item) => item.text).join("") ?? "";
  return (
    <section aria-label="所选执行脱敏日志" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Button
          size="sm"
          disabled={!canEarlier || earlier.isPending}
          onClick={() =>
            earlier.mutate({
              expectedStream: data?.stream_id,
              before: window?.items[0]?.sequence,
              controller: new AbortController(),
            })
          }
        >
          加载更早日志
        </Button>
        <CopyButton
          value={text}
          label="复制当前日志"
          successMessage="已复制当前已加载日志"
          disabled={!text}
        >
          复制当前日志
        </CopyButton>
        <label className="flex items-center gap-2 text-xs">
          <input
            type="checkbox"
            checked={following}
            onChange={(event) => setFollowing(event.target.checked)}
          />
          跟随最新日志
        </label>
      </div>
      {(error || earlierError) && (
        <p role="alert" className="text-xs text-status-caution">
          日志连接中断：
          <CopyableError message={earlierError || error} />
          。已显示的内容会保留，恢复连接后继续读取。
        </p>
      )}
      {!!window?.gaps.length && (
        <p className="text-xs text-muted-foreground">
          部分日志已因保留窗口或本地容量限制移除。
          {window.gaps
            .slice(-3)
            .map((gap) => `（${gap.after + 1}–${gap.through}）`)
            .join(" ")}
        </p>
      )}
      {pending && !text ? (
        <p role="status" className="py-5 text-xs text-muted-foreground">
          正在加载脱敏日志…
        </p>
      ) : !text ? (
        <p className="py-5 text-xs text-muted-foreground">
          此执行暂无可用的实时日志；历史产物可在“产物与记录”下载脱敏副本。
        </p>
      ) : (
        <pre
          ref={box}
          role="region"
          tabIndex={0}
          aria-label="脱敏日志内容"
          onScroll={(event) => {
            const element = event.currentTarget;
            if (element.scrollHeight - element.scrollTop - element.clientHeight > 32)
              setFollowing(false);
          }}
          className="max-h-96 overflow-auto whitespace-pre-wrap break-all rounded-md border border-border bg-muted p-3 font-mono text-2xs leading-relaxed focus-visible:ring-2 focus-visible:ring-ring"
        >
          {window?.items.map((item) => (
            <span key={item.sequence} data-sequence={item.sequence}>
              {item.text}
            </span>
          ))}
        </pre>
      )}
      <p className="text-2xs text-muted-foreground">
        仅保留近期最多 512
        个日志块。URL、凭据和主机路径在保存前脱敏，超长行会整体省略。关闭跟随可阅读较早内容。
      </p>
    </section>
  );
}
