import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { MyBatchItem, ReviewingBatchItem } from "@/api/dashboard";
import { BatchCardGrid } from "./BatchCardGrid";
import { ReviewBatchCardGrid } from "../Review/ReviewBatchCardGrid";

vi.mock("@/components/Thumbnail", () => ({ Thumbnail: () => null }));
const batches: Array<MyBatchItem & ReviewingBatchItem> = Array.from({ length: 25 }, (_, index) => ({
  batch_id: `batch-${index + 1}`,
  batch_name: `批次 ${index + 1}`,
  batch_display_id: `B-${String(index + 1).padStart(2, "0")}`,
  project_id: "p1",
  project_name: "项目一",
  status: "annotating",
  total_tasks: 2,
  in_progress_tasks: 1,
  review_tasks: 1,
  completed_tasks: 0,
  annotator: null,
  approved_tasks: 0,
  rejected_tasks: 0,
  progress_pct: 50,
  review_feedback: null,
  reviewed_at: null,
  reviewer: null,
}));

describe("batch card pagination", () => {
  it.each(["annotate", "review"])(
    "pages the complete sorted %s collection and resets when row count changes",
    (area) => {
      const onSelect = vi.fn();
      render(
        area === "annotate" ? (
          <BatchCardGrid batches={batches} onSelect={onSelect} />
        ) : (
          <ReviewBatchCardGrid batches={batches} onSelect={onSelect} />
        ),
      );
      expect(screen.getByText("批次 1")).toBeInTheDocument();
      expect(screen.queryByText("批次 21")).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "下一页" }));
      expect(screen.getByText("批次 21")).toBeInTheDocument();
      expect(screen.queryByText("批次 1")).not.toBeInTheDocument();
      fireEvent.click(screen.getByText("批次 21"));
      expect(onSelect).toHaveBeenCalledWith(batches[20]);
      fireEvent.change(screen.getByLabelText("每页批次数"), { target: { value: "10" } });
      expect(screen.getByText("批次 1")).toBeInTheDocument();
      expect(screen.queryByText("批次 11")).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
    },
  );
});
