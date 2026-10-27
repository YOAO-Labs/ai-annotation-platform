"""Pure contracts for the offline prediction-input fixture; no database I/O."""

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from botocore.exceptions import ClientError
from PIL import Image

from worktree_recovery_prediction_inputs import (
    CASES,
    _BeforePutStorage,
    _PlannedInterruption,
    _close_case,
    _frozen_owner,
    _guard,
    _seed_case,
    _verify_physical,
    _verify_historical_rejections,
    enable_owned_import_versioning,
    owner_identifier,
    seed_prediction_inputs,
)


def task():
    return SimpleNamespace(
        id=uuid.uuid4(),
        batch_id=uuid.uuid4(),
        dataset_item_id=uuid.uuid4(),
        file_path="registered/original.png",
        file_name="original.png",
        version=7,
    )


def absent():
    return ClientError(
        {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
        "HeadObject",
    )


class PredictionInputFixtureContracts(unittest.IsolatedAsyncioTestCase):
    def test_case_owner_ids_are_stable_and_separate(self):
        self.assertEqual(owner_identifier("verified"), owner_identifier("verified"))
        self.assertNotEqual(owner_identifier("verified"), owner_identifier("planned"))
        with self.assertRaises(AssertionError):
            owner_identifier("legacy")

    def test_synthetic_interruption_never_sends_put_and_closes_real_client(self):
        underlying = SimpleNamespace(client=Mock(), marker=object())
        wrapped = _BeforePutStorage(underlying)
        self.assertIs(wrapped.client.meta, underlying.client.meta)
        self.assertIs(wrapped.marker, underlying.marker)
        lease = uuid.uuid4()
        with self.assertRaises(_PlannedInterruption):
            wrapped.client.put_object(
                Bucket="saved",
                Key=f"prediction-inputs/{lease}/crops/exact.jpg",
                Body=b"private",
                IfNoneMatch="*",
                Metadata={
                    "aap-prediction-lease": str(lease),
                    "aap-prediction-write": str(uuid.uuid4()),
                },
            )
        underlying.client.put_object.assert_not_called()
        self.assertEqual(wrapped.client.attempts, 1)
        wrapped.client.close()
        underlying.client.close.assert_called_once_with()
        self.assertTrue(wrapped.client.closed)

    async def test_guard_preserves_task_version_and_full_original_facts(self):
        from app.services import prediction_originals as originals, reference_bindings
        from app.services.content_snapshot import ContentError

        row = task()
        original = {"bucket": "source", "facts_digest": "historical"}
        binding = {**reference_bindings.task_binding(row), "original": original}
        db = SimpleNamespace(get=AsyncMock(return_value=row))
        current = AsyncMock(return_value=deepcopy(original))
        with patch.object(originals, "managed_binding", current):
            await _guard(binding)(db)
            row.version += 1
            with self.assertRaisesRegex(ContentError, "content_binding_changed"):
                await _guard(binding)(db)
            row.version -= 1
            current.return_value = {**original, "bucket": "target"}
            with self.assertRaisesRegex(ContentError, "content_binding_changed"):
                await _guard(binding)(db)
        self.assertEqual(binding["original"], original)

    async def test_seed_uses_real_service_order_and_closes_under_live_permit(self):
        from app.services import prediction_inputs, prediction_originals as originals
        from app.services import storage_access
        from app.services.content_snapshot import ContentError

        for case in CASES:
            with self.subTest(case=case):
                events = []
                row = task()
                output_permit = SimpleNamespace(_settled=False)
                source_permit = SimpleNamespace(_settled=False)
                snapshot = SimpleNamespace(file=io.BytesIO(b"source"), _closed=False)
                images = []
                real_storage = SimpleNamespace(client=Mock())
                record = SimpleNamespace(
                    id=uuid.uuid4(),
                    task_id=row.id,
                    storage_ref={"location_id": str(uuid.uuid4())},
                    maintenance_revision=3,
                )
                inputs = SimpleNamespace(record=record, storage=None)

                async def upload_snapshot(value):
                    self.assertIs(value, snapshot)
                    self.assertFalse(output_permit._settled)
                    events.append("verified-original")

                def load_image(value):
                    self.assertIs(value, row)
                    image = Image.new("RGB", (2, 2), "green")
                    images.append(image)
                    return image

                async def upload_crop(value, index, content):
                    self.assertIs(value, row)
                    self.assertEqual(index, 0)
                    with Image.open(io.BytesIO(content)) as crop:
                        self.assertEqual(crop.size, (1, 1))
                    events.append("reserved-crop")
                    if inputs.storage is not None:
                        try:
                            inputs.storage.client.put_object(
                                Body=content,
                                IfNoneMatch="*",
                                Key=f"prediction-inputs/{record.id}/crops/exact.jpg",
                                Metadata={
                                    "aap-prediction-lease": str(record.id),
                                    "aap-prediction-write": str(uuid.uuid4()),
                                },
                            )
                        except _PlannedInterruption:
                            raise ContentError("prediction_input_upload_failed", 503)
                        finally:
                            inputs.storage.client.close()
                    events.append("verified-crop")

                async def close_inputs():
                    self.assertFalse(output_permit._settled)
                    self.assertFalse(source_permit._settled)
                    events.append("close-inputs-with-permit")
                    for image in images:
                        image.close()
                    snapshot.file.close()
                    snapshot._closed = True

                async def close_source(_failure=None):
                    self.assertTrue(snapshot.file.closed)
                    events.append("close-source")
                    source_permit._settled = True

                async def close_permit(permit, failure=None):
                    self.assertIs(permit, output_permit)
                    self.assertIsNone(failure)
                    self.assertTrue(source_permit._settled)
                    events.append("settle-output")
                    permit._settled = True

                async def physical(call, *args):
                    return call(*args)

                async def settled(awaitable, **_kwargs):
                    return await awaitable

                @asynccontextmanager
                async def factory():
                    yield SimpleNamespace()

                inputs.upload_snapshot = AsyncMock(side_effect=upload_snapshot)
                inputs.upload_crop = AsyncMock(side_effect=upload_crop)
                inputs.load_image = load_image
                inputs.close = AsyncMock(side_effect=close_inputs)
                source = SimpleNamespace(
                    snapshot=snapshot,
                    permit=source_permit,
                    close=AsyncMock(side_effect=close_source),
                )
                create = AsyncMock(return_value=inputs)
                with (
                    patch.object(prediction_inputs.PredictionInputs, "create", create),
                    patch.object(
                        prediction_inputs, "make_storage", return_value=real_storage
                    ),
                    patch.object(
                        originals,
                        "managed_binding",
                        AsyncMock(return_value={"bucket": "source"}),
                    ),
                    patch.object(
                        originals, "open_snapshot", AsyncMock(return_value=source)
                    ) as capture,
                    patch.object(originals, "physical", physical),
                    patch.object(originals, "settled", settled),
                    patch.object(originals, "close_permit", close_permit),
                    patch.object(
                        storage_access, "acquire", AsyncMock(return_value=output_permit)
                    ) as acquire,
                ):
                    await _seed_case(SimpleNamespace(), factory, row, case)
                self.assertEqual(create.call_args.kwargs["owner_kind"], "predict_test")
                self.assertEqual(
                    create.call_args.kwargs["owner_id"], owner_identifier(case)
                )
                self.assertEqual(create.call_args.kwargs["source_ref"]["version"], 7)
                purpose = prediction_inputs.purpose(record)
                self.assertEqual(acquire.call_args.args[2], purpose)
                self.assertEqual(capture.call_args.kwargs["purpose"], purpose)
                self.assertEqual(events[:2], ["verified-original", "reserved-crop"])
                self.assertEqual(
                    events[-3:],
                    ["close-inputs-with-permit", "close-source", "settle-output"],
                )
                self.assertEqual("verified-crop" in events, case == "verified")
                real_storage.client.put_object.assert_not_called()
                self.assertEqual(
                    real_storage.client.close.call_count, case == "planned"
                )

    async def test_explicit_selection_never_discovers_neighbor_tasks(self):
        rows = {case: task() for case in CASES}
        selected = {case: row.id for case, row in rows.items()}
        db = SimpleNamespace(get=AsyncMock(side_effect=list(rows.values())))
        with patch(
            "worktree_recovery_prediction_inputs._seed_case", AsyncMock()
        ) as seed:
            await seed_prediction_inputs(db, factory="owned-factory", task_ids=selected)
        self.assertEqual(
            [call.args[2] for call in seed.await_args_list], list(rows.values())
        )
        self.assertEqual([call.args[3] for call in seed.await_args_list], list(CASES))
        self.assertEqual(
            [call.args[1] for call in db.get.await_args_list], list(selected.values())
        )
        with self.assertRaises(AssertionError):
            await seed_prediction_inputs(
                db, factory=None, task_ids={**selected, "other": uuid.uuid4()}
            )

    def test_frozen_owner_does_not_alias_inventory_nonce_or_historical_source(self):
        from app.services import prediction_inputs

        started = datetime(2026, 10, 24, tzinfo=timezone.utc)
        values = dict.fromkeys(prediction_inputs._FACTS)
        values.update(
            created_at=started,
            expires_at=started + timedelta(hours=2),
            retain_until=started + timedelta(hours=2, days=7),
            storage_ref={"bucket": "saved"},
            source_ref={"original": {"bucket": "source", "facts_digest": "frozen"}},
            inventory={
                "schema": 1,
                "objects": {"crop": {"write_nonce": str(uuid.uuid4())}},
            },
            state="cleanup_pending",
            uncertain_write=True,
            object_count=1,
            size_bytes=3,
            deleted_at=None,
            next_cleanup_at=datetime(2026, 10, 25, tzinfo=timezone.utc),
            last_cleanup_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
            error_code="prediction_input_cleanup_busy",
        )
        row = SimpleNamespace(**values)
        frozen = _frozen_owner(row)
        row.inventory["objects"]["crop"]["write_nonce"] = "changed"
        row.source_ref["original"]["bucket"] = "changed"
        row.storage_ref["bucket"] = "changed"
        self.assertNotEqual(frozen.inventory, row.inventory)
        self.assertEqual(frozen.source_ref["original"]["bucket"], "source")
        self.assertEqual(frozen.storage_ref["bucket"], "saved")
        self.assertEqual(frozen.next_cleanup_at, row.next_cleanup_at)
        self.assertEqual(frozen.last_cleanup_at, row.last_cleanup_at)
        self.assertEqual(frozen.error_code, row.error_code)
        for field in ("created_at", "expires_at", "retain_until"):
            self.assertEqual(getattr(frozen, field), getattr(row, field))

    def test_physical_verification_uses_saved_owner_and_keeps_planned_absent(self):
        storage = SimpleNamespace(client=Mock())
        storage.client.head_object.side_effect = absent()
        record = SimpleNamespace(
            id=uuid.uuid4(), bucket="saved-target", storage_ref={"key_prefix": "exact/"}
        )
        objects = {
            "original/hash.png": {"state": "verified", "version_id": "historical"},
            "crops/0-hash.jpg": {"state": "planned", "etag": None, "version_id": None},
        }
        frozen = deepcopy(objects)
        with patch(
            "worktree_recovery_prediction_inputs._read_restored_content"
        ) as read:
            self.assertEqual(_verify_physical(storage, record, objects), (1, 1))
        self.assertEqual(
            read.call_args.args[:4],
            (
                storage.client,
                record,
                "exact/original/hash.png",
                objects["original/hash.png"],
            ),
        )
        storage.client.head_object.assert_called_once_with(
            Bucket="saved-target", Key="exact/crops/0-hash.jpg"
        )
        self.assertEqual(objects, frozen)
        storage.client.head_object.side_effect = None
        with patch("worktree_recovery_prediction_inputs._read_restored_content"):
            with self.assertRaisesRegex(AssertionError, "must remain absent"):
                _verify_physical(storage, record, objects)
        storage.client.head_object.side_effect = RuntimeError("transport failure")
        with patch("worktree_recovery_prediction_inputs._read_restored_content"):
            with self.assertRaisesRegex(RuntimeError, "transport failure"):
                _verify_physical(storage, record, objects)

    def test_restored_current_reader_checks_bytes_metadata_and_observed_version(self):
        from app.services.content_snapshot import ContentError

        raw = b"tiny verified image fixture"
        identifier, nonce = uuid.uuid4(), str(uuid.uuid4())
        record = SimpleNamespace(
            id=identifier, bucket="saved", storage_ref={"key_prefix": "exact/"}
        )
        objects = {
            "original/hash.png": {
                "state": "verified",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
                "etag": '"historical-etag"',
                "version_id": "historical-version",
                "write_nonce": nonce,
                "media_type": "image/png",
            }
        }
        before = deepcopy(objects)
        for version in (None, "null", "restored-new-version"):
            observation = {
                "ContentLength": len(raw),
                "ETag": '"target-etag"',
                "ContentType": "image/png",
                "ResponseMetadata": {"HTTPStatusCode": 200},
                "Metadata": {
                    "aap-prediction-lease": str(identifier),
                    "aap-prediction-write": nonce,
                },
                **({"VersionId": version} if version is not None else {}),
            }
            for tamper in (
                None,
                "same-length",
                "trailing",
                "wrong-write",
                "wrong-lease",
                "changed-etag",
                "changed-version",
                "changed-metadata",
                "replacement-after-read",
            ):
                with self.subTest(version=version, tamper=tamper):
                    payload = b"X" * len(raw) if tamper == "same-length" else raw
                    if tamper == "trailing":
                        payload += b"X"
                    body = io.BytesIO(payload)
                    get, after = deepcopy(observation), deepcopy(observation)
                    if tamper == "wrong-write":
                        get["Metadata"]["aap-prediction-write"] = str(uuid.uuid4())
                    if tamper == "wrong-lease":
                        get["Metadata"]["aap-prediction-lease"] = str(uuid.uuid4())
                    if tamper == "changed-etag":
                        get["ETag"] = '"new-etag"'
                    if tamper == "changed-version":
                        get["VersionId"] = "unexpected-version"
                    if tamper == "changed-metadata":
                        get["CacheControl"] = "changed"
                    if tamper == "replacement-after-read":
                        after["VersionId"] = "replacement"
                    client = SimpleNamespace(
                        head_object=Mock(side_effect=[deepcopy(observation), after]),
                        get_object=Mock(return_value={**get, "Body": body}),
                    )
                    if tamper is None:
                        self.assertEqual(
                            _verify_physical(
                                SimpleNamespace(client=client), record, objects
                            ),
                            (1, 0),
                        )
                        client.get_object.assert_called_once_with(
                            Bucket="saved",
                            Key="exact/original/hash.png",
                            IfMatch='"target-etag"',
                            **({"VersionId": version} if version is not None else {}),
                        )
                        self.assertEqual(client.head_object.call_count, 2)
                    else:
                        with self.assertRaises(ContentError):
                            _verify_physical(
                                SimpleNamespace(client=client), record, objects
                            )
                    self.assertTrue(body.closed)
                    self.assertEqual(objects, before)

    def test_actual_owner_cleanup_rejects_restored_null_version_before_delete(self):
        from app.services import prediction_inputs

        identifier = uuid.uuid4()
        prefix = prediction_inputs.input_prefix(identifier)
        raw = b"verified"
        entry = {
            "state": "verified",
            "version_id": "old-version",
            "etag": '"same-etag"',
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "media_type": "image/png",
            "write_nonce": str(uuid.uuid4()),
        }
        endpoint, bucket = prediction_inputs._endpoint(), "saved"
        storage_ref = {
            "schema": 1,
            "purpose": "import",
            "location_id": str(prediction_inputs.LEGACY_LOCATION_ID),
            "config_revision": 1,
            "binding_generation": 1,
            "endpoint_url": endpoint,
            "bucket": bucket,
            "key_prefix": prefix,
            "storage_identity": prediction_inputs._identity(endpoint, bucket),
        }
        record = SimpleNamespace(
            id=identifier,
            contract_version=2,
            owner_kind="predict_test",
            maintenance_revision=1,
            bucket=bucket,
            storage_identity=storage_ref["storage_identity"],
            storage_ref=storage_ref,
        )
        item = {
            "Key": prefix + "original/hash.png",
            "Size": len(raw),
            "ETag": '"same-etag"',
        }
        response = {"ResponseMetadata": {"HTTPStatusCode": 200}, "IsTruncated": False}
        client = Mock()
        client.head_object.side_effect = ClientError(
            {
                "Error": {"Code": "NoSuchVersion"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "HeadObject",
        )
        client.list_objects_v2.return_value = {**response, "Contents": [item]}
        objects = {"original/hash.png": entry}
        before = deepcopy(objects)
        owner_before = deepcopy(vars(record))
        for version in ("null", "restored-new-version"):
            with self.subTest(version=version):
                client.list_object_versions.return_value = {
                    **response,
                    "Versions": [{**item, "VersionId": version, "IsLatest": True}],
                }
                self.assertEqual(
                    _verify_historical_rejections(
                        SimpleNamespace(client=client), record, objects
                    ),
                    (1, 1),
                )
                self.assertEqual(
                    client.head_object.call_args.kwargs["VersionId"], "old-version"
                )
                client.delete_object.assert_not_called()
                client.get_object.assert_not_called()
                self.assertEqual(objects, before)
                self.assertEqual(vars(record), owner_before)
        self.assertEqual(client.list_objects_v2.call_count, 2)
        self.assertEqual(client.list_object_versions.call_count, 2)

    def test_enable_versioning_requires_exact_owned_test_bucket_before_mutation(self):
        from app.config import settings
        from worktree_env import WorktreeError
        from worktree_resources import OWNER_TAG

        root = Path("/tmp/explicit-fixture-root")
        resources = {
            "owner": "aap-worktree:fixture:test:owned",
            "buckets": {"MINIO_IMPORT_BUCKET": "exact-owned-import"},
        }
        for failure in (
            None,
            "mode",
            "settings",
            "storage",
            "owner",
            "duplicate-owner",
            "suspended",
            "mfa",
        ):
            with self.subTest(failure=failure):
                client = Mock()
                tags = [{"Key": OWNER_TAG, "Value": resources["owner"]}]
                if failure == "owner":
                    tags[0]["Value"] = "foreign"
                if failure == "duplicate-owner":
                    tags *= 2
                client.get_bucket_tagging.return_value = {"TagSet": tags}
                initial = (
                    {"Status": "Suspended"}
                    if failure == "suspended"
                    else ({"MFADelete": "Enabled"} if failure == "mfa" else {})
                )
                client.get_bucket_versioning.side_effect = [
                    initial,
                    {"Status": "Enabled"},
                ]
                storage = SimpleNamespace(
                    client=client,
                    import_bucket="foreign"
                    if failure == "storage"
                    else "exact-owned-import",
                )
                with (
                    patch.dict(
                        os.environ,
                        {"AAP_WORKTREE_MODE": "dev" if failure == "mode" else "test"},
                    ),
                    patch.object(
                        settings,
                        "minio_import_bucket",
                        "foreign" if failure == "settings" else "exact-owned-import",
                    ),
                    patch(
                        "worktree_env.load_identity", return_value={"id": "fixture"}
                    ) as load,
                    patch("worktree_env.topology", return_value=resources) as topology,
                ):
                    if failure:
                        with self.assertRaises(WorktreeError):
                            enable_owned_import_versioning(storage, root=root)
                        client.put_bucket_versioning.assert_not_called()
                    else:
                        enable_owned_import_versioning(storage, root=root)
                        load.assert_called_once_with(root)
                        topology.assert_called_once_with(
                            root, {"id": "fixture"}, "test"
                        )
                        client.get_bucket_tagging.assert_called_once_with(
                            Bucket="exact-owned-import"
                        )
                        client.put_bucket_versioning.assert_called_once_with(
                            Bucket="exact-owned-import",
                            VersioningConfiguration={"Status": "Enabled"},
                        )

    def test_already_enabled_owned_bucket_is_only_observed(self):
        from app.config import settings
        from worktree_resources import OWNER_TAG

        resources = {"owner": "exact", "buckets": {"MINIO_IMPORT_BUCKET": "owned"}}
        client = Mock()
        client.get_bucket_tagging.return_value = {
            "TagSet": [{"Key": OWNER_TAG, "Value": "exact"}]
        }
        client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        with (
            patch.dict(os.environ, {"AAP_WORKTREE_MODE": "test"}),
            patch.object(settings, "minio_import_bucket", "owned"),
            patch("worktree_env.load_identity", return_value={}),
            patch("worktree_env.topology", return_value=resources),
        ):
            enable_owned_import_versioning(
                SimpleNamespace(client=client, import_bucket="owned"),
                root="/tmp/actual-root",
            )
        client.put_bucket_versioning.assert_not_called()
        self.assertEqual(client.get_bucket_versioning.call_count, 2)

    async def test_unknown_close_retains_permits_and_still_attempts_other_closes(self):
        from app.services import prediction_originals as originals, storage_access

        failure = storage_access.mark_unsettled(RuntimeError("unknown close"))
        inputs = SimpleNamespace(close=AsyncMock(side_effect=failure))
        source = SimpleNamespace(close=AsyncMock())
        permit = SimpleNamespace()
        interrupted = SimpleNamespace(
            client=SimpleNamespace(closed=False, close=Mock())
        )
        with patch.object(originals, "close_permit", AsyncMock()) as settle:
            with self.assertRaisesRegex(RuntimeError, "unknown close"):
                await _close_case(inputs, source, permit, interrupted, None)
        source.close.assert_awaited_once_with(failure)
        interrupted.client.close.assert_called_once_with()
        settle.assert_awaited_once_with(permit, failure)
