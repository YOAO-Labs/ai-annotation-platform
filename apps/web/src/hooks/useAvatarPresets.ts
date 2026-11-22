import { useQuery } from "@tanstack/react-query";

export interface AvatarPreset {
  id: string;
  label: string;
}

export interface AvatarPresetManifest {
  style: string;
  creator: string;
  license: string;
  licenseUrl: string;
  sourceUrl: string;
  items: AvatarPreset[];
}

export const AVATAR_PRESET_MANIFEST_URL = "/avatars/pixel/manifest.json";

export function useAvatarPresets() {
  return useQuery({
    queryKey: ["avatar-presets"],
    staleTime: Infinity,
    retry: 1,
    queryFn: async (): Promise<AvatarPresetManifest> => {
      const response = await fetch(AVATAR_PRESET_MANIFEST_URL);
      if (!response.ok) {
        throw new Error(`头像目录加载失败 (HTTP ${response.status})`);
      }
      return (await response.json()) as AvatarPresetManifest;
    },
  });
}
