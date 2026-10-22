export const listStyles = {
  surface: "overflow-hidden rounded-xl border border-border bg-card",
  scroll: "overflow-x-auto",
  table:
    "w-full border-collapse text-left text-sm [&_th]:h-9 [&_th]:whitespace-nowrap [&_thead_th]:bg-muted/50 [&_th]:px-3 [&_th]:py-2 [&_th]:text-xs [&_th]:font-medium [&_th]:text-muted-foreground [&_td]:px-3 [&_td]:py-2.5 [&_td]:align-middle [&_tbody>tr]:border-b [&_tbody>tr]:border-border [&_tbody>tr]:transition-colors [&_tbody>tr:last-child]:border-b-0 [&_tbody>tr:hover]:bg-muted/40",
  row: "border-b border-border px-3 py-2.5 transition-colors last:border-b-0 hover:bg-muted/40 sm:px-4",
  header:
    "border-b border-border bg-muted/50 px-3 py-2 text-xs font-medium text-muted-foreground sm:px-4",
  empty: "px-4 py-10 text-center text-sm text-muted-foreground",
} as const;
