import { cn } from "@/lib/utils";

import { AVATAR_SIZE_CLASS, type AvatarSize } from "./avatarSizes";
import { useElementStyle } from "./useElementStyle";

/**
 * Avatar —— 首字母头像(v0.17.2)。
 * 纯首字母(无图片加载),故不用 shadcn `<Avatar>` 的图片回退机制,直接渲染中性圆片
 * (设计「中性基底」:`bg-muted`)。保留 `style` 透传(useElementStyle),调用方仍可覆盖配色。
 *
 * 需要渲染用户上传 / 内置像素头像时用 `UserAvatar`,它复用同一份尺寸表。
 */
interface AvatarProps {
  initial: string;
  size?: AvatarSize;
  style?: React.CSSProperties;
}

export function Avatar({ initial, size = "sm", style }: AvatarProps) {
  const styleRef = useElementStyle<HTMLDivElement>(style);
  return (
    <div
      ref={styleRef}
      className={cn(
        "inline-flex shrink-0 select-none items-center justify-center rounded-full bg-muted font-semibold text-foreground",
        AVATAR_SIZE_CLASS[size],
      )}
    >
      {initial}
    </div>
  );
}
