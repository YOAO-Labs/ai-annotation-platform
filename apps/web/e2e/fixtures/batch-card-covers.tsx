import { useState } from "react";
import { createRoot } from "react-dom/client";
import type { MyBatchItem, ReviewingBatchItem } from "@/api/dashboard";
import { Thumbnail } from "@/components/Thumbnail";
import { BatchCardGrid } from "@/pages/Annotate/BatchCardGrid";
import { ReviewBatchCardGrid } from "@/pages/Review/ReviewBatchCardGrid";
import "@/styles/shadcn.css";

const blurhash = "LEHV6nWB2yk8pyo0adR*.7kCMdnj";
const samples = [
  { name: "图片成功", thumbnail_url: "/test-cover/success.svg", cover_blurhash: blurhash },
  { name: "预览保留", thumbnail_url: "/test-cover/with-preview.svg", cover_blurhash: blurhash },
  { name: "图片失败", thumbnail_url: "/test-cover/failure.svg" },
  { name: "没有封面" },
];
const batches: Array<MyBatchItem & ReviewingBatchItem> = samples.map(({ name, ...media }, i) => ({
  ...media,
  batch_id: `batch-${i}`,
  batch_display_id: `B-${i}`,
  batch_name: name,
  project_id: "project",
  project_name: "项目",
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
const annotator = { id: "u1", email: "u1@example.com", name: "小王", avatar_initial: "王" };
const review = new URLSearchParams(location.search).get("area") === "review";

function Fixture() {
  const [showAnnotator, setShowAnnotator] = useState(false);
  return (
    <>
      <table>
        <tbody>
          <tr>
            <td data-testid="table-thumbnail">
              <Thumbnail src={null} width={40} height={40} />
            </td>
            <td data-testid="default-thumbnail">
              <Thumbnail src={null} />
            </td>
          </tr>
        </tbody>
      </table>
      {review ? (
        <>
          <button
            type="button"
            aria-pressed={showAnnotator}
            onClick={() => setShowAnnotator(!showAnnotator)}
          >
            显示标注员
          </button>
          <ReviewBatchCardGrid
            batches={showAnnotator ? batches.map((b) => ({ ...b, annotator })) : batches}
            onSelect={() => {}}
          />
        </>
      ) : (
        <BatchCardGrid batches={batches} onSelect={() => {}} />
      )}
    </>
  );
}

createRoot(document.getElementById("root")!).render(<Fixture />);
