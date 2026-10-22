import { apiClient } from "./client";
import type {
  TrainingConfigurationResolve,
  TrainingOrigin,
  TrainingRecipeOut,
  TrainingRecipePage,
  TrainingRecipeWrite,
  TrainingReuseCandidate,
} from "./generated/types.gen";
import type { ParameterValue } from "@/pages/Training/trainingConfiguration";
import type { FrozenTrainingInitialization, TrainingInitialization } from "./trainingRuns";
export type { TrainingOrigin, TrainingRecipeOut, TrainingRecipeWrite, TrainingReuseCandidate };
export type ReusableSnapshot = {
  initialization?: FrozenTrainingInitialization;
  framework: "ultralytics" | "libreyolo";
  framework_version: string;
  image_digest: string;
  base_model: { id: string; sha256: string; filename: string };
  configuration: {
    identity: { schema_id: string; schema_digest: string; task?: string };
    user_values: Record<string, ParameterValue>;
    effective_values: Record<string, ParameterValue>;
  };
};
export function initializationReference(
  pin?: FrozenTrainingInitialization | null,
): TrainingInitialization | undefined {
  return pin
    ? {
        kind: "trained_model",
        model_version_id: pin.model_version_id,
        source_digest: pin.source_digest,
      }
    : undefined;
}
const base = (id: string) => `/projects/${id}/training-recipes`;
export const trainingRecipesApi = {
  list: (id: string, offset = 0, archived = false, limit = 20) =>
    apiClient.get<TrainingRecipePage>(
      `${base(id)}?limit=${limit}&offset=${offset}&include_archived=${archived}`,
    ),
  get: (id: string, recipe: string, revision?: string) =>
    apiClient.get<TrainingRecipeOut>(
      `${base(id)}/${recipe}${revision ? `?revision_id=${encodeURIComponent(revision)}` : ""}`,
    ),
  revisions: (id: string, recipe: string, offset = 0, limit = 20) =>
    apiClient.get<TrainingRecipePage>(
      `${base(id)}/${recipe}/revisions?limit=${limit}&offset=${offset}`,
    ),
  save: (id: string, body: TrainingRecipeWrite, recipe?: string) =>
    apiClient.post<TrainingRecipeOut>(`${base(id)}${recipe ? `/${recipe}/revisions` : ""}`, body),
  archive: (id: string, recipe: string, revision: number) =>
    apiClient.post<TrainingRecipeOut>(
      `${base(id)}/${recipe}/archive?expected_head_revision=${revision}`,
    ),
  clone: (id: string, run: string, attempt?: string) =>
    apiClient.get<TrainingReuseCandidate>(
      `${base(id)}/clone/${run}${attempt ? `?attempt_id=${encodeURIComponent(attempt)}` : ""}`,
    ),
  resolve: (id: string, body: TrainingConfigurationResolve) =>
    apiClient.post<ReusableSnapshot>(`${base(id)}:resolve`, body),
};
export function recipeCandidate(recipe: TrainingRecipeOut): TrainingReuseCandidate {
  const revision = recipe.revision;
  return {
    snapshot: revision.snapshot,
    snapshot_digest: revision.snapshot_digest,
    origin: {
      kind: "recipe",
      recipe_id: recipe.id,
      revision_id: revision.id,
      digest: revision.snapshot_digest,
    },
    label: `${revision.name} · 修订 ${revision.ordinal}`,
    compatible: !recipe.archived && recipe.compatible,
    incompatibility: recipe.archived ? "配方已归档，不能新采用" : recipe.incompatibility,
  };
}
