import { apiClient } from "./client";

export type VideoChapterSource = "manual" | "sampled";

export interface VideoChapter {
  id: string;
  dataset_item_id: string;
  start_frame: number;
  end_frame: number;
  title: string;
  color: string | null;
  metadata: Record<string, unknown>;
  frame_step: number | null;
  source: VideoChapterSource;
  created_by: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface VideoChapterCreatePayload {
  start_frame: number;
  end_frame: number;
  title: string;
  color?: string | null;
  metadata?: Record<string, unknown>;
  frame_step?: number | null;
  source?: VideoChapterSource | null;
}

export interface VideoChapterUpdatePayload {
  start_frame?: number;
  end_frame?: number;
  title?: string;
  color?: string | null;
  metadata?: Record<string, unknown>;
  frame_step?: number | null;
  source?: VideoChapterSource | null;
}

interface VideoChapterListResponse {
  chapters: VideoChapter[];
}

export const videoChaptersApi = {
  list: (datasetItemId: string, init?: RequestInit) =>
    apiClient
      .get<VideoChapterListResponse>(`/videos/${datasetItemId}/chapters`, init)
      .then((res) => res.chapters ?? []),
  create: (datasetItemId: string, payload: VideoChapterCreatePayload) =>
    apiClient.post<VideoChapter>(`/videos/${datasetItemId}/chapters`, payload),
  update: (datasetItemId: string, chapterId: string, payload: VideoChapterUpdatePayload) =>
    apiClient.patch<VideoChapter>(`/videos/${datasetItemId}/chapters/${chapterId}`, payload),
  delete: (datasetItemId: string, chapterId: string) =>
    apiClient.delete<void>(`/videos/${datasetItemId}/chapters/${chapterId}`),
};
