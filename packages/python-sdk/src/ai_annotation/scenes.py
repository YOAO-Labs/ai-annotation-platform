"""Scene metadata and ordered frame trajectory API."""

from __future__ import annotations

from uuid import UUID

from ai_annotation._http import HttpTransport, _idempotency_headers
from ai_annotation.scene_models import Scene, SceneTrajectory, SceneUpdate

IdLike = str | UUID


class Scenes:
    def __init__(self, http: HttpTransport):
        self._http = http

    def list(self, dataset_id: IdLike) -> list[Scene]:
        resp = self._http.request(
            "GET", "/scenes", params={"dataset_id": str(dataset_id)}
        )
        return [Scene.model_validate(item) for item in resp.json()]

    def get(self, scene_id: IdLike) -> Scene:
        resp = self._http.request("GET", f"/scenes/{scene_id}")
        return Scene.model_validate(resp.json())

    def trajectory(self, scene_id: IdLike) -> SceneTrajectory:
        resp = self._http.request("GET", f"/scenes/{scene_id}/trajectory")
        return SceneTrajectory.model_validate(resp.json())

    def update(
        self,
        scene_id: IdLike,
        body: SceneUpdate,
        *,
        idempotency_key: IdLike | None = None,
    ) -> Scene:
        resp = self._http.request(
            "PATCH",
            f"/scenes/{scene_id}",
            json=body.model_dump(mode="json", exclude_unset=True),
            headers=_idempotency_headers(idempotency_key),
        )
        return Scene.model_validate(resp.json())
