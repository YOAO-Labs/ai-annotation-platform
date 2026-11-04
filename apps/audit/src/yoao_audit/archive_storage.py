"""Audit-only S3 adapter for the existing, pre-provisioned archive bucket."""

from __future__ import annotations

from botocore.config import Config


class AuditArchiveStorage:
    def __init__(
        self,
        *,
        endpoint_url: str,
        region: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
    ) -> None:
        import boto3

        self.audit_archive_bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(connect_timeout=5, read_timeout=60, max_pool_connections=2),
        )

    def ensure_bucket(self, bucket: str | None = None) -> None:
        target = bucket or self.audit_archive_bucket
        if target != self.audit_archive_bucket:
            raise ValueError(
                "Audit storage may access only its configured archive bucket"
            )
        self.client.head_bucket(Bucket=target)

    def list_objects(self, prefix: str, bucket: str | None = None) -> list[dict]:
        target = bucket or self.audit_archive_bucket
        if target != self.audit_archive_bucket:
            raise ValueError(
                "Audit storage may access only its configured archive bucket"
            )
        result: list[dict] = []
        continuation_token = None
        while True:
            request = {"Bucket": target, "Prefix": prefix}
            if continuation_token:
                request["ContinuationToken"] = continuation_token
            response = self.client.list_objects_v2(**request)
            for item in response.get("Contents", []):
                result.append(
                    {
                        "key": item["Key"],
                        "size": item["Size"],
                        "last_modified": item["LastModified"],
                    }
                )
            if not response.get("IsTruncated"):
                return result
            continuation_token = response["NextContinuationToken"]

    def close(self) -> None:
        self.client.close()
