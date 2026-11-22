import type { CSSProperties } from "react";

export const FS_XS = 11;
export const FS_SM = 12;
export const FS_MD = 13;
export const FS_LG = 16;
export const FS_XL = 20;
export const FS_HUGE = 24;

export const PAGE_PADDING_X = 28;
export const PAGE_PADDING_Y = 20;
export const CARD_PADDING = 16;
export const CARD_HEADER_PADDING_X = 16;
export const CARD_HEADER_PADDING_Y = 12;
export const SECTION_GAP = 16;
export const FIELD_GAP = 12;

export const CHIPS_MAX_HEIGHT = 120;
export const CHIPS_SHOW_SEARCH_THRESHOLD = 6;
export const HISTORY_PAGE_SIZE = 20;

export const labelStyle: CSSProperties = {
  display: "block",
  fontSize: FS_SM,
  fontWeight: 600,
  marginBottom: 6,
  color: "var(--sc-foreground)",
};

export const selectStyle: CSSProperties = {
  width: "100%",
  boxSizing: "border-box",
  padding: "6px 10px",
  fontSize: FS_MD,
  background: "var(--sc-muted)",
  border: "1px solid var(--sc-border)",
  borderRadius: "var(--radius-sm)",
  color: "var(--sc-foreground)",
  fontFamily: "inherit",
};

export const cardHeaderStyle: CSSProperties = {
  padding: `${CARD_HEADER_PADDING_Y}px ${CARD_HEADER_PADDING_X}px`,
  borderBottom: "1px solid var(--sc-border)",
  fontSize: FS_MD,
  fontWeight: 600,
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: 10,
};

export const cardBodyStyle: CSSProperties = {
  padding: CARD_PADDING,
  display: "flex",
  flexDirection: "column",
  gap: FIELD_GAP,
};

export const helperTextStyle: CSSProperties = {
  fontSize: FS_XS,
  color: "var(--sc-muted-foreground)",
  marginTop: 4,
};

export const aliasChipStyle: CSSProperties = {
  fontSize: FS_XS,
  padding: "3px 9px",
  background: "color-mix(in srgb, var(--sc-chart-4) 14%, transparent)",
  border: "1px solid var(--sc-border)",
  borderRadius: 999,
  color: "var(--sc-foreground)",
  cursor: "pointer",
  fontFamily: "inherit",
  display: "inline-flex",
  alignItems: "center",
  gap: 4,
  transition: "background 120ms ease, border-color 120ms ease",
};

export const aliasChipActiveStyle: CSSProperties = {
  ...aliasChipStyle,
  background: "color-mix(in oklab, var(--sc-chart-4) 18%, transparent)",
  borderColor: "var(--sc-chart-4)",
  boxShadow: "inset 2px 0 0 var(--sc-chart-4)",
};

export const tableHeaderCellStyle: CSSProperties = {
  padding: "6px 10px",
  textAlign: "left",
  fontWeight: 500,
  color: "var(--sc-muted-foreground)",
  borderBottom: "1px solid var(--sc-border)",
  cursor: "pointer",
  userSelect: "none",
  whiteSpace: "nowrap",
};

export const tableBodyCellStyle: CSSProperties = {
  padding: "6px 10px",
  borderBottom: "1px solid var(--sc-border)",
};
