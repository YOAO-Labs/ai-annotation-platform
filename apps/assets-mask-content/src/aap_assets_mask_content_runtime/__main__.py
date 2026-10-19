"""Start the standalone Mask content HTTP process."""

from __future__ import annotations

from .config import load_config


def main() -> None:
    import boto3
    import uvicorn

    from aap_assets_mask_content.storage import S3MaskContentStore

    config = load_config()

    client = boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.access_key,
        aws_secret_access_key=config.secret_key,
        region_name=config.region,
    )
    store = S3MaskContentStore(client, config.bucket)

    from .api import create_app

    uvicorn.run(
        create_app(store, config.token, config.role),
        host=config.host,
        port=config.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
