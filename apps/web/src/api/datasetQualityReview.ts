import { apiClient } from "./client";
import type {
  QualityReviewPreview,
  QualityReviewBatchOut,
  QualityReviewBatchList,
  QualityReviewMemberList,
} from "./generated/types.gen";

export type ReviewBatch = QualityReviewBatchOut;
export type ReviewPreview = QualityReviewPreview;
const base = (dataset: string, run: string) =>
  `/datasets/${encodeURIComponent(dataset)}/quality-runs/${encodeURIComponent(run)}/review-batches`;
export const datasetQualityReviewApi = {
  preview: (dataset: string, run: string, body: ReviewPreview) =>
    apiClient.post<ReviewBatch>(`${base(dataset, run)}/preview`, body),
  list: (dataset: string, run: string, page: number, size: number, signal?: AbortSignal) =>
    apiClient.get<QualityReviewBatchList>(
      `${base(dataset, run)}?limit=${size}&offset=${page * size}`,
      { signal },
    ),
  get: (dataset: string, run: string, batch: string, signal?: AbortSignal) =>
    apiClient.get<ReviewBatch>(`${base(dataset, run)}/${encodeURIComponent(batch)}`, { signal }),
  members: (
    dataset: string,
    run: string,
    batch: string,
    page: number,
    size: number,
    signal?: AbortSignal,
  ) =>
    apiClient.get<QualityReviewMemberList>(
      `${base(dataset, run)}/${encodeURIComponent(batch)}/members?limit=${size}&offset=${page * size}`,
      { signal },
    ),
  execute: (dataset: string, run: string, batch: ReviewBatch) =>
    apiClient.post<ReviewBatch>(`${base(dataset, run)}/${encodeURIComponent(batch.id)}/execute`, {
      scope_digest: batch.scope_digest,
      range_policy: "snapshot",
    }),
  resume: (dataset: string, run: string, batch: string) =>
    apiClient.post<ReviewBatch>(`${base(dataset, run)}/${encodeURIComponent(batch)}/resume`, {}),
};
