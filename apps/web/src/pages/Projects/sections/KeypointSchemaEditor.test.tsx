import { useState } from "react";
import { describe, it, expect } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import type { KeypointSchema } from "@/types";
import { KeypointSchemaEditor, keypointNodePos } from "./KeypointSchemaEditor";

function Harness({ initial }: { initial?: KeypointSchema }) {
  const [schema, setSchema] = useState<KeypointSchema>(initial ?? { nodes: [], edges: [] });
  return (
    <>
      <KeypointSchemaEditor value={schema} onChange={setSchema} />
      <output data-testid="state">{JSON.stringify(schema)}</output>
    </>
  );
}

const readState = (): KeypointSchema => JSON.parse(screen.getByTestId("state").textContent || "{}");

describe("keypointNodePos", () => {
  it("有模板坐标时原样返回", () => {
    expect(keypointNodePos({ name: "a", x: 0.2, y: 0.8 }, 0, 3)).toEqual({ x: 0.2, y: 0.8 });
  });

  it("缺坐标时退化到圆周布局 (落在 [0,1] 内)", () => {
    const p = keypointNodePos({ name: "a" }, 1, 4);
    expect(p.x).toBeGreaterThanOrEqual(0);
    expect(p.x).toBeLessThanOrEqual(1);
    expect(p.y).toBeGreaterThanOrEqual(0);
    expect(p.y).toBeLessThanOrEqual(1);
  });
});

describe("KeypointSchemaEditor", () => {
  it("新增节点带默认模板坐标", () => {
    render(<Harness />);
    fireEvent.click(screen.getByText("新增节点"));
    const s = readState();
    expect(s.nodes).toHaveLength(1);
    expect(s.nodes[0].x).not.toBeNull();
    expect(s.nodes[0].y).not.toBeNull();
  });

  it("可编辑节点子标签", () => {
    render(<Harness initial={{ nodes: [{ name: "shoulder" }], edges: [] }} />);
    const sub = screen.getByLabelText("节点 1 子标签");
    fireEvent.change(sub, { target: { value: "left" } });
    expect(readState().nodes[0].sublabel).toBe("left");
  });

  it("依次点击两个节点连成一条边", () => {
    render(
      <Harness
        initial={{
          nodes: [
            { name: "a", x: 0.2, y: 0.2 },
            { name: "b", x: 0.8, y: 0.8 },
          ],
          edges: [],
        }}
      />,
    );
    const n0 = screen.getByTestId("keypoint-node-0");
    const n1 = screen.getByTestId("keypoint-node-1");
    fireEvent.pointerDown(n0);
    fireEvent.pointerUp(n0);
    fireEvent.pointerDown(n1);
    fireEvent.pointerUp(n1);
    expect(readState().edges).toEqual([[0, 1]]);
  });

  it("删除节点会重映射连线索引", () => {
    render(
      <Harness
        initial={{
          nodes: [{ name: "a" }, { name: "b" }, { name: "c" }],
          edges: [
            [0, 2],
            [1, 2],
          ],
        }}
      />,
    );
    const list = screen.getByText("关键点节点 (3)").closest("div")!.parentElement!;
    const firstRow = within(list).getByLabelText("节点 1 名称").closest("li")!;
    fireEvent.click(within(firstRow).getByTitle("删除节点"));
    const s = readState();
    expect(s.nodes).toHaveLength(2);
    expect(s.edges).toEqual([[0, 1]]);
  });
});
