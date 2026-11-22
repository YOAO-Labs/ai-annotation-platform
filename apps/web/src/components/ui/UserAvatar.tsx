import { useState } from "react";

import { cn } from "@/lib/utils";
import { avatarInitial, resolveAvatarUrl } from "@/utils/avatar";

import { AVATAR_SIZE_CLASS, type AvatarSize } from "./avatarSizes";

export interface AvatarUserLike {
  id?: string;
  name?: string | null;
  email?: string | null;
  avatar_initial?: string | null;
  avatar_ref?: string | null;
}

interface UserAvatarProps {
  user: AvatarUserLike;
  size?: AvatarSize;
  className?: string;
}

export function UserAvatar({ user, size = "sm", className }: UserAvatarProps) {
  const url = resolveAvatarUrl(user.avatar_ref);
  const [brokenUrl, setBrokenUrl] = useState<string | null>(null);
  const initial = avatarInitial(user.name, user.email, user.avatar_initial);
  const showImage = url !== null && url !== brokenUrl;

  return (
    <span
      className={cn(
        "relative inline-flex shrink-0 select-none items-center justify-center overflow-hidden rounded-full border border-border/60 bg-muted font-semibold text-foreground",
        AVATAR_SIZE_CLASS[size],
        className,
      )}
    >
      <span aria-hidden="true" className="absolute inset-0 flex items-center justify-center">
        {initial}
      </span>
      {showImage && (
        <img
          src={url}
          alt=""
          aria-hidden="true"
          loading="lazy"
          decoding="async"
          className="relative size-full object-cover"
          onError={() => setBrokenUrl(url)}
        />
      )}
    </span>
  );
}
