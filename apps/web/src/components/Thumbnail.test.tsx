import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Thumbnail } from "./Thumbnail";

describe("Thumbnail", () => {
  it("retries an image when its source changes after an error", () => {
    const view = render(<Thumbnail src="/expired.jpg" alt="task" />);
    fireEvent.error(screen.getByRole("img", { name: "task" }));
    expect(screen.queryByRole("img", { name: "task" })).not.toBeInTheDocument();

    view.rerender(<Thumbnail src="/refreshed.jpg" alt="task" />);
    expect(screen.getByRole("img", { name: "task" })).toHaveAttribute("src", "/refreshed.jpg");
  });
});
