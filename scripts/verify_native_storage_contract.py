"""Qualify the locked Native-only S3 target before any database acceptance.

Failed runs retain their exact owner journal for separately approved retirement.
This does not qualify a general MinIO replacement or a production deployment.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
import urllib.request

from botocore.exceptions import ClientError

from storage_s3_contract import (
    Budget,
    ContractError,
    load_lock,
    public_bytes,
    read_body,
    require,
    safe_error,
)
from native_storage_identity import (
    NATIVE_CONNECTION_BASE_PREFIX,
    _native_connection_storage_policy,
    _native_source_storage_policy,
    client_for,
    identity_state,
    policy_fingerprint,
    provision_identity,
)
from verify_storage_candidates import (
    ROOT,
    Owner,
    private_write,
    ready,
    start_candidate,
)


PAGE_SIZE = 100


def expect_error(call, status, codes):
    try:
        response = call()
    except ClientError as exc:
        require(
            exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == status
            and exc.response.get("Error", {}).get("Code") in codes,
            "unexpected_error_identity",
        )
        return
    if isinstance(response, dict) and "Body" in response:
        response["Body"].close()
    raise ContractError("required_error_missing")


def upload_pages(client, bucket, expected, *, cursor=None):
    """Prove bounded pages and exact completeness, including a same-key cursor."""
    observed = set()
    cursors = set()
    counts = []
    first_cursor = None
    for _ in range(4):
        params = {"Bucket": bucket, "MaxUploads": PAGE_SIZE}
        if cursor is not None:
            params.update(KeyMarker=cursor[0], UploadIdMarker=cursor[1])
        page = client.list_multipart_uploads(**params)
        items = page.get("Uploads", [])
        require(
            isinstance(items, list) and len(items) <= PAGE_SIZE, "upload_page_bound"
        )
        require(page.get("MaxUploads") == PAGE_SIZE, "upload_page_limit_echo")
        require(page.get("Bucket") == bucket, "upload_page_bucket")
        require(
            (page.get("KeyMarker", ""), page.get("UploadIdMarker", ""))
            == (cursor or ("", "")),
            "upload_page_cursor_echo",
        )
        pairs = []
        for item in items:
            pair = (item.get("Key"), item.get("UploadId"))
            require(
                all(isinstance(part, str) and part for part in pair), "upload_identity"
            )
            require(pair in expected and pair not in observed, "upload_page_identity")
            observed.add(pair)
            pairs.append(pair)
        counts.append(len(items))
        truncated = page.get("IsTruncated")
        require(isinstance(truncated, bool), "upload_page_terminal")
        if not truncated:
            require(
                not page.get("NextKeyMarker") and not page.get("NextUploadIdMarker"),
                "upload_terminal_cursor",
            )
            require(observed == expected, "upload_enumeration_incomplete")
            return counts, first_cursor
        next_cursor = (page.get("NextKeyMarker"), page.get("NextUploadIdMarker"))
        require(
            pairs and next_cursor == pairs[-1] and next_cursor not in cursors,
            "upload_cursor_progress",
        )
        cursors.add(next_cursor)
        if first_cursor is None:
            first_cursor = next_cursor
        cursor = next_cursor
    raise ContractError("upload_page_budget")


def verify_scoped_access(client, iam, identity, bucket, key, other_bucket):
    denied = {"AccessDenied", "403"}
    expect_error(
        lambda: client.put_object(Bucket=bucket, Key="foreign/object", Body=b"x"),
        403,
        denied,
    )
    expect_error(
        lambda: client.get_object(Bucket=other_bucket, Key="foreign/object"),
        403,
        denied,
    )
    expect_error(
        lambda: client.delete_object(Bucket=other_bucket, Key="foreign/object"),
        403,
        denied,
    )
    expect_error(
        lambda: client.list_objects_v2(Bucket=bucket, Prefix="foreign/"), 403, denied
    )
    expect_error(
        lambda: client.list_object_versions(Bucket=bucket, Prefix="foreign/"),
        403,
        denied,
    )
    expect_error(lambda: iam.list_users(), 403, denied)
    expect_error(lambda: iam.create_access_key(UserName=identity[0]), 403, denied)
    prefix = key.rsplit("/", 1)[0] + "/"
    listed = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=10)
    require(
        not listed.get("IsTruncated")
        and key in {i["Key"] for i in listed.get("Contents", [])},
        "scoped_list",
    )
    versions = client.list_object_versions(Bucket=bucket, Prefix=prefix, MaxKeys=10)
    require(
        versions.get("IsTruncated") is False
        and key in {item["Key"] for item in versions.get("Versions", [])},
        "scoped_version_list",
    )


def _read_saved_object(client, params, etag, body, budget):
    response = client.get_object(**params, IfMatch=etag)
    try:
        require(response.get("ETag") == etag, "get_etag_receipt")
        require(
            response.get("VersionId") == params.get("VersionId"),
            "get_version_receipt",
        )
    except Exception:
        response["Body"].close()
        raise
    read_body(response, budget, expected=body)


def verify_object(client, bucket, key, body, budget, *, versioned):
    put = client.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch="*")
    etag = put["ETag"]
    require(isinstance(etag, str) and len(etag) > 2, "raw_etag")
    version = put.get("VersionId")
    require(
        isinstance(version, str) and bool(version) and version != "null"
        if versioned
        else version in (None, "null"),
        "raw_version_receipt",
    )
    params = {"Bucket": bucket, "Key": key}
    if version is not None:
        params["VersionId"] = version
    expect_error(
        lambda: client.put_object(
            Bucket=bucket, Key=key, Body=b"overwrite", IfNoneMatch="*"
        ),
        412,
        {"PreconditionFailed", "412"},
    )
    for operation in (client.head_object, client.get_object):
        expect_error(
            lambda operation=operation: operation(**params, IfMatch='"mismatch"'),
            412,
            {"PreconditionFailed", "412"},
        )
    head = client.head_object(**params, IfMatch=etag)
    require(
        head["ETag"] == etag and head["ContentLength"] == len(body), "conditional_head"
    )
    require(head.get("VersionId") == version, "head_version_receipt")
    _read_saved_object(client, params, etag, body, budget)
    expect_error(
        lambda: client.delete_object(**params, IfMatch='"mismatch"'),
        412,
        {"PreconditionFailed", "412"},
    )
    _read_saved_object(client, params, etag, body, budget)
    url = client.generate_presigned_url("get_object", Params=params, ExpiresIn=60)
    with urllib.request.urlopen(url, timeout=15) as response:
        data = response.read(len(body) + 1)
        budget.consume("download", len(data))
        require(response.status == 200 and data == body, "headerless_signed_read")
    return params, etag


def qualify(owner, lock, candidate, budget, report):
    from urllib.parse import urlsplit

    def stage(name):
        report["stage"] = name
        print(json.dumps({"stage": name}), flush=True)
        budget.check()

    stage("provider_start")
    container, startup_client, _, _, _, credentials = start_candidate(
        owner,
        lock,
        candidate,
        budget,
        report,
        native_identity_management=True,
        publish_on_bridge=True,
    )
    endpoint = startup_client.meta.endpoint_url
    startup_client.close()
    os.environ["NO_PROXY"] = "127.0.0.1,localhost," + urlsplit(endpoint).hostname
    os.environ["no_proxy"] = os.environ["NO_PROXY"]
    report["publication"] = "owned_private_bridge"
    clients = []
    root = client_for("s3", endpoint, credentials, budget)
    iam = client_for("iam", endpoint, credentials, budget)
    clients += [root, iam]
    nonce = owner.data["nonce"][:16]
    buckets = ["native-contract-" + nonce + suffix for suffix in ("-a", "-b")]
    identities = []
    tags = {"TagSet": [{"Key": "aap-owner", "Value": owner.data["nonce"]}]}
    try:
        stage("bucket_and_identity_contract")
        require(root.list_buckets()["Buckets"] == [], "provider_empty_birth")
        for bucket in buckets:
            root.create_bucket(Bucket=bucket)
            root.put_bucket_tagging(Bucket=bucket, Tagging=tags)
            root.put_object(Bucket=bucket, Key="foreign/object", Body=b"foreign")
        require(
            not root.get_bucket_versioning(Bucket=buckets[0]).get("Status"),
            "unversioned_birth",
        )
        root.put_bucket_versioning(
            Bucket=buckets[1], VersioningConfiguration={"Status": "Enabled"}
        )
        policies = [
            _native_source_storage_policy({"bucket": buckets[0]}),
            _native_connection_storage_policy({"bucket": buckets[1]}),
        ]
        scoped = []
        for index, policy in enumerate(policies):
            identity = provision_identity(
                iam, "aap-native-" + nonce + str(index), policy
            )
            identities.append(identity)
            pair = (identity[1]["AccessKeyId"], identity[1]["SecretAccessKey"])
            client = client_for("s3", endpoint, pair, budget)
            scoped_iam = client_for("iam", endpoint, pair, budget)
            clients += [client, scoped_iam]
            scoped.append((client, scoped_iam))
        private_write(
            owner.private / "native-identities.json", json.dumps(identities).encode()
        )
        keys = [
            "image-pyramids/objects/" + nonce + "/object",
            NATIVE_CONNECTION_BASE_PREFIX + "/object",
        ]
        receipts = []
        stage("conditional_production_read_delete")
        for index, ((client, scoped_iam), bucket, key) in enumerate(
            zip(scoped, buckets, keys, strict=True)
        ):
            body = ("native-object-" + str(index)).encode()
            receipt = verify_object(
                client, bucket, key, body, budget, versioned=bool(index)
            )
            receipts.append((receipt, body))
            verify_scoped_access(
                client, scoped_iam, identities[index], bucket, key, buckets[1 - index]
            )
        report["conditional_io_and_prefix_iam_passed"] = True
        stage("multipart_bounded_pages")
        # Page 100 splits multiple uploads for the same key; the owned upload is later.
        upload_keys = [f"a-foreign/{i:03d}" for i in range(98)]
        upload_keys += ["b-same-key"] * 3
        upload_keys += [f"c-foreign/{i:03d}" for i in range(20)]
        owned_key = "image-pyramids/objects/" + nonce + "/pending"
        upload_keys.append(owned_key)
        uploads = []
        for key in upload_keys:
            budget.consume("upload", 0, (buckets[0], key))
            upload = root.create_multipart_upload(Bucket=buckets[0], Key=key)
            uploads.append((key, upload["UploadId"]))
        private_write(
            owner.private / "multipart-identities.json", json.dumps(uploads).encode()
        )
        expected = set(uploads)
        part_body = b"p" * (5 * 1024 * 1024)
        owned_upload = uploads[-1][1]
        part = root.upload_part(
            Bucket=buckets[0],
            Key=owned_key,
            UploadId=owned_upload,
            PartNumber=1,
            Body=part_body,
        )
        counts, cursor = upload_pages(scoped[0][0], buckets[0], expected)
        require(
            counts == [100, 22] and cursor[0] == "b-same-key", "same_key_page_boundary"
        )
        report["multipart_pages_before_restart"] = counts
        connection_upload_key = NATIVE_CONNECTION_BASE_PREFIX + "/pending"
        budget.consume("upload", 0, (buckets[1], connection_upload_key))
        connection_upload = root.create_multipart_upload(
            Bucket=buckets[1], Key=connection_upload_key
        )["UploadId"]
        private_write(
            owner.private / "connection-multipart-identity.json",
            json.dumps([buckets[1], connection_upload_key, connection_upload]).encode(),
        )
        multipart_targets = [
            (buckets[0], owned_key, owned_upload),
            (buckets[1], connection_upload_key, connection_upload),
        ]
        for (client, _), (bucket, key, upload_id) in zip(
            scoped, multipart_targets, strict=True
        ):
            expect_error(
                lambda: client.list_parts(Bucket=bucket, Key=key, UploadId=upload_id),
                403,
                {"AccessDenied", "403"},
            )
            expect_error(
                lambda: client.abort_multipart_upload(
                    Bucket=bucket, Key=key, UploadId=upload_id
                ),
                403,
                {"AccessDenied", "403"},
            )
        report["native_multipart_permission_denials_passed"] = True
        # Record the exact first page, not a guessed ordering among same-key upload IDs.
        first_page = scoped[0][0].list_multipart_uploads(
            Bucket=buckets[0], MaxUploads=PAGE_SIZE
        )
        first_pairs = {
            (item["Key"], item["UploadId"]) for item in first_page["Uploads"]
        }
        require(
            (first_page["NextKeyMarker"], first_page["NextUploadIdMarker"]) == cursor,
            "stable_upload_cursor",
        )
        stage("restart_durable_enumeration_and_identity")
        owner.action(container, "restart", "--time", "20", timeout=60)
        ready(root, budget)
        report["multipart_pages_after_restart"] = upload_pages(
            scoped[0][0], buckets[0], expected
        )[0]
        report["multipart_resumed_pages_after_restart"] = upload_pages(
            scoped[0][0], buckets[0], expected - first_pairs, cursor=cursor
        )[0]
        upload_pages(
            scoped[1][0],
            buckets[1],
            {(connection_upload_key, connection_upload)},
        )
        parts = root.list_parts(
            Bucket=buckets[0], Key=owned_key, UploadId=owned_upload, MaxParts=10
        )
        require(
            not parts.get("IsTruncated")
            and [(p["PartNumber"], p["ETag"], p["Size"]) for p in parts["Parts"]]
            == [(1, part["ETag"], len(part_body))],
            "durable_uploaded_part",
        )
        for index, identity in enumerate(identities):
            identity_state(iam, identity)
            require(
                root.get_bucket_tagging(Bucket=buckets[index])["TagSet"]
                == tags["TagSet"],
                "durable_bucket_tags",
            )
            (params, etag), body = receipts[index]
            _read_saved_object(scoped[index][0], params, etag, body, budget)
            verify_scoped_access(
                scoped[index][0],
                scoped[index][1],
                identity,
                buckets[index],
                keys[index],
                buckets[1 - index],
            )
        require(
            root.get_bucket_versioning(Bucket=buckets[1])["Status"] == "Enabled",
            "durable_versioning",
        )
        report["restart_complete_enumeration_passed"] = True
        stage("exact_delete_and_identity_retirement")
        for bucket in buckets:
            read_body(
                root.get_object(Bucket=bucket, Key="foreign/object"),
                budget,
                expected=b"foreign",
            )
        for key, upload_id in uploads:
            root.abort_multipart_upload(Bucket=buckets[0], Key=key, UploadId=upload_id)
            expect_error(
                lambda key=key, upload_id=upload_id: root.list_parts(
                    Bucket=buckets[0], Key=key, UploadId=upload_id
                ),
                404,
                {"NoSuchUpload"},
            )
        upload_pages(scoped[0][0], buckets[0], set())
        root.abort_multipart_upload(
            Bucket=buckets[1], Key=connection_upload_key, UploadId=connection_upload
        )
        expect_error(
            lambda: root.list_parts(
                Bucket=buckets[1],
                Key=connection_upload_key,
                UploadId=connection_upload,
            ),
            404,
            {"NoSuchUpload"},
        )
        upload_pages(scoped[1][0], buckets[1], set())
        for index, ((params, etag), body) in enumerate(receipts):
            client = scoped[index][0]
            if index:
                replacement = client.put_object(
                    Bucket=buckets[index], Key=keys[index], Body=b"current-version"
                )
                require(
                    replacement["VersionId"] != params["VersionId"], "distinct_version"
                )
            client.delete_object(**params, IfMatch=etag)
            expect_error(
                lambda client=client, params=params: client.head_object(**params),
                404,
                {"404", "NoSuchKey", "NoSuchVersion"},
            )
            if index:
                read_body(
                    client.get_object(Bucket=buckets[index], Key=keys[index]),
                    budget,
                    expected=b"current-version",
                )
                client.delete_object(
                    Bucket=buckets[index],
                    Key=keys[index],
                    VersionId=replacement["VersionId"],
                    IfMatch=replacement["ETag"],
                )
            # Disable must revoke actual I/O; re-enable proves this was not an absent key.
            client.put_object(Bucket=buckets[index], Key=keys[index], Body=body)
            user, access, arn, _ = identities[index]
            iam.update_access_key(
                UserName=user, AccessKeyId=access["AccessKeyId"], Status="Inactive"
            )
            expect_error(
                lambda client=client, index=index: client.get_object(
                    Bucket=buckets[index], Key=keys[index]
                ),
                403,
                {"AccessDenied", "InvalidAccessKeyId", "403"},
            )
            iam.update_access_key(
                UserName=user, AccessKeyId=access["AccessKeyId"], Status="Active"
            )
            read_body(
                client.get_object(Bucket=buckets[index], Key=keys[index]),
                budget,
                expected=body,
            )
            iam.delete_access_key(UserName=user, AccessKeyId=access["AccessKeyId"])
            require(
                not iam.list_access_keys(UserName=user)["AccessKeyMetadata"],
                "iam_key_removed",
            )
            iam.detach_user_policy(UserName=user, PolicyArn=arn)
            iam.delete_user(UserName=user)
            expect_error(
                lambda user=user: iam.get_user(UserName=user), 404, {"NoSuchEntity"}
            )
            iam.delete_policy(PolicyArn=arn)
            expect_error(
                lambda arn=arn: iam.get_policy(PolicyArn=arn), 404, {"NoSuchEntity"}
            )
        report["native_target_contract_passed"] = True
    finally:
        for client in clients:
            client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    require(not args.report.exists(), "report_exists")
    for name in tuple(os.environ):
        if name.startswith(("AWS_", "MINIO_")):
            del os.environ[name]
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = os.environ["NO_PROXY"]
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    lock = load_lock(ROOT / "scripts/storage_s3_candidates.lock.json", ROOT)
    candidate = next(item for item in lock["candidates"] if item["name"] == "seaweedfs")
    owner = Owner(args.manifest, create=True)
    budget = Budget()
    started = time.monotonic()
    report = {
        "schema": "aap.native-storage-contract/v1",
        "scope": "native_saved_output_target_only",
        "candidate": candidate["name"],
        "image": candidate["image"],
        "policy_sha256": policy_fingerprint(),
        "native_target_contract_passed": False,
        "general_storage_replacement_qualified": False,
        "native_lifecycle_qualified": False,
        "runtime_retired": False,
    }
    try:
        qualify(owner, lock, candidate, budget, report)
        report["runtime_retired"] = owner.cleanup()
        require(report["runtime_retired"], "owner_retirement_failed")
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = safe_error(exc)
        # Preserve provider details privately; no URLs, credentials or messages in public evidence.
        if owner.private.exists():
            private_write(
                owner.private / "failure.json",
                json.dumps({"type": type(exc).__name__, "detail": str(exc)}).encode(),
            )
        report["retirement_requires_separate_approval"] = True
    report["budget"] = budget.summary()
    report["wall_seconds"] = round(time.monotonic() - started, 3)
    private_write(args.report, public_bytes(report))
    print(
        json.dumps(
            {
                "report": str(args.report),
                "passed": report["native_target_contract_passed"],
                "retired": report["runtime_retired"],
            }
        ),
        flush=True,
    )
    return (
        0
        if report["native_target_contract_passed"] and report["runtime_retired"]
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
