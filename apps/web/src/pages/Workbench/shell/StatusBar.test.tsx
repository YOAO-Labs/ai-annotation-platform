import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { StatusBar } from "./StatusBar";

it("keeps the cursor slot mounted when the pointer enters and leaves the image", () => {
  const props = {
    userBoxesCount: 0,
    aiBoxesCount: 0,
    activeClass: "person",
    imageWidth: 1280,
    imageHeight: 720,
    cursor: null,
    preannotationProgress: null,
    preannotationConn: "open" as const,
    preannotationRetries: 0,
  };
  const { rerender } = render(<StatusBar {...props} />);
  const slot = screen.getByTestId("status-cursor");
  expect(slot).toHaveTextContent("—");
  rerender(<StatusBar {...props} cursor={{ x: 0.5, y: 0.5 }} />);
  expect(screen.getByTestId("status-cursor")).toBe(slot);
  expect(slot).toHaveTextContent("(640, 360)");
  rerender(<StatusBar {...props} />);
  expect(screen.getByTestId("status-cursor")).toBe(slot);
  expect(slot).toHaveTextContent("—");
});

it.each([
  ["saving", "保存中…"],
  ["saved", "已保存"],
  ["local", "仅保存在本机"],
  ["sync-error", "同步失败"],
] as const)(
  "renders the derived save state %s independently from preannotation status",
  (state, label) => {
    render(
      <StatusBar
        userBoxesCount={0}
        aiBoxesCount={0}
        activeClass="person"
        imageWidth={null}
        imageHeight={null}
        cursor={null}
        preannotationProgress={null}
        preannotationConn="reconnecting"
        preannotationRetries={2}
        saveState={state}
      />,
    );

    expect(screen.getByTestId("workbench-save-state")).toHaveTextContent(label);
    expect(screen.getByText(/重连中/)).toBeInTheDocument();
  },
);

it("renders active tool gesture hint pill when toolHint prop is provided", () => {
  render(
    <StatusBar
      userBoxesCount={3}
      aiBoxesCount={1}
      activeClass="vehicle"
      imageWidth={1920}
      imageHeight={1080}
      cursor={null}
      preannotationProgress={null}
      preannotationConn="open"
      preannotationRetries={0}
      toolHint={{ tool: "polygon" }}
    />,
  );

  expect(screen.getByTestId("tool-gesture-hint-pill")).toBeInTheDocument();
  expect(screen.getByText("多边形")).toBeInTheDocument();
  expect(screen.getByText(/Enter\/双击闭合/)).toBeInTheDocument();
});
