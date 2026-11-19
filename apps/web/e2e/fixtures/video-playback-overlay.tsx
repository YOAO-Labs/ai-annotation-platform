import { useState } from "react";
import { createRoot } from "react-dom/client";
import { VideoPlaybackOverlay } from "@/pages/Workbench/stage/VideoPlaybackOverlay";
import "@/styles/shadcn.css";

document.documentElement.dataset.theme = matchMedia("(prefers-color-scheme: dark)").matches
  ? "dark"
  : "light";

function Fixture() {
  const [frame, setFrame] = useState(50);
  const [playing, setPlaying] = useState(false);
  const [loop, setLoop] = useState({ startFrame: 10, endFrame: 20 });
  const [propagation, setPropagation] = useState({ startFrame: 70, endFrame: 90 });
  const [selection, setSelection] = useState("");
  const purpose = location.search === "?chapter" ? "chapter-draft" : "propagate-range";
  return (
    <main className="relative h-screen bg-background text-foreground">
      <output aria-label="提交的区间">{selection}</output>
      <VideoPlaybackOverlay
        visible
        frameIndex={frame}
        maxFrame={100}
        timebase={{ fps: 25, frameCount: 101, source: "estimated", ptsMs: null }}
        isPlaying={playing}
        currentFrameEntryCount={0}
        loopRegion={loop}
        propagateRange={propagation}
        rangeSelectPurpose={purpose}
        onSeek={setFrame}
        onSeekByFrames={(delta) => setFrame((n) => Math.max(0, Math.min(100, n + delta)))}
        onTogglePlay={() => setPlaying((value) => !value)}
        onLoopRegionChange={setLoop}
        onRangeSelect={(kind, range) => {
          if (kind === "propagate-range") setPropagation(range);
          setSelection(`${kind} F${range.startFrame}–${range.endFrame}`);
        }}
      />
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<Fixture />);
