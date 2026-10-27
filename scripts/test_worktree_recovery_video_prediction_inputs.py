"""Read-only v3 recovery verifies content without adopting historical versions."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from botocore.exceptions import ClientError

import worktree_recovery_prediction_inputs as subject
from worktree_recovery_prediction_inputs import verify_video_input_copy


def probe():
    return {
        "probe_contract": 1,
        "video": {
            "width": 32,
            "height": 24,
            "fps": 2.0,
            "frame_count": 3,
            "duration_ms": 1500,
            "codec": "mpeg4",
        },
    }


def missing():
    return ClientError(
        {
            "Error": {"Code": "NoSuchVersion"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        "HeadObject",
    )


def fixture(*, state="verified", absent=False, corrupt=False):
    from app.services import prediction_inputs as owner

    identifier, nonce = uuid.uuid4(), str(uuid.uuid4())
    # Transport-only unit bytes; actual fixture qualification borrows canonical
    # ffmpeg video plus the source owner's same-FD probe, never this sentinel.
    raw = b"RIFF\0\0\0\0AVI restored content with durable receipt"
    sha = hashlib.sha256(raw).hexdigest()
    relative = "video/" + sha + ".avi"
    prefix = owner.input_prefix(identifier)
    endpoint, bucket = owner._endpoint(), "restored-import"
    ref = {
        "schema": 1,
        "purpose": "import",
        "location_id": str(owner.LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": bucket,
        "key_prefix": prefix,
        "storage_identity": owner._identity(endpoint, bucket),
    }
    value = {
        "sha256": sha,
        "size_bytes": len(raw),
        "media_type": "application/octet-stream",
        "state": state,
        "etag": '"same-etag"' if state == "verified" else None,
        "version_id": "immutable-source-version" if state == "verified" else None,
        "write_nonce": nonce,
        "container": "avi",
        "probe": probe(),
    }
    started = datetime(2026, 10, 25, tzinfo=timezone.utc)
    row = SimpleNamespace(
        id=identifier,
        owner_id=uuid.uuid4(),
        owner_kind="video_predict_test",
        task_id=uuid.uuid4(),
        contract_version=3,
        bucket=bucket,
        storage_identity=ref["storage_identity"],
        storage_ref=ref,
        maintenance_revision=4,
        created_at=started,
        expires_at=started + timedelta(hours=2),
        retain_until=started + timedelta(hours=2, days=7),
        source_ref={
            "original": {
                "bucket": "historical-source",
                "sha256": sha,
                "size_bytes": len(raw),
            },
            "probe": probe(),
        },
        inventory={"schema": 1, "objects": {relative: value}},
        object_count=1,
        size_bytes=len(raw),
        state="cleanup_pending",
        uncertain_write=state == "planned",
        deleted_at=None,
        next_cleanup_at=started + timedelta(hours=2, days=7),
        last_cleanup_at=started + timedelta(hours=2),
        error_code="prediction_input_immutable_version_required",
    )
    header = {
        "ContentLength": len(raw),
        "ContentType": "application/octet-stream",
        "ETag": '"same-etag"',
        "Metadata": {
            "aap-prediction-lease": str(identifier),
            "aap-prediction-write": nonce,
        },
        "ResponseMetadata": {"HTTPStatusCode": 200},
    }
    bodies = []

    def head(**params):
        if absent or params.get("VersionId") not in {None, "null"}:
            raise missing()
        return deepcopy(header)

    def get(**params):
        assert params.get("VersionId") in {None, "null"}
        content = b"X" * len(raw) if corrupt else raw
        body = io.BytesIO(content)
        bodies.append(body)
        return {**deepcopy(header), "Body": body}

    client = SimpleNamespace(
        head_object=Mock(side_effect=head),
        get_object=Mock(side_effect=get),
        delete_object=Mock(
            side_effect=AssertionError("No DELETE permitted in restore validation")
        ),
    )
    item = {"Key": prefix + relative, "Size": len(raw), "ETag": header["ETag"]}
    client.list_objects_v2 = Mock(
        return_value={
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "IsTruncated": False,
            "Contents": [] if absent else [item],
        }
    )
    client.list_object_versions = Mock(
        return_value={
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "IsTruncated": False,
            "Versions": [] if absent else [{**item, "VersionId": "null"}],
            "DeleteMarkers": [],
        }
    )
    return row, SimpleNamespace(client=client), bodies


class VideoInputRecoveryContracts(unittest.TestCase):
    def test_verified_video_copy_keeps_probe_nonce_source_and_original_deadline(self):
        row, storage, bodies = fixture()
        before = deepcopy(vars(row))
        result = verify_video_input_copy(storage, row)
        self.assertEqual(result["verified_video_input_objects"], 1)
        self.assertEqual(result["restored_video_input_objects"], 1)
        self.assertEqual(result["historical_video_input_receipts_rejected"], 1)
        self.assertEqual(result["historical_video_input_cleanup_rejected"], 1)
        self.assertFalse(result["video_input_receipts_rewritten"])
        self.assertFalse(result["video_input_queue_resumed"])
        self.assertEqual(vars(row), before)
        self.assertTrue(all(body.closed for body in bodies))
        storage.client.delete_object.assert_not_called()
        storage.client.list_objects_v2.assert_called_once()
        storage.client.list_object_versions.assert_called_once()
        self.assertEqual(
            storage.client.head_object.call_args.kwargs["VersionId"],
            "immutable-source-version",
        )

    def test_planned_video_retains_absent_or_exact_owned_copy_without_publish(self):
        for absent in (True, False):
            row, storage, bodies = fixture(state="planned", absent=absent)
            before = deepcopy(vars(row))
            result = verify_video_input_copy(storage, row)
            self.assertEqual(result["planned_video_input_objects"], 1)
            self.assertEqual(result["verified_video_input_objects"], 0)
            self.assertEqual(result["restored_video_input_objects"], int(not absent))
            self.assertEqual(
                result["historical_video_input_cleanup_rejected"], int(not absent)
            )
            self.assertEqual(vars(row), before)
            self.assertTrue(all(body.closed for body in bodies))
            storage.client.delete_object.assert_not_called()
            self.assertEqual(storage.client.list_objects_v2.call_count, int(not absent))
            self.assertEqual(
                storage.client.list_object_versions.call_count, int(not absent)
            )

    def test_actual_corrupt_content_rejects_without_receipt_rewrite_or_delete(self):
        from app.services.content_snapshot import ContentError

        row, storage, bodies = fixture(corrupt=True)
        before = deepcopy(vars(row))
        with self.assertRaises(ContentError):
            verify_video_input_copy(storage, row)
        self.assertEqual(vars(row), before)
        self.assertTrue(all(body.closed for body in bodies))
        storage.client.delete_object.assert_not_called()

    def test_same_content_with_another_ownership_nonce_refuses_without_adoption(self):
        from app.services.content_snapshot import ContentError

        for state in ("verified", "planned"):
            row, storage, _bodies = fixture(state=state)
            next(iter(row.inventory["objects"].values()))["write_nonce"] = str(
                uuid.uuid4()
            )
            before = deepcopy(vars(row))
            with self.assertRaises(ContentError):
                verify_video_input_copy(storage, row)
            self.assertEqual(vars(row), before)
            storage.client.get_object.assert_not_called()
            storage.client.delete_object.assert_not_called()

    def test_video_contract_format_and_probe_refuse_before_object_io(self):
        from app.services.content_snapshot import ContentError

        for change in (
            "owner",
            "contract",
            "container",
            "probe",
            "size",
            "deadline",
            "unknown_field",
            "nonce",
        ):
            row, storage, _ = fixture()
            value = next(iter(row.inventory["objects"].values()))
            if change == "owner":
                row.owner_kind = "predict_test"
            elif change == "contract":
                row.contract_version = 2
            elif change == "container":
                value["container"] = "mp4"
            elif change == "probe":
                value["probe"]["video"]["frame_count"] = True
            elif change == "size":
                row.size_bytes = value["size_bytes"] = 5 * 1024**3 + 1
            elif change == "deadline":
                row.expires_at += timedelta(seconds=1)
            elif change == "nonce":
                value["write_nonce"] = "not-a-durable-uuid"
            else:
                value["guessed_dimensions"] = [32, 24]
            before = deepcopy(vars(row))
            with self.assertRaises(ContentError):
                verify_video_input_copy(storage, row)
            self.assertEqual(vars(row), before)
            storage.client.head_object.assert_not_called()
            storage.client.delete_object.assert_not_called()


class VideoInputSeedingContracts(unittest.IsolatedAsyncioTestCase):
    async def test_video_seed_uses_explicit_committed_tasks_without_regenerating_sources(
        self,
    ):
        tasks = {case: uuid.uuid4() for case in subject.CASES}
        rows = {
            identifier: SimpleNamespace(id=identifier, file_type="video")
            for identifier in tasks.values()
        }
        db = SimpleNamespace(
            get=AsyncMock(side_effect=lambda _model, key, **_kwargs: rows[key])
        )
        seed = AsyncMock()
        factory = object()
        with patch.object(subject, "_seed_video_case", seed):
            await subject.seed_video_prediction_inputs(
                db, factory=factory, task_ids=tasks
            )
        self.assertEqual(
            seed.await_args_list[0].args,
            (db, factory, rows[tasks["verified"]], "verified"),
        )
        self.assertEqual(
            seed.await_args_list[1].args,
            (db, factory, rows[tasks["planned"]], "planned"),
        )
        self.assertNotEqual(
            subject.video_owner_identifier("verified"),
            subject.owner_identifier("verified"),
        )

    async def test_video_seed_rejects_nonvideo_or_ambiguous_task_selection(self):
        tasks = {case: uuid.uuid4() for case in subject.CASES}
        db = SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(file_type="image"))
        )
        with patch.object(subject, "_seed_video_case", AsyncMock()) as seed:
            for values in (
                tasks,
                {"verified": tasks["verified"], "planned": tasks["verified"]},
            ):
                with self.assertRaises(AssertionError):
                    await subject.seed_video_prediction_inputs(
                        db, factory=object(), task_ids=values
                    )
            seed.assert_not_awaited()

    async def test_synthetic_video_interruption_retains_exact_completed_observation_without_publish(
        self,
    ):
        row, _storage, _bodies = fixture(state="planned")
        relative, expected = next(iter(row.inventory["objects"].items()))
        verified = {
            **deepcopy(expected),
            "state": "verified",
            "etag": '"actual-version-etag"',
            "version_id": "actual-version",
        }
        before = deepcopy(expected)
        boundary = subject._BeforeVideoPublish(probe())
        with self.assertRaises(subject._VideoReceiptInterruption):
            await boundary(relative, expected, verified)
        self.assertEqual(expected, before)
        self.assertEqual(boundary.observed, verified)
        verified["probe"]["video"]["width"] += 1
        self.assertEqual(boundary.observed["probe"], probe())

    async def test_source_and_samepurpose_permit_close_only_after_inputs_sealed(self):
        order = []

        async def close_inputs():
            order.append("inputs-sealed")

        async def close_source(failure):
            self.assertIsNone(failure)
            order.append("source-physically-closed")

        async def settle(*_args):
            order.append("permit-settled")

        inputs = SimpleNamespace(close=close_inputs)
        source = SimpleNamespace(close=close_source)
        permit = SimpleNamespace(__aexit__=settle)
        await subject._close_case(inputs, source, permit, None, None)
        self.assertEqual(
            order, ["inputs-sealed", "source-physically-closed", "permit-settled"]
        )


if __name__ == "__main__":
    unittest.main()
