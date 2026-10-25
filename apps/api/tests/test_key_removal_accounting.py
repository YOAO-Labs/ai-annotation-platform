"""Key-only DELETE proves a new marker, never historical-version deletion."""

import pytest

from app.services import export_media_removal_accounting as version_removal
from app.services import key_removal_accounting as removal
from app.services import object_removal_accounting
from tests.test_export_media_removal_accounting import KEY, MemoryRecorder, Provider


class MarkerProvider(Provider):
    def __init__(self, endpoint="http://saved.example"):
        super().__init__(endpoint)
        self.marker, self.is_marker = "new-marker", True
        self.fail_delete = None
        self.parameter_changes = {}

    def delete_object(self, **params):
        if self.hooks:
            for _, result in self.meta.events.emit(
                "before-parameter-build.s3.DeleteObject",
                params={**params, **self.parameter_changes},
            ):
                assert result is None
            for _ in range(self.sends):
                for _, result in self.meta.events.emit("before-send.s3.DeleteObject"):
                    assert result is None
        self.calls.append(dict(params))
        if self.fail_delete:
            raise self.fail_delete
        self.seed(self.marker, marker=self.is_marker, key=params["Key"])
        if self.mutate:
            self.mutate(self)
        if self.lose_ack:
            raise OSError("lost deletion ACK")
        return {
            "ResponseMetadata": {"HTTPStatusCode": self.status},
            "DeleteMarker": self.is_marker,
            "VersionId": self.marker
            if self.ack_version == "requested"
            else self.ack_version,
        }


def observe(client):
    rec = MemoryRecorder()
    observation = removal.Observation(rec, client, KEY)
    response = observation.submit(
        lambda: client.delete_object(Bucket=rec.bucket, Key=KEY)
    )
    observation.complete(response)
    return observation.attempt.evidence


@pytest.mark.parametrize("status", [200, 204])
@pytest.mark.parametrize("before,delta", [("object", -3), ("marker", 0), ("absent", 0)])
def test_exact_new_marker_proves_current_delta_only(before, delta, status):
    client = MarkerProvider()
    client.status = status
    if before != "absent":
        client.seed("old-object")
    if before == "marker":
        client.seed("old-marker", marker=True)
    client.seed("neighbor", b"not this key", key=KEY + "/neighbor")
    evidence = observe(client)
    assert evidence["current_delta_bytes"] == delta
    assert evidence["delete_marker_created"] is True
    assert evidence["physical_outcome"] == "unknown"
    assert evidence["returned_version"] == "new-marker"
    assert client.calls == [{"Bucket": "saved", "Key": KEY}]
    assert not evidence.get("version_removed")


@pytest.mark.parametrize(
    "problem",
    [
        "null",
        "missing_version",
        "no_hooks",
        "retry",
        "no_send",
        "ack_wrong",
        "list_denied",
        "concurrent",
        "old_changed",
        "old_removed",
        "not_marker",
        "existing_marker",
        "wrong_head",
        "status",
        "error",
        "missing_events",
    ],
)
def test_uncertain_marker_or_window_preserves_delete_success(problem):
    client = MarkerProvider()
    client.seed("old-object")
    if problem == "null":
        client.marker = "null"
    elif problem == "missing_version":
        client.ack_version = None
    elif problem == "no_hooks":
        client.hooks = False
    elif problem == "retry":
        client.sends = 2
    elif problem == "no_send":
        client.sends = 0
    elif problem == "ack_wrong":
        client.ack_version = "other"
    elif problem == "list_denied":
        client.fail_list = True
    elif problem == "concurrent":
        client.mutate = lambda c: c.seed("concurrent", b"new")
    elif problem == "old_changed":
        client.mutate = lambda c: c.rows[KEY][0].update(data=b"changed")
    elif problem == "old_removed":
        client.mutate = lambda c: c.rows[KEY].pop(0)
    elif problem == "not_marker":
        client.is_marker = False
    elif problem == "existing_marker":
        client.seed("new-marker", marker=True)
    elif problem == "wrong_head":
        client.mutate = lambda c: c.rows[KEY].reverse()
    elif problem == "status":
        client.status = 500
    elif problem == "error":
        original = client.delete_object
        client.delete_object = lambda **params: {
            **original(**params),
            "Error": {"Code": "x"},
        }
    elif problem == "missing_events":
        client.meta.events = None
        client.hooks = False
    evidence = observe(client)
    assert evidence["physical_outcome"] == "unknown"
    assert "current_delta_bytes" not in evidence
    assert "delete_marker_created" not in evidence
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "changes",
    [{"Bucket": "other"}, {"Key": "other"}, {"VersionId": None}, {"VersionId": "v1"}],
)
def test_key_only_parameters_must_match_without_any_version(changes):
    client = MarkerProvider()
    client.seed("old-object")
    client.parameter_changes = changes
    assert "current_delta_bytes" not in observe(client)


def test_lost_ack_keeps_original_failure_and_independent_unknown():
    client, rec = MarkerProvider(), MemoryRecorder()
    client.lose_ack = True
    observation = removal.Observation(rec, client, KEY)
    with pytest.raises(OSError, match="lost deletion ACK"):
        observation.submit(lambda: client.delete_object(Bucket=rec.bucket, Key=KEY))
    assert len(rec.attempts) == 1
    assert "current_delta_bytes" not in rec.attempts[0].evidence


def test_intent_failure_precedes_snapshot_and_actual_delete():
    client, rec = MarkerProvider(), MemoryRecorder()

    def broken(*args):
        raise OSError("intent failed")

    rec.begin = broken
    with pytest.raises(OSError, match="intent failed"):
        removal.Observation(rec, client, KEY)
    assert client.calls == []


def test_protocol_reuses_existing_recorder_factory_and_finish():
    assert removal.Recorder is version_removal.Recorder
    assert removal.finish is version_removal.finish
    assert removal.recorder is object_removal_accounting.recorder
