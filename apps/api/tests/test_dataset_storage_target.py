"""The switch probes the same empty and multipart conditions as real writes."""

from contextlib import contextmanager
import uuid

import pytest

from app.services import dataset_storage_target as target_service
from app.services.dataset_storage import DatasetStorageError
from app.services.managed_s3_target import Target
from tests.test_managed_original_writer import MemoryS3, error, response


class ProbeS3(MemoryS3):
    def __init__(self, failure=None):
        super().__init__()
        self.failure, self.completes, self.closed = failure, 0, 0

    def complete_multipart_upload(self, **params):
        self.completes += 1
        if self.failure in {"first_unknown", "abort_unknown"} and self.completes == 1:
            raise RuntimeError("private completion detail")
        if self.failure == "duplicate_unknown" and self.completes == 2:
            raise RuntimeError("private duplicate detail")
        if (
            self.failure in {"duplicate_denied", "duplicate_denied_abort"}
            and self.completes == 2
        ):
            raise error("AccessDenied", 403)
        return super().complete_multipart_upload(**params)

    def abort_multipart_upload(self, **params):
        if self.failure in {"abort_unknown", "duplicate_denied_abort"}:
            raise RuntimeError("private abort detail")
        return super().abort_multipart_upload(**params)

    def delete_object(self, **params):
        self.record("delete", params)
        pair = params["Bucket"], params["Key"]
        if params.get("VersionId") is not None:
            self.versions.pop((*pair, params["VersionId"]), None)
        if pair in self.objects and (
            params.get("VersionId") is None
            or self.objects[pair]["VersionId"] == params["VersionId"]
        ):
            self.objects.pop(pair)
        return response()

    @contextmanager
    def client(self, *, read_timeout=None):
        assert read_timeout == 60
        try:
            yield self
        finally:
            self.closed += 1


def target(client):
    return Target(
        uuid.uuid4(),
        1,
        1,
        "http://objects.example.test",
        "owned",
        "_aap_managed/datasets/",
        purpose="datasets",
        _access=client,
    )


@pytest.mark.parametrize("versioned", [False, True])
def test_empty_and_multipart_probes_use_conditions_and_clean_every_handle(versioned):
    client = ProbeS3()
    client.versioned = versioned
    result = target_service.probe_target(target(client))
    assert result["empty_put"] and result["multipart"]
    assert result["conditional_write"] and result["full_read"] and result["delete"]
    assert not client.objects and not client.versions and not client.uploads
    assert client.closed == 1 and all(body.closed for body in client.bodies)
    assert len([call for call in client.calls if call[0] == "put"]) == 2
    assert len([call for call in client.calls if call[0] == "complete"]) == 2


@pytest.mark.parametrize(
    "failure",
    [
        "first_unknown",
        "duplicate_unknown",
        "duplicate_denied",
        "abort_unknown",
        "duplicate_denied_abort",
    ],
)
def test_failed_completion_keeps_unknown_responsibility_and_exact_abort(failure):
    client = ProbeS3(failure)
    with pytest.raises(DatasetStorageError) as caught:
        target_service.probe_target(target(client))
    assert bool(getattr(caught.value, "storage_operation_unsettled", False)) == (
        failure != "duplicate_denied"
    )
    assert client.closed == 1 and "private" not in str(caught.value)
    assert bool(client.uploads) == (
        failure in {"abort_unknown", "duplicate_denied_abort"}
    )
    if failure == "duplicate_unknown":
        assert len(client.objects) == 1
        nonempty_deletes = [
            params
            for operation, params in client.calls
            if operation == "delete"
            and params["Key"] in {key for _, key in client.objects}
        ]
        assert not nonempty_deletes
    elif failure != "abort_unknown":
        assert not client.objects
