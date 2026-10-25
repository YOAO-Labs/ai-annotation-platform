"""Frame delivery preserves bytes and the established image lease contract."""

import io
from types import SimpleNamespace

from PIL import Image
import pytest

from app.services import prediction_inputs as service
from tests.test_prediction_inputs import get_record, input_world as frame_world

input_world = frame_world


@pytest.mark.parametrize(
    "format,media_type,extension",
    [("PNG", "image/png", "png"), ("JPEG", "image/jpeg", "jpg")],
)
async def test_frame_delivery_preserves_exact_encoded_bytes(
    input_world,
    format,
    media_type,
    extension,  # noqa: F811
):
    world = input_world
    with Image.new("RGB", (4, 3)) as image, io.BytesIO() as stream:
        image.save(stream, format=format)
        raw = stream.getvalue()
    inputs = await service.PredictionInputs.create(
        world.factory,
        owner_id=world.owner_id,
        owner_kind="predict_test",
        task_id=world.task_id,
    )
    try:
        url = await inputs.upload_frame(SimpleNamespace(id=world.task_id), raw)
        assert (
            url
            and inputs.record.source_ref is None
            and inputs.record.contract_version == 2
        )
        assert len(world.objects) == 1
        (bucket, key), value = next(iter(world.objects.items()))
        assert bucket == inputs.record.bucket and key.startswith(
            inputs.prefix + "original/"
        )
        assert key.endswith("." + extension) and value == raw
        assert world.metadata[bucket, key]["aap-prediction-lease"] == str(
            inputs.record.id
        )
        entry = next(
            iter(
                (await get_record(world, inputs.record.id))
                .inventory["objects"]
                .values()
            )
        )
        assert entry["media_type"] == media_type
    finally:
        await inputs.close()


async def test_non_image_secondary_delivers_existing_crop_inside_lease(
    db_session, super_admin, monkeypatch
):
    from contextlib import asynccontextmanager
    from tests.factory import create_project, create_task
    from tests.test_secondary_inference import _fake_backend, _mk_box, _patch_predict
    from app.services.secondary_inference import run_secondary_inference

    actor = super_admin[0]
    project = await create_project(db_session, owner_id=actor.id)
    task = await create_task(db_session, project_id=project.id)
    task.file_type = "video"
    await db_session.flush()
    annotation = await _mk_box(db_session, task.id, actor.id)
    image = Image.new("RGB", (200, 200))
    monkeypatch.setattr("app.workers.tasks._load_task_image", lambda _: image)
    _patch_predict(monkeypatch, [])
    marker, captured = object(), {}

    @asynccontextmanager
    async def lease(factory, **owner):
        assert factory is marker
        captured.update(owner)

        async def upload_crop(saved_task, index, content):
            assert saved_task.id == task.id and index == 0
            captured["bytes"] = content
            return "http://unit-input/0.jpg"

        try:

            async def close():
                captured["closed"] = True

            yield SimpleNamespace(upload_crop=upload_crop, close=close)
        finally:
            captured["closed"] = True

    monkeypatch.setattr(service, "prediction_inputs", lease)
    await run_secondary_inference(
        db_session,
        input_session_factory=marker,
        annotation=annotation,
        task=task,
        backend=_fake_backend(),
        write_target="geometry",
        write_keys=None,
        label=None,
        model_id="det",
        model_variants=None,
        params=None,
        task_type="detection",
        prompt=None,
        class_filter=None,
        pad=0.08,
        user_id=actor.id,
    )
    assert captured["closed"] and captured["owner_kind"] == "secondary_inference"
    assert "source_ref" not in captured and captured["task_id"] == task.id
    with Image.open(io.BytesIO(captured["bytes"])) as crop:
        assert crop.format == "JPEG"
    with pytest.raises(ValueError):
        image.getpixel((0, 0))
