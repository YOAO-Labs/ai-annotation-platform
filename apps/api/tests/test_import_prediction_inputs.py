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
