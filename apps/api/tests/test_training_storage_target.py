"""The training probe exercises ordinary writes without extra provider gates."""

from contextlib import contextmanager
from dataclasses import replace

import pytest

from app.services import training_storage_target as service
from app.services.training_dataset_bridge import TrainingDatasetError
from tests.test_dataset_storage_target import ProbeS3, target
from tests.test_managed_original_writer import error, response


class TrainingS3(ProbeS3):
    def __init__(self, failure=None):
        super().__init__(failure)
        self.ignore_get_condition = True

    def put_object(self, **params):
        assert params["Body"] and "IfNoneMatch" not in params
        self.record("put", params)
        if self.failure == "denied":
            raise error("AccessDenied", 403)
        value = self.store(params["Bucket"], params["Key"], params["Body"])
        if self.failure == "put_unknown":
            raise OSError("private PUT reply lost")
        return response(VersionId=value["VersionId"])

    def complete_multipart_upload(self, **params):
        assert "IfNoneMatch" not in params
        self.record("complete", params)
        upload = self.uploads.pop(params["UploadId"])
        data = b"".join(
            upload["parts"][p["PartNumber"]] for p in params["MultipartUpload"]["Parts"]
        )
        assert data
        value = self.store(params["Bucket"], params["Key"], data)
        if self.failure in {"complete_unknown", "abort_unknown"}:
            raise OSError("private MPU reply lost")
        return response(VersionId=value["VersionId"])

    def list_objects_v2(self, **params):
        self.record("list", params)
        values = [
            {"Key": key, "Size": len(value["data"])}
            for (bucket, key), value in self.objects.items()
            if bucket == params["Bucket"] and key.startswith(params["Prefix"])
        ]
        return response(Contents=values, IsTruncated=self.failure == "list")

    def delete_objects(self, **params):
        self.record("batch_delete", params)
        if self.failure == "delete":
            return response(
                Errors=[
                    {
                        "Key": params["Delete"]["Objects"][0]["Key"],
                        "Code": "AccessDenied",
                    }
                ]
            )
        for item in params["Delete"]["Objects"]:
            self.delete_object(Bucket=params["Bucket"], **item)
        return response()

    def get_object(self, **params):
        result = super().get_object(**params)
        if self.failure == "read":
            result["Body"].write(b"wrong bytes")
            result["Body"].seek(0)
        return result

    def head_object(self, **params):
        result = super().head_object(**params)
        if self.failure == "version":
            result["VersionId"] = "unreceipted-version"
        return result


@pytest.mark.parametrize("versioned", [False, True])
def test_ordinary_put_mpu_full_read_list_batch_delete_and_close(versioned):
    client = TrainingS3()
    client.versioned = versioned
    client.store("owned", "unrelated", b"keep")
    result = service.probe_target(
        replace(target(client), purpose="training", key_prefix="_aap_managed/training/")
    )
    assert result == {
        "write": True,
        "multipart": True,
        "full_read": True,
        "list": True,
        "delete": True,
    }
    assert set(client.objects) == {("owned", "unrelated")}
    assert (
        not client.uploads
        and client.closed == 1
        and all(body.closed for body in client.bodies)
    )
    assert len([call for call in client.calls if call[0] == "put"]) == 1
    assert len([call for call in client.calls if call[0] == "complete"]) == 1
    assert len([call for call in client.calls if call[0] == "batch_delete"]) == 1
    assert not any("IfNoneMatch" in params for _, params in client.calls)


@pytest.mark.parametrize(
    "failure,unsettled",
    [
        ("denied", False),
        ("read", False),
        ("list", False),
        ("delete", True),
        ("put_unknown", True),
        ("complete_unknown", True),
        ("abort_unknown", True),
        ("version", True),
    ],
)
def test_failure_preserves_unknown_responsibility_and_closes_handles(
    failure, unsettled
):
    client = TrainingS3(failure)
    with pytest.raises(TrainingDatasetError) as caught:
        service.probe_target(
            replace(
                target(client), purpose="training", key_prefix="_aap_managed/training/"
            )
        )
    assert (
        bool(getattr(caught.value, "storage_operation_unsettled", False)) == unsettled
    )
    assert "private" not in str(caught.value)
    assert client.closed == 1 and all(body.closed for body in client.bodies)
    if failure in {
        "put_unknown",
        "complete_unknown",
        "abort_unknown",
        "version",
        "delete",
    }:
        assert client.objects
    else:
        assert not client.objects


@pytest.mark.parametrize("failure", [None, "read"])
def test_client_close_failure_preserves_unsettled_responsibility(failure):
    class UnclosedS3(TrainingS3):
        @contextmanager
        def client(self, *, read_timeout=None):
            with super().client(read_timeout=read_timeout) as client:
                yield client
            raise OSError("private close detail")

    client = UnclosedS3(failure)
    with pytest.raises(TrainingDatasetError) as caught:
        service.probe_target(
            replace(
                target(client), purpose="training", key_prefix="_aap_managed/training/"
            )
        )
    assert caught.value.storage_operation_unsettled
    assert "private" not in str(caught.value)
    assert client.closed == 1 and all(body.closed for body in client.bodies)
    assert not client.objects and not client.uploads


def test_failed_client_entry_keeps_unsettled_responsibility():
    class UnopenedS3(TrainingS3):
        @contextmanager
        def client(self, *, read_timeout=None):
            raise OSError("private client open detail")
            yield self

    client = UnopenedS3()
    with pytest.raises(TrainingDatasetError) as caught:
        service.probe_target(
            replace(
                target(client), purpose="training", key_prefix="_aap_managed/training/"
            )
        )
    assert caught.value.storage_operation_unsettled
    assert "private" not in str(caught.value)
    assert not client.calls and not client.objects
