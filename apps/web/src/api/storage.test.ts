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

it("routes import binding review and confirmation to its own endpoints", () => {
  const signal = new AbortController().signal;
  storageApi.bindingCandidates("import", 40, signal, 20);
  expect(api.get).toHaveBeenCalledWith(
    "/storage-locations/bindings/import/candidates?limit=20&offset=40",
    { signal },
  );
  const payload = {
    expected_generation: 3,
    target_location_id: "target",
    target_config_revision: 7,
  };
  storageApi.previewBinding("import", payload);
  expect(api.post).toHaveBeenCalledWith(
    "/storage-locations/bindings/import/change-preview",
    payload,
  );
  storageApi.applyBinding("import", { ...payload, impact_token: "import-reviewed" });
  expect(api.patch).toHaveBeenCalledWith("/storage-locations/bindings/import", {
    ...payload,
    impact_token: "import-reviewed",
  });
});
