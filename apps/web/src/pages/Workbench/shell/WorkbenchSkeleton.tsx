function Block({ w, h, mb = 0 }: { w: number | string; h: number; mb?: 0 | 6 | 8 | 10 | 20 }) {
  const mbClass =
    mb === 6 ? "mb-1.5" : mb === 8 ? "mb-2" : mb === 10 ? "mb-2.5" : mb === 20 ? "mb-5" : "";
  const wClass =
    typeof w === "number"
      ? `w-[${w}px]`
      : w === "80%"
        ? "w-4/5"
        : w === "60%"
          ? "w-3/5"
          : w === "50%"
            ? "w-1/2"
            : w === "100%"
              ? "w-full"
              : w === "40%"
                ? "w-2/5"
                : "";
  return <div className={`rounded animate-pulse bg-muted ${wClass} h-[${h}px] ${mbClass}`} />;
}

export function WorkbenchSkeleton() {
  return (
    <div className="grid grid-cols-[260px_1fr_280px] h-full overflow-hidden bg-muted">
      <div className="p-3.5 border-r border-border bg-card">
        <Block w={120} h={16} mb={10} />
        <Block w="80%" h={11} mb={20} />
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="flex gap-2 mb-2.5">
            <Block w={40} h={40} />
            <div className="flex-1">
              <Block w="60%" h={11} mb={6} />
              <Block w="80%" h={10} />
            </div>
          </div>
        ))}
      </div>

      <div className="flex flex-col">
        <div className="flex gap-1.5 p-2.5 border-b border-border bg-card">
          <Block w={60} h={26} />
          <Block w={80} h={26} />
          <Block w={26} h={26} />
          <Block w={26} h={26} />
          <div className="flex-1" />
          <Block w={120} h={26} />
          <Block w={80} h={26} />
        </div>
        <div className="relative flex-1 p-10">
          <div className="absolute inset-10 rounded-md animate-pulse bg-muted" />
        </div>
        <div className="p-2 border-t border-border bg-card">
          <Block w="40%" h={11} />
        </div>
      </div>

      <div className="p-3.5 border-l border-border bg-card">
        <Block w="50%" h={14} mb={10} />
        <Block w="100%" h={32} mb={10} />
        <Block w="100%" h={28} mb={20} />
        {Array.from({ length: 5 }).map((_, i) => (
          <Block key={i} w="100%" h={42} mb={8} />
        ))}
      </div>
    </div>
  );
}
