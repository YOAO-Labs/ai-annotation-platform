/**
 * 头像尺寸档位的唯一定义。
 *
 * `Avatar`(纯首字母)与 `UserAvatar`(图片 + 首字母回退)共用本表，避免两套尺寸
 * 逐渐漂移；新增档位只改这里。
 */
export type AvatarSize = "sm" | "md" | "lg";

export const AVATAR_SIZE_CLASS: Record<AvatarSize, string> = {
  sm: "size-5 text-2xs",
  md: "size-7 text-xs",
  lg: "size-9 text-sm",
};
