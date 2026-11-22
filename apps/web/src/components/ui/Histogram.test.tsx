import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Histogram } from "./Histogram";

function getBars(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll("div")).filter((el) => {
    const s = (el as HTMLElement).style;
    return s.borderRadius === "2px 2px 0 0";
  }) as HTMLElement[];
}

describe("Histogram", () => {
  it("values N 个 → 渲染 N 个 bar", () => {
    const { container } = render(<Histogram values={[1, 2, 3, 4]} />);
    expect(getBars(container).length).toBe(4);
  });

  it("values 全 0 仍渲染同样数量 bar（minHeight 1px 兜底）", () => {
    const { container } = render(<Histogram values={[0, 0, 0]} />);
    expect(getBars(container).length).toBe(3);
  });

  it("空数组 → 0 个 bar，不抛错", () => {
    const { container } = render(<Histogram values={[]} />);
    expect(getBars(container).length).toBe(0);
  });

  it("xLabels 仅首末两个出现在轴下方", () => {
    render(<Histogram values={[1, 2, 3, 4]} xLabels={["A", "B", "C", "Z"]} />);
    expect(screen.getByText("A")).toBeInTheDocument();
    expect(screen.getByText("Z")).toBeInTheDocument();
    expect(screen.queryByText("B")).not.toBeInTheDocument();
    expect(screen.queryByText("C")).not.toBeInTheDocument();
  });

  it("markers 渲染对应 label 文字", () => {
    render(
      <Histogram
        values={[1, 2, 3, 4, 5]}
        markers={[
          { index: 1, label: "p50" },
          { index: 4, label: "p95" },
        ]}
      />,
    );
    expect(screen.getByText("p50")).toBeInTheDocument();
    expect(screen.getByText("p95")).toBeInTheDocument();
  });

  it("自定义 height 透传到 bar 容器 style.height", () => {
    const { container } = render(<Histogram values={[1, 2, 3]} height={120} />);
    const root = container.firstChild as HTMLElement;
    const wrap = root.firstChild as HTMLElement;
    expect(wrap.style.height).toBe("120px");
  });

  it("title 提示包含 xLabels 对应值", () => {
    const { container } = render(<Histogram values={[7, 9]} xLabels={["AM", "PM"]} />);
    const bars = getBars(container);
    expect(bars[0].title).toBe("AM: 7");
    expect(bars[1].title).toBe("PM: 9");
  });

  it("无 xLabels 时 title 为纯数值", () => {
    const { container } = render(<Histogram values={[3]} />);
    const bars = getBars(container);
    expect(bars[0].title).toBe("3");
  });
});
