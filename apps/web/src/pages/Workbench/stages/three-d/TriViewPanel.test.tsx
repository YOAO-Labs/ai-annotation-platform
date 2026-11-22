import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TriViewPanel } from "./TriViewPanel";

describe("TriViewPanel layer coverage", () => {
  it("三行连续填满面板，不给共享主画布留下透明 padding 或 gap", () => {
    render(
      <TriViewPanel
        scene={null}
        selected={null}
        editable={false}
        layoutKey="test"
        zoomByView={{ top: 1, side: 1, front: 1 }}
        onZoomChange={vi.fn()}
        onEditPsr={vi.fn()}
      />,
    );

    const panel = screen.getByTestId("tri-view-renderer-panel");
    expect(panel.className).toContain("gap-0");
    expect(panel.className).toContain("p-0");
    expect(panel.className).not.toContain("gap-1.5");
    expect(panel.className).not.toContain("p-1.5");
  });
});
