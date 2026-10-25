"""Use cache PUT/read/LIST conditions and retain unknown final responsibility."""

from contextlib import contextmanager
import uuid

import pytest

from app.services import media_cache_storage_target as service
from app.services.managed_s3_target import Target
from tests.test_dataset_storage_target import ProbeS3
from tests.test_managed_original_writer import error, response


class CacheS3(ProbeS3):
    def __init__(self, failure=None):
        super().__init__(failure)
        self.ignore_get_condition = True
        self.puts = 0

    def put_object(self, **params):
        self.record("put", params)
        self.puts += 1
        assert params["IfNoneMatch"] == "*" and params["Body"]
        if self.puts == 2 and self.failure == "duplicate_unknown":
            self.store(params["Bucket"], params["Key"], b"unreceipted")
            raise RuntimeError("private unknown duplicate")
        if (params["Bucket"], params["Key"]) in self.objects:
            raise error("PreconditionFailed", 412)
        value = self.store(params["Bucket"], params["Key"], params["Body"])
        if self.failure == "first_unknown":
            raise RuntimeError("private unknown put")
        return response(ETag=value["ETag"], VersionId=value["VersionId"])

    def list_objects_v2(self, **params):
        self.record("list", params)
        if self.failure == "list_denied":
            raise error("AccessDenied", 403)
        return response(
            IsTruncated=False,
            Contents=[
                {"Key": key, "Size": len(value["data"])}
                for (bucket, key), value in self.objects.items()
                if bucket == params["Bucket"] and key.startswith(params["Prefix"])
            ],
        )

    def delete_object(self, **params):
        if self.failure == "cleanup_unknown":
            raise RuntimeError("private cleanup")
        return super().delete_object(**params)

    @contextmanager
    def client(self, *, read_timeout=None):
        assert read_timeout == 60
        try:
            yield self
        finally:
            self.closed += 1
            if self.failure == "close_unknown":
                raise RuntimeError("private close")


def target(client):
    return Target(
        uuid.uuid4(),
        1,
        1,
        "https://objects.example.test",
        "saved-cache",
        "company/_aap_managed/media-cache/",
        purpose="media-cache",
        _access=client,
    )


@pytest.mark.parametrize("versioned", [False, True])
def test_real_cache_contract_uses_only_put_full_read_list_and_exact_cleanup(versioned):
    client = CacheS3()
    client.versioned = versioned
    result = service.probe_target(target(client))
    assert result == {
        "conditional_write": True,
        "full_read": True,
        "list": True,
        "delete": True,
    }
    assert not client.objects and not client.versions and not client.uploads
    assert client.closed == 1 and all(body.closed for body in client.bodies)
    assert not any(
        operation in {"create", "part", "complete"} for operation, _ in client.calls
    )


@pytest.mark.parametrize(
    "failure",
    [
        "first_unknown",
        "duplicate_unknown",
        "list_denied",
        "cleanup_unknown",
        "close_unknown",
    ],
)
def test_probe_does_not_blind_delete_unknown_final_or_release_unknown_handle(failure):
    client = CacheS3(failure)
    with pytest.raises(Exception) as caught:
        service.probe_target(target(client))
    assert bool(getattr(caught.value, "storage_operation_unsettled", False)) == (
        failure != "list_denied"
    )
    assert client.closed == 1
    if failure in {"first_unknown", "duplicate_unknown"}:
        assert client.objects and not any(
            operation == "delete" for operation, _ in client.calls
        )
