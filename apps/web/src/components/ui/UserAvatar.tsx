import { useState } from "react";

import { cn } from "@/lib/utils";
import { avatarInitial, resolveAvatarUrl } from "@/utils/avatar";

import { AVATAR_SIZE_CLASS, type AvatarSize } from "./avatarSizes";

/**
 * UserAvatar —— 用户头像(图片优先,首字母回退)。
 *
 * 视觉与 `Avatar`(纯首字母)一致:中性 `bg-muted` 圆片 + 同一份尺寸表,只有「有无图片」不同。
 *
 * 有意**不引入 Radix Avatar 原语**:入口 chunk 的 size-limit 预算只有约 0.2 KB 余量,而
 * 该原语会新增约 3 KB 到入口包;`<img onError>` 三行即可覆盖同一语义(加载中/失败显示首字母)。
 * 若将来入口包有余量且需要统一的延迟显示策略,再改回复用 `components/shadcn/ui/avatar.tsx`。
 *
 * `alt=""` + `aria-hidden`:头像与旁边/tooltip 中的姓名重复,读屏不应重复播报。
 */

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
  // 记录**失败的那个 URL**而不是布尔值：`avatar_ref` 在同一挂载实例内变化（如设置页
  // 保存新头像）时,新 URL 必须重新尝试加载,只有同一 URL 再次失败才继续回退首字母。
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
      {/* 首字母始终渲染在底层:图片加载中或加载失败时可见,无需额外状态。 */}
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
