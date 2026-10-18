"""v0.15.0 · 给已导入但缺 ego pose 的 nuScenes dataset 回填 scene_frame_poses。

v0.15.0 之前由 import_nuscenes_scene.py 导入的 dataset(如本机
DS-NU-nu-scene-0061 / DS-NU-nu-mini-multi)只有 scene + frame_index,
没有逐帧 ego pose。本脚本按 scene.source_metadata.scene_token 反查
nuScenes 根目录的 ego_pose.json / sample_data.json,upsert 每帧位姿
(幂等,可重跑;upsert 语义同 importer)。

用法:
    cd apps/api
    PYTHONPATH=. uv run python scripts/backfill_frame_poses.py \
        --dataset-id DS-NU-nu-scene-0061 \
        --nuscenes-root ~/data/nuscenes-mini

`--dataset-id` 接受 UUID 或 display_id(DS-NU-*)。版本子目录默认取
scene.source_metadata.nuscenes_version,缺失时用 --version(默认 v1.0-mini)。
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from pathlib import Path

from scripts.import_nuscenes_scene import (
    _index_by_token,
    _key_sample_data_by_channel,
    _load_table,
    _ordered_samples,
)


async def backfill_frame_poses(
    db,
    *,
    dataset_ref: str,
    nuscenes_root: Path,
    version: str = "v1.0-mini",
) -> list[dict]:
    """对 dataset 下所有 source_format=nuscenes 的 scene 回填逐帧 ego pose。

    返回 [{"name", "poses"}...];scene_token 在 nuScenes 元数据中找不到时
    报错(数据与库不匹配,不静默跳过)。
    """
    from sqlalchemy import select

    from app.db.models.dataset import Dataset, Scene
    from aap_assets_catalog.spatial_values import FramePose
    from app.services import scene_pose as scene_pose_svc

    try:
        dataset_id = uuid.UUID(dataset_ref)
        ds = await db.get(Dataset, dataset_id)
    except ValueError:
        ds = await db.scalar(select(Dataset).where(Dataset.display_id == dataset_ref))
    if ds is None:
        raise ValueError(f"dataset {dataset_ref!r} 不存在")

    scenes = (
        (
            await db.execute(
                select(Scene)
                .where(Scene.dataset_id == ds.id)
                .where(Scene.source_format == "nuscenes")
                .order_by(Scene.name)
            )
        )
        .scalars()
        .all()
    )
    if not scenes:
        raise ValueError(
            f"dataset {ds.display_id} 下无 source_format=nuscenes 的 scene"
        )

    # 元数据表按 version 子目录缓存(同 dataset 各 scene 通常同 version)
    tables_cache: dict[str, dict] = {}

    def _tables(ver: str) -> dict:
        if ver not in tables_cache:
            meta_dir = nuscenes_root / ver
            if not meta_dir.exists():
                raise FileNotFoundError(f"nuScenes 元数据目录缺失: {meta_dir}")
            tables_cache[ver] = {
                "scene_by_token": _index_by_token(_load_table(meta_dir, "scene")),
                "samples_by_token": _index_by_token(_load_table(meta_dir, "sample")),
                "sample_data": _load_table(meta_dir, "sample_data"),
                "cs_by_token": _index_by_token(
                    _load_table(meta_dir, "calibrated_sensor")
                ),
                "sensor_by_token": _index_by_token(_load_table(meta_dir, "sensor")),
                "ego_by_token": _index_by_token(_load_table(meta_dir, "ego_pose")),
            }
        return tables_cache[ver]

    report: list[dict] = []
    for scene in scenes:
        meta = scene.source_metadata or {}
        scene_token = meta.get("scene_token")
        if not scene_token:
            raise ValueError(
                f"scene {scene.name!r} 的 source_metadata 缺 scene_token,无法反查"
            )
        t = _tables(meta.get("nuscenes_version") or version)
        scene_row = t["scene_by_token"].get(scene_token)
        if scene_row is None:
            raise ValueError(
                f"scene_token {scene_token!r}(scene {scene.name!r})在 nuScenes "
                "元数据中不存在;--nuscenes-root 是否指向导入时的同一份数据?"
            )

        samples = _ordered_samples(scene_row, t["samples_by_token"])
        poses: list[FramePose] = []
        for frame_idx, sample in enumerate(samples):
            by_channel = _key_sample_data_by_channel(
                sample["token"],
                t["sample_data"],
                t["cs_by_token"],
                t["sensor_by_token"],
            )
            lidar_sd = None
            for sd in by_channel.values():
                cs = t["cs_by_token"][sd["calibrated_sensor_token"]]
                if t["sensor_by_token"][cs["sensor_token"]]["modality"] == "lidar":
                    lidar_sd = sd
                    break
            if lidar_sd is None:
                raise ValueError(
                    f"scene {scene.name!r} sample {sample['token']!r} 无 lidar keyframe"
                )
            ego = t["ego_by_token"][lidar_sd["ego_pose_token"]]
            poses.append(
                FramePose(
                    frame_index=frame_idx,
                    timestamp_us=int(lidar_sd["timestamp"]),
                    ego_translation=[float(v) for v in ego["translation"]],
                    ego_rotation=[float(v) for v in ego["rotation"]],
                    source_metadata={"ego_pose_token": lidar_sd["ego_pose_token"]},
                )
            )

        await scene_pose_svc.upsert_frame_poses(db, scene_id=scene.id, poses=poses)
        report.append({"name": scene.name, "poses": len(poses)})

    return report


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="给已导入的 nuScenes dataset 回填逐帧 ego pose(scene_frame_poses)。"
    )
    parser.add_argument(
        "--dataset-id",
        required=True,
        help="dataset UUID 或 display_id(如 DS-NU-nu-scene-0061)",
    )
    parser.add_argument(
        "--nuscenes-root",
        required=True,
        help="nuScenes 根目录(含 <version>/*.json)",
    )
    parser.add_argument(
        "--version",
        default="v1.0-mini",
        help="scene.source_metadata 缺 nuscenes_version 时的回退版本子目录",
    )
    args = parser.parse_args()

    from app.db.base import async_session

    async with async_session() as db:
        report = await backfill_frame_poses(
            db,
            dataset_ref=args.dataset_id,
            nuscenes_root=Path(args.nuscenes_root).expanduser(),
            version=args.version,
        )
        await db.commit()

    print("=== ego pose 回填完成 ===")
    for s in report:
        print(f"  scene {s['name']}: {s['poses']} poses")


if __name__ == "__main__":
    asyncio.run(main())
