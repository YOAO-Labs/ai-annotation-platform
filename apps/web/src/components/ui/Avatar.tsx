import { cn } from "@/lib/utils";

import { AVATAR_SIZE_CLASS, type AvatarSize } from "./avatarSizes";
import { useElementStyle } from "./useElementStyle";

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
