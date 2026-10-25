"""The annotation switch probes the actual empty and nonempty original writer."""

from dataclasses import replace

import pytest

from app.services import annotations_storage_target as service
from app.services.annotations_storage import AnnotationsStorageError
from tests.test_dataset_storage_target import ProbeS3, target


@pytest.mark.parametrize("versioned", [False, True])
def test_existing_probe_conditions_read_and_exact_cleanup(versioned):
    client = ProbeS3()
    client.versioned = versioned
    result = service.probe_target(
        replace(
            target(client),
            purpose="annotations",
            key_prefix="_aap_managed/annotations/",
        )
    )
    assert result == {
        "conditional_write": True,
        "full_read": True,
        "delete": True,
        "empty_put": True,
        "multipart": True,
    }
    assert not client.objects and not client.versions and not client.uploads
    assert client.closed == 1 and all(body.closed for body in client.bodies)


def test_unknown_completion_preserves_unsettled_responsibility():
    client = ProbeS3("duplicate_unknown")
    with pytest.raises(AnnotationsStorageError) as caught:
        service.probe_target(replace(target(client), purpose="annotations"))
    assert caught.value.storage_operation_unsettled
    assert client.objects and client.closed == 1
