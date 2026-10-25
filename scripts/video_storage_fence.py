"""Observe the same scoped S3 write before and after external credential fencing.

No IAM administration or object cleanup is performed here. The caller owns the
scoped principal, bucket and unique probe key, and retains the baseline version
for cleanup. A missing bucket or unavailable transport is not a credential fence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import re
from typing import Any

from scripts.video_worker_fence import LocalFenceRefused


S3_PROBE_BODY = b"owned-legacy-video-fence-probe\n"


class ScopedStorageFenceProbe:
    """Hold a real authorized baseline and the same client entirely in memory."""

    def __init__(self, client, bucket: str, key: str) -> None:
        if not bucket or not re.fullmatch(
            r"projects/[0-9a-f-]{36}/guide/legacy-video-fence-[0-9a-f]{32}\.txt", key
        ):
            raise LocalFenceRefused("s3_probe_scope_invalid")
        self._client = client
        self._bucket = bucket
        self._key = key
        self._started_at = datetime.now(timezone.utc).isoformat()
        try:
            result = client.put_object(Bucket=bucket, Key=key, Body=S3_PROBE_BODY)
            response = client.get_object(Bucket=bucket, Key=key)
            body = response["Body"]
            try:
                if body.read() != S3_PROBE_BODY:
                    raise LocalFenceRefused("s3_probe_content_mismatch")
            finally:
                body.close()
        except LocalFenceRefused:
            raise
        except Exception:
            raise LocalFenceRefused("s3_authorized_probe_unavailable") from None
        self._baseline = {
            "operation": "PutObject",
            "key": key,
            "payload_sha256": sha256(S3_PROBE_BODY).hexdigest(),
            "succeeded_before_revoke": True,
            "version_id": result.get("VersionId"),
        }

    @property
    def baseline(self) -> dict[str, Any]:
        return dict(self._baseline)

    def observe_denied(self) -> dict[str, Any]:
        from botocore.exceptions import ClientError

        try:
            self._client.put_object(
                Bucket=self._bucket, Key=self._key, Body=S3_PROBE_BODY
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code not in {
                "InvalidAccessKeyId",
                "AccessDenied",
                "AllAccessDisabled",
                "403",
            }:
                raise LocalFenceRefused(
                    "stale_s3_credential_probe_inconclusive"
                ) from None
        except Exception:
            raise LocalFenceRefused("stale_s3_credential_probe_inconclusive") from None
        else:
            raise LocalFenceRefused("stale_s3_credential_still_works")
        return {
            "schema": "aap-local-storage-fence-facts/v1",
            "bucket": self._bucket,
            "write_probe": self.baseline,
            "denial_code": str(code),
            "old_credential_denied": True,
            "observation_started_at": self._started_at,
            "observation_completed_at": datetime.now(timezone.utc).isoformat(),
            "other_storage_principals_fenced": False,
            "jobs_legacy_retirement_proof_created": False,
        }
