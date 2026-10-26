import type { FilteringSeedManifest, SeedAPI } from "./seed";

export type { FilteringSeedManifest } from "./seed";

export async function resetFiltering(
  seed: SeedAPI,
  options: { videoReady?: boolean } = {},
): Promise<FilteringSeedManifest> {
  return seed.filtering(options);
}

export interface FilteringSeedSession {
  manifest: FilteringSeedManifest;
  tokens: {
    admin: string;
    annotator: string;
    reviewer: string;
  };
}

export async function filteringSession(seed: SeedAPI): Promise<FilteringSeedSession> {
  const manifest = await resetFiltering(seed);
  const [admin, annotator, reviewer] = await Promise.all([
    seed.accessToken(manifest.user_emails.admin),
    seed.accessToken(manifest.user_emails.anno),
    seed.accessToken(manifest.user_emails.rev),
  ]);
  return { manifest, tokens: { admin, annotator, reviewer } };
}
