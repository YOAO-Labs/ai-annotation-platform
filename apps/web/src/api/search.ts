import { apiClient } from "./client";

export interface SearchProjectItem {
  id: string;
  display_id: string;
  name: string;
  type_key: string;
  data_type: string;
  type_label: string;
}

export interface SearchTaskItem {
  id: string;
  display_id: string;
  file_name: string;
  project_id: string;
  project_name: string;
}

export interface SearchDatasetItem {
  id: string;
  name: string;
  data_type: string;
}

export interface SearchMemberItem {
  id: string;
  name: string;
  email: string;
  role: string;
}

export interface SearchResponse {
  projects: SearchProjectItem[];
  tasks: SearchTaskItem[];
  datasets: SearchDatasetItem[];
  members: SearchMemberItem[];
}

export const searchApi = {
  query: (q: string, limit = 5, init?: RequestInit) => {
    const path = `/search?q=${encodeURIComponent(q)}&limit=${limit}`;
    return init ? apiClient.get<SearchResponse>(path, init) : apiClient.get<SearchResponse>(path);
  },
};
