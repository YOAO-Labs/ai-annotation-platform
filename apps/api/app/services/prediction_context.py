from __future__ import annotations

import io


def load_task_image(task):
    """Read the original task image from object storage as an RGB PIL Image for ROI crops.

    Read through StorageService's boto3 client and internal endpoint, not through a presigned
    URL or host rewrite, which may be unreachable from workers.
    """
    from PIL import Image

    from app.services.storage import StorageService

    storage = StorageService()
    bucket = storage.datasets_bucket if task.dataset_item_id else storage.bucket
    body = None
    try:
        obj = storage.client.get_object(Bucket=bucket, Key=task.file_path)
        body = obj["Body"]
        raw = body.read()
        with Image.open(io.BytesIO(raw)) as image:
            return image.convert("RGB")
    finally:
        try:
            if body is not None:
                body.close()
        finally:
            storage.client.close()
