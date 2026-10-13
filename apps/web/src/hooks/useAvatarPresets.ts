import { useQuery } from "@tanstack/react-query";

/**
 * 内置像素头像目录。
 *
 * 真值源是构建期生成、随前端产物一起发布的 `public/avatars/pixel/manifest.json`
 * （生成脚本见 `apps/web/scripts/gen-pixel-avatars.mjs`）。后端只校验引用语法，
 * 不维护白名单，因此这里拉不到 manifest 时表现为「选择器空态 + 提示」，
 * 而不会影响上传与恢复默认。
 */

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
    // 随发布产物变化，进程内不需要再次校验。
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
