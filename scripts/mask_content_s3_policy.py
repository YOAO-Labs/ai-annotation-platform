"""Render the fixed Mask byte owner's content or cleanup S3 identity policy."""

import argparse
import json
import re


def mask_content_policy(bucket: str, *, cleanup: bool = False) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise ValueError("invalid S3 bucket name")
    arn = f"arn:aws:s3:::{bucket}"
    statements = [
        {
            "Effect": "Allow",
            "Action": ["s3:GetBucketLocation"],
            "Resource": [arn],
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:DeleteObject" if cleanup else "s3:PutObject",
            ],
            "Resource": [f"{arn}/raster-masks/sha256/*"],
        },
    ]
    if cleanup:
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": [f"{arn}/raster-mask-gc/v1/*"],
            }
        )
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": [arn],
                "Condition": {
                    "StringLike": {
                        "s3:prefix": ["raster-masks/sha256/*", "raster-mask-gc/v1/*"]
                    }
                },
            }
        )
        # An explicit deny also fences versions under a broader parent identity.
        statements.append(
            {
                "Effect": "Deny",
                "Action": ["s3:DeleteObjectVersion"],
                "Resource": [
                    f"{arn}/raster-masks/sha256/*",
                    f"{arn}/raster-mask-gc/v1/*",
                ],
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--role", choices=("content", "cleanup"), required=True)
    options = parser.parse_args()
    print(
        json.dumps(
            mask_content_policy(options.bucket, cleanup=options.role == "cleanup"),
            indent=2,
        )
    )
