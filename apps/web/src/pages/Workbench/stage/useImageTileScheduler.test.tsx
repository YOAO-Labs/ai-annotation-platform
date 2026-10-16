import { useEffect } from "react";
import { render, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ImagePyramidManifestV1, WorkbenchImageSource } from "./imagePyramid";
import { useImageTileScheduler } from "./useImageTileScheduler";

const schedulerMock = vi.hoisted(() => {
  const instances: MockScheduler[] = [];

  class MockScheduler {
    readonly sourceIdentity: string;
    private listener: (() => void) | null = null;

    constructor(options: { sourceIdentity: string }) {
      this.sourceIdentity = options.sourceIdentity;
      instances.push(this);
    }

    subscribe(listener: () => void) {
      this.listener = listener;
      return () => {
        this.listener = null;
      };
    }

    setPrefetchPaused() {}
    update() {
      this.listener?.();
    }
    dispose() {}
    getSnapshot() {
      return null;
    }
    getTiles() {
      return [{ key: `${this.sourceIdentity}/tile` }];
    }
  }

  return { instances, MockScheduler };
});

vi.mock("./imageTileScheduler", () => ({
  ImageTileScheduler: schedulerMock.MockScheduler,
}));

const manifest: ImagePyramidManifestV1 = {
  schema: "aap-image-pyramid/v1",
  generation: 1,
  sourceFingerprint: "sha256:source",
  normalizationVersion: "exif-autorotate-srgb-v1",
  width: 1_024,
  height: 512,
  tileSize: 512,
  overlap: 1,
  format: "webp",
  levels: [{ level: 0, scaleFactor: 1, width: 1_024, height: 512, columns: 2, rows: 1 }],
  overview: { width: 512, height: 256, contentDigest: "sha256:overview" },
};

function pyramidSource(identity: string): WorkbenchImageSource {
  return {
    kind: "pyramid",
    taskId: identity,
    identity,
    generation: 1,
    manifest,
  };
}

function Harness({
  source,
  onRender,
}: {
  source: WorkbenchImageSource;
  onRender: (keys: string[]) => void;
}) {
  const state = useImageTileScheduler({
    source,
    viewport: { scale: 1, tx: 0, ty: 0 },
    viewportSize: { w: 800, h: 600 },
    deviceMemory: 8,
    devicePixelRatio: 1,
  });
  useEffect(() => {
    onRender(state.scheduler?.getTiles().map((tile) => tile.key) ?? []);
  }, [state, onRender]);
  return null;
}

describe("useImageTileScheduler", () => {
  it("切换 source 时不把已释放的旧瓦片交给新图层", async () => {
    const renders: string[][] = [];
    const onRender = (keys: string[]) => renders.push(keys);
    const { rerender } = render(<Harness source={pyramidSource("task-a")} onRender={onRender} />);

    await waitFor(() => expect(renders.some((keys) => keys.includes("task-a/tile"))).toBe(true));
    const switchStart = renders.length;

    rerender(<Harness source={pyramidSource("task-b")} onRender={onRender} />);
    await waitFor(() => expect(renders.some((keys) => keys.includes("task-b/tile"))).toBe(true));

    expect(renders.slice(switchStart).flat()).not.toContain("task-a/tile");
    expect(schedulerMock.instances[schedulerMock.instances.length - 1]?.sourceIdentity).toBe(
      "task-b",
    );
  });
});
