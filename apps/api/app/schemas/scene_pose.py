"""v0.15.0 · Scene frame pose schemas

逐帧 ego pose(ego→global)的 Pydantic 表示;trajectory API / manifest 透出共用。
"""

from uuid import UUID

from pydantic import BaseModel, Field

from aap_assets_catalog.spatial_values import FramePose


class TrajectoryResponse(BaseModel):
    """scene 的有序逐帧轨迹(按 frame_index 升序);无位姿 scene → poses=[]。"""

    scene_id: UUID
    poses: list[FramePose] = Field(default_factory=list)
