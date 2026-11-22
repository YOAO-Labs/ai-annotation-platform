import { Icon } from "@/components/ui/Icon";

export interface MetaFooterRow {
  label: string;
  value: string;
  mono?: boolean;
}

export interface MetaFooterProps {
  id: string;
  source?: string;
  createdAt?: string | null;
  updatedAt?: string | null;
  zOrder?: number;
  extra?: MetaFooterRow[];
}

function shortId(id: string): string {
  return id.length > 12 ? id.slice(0, 8) : id;
}

function sourceText(source: string): string {
  if (source === "manual") return "手动";
  if (source.includes("prediction")) return "AI 采纳";
  if (source.includes("import")) return "导入";
  return source;
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => `${n}`.padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

const FOOTER_CLASS = "group border-t border-border pt-1.5";
const SUMMARY_CLASS =
  "flex cursor-pointer select-none list-none items-center gap-1 text-xs text-muted-foreground [&::-webkit-details-marker]:hidden hover:text-foreground";
const CHEV_CLASS =
  "inline-flex text-muted-foreground transition-transform duration-150 group-open:rotate-90 motion-reduce:transition-none";
const ROWS_CLASS = "mt-1.5 grid grid-cols-[auto_1fr] gap-x-2.5 gap-y-[3px]";
const ROW_CLASS = "contents";
const ROW_LABEL_CLASS = "text-xs text-muted-foreground";
const ROW_VALUE_CLASS = "truncate text-xs text-muted-foreground";

export function MetaFooter({ id, source, createdAt, updatedAt, zOrder, extra }: MetaFooterProps) {
  const rows: MetaFooterRow[] = [{ label: "ID", value: shortId(id), mono: true }];
  if (source) rows.push({ label: "来源", value: sourceText(source) });
  if (createdAt) rows.push({ label: "创建", value: formatTime(createdAt), mono: true });
  if (updatedAt) rows.push({ label: "更新", value: formatTime(updatedAt), mono: true });
  if (typeof zOrder === "number") rows.push({ label: "层级", value: `${zOrder}`, mono: true });
  if (extra?.length) rows.push(...extra);

  return (
    <details className={FOOTER_CLASS}>
      <summary className={SUMMARY_CLASS}>
        <span className={CHEV_CLASS} aria-hidden="true">
          <Icon name="chevRight" size={12} />
        </span>
        更多信息
      </summary>
      <dl className={ROWS_CLASS}>
        {rows.map((r) => (
          <div key={r.label} className={ROW_CLASS}>
            <dt className={ROW_LABEL_CLASS}>{r.label}</dt>
            <dd
              className={r.mono ? `${ROW_VALUE_CLASS} mono` : ROW_VALUE_CLASS}
              title={r.label === "ID" ? id : undefined}
            >
              {r.value}
            </dd>
          </div>
        ))}
      </dl>
    </details>
  );
}
