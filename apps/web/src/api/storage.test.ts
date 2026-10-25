import { beforeEach, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn() }));
vi.mock("./client", () => ({ apiClient: api }));
import { storageApi } from "./storage";

beforeEach(() => vi.clearAllMocks());

it("pages avatar binding candidates with cancellation and uses reviewed apply payloads", () => {
  const signal = new AbortController().signal;
  storageApi.avatarBindingCandidates(40, signal, 20);
  expect(api.get).toHaveBeenCalledWith(
    "/storage-locations/bindings/avatars/candidates?limit=20&offset=40",
    { signal },
  );
  const payload = {
    expected_generation: 3,
    target_location_id: "target",
    target_config_revision: 7,
  };
  storageApi.previewAvatarBinding(payload);
  expect(api.post).toHaveBeenCalledWith(
    "/storage-locations/bindings/avatars/change-preview",
    payload,
  );
  storageApi.applyAvatarBinding({ ...payload, impact_token: "reviewed" });
  expect(api.patch).toHaveBeenCalledWith("/storage-locations/bindings/avatars", {
    ...payload,
    impact_token: "reviewed",
  });
});

it.each(["import", "datasets", "media-cache", "annotations", "training"] as const)(
  "routes %s binding review and confirmation to its own endpoints",
  (purpose) => {
    const signal = new AbortController().signal;
    storageApi.bindingCandidates(purpose, 40, signal, 20);
    expect(api.get).toHaveBeenCalledWith(
      `/storage-locations/bindings/${purpose}/candidates?limit=20&offset=40`,
      { signal },
    );
    const payload = {
      expected_generation: 3,
      target_location_id: "target",
      target_config_revision: 7,
    };
    storageApi.previewBinding(purpose, payload);
    expect(api.post).toHaveBeenCalledWith(
      `/storage-locations/bindings/${purpose}/change-preview`,
      payload,
    );
    storageApi.applyBinding(purpose, { ...payload, impact_token: `${purpose}-reviewed` });
    expect(api.patch).toHaveBeenCalledWith(`/storage-locations/bindings/${purpose}`, {
      ...payload,
      impact_token: `${purpose}-reviewed`,
    });
  },
);
