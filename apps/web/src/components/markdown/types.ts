export type MarkdownImageResolver = (
  src: string,
  options?: { refresh?: boolean },
) => Promise<{ url: string; expiresAt?: number }>;
