import type { HTMLAttributes } from "react";
import { cn } from "@/lib/utils";

export function PageContainer({ className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "mx-auto w-full max-w-[1480px] px-7 pb-10 pt-5 text-foreground max-[900px]:px-4 max-[900px]:pb-6 max-[900px]:pt-4",
        className,
      )}
      {...rest}
    />
  );
}
