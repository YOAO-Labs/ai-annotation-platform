"""Start the standalone Mask content HTTP process."""

from __future__ import annotations

from .config import load_config


def main() -> None:
    import boto3
    from botocore.config import Config
    import uvicorn

    from aap_assets_mask_content.gc_storage import S3MaskGcStore
    from aap_assets_mask_content.storage import S3MaskContentStore

    config = load_config()

    client_options = {}
    if config.role == "cleanup":
        client_options["config"] = Config(
            connect_timeout=3,
            read_timeout=8,
            retries={"total_max_attempts": 1},
        )
    client = boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.access_key,
        aws_secret_access_key=config.secret_key,
        region_name=config.region,
        **client_options,
    )
    store = S3MaskContentStore(client, config.bucket)
    gc_store = (
        S3MaskGcStore(client, config.bucket) if config.role == "cleanup" else None
    )

    from .api import create_app

    uvicorn.run(
        create_app(store, config.token, config.role, gc_store=gc_store),
        host=config.host,
        port=config.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
