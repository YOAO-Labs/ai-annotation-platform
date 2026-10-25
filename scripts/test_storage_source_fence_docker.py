"""Pure native-boundary checks; the independent lab owns real Docker/MinIO I/O."""

from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import storage_source_fence_docker as native
from storage_source_fence_state import FenceError


class ProviderError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class World:
    def __init__(self, directory):
        self.config = dict(
            schema="aap.storage-source-fence-config/v1",
            owner="a" * 32,
            checkout=str(native._ROOT),
            source_container="1" * 64,
            gateway_container="2" * 64,
            network_id="3" * 64,
            gateway_frontend_network_id="4" * 64,
            source_volume_name="owned-source-data",
            gateway_config_directory=str(directory),
            public_endpoint="http://127.0.0.1:19234",
            purpose_buckets={
                purpose: "owned-" + purpose for purpose in native.PURPOSES
            },
        )
        self.ip = "172.30.20.2"
        self.counter = 0
        self.calls = []
        self.native_records = b"0\n"
        self.source = self.container("source", native._MINIO_ID)
        self.source["Config"].update(
            Entrypoint=["/usr/bin/docker-entrypoint.sh"],
            Cmd=["server", "/data", "--console-address", ":9001"],
        )
        self.gateway = self.container("gateway", native._NGINX_ID)
        self.source["Mounts"] = [
            dict(
                Type="volume",
                Name=self.config["source_volume_name"],
                Destination="/data",
                RW=True,
            )
        ]
        self.source["HostConfig"]["PortBindings"] = {}
        self.gateway["Mounts"] = [
            dict(Type="bind", Source=str(directory), Destination="/etc/nginx", RW=False)
        ]
        self.gateway["Config"].update(Entrypoint=["nginx"], Cmd=["-g", "daemon off;"])
        self.gateway["HostConfig"]["Sysctls"] = {"net.ipv4.ip_forward": "0"}
        self.gateway["NetworkSettings"]["Networks"]["owned-frontend"] = {
            "NetworkID": self.config["gateway_frontend_network_id"],
            "IPAddress": "172.31.20.2",
        }
        self.gateway["NetworkSettings"]["Ports"] = {
            "80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "19234"}]
        }
        self.network = dict(
            Id=self.config["network_id"],
            Internal=True,
            Labels=self.labels("network"),
            Containers={
                self.config["source_container"]: {},
                self.config["gateway_container"]: {},
            },
        )
        self.volume = dict(
            Name=self.config["source_volume_name"],
            Driver="local",
            Options=None,
            Labels=self.labels("data"),
        )
        self.frontend = dict(
            Id=self.config["gateway_frontend_network_id"],
            Driver="bridge",
            Internal=False,
            Labels=self.labels("frontend"),
            Containers={self.config["gateway_container"]: {}},
        )
        self.images = {
            native._MINIO_ID: dict(
                Id=native._MINIO_ID, RepoDigests=[native.MINIO_IMAGE]
            ),
            native._NGINX_ID: dict(
                Id=native._NGINX_ID, RepoDigests=[native.NGINX_IMAGE]
            ),
        }
        self.write(False)

    def labels(self, role):
        return {
            native.OWNER_LABEL: self.config["owner"],
            native.CHECKOUT_LABEL: self.config["checkout"],
            native.ROLE_LABEL: role,
        }

    def container(self, role, image):
        identity = self.config[
            "source_container" if role == "source" else "gateway_container"
        ]
        return dict(
            Id=identity,
            Image=image,
            Config={"Labels": self.labels(role)},
            HostConfig={
                "Privileged": False,
                "PidMode": "",
                "NetworkMode": "owned-network",
            },
            NetworkSettings={
                "Networks": {
                    "owned-network": {
                        "NetworkID": self.config["network_id"],
                        "IPAddress": self.ip,
                    }
                },
                "Ports": {},
            },
            State={
                "Running": True,
                "Pid": 123 if role == "source" else 124,
                "StartedAt": "start-0",
            },
            Mounts=[],
        )

    def write(self, readonly):
        path = Path(self.config["gateway_config_directory"]) / "nginx.conf"
        path.write_bytes(native.gateway_config(self.ip, readonly))
        path.chmod(0o644)

    def docker(self, *args):
        self.calls.append(args)
        if args[0] == "exec":
            assert args == (
                "exec",
                self.config["source_container"],
                "/bin/sh",
                "-c",
                native._MULTIPART_RECORDS,
            )
            return self.native_records
        if len(args) == 3 and args[1] == "inspect":
            kind, _, identity = args
            if kind == "container":
                value = (
                    self.source
                    if identity == self.config["source_container"]
                    else self.gateway
                )
            elif kind == "network":
                value = (
                    self.network
                    if identity == self.config["network_id"]
                    else self.frontend
                )
            elif kind == "volume":
                value = self.volume
            else:
                value = self.images[identity]
            return json.dumps([value]).encode()
        self.counter += 1
        if args[0] == "stop":
            self.source["State"].update(
                Running=False, Pid=0, FinishedAt=f"stop-{self.counter}"
            )
            self.source["NetworkSettings"]["Networks"]["owned-network"]["IPAddress"] = (
                ""
            )
            self.network["Containers"].pop(self.config["source_container"], None)
        elif args[0] == "start":
            self.source["State"].update(
                Running=True, Pid=200 + self.counter, StartedAt=f"start-{self.counter}"
            )
            self.source["NetworkSettings"]["Networks"]["owned-network"]["IPAddress"] = (
                self.ip
            )
            self.network["Containers"][self.config["source_container"]] = {}
        elif args[0] == "restart":
            self.gateway["State"].update(
                Pid=300 + self.counter, StartedAt=f"gateway-{self.counter}"
            )
        else:
            raise AssertionError(args)
        return b"done"


class Client:
    def __init__(self, buckets):
        self.buckets = buckets
        self.rules = {bucket: None for bucket in buckets}
        self.objects = {bucket: [] for bucket in buckets}
        self.uploads = {bucket: [] for bucket in buckets}
        self.cache_missing = False
        self.calls = []
        self.heads = {}
        self.closed = 0
        self.replication_error = "ReplicationConfigurationNotFoundError"
        self.meta = SimpleNamespace(events=SimpleNamespace(register=self.register))

    def register(self, name, callback):
        self.hook = callback

    def list_buckets(self, **kwargs):
        return {"Buckets": [{"Name": bucket} for bucket in self.buckets]}

    def get_bucket_replication(self, **kwargs):
        if self.replication_error:
            raise ProviderError(self.replication_error)
        return {"ReplicationConfiguration": {}}

    def get_bucket_lifecycle_configuration(self, *, Bucket):
        if self.rules[Bucket] is None:
            raise ProviderError("NoSuchLifecycleConfiguration")
        return {"Rules": copy.deepcopy(self.rules[Bucket])}

    def put_bucket_lifecycle_configuration(self, *, Bucket, LifecycleConfiguration):
        self.calls.append(("lifecycle", Bucket, copy.deepcopy(LifecycleConfiguration)))
        self.rules[Bucket] = copy.deepcopy(LifecycleConfiguration["Rules"])

    def get_bucket_versioning(self, **kwargs):
        return {"Status": "Enabled"}

    def list_object_versions(self, *, Bucket, **kwargs):
        return {"Versions": self.objects[Bucket], "IsTruncated": False}

    def head_object(self, **kwargs):
        self.calls.append(("head", dict(kwargs)))
        version = kwargs.get("VersionId")
        return self.heads.get(version, {"VersionId": version})

    def list_multipart_uploads(self, *, Bucket, **kwargs):
        return {
            "Uploads": [] if self.cache_missing else list(self.uploads[Bucket]),
            "IsTruncated": False,
        }

    def list_parts(self, **kwargs):
        self.calls.append(("parts", dict(kwargs)))
        if any(
            value["Key"] == kwargs["Key"] and value["UploadId"] == kwargs["UploadId"]
            for value in self.uploads[kwargs["Bucket"]]
        ):
            return {"Parts": []}
        raise ProviderError("NoSuchUpload")

    def abort_multipart_upload(self, **kwargs):
        self.calls.append(("abort", dict(kwargs)))
        self.uploads[kwargs["Bucket"]] = [
            value
            for value in self.uploads[kwargs["Bucket"]]
            if value["UploadId"] != kwargs["UploadId"]
        ]

    def close(self):
        self.closed += 1


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aap-fence-unit-")
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        directory.chmod(0o700)
        self.world = World(directory)
        self.adapter = native.DockerAdapter(self.world.config)
        self.adapter._docker = self.world.docker
        self.adapter._ready = lambda: None
        self.adapter._admin = lambda: None
        self.client = Client(sorted(set(self.world.config["purpose_buckets"].values())))

    @contextmanager
    def client_context(self):
        try:
            yield self.client
        finally:
            self.client.close()

    def use_client(self):
        self.adapter._client = self.client_context

    def code(self, expected, function):
        with self.assertRaises(FenceError) as raised:
            function()
        self.assertEqual(str(raised.exception), expected)

    def test_gateway_preserves_signature_and_has_real_readonly_filter(self):
        data = native.gateway_config(self.world.ip, True).decode()
        self.assertIn("listen 80;", data)
        self.assertIn("proxy_set_header Host $http_host;", data)
        self.assertIn("return 405", data)
        self.assertIn("proxy_ignore_client_abort on", data)
        self.assertIn("proxy_request_buffering off", data)
        self.assertIn("location ~ ^/(minio|api)(/|$)", data)
        self.assertNotIn("rewrite", data)
        self.assertNotIn(
            "return 405", native.gateway_config(self.world.ip, False).decode()
        )

    def test_gateway_rejects_injected_address(self):
        self.code(
            "native_address_invalid",
            lambda: native.gateway_config("1.2.3.4;return 200", True),
        )

    def test_config_extra_fields_and_bad_identity_rejected(self):
        for delta in (
            {"extra": True},
            {"owner": "a" * 31},
            {"checkout": "/tmp/other"},
            {"purpose_buckets": {}},
        ):
            with self.subTest(delta=delta):
                with self.assertRaises(FenceError):
                    native.DockerAdapter({**self.world.config, **delta})

    def test_https_or_non_loopback_is_not_qualified(self):
        for endpoint in (
            "https://127.0.0.1:19234",
            "http://10.0.0.2:19234",
            "http://u:p@127.0.0.1:19234",
            "http://127.0.0.1:19234/a",
        ):
            with self.subTest(endpoint=endpoint):
                self.code(
                    "native_endpoint_unsupported",
                    lambda: native.DockerAdapter(
                        {**self.world.config, "public_endpoint": endpoint}
                    ),
                )

    def test_config_file_symlink_and_permissions_rejected(self):
        path = Path(self.world.config["gateway_config_directory"]) / "nginx.conf"
        path.chmod(0o666)
        self.code("native_config_permissions", self.adapter._files)
        path.unlink()
        path.symlink_to("/dev/null")
        self.code("native_config_permissions", self.adapter._files)

    def test_topology_and_image_ownership_are_checked_before_writing(self):
        mutations = [
            lambda: self.world.network.update(Internal=False),
            lambda: self.world.network["Containers"].update({"9" * 64: {}}),
            lambda: self.world.source["Config"]["Labels"].update(
                {native.OWNER_LABEL: "b" * 32}
            ),
            lambda: self.world.gateway["Mounts"][0].update(RW=True),
            lambda: self.world.source.update(Image=native._NGINX_ID),
            lambda: self.world.source["HostConfig"].update(
                PortBindings={"9000/tcp": [{"HostPort": "9000"}]}
            ),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                saved = copy.deepcopy(self.world.__dict__)
                mutate()
                with self.assertRaises(FenceError):
                    self.adapter.readonly()
                self.assertFalse(
                    any(
                        call[0] in {"restart", "stop", "start"}
                        for call in self.world.calls
                    )
                )
                self.world.__dict__ = saved

    def test_frontend_descriptor_is_required_and_cannot_alias_private_network(self):
        missing = dict(self.world.config)
        missing.pop("gateway_frontend_network_id")
        self.code("native_config_invalid", lambda: native.DockerAdapter(missing))
        self.code(
            "native_config_invalid",
            lambda: native.DockerAdapter(
                {
                    **self.world.config,
                    "gateway_frontend_network_id": self.world.config["network_id"],
                }
            ),
        )

    def test_two_network_gateway_never_grants_source_or_routing_bypass(self):
        mutations = [
            lambda: self.world.frontend.update(Internal=True),
            lambda: self.world.frontend.update(Driver="overlay"),
            lambda: self.world.frontend.update(Id="5" * 64),
            lambda: self.world.frontend["Labels"].update(
                {native.ROLE_LABEL: "network"}
            ),
            lambda: self.world.frontend["Labels"].update(
                {native.OWNER_LABEL: "b" * 32}
            ),
            lambda: self.world.frontend["Containers"].update(
                {self.world.config["source_container"]: {}}
            ),
            lambda: self.world.gateway["NetworkSettings"]["Networks"].pop(
                "owned-frontend"
            ),
            lambda: self.world.source["NetworkSettings"]["Networks"].update(
                {
                    "owned-frontend": {
                        "NetworkID": self.world.config["gateway_frontend_network_id"]
                    }
                }
            ),
            lambda: self.world.gateway["HostConfig"].update(
                Sysctls={"net.ipv4.ip_forward": "1"}
            ),
            lambda: self.world.gateway["HostConfig"].pop("Sysctls"),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                saved = copy.deepcopy(self.world.__dict__)
                mutate()
                with self.assertRaises(FenceError):
                    self.adapter.readonly()
                self.assertFalse(
                    any(
                        call[0] in {"restart", "stop", "start"}
                        for call in self.world.calls
                    )
                )
                self.world.__dict__ = saved

    def test_noncanonical_gateway_file_rejected(self):
        (Path(self.world.config["gateway_config_directory"]) / "nginx.conf").write_text(
            "events {}"
        )
        self.code("native_config_changed", self.adapter.readonly)

    def test_readonly_always_restarts_even_when_file_already_readonly(self):
        self.world.write(True)
        self.adapter.readonly()
        self.adapter.readonly()
        self.assertEqual(sum(call[0] == "restart" for call in self.world.calls), 2)

    def test_gateway_generation_drift_rejected(self):
        self.adapter.readonly()
        self.world.gateway["State"]["StartedAt"] = "outside-restart"
        self.code("native_gateway_generation_changed", self.adapter._observe)

    def test_barrier_proves_exit_despite_removed_network_member(self):
        self.adapter.readonly()
        receipt = self.adapter.barrier()
        self.assertEqual(receipt["stopped_pid"], 0)
        self.assertEqual(receipt["started_at"], self.world.source["State"]["StartedAt"])
        self.assertEqual(
            receipt["gateway"], self.adapter._generation(self.world.gateway)
        )
        self.assertTrue(any(call[0] == "stop" for call in self.world.calls))
        self.assertTrue(any(call[0] == "start" for call in self.world.calls))

    def test_barrier_resumes_an_exact_owned_stopped_source(self):
        self.world.write(True)
        self.world.docker("stop", "--time", "5", self.world.config["source_container"])
        receipt = self.adapter.barrier()
        self.assertEqual(receipt["stopped_pid"], 0)
        self.assertTrue(self.world.source["State"]["Running"])

    def test_public_readonly_resumes_stopped_source_after_real_gateway_restart(self):
        self.world.docker("stop", "--time", "5", self.world.config["source_container"])
        self.world.calls.clear()
        self.adapter.readonly()
        changes = [
            call[0] for call in self.world.calls if call[0] in {"restart", "start"}
        ]
        self.assertEqual(changes, ["restart", "start"])
        self.assertTrue(self.world.source["State"]["Running"])
        self.assertEqual(
            self.adapter._files(), native.gateway_config(self.world.ip, True)
        )
        self.assertEqual(self.adapter.known_uploads, [])
        self.use_client()
        snapshot = self.adapter.prepare()
        self.adapter.disable_lifecycle(snapshot)
        self.adapter.barrier()

    def test_restore_entry_restores_stopped_source_readonly_before_lifecycle(self):
        self.use_client()
        self.adapter.readonly()
        bucket = self.client.buckets[0]
        original = [{"ID": "old", "Status": "Enabled", "Expiration": {"Days": 1}}]
        self.client.rules[bucket] = copy.deepcopy(original)
        self.client.uploads[bucket] = [{"Key": "saved", "UploadId": "known"}]
        snapshot = self.adapter.prepare()
        self.adapter.disable_lifecycle(snapshot)
        self.world.docker("stop", "--time", "5", self.world.config["source_container"])
        self.world.calls.clear()
        put = self.client.put_bucket_lifecycle_configuration

        def restore(**kwargs):
            self.assertTrue(self.world.source["State"]["Running"])
            self.assertEqual(
                self.adapter._files(), native.gateway_config(self.world.ip, True)
            )
            changes = [
                call[0] for call in self.world.calls if call[0] in {"restart", "start"}
            ]
            self.assertEqual(changes, ["restart", "start"])
            put(**kwargs)

        self.client.put_bucket_lifecycle_configuration = restore
        self.adapter.restore_lifecycle(snapshot)
        self.assertEqual(self.client.rules[bucket], original)
        self.assertEqual(self.adapter.known_uploads, snapshot["known_uploads"])
        self.adapter.writable()
        self.adapter.verify_open(snapshot)

    def test_release_without_snapshot_starts_stopped_source_behind_readonly(self):
        self.world.docker("stop", "--time", "5", self.world.config["source_container"])
        self.world.calls.clear()
        self.adapter.writable()
        changes = [
            call[0] for call in self.world.calls if call[0] in {"restart", "start"}
        ]
        self.assertEqual(changes, ["restart", "start", "restart"])
        self.adapter.verify_open(None)

    def test_readonly_resume_rejects_source_address_drift_before_ready(self):
        self.world.docker("stop", "--time", "5", self.world.config["source_container"])
        docker = self.world.docker
        ready = []

        def changed(*args):
            result = docker(*args)
            if args[0] == "start":
                self.world.source["NetworkSettings"]["Networks"]["owned-network"][
                    "IPAddress"
                ] = "172.30.20.9"
            return result

        self.adapter._docker = changed
        self.adapter._ready = lambda: ready.append(True)
        self.code("native_address_changed", self.adapter.readonly)
        self.assertEqual(ready, [])

    def test_preview_and_observation_never_start_a_stopped_source(self):
        self.use_client()
        self.world.docker("stop", "--time", "5", self.world.config["source_container"])
        self.world.calls.clear()
        for call in (self.adapter.preview, self.adapter._observe):
            with self.assertRaises(FenceError):
                call()
        self.assertFalse(
            any(call[0] in {"restart", "start"} for call in self.world.calls)
        )

    def test_restore_invalid_snapshot_does_not_restart_stopped_source(self):
        self.use_client()
        self.adapter.readonly()
        snapshot = self.adapter.prepare()
        snapshot.pop("known_uploads")
        self.world.docker("stop", "--time", "5", self.world.config["source_container"])
        self.world.calls.clear()
        self.code(
            "native_snapshot_changed", lambda: self.adapter.restore_lifecycle(snapshot)
        )
        self.assertFalse(
            any(call[0] in {"restart", "start"} for call in self.world.calls)
        )

    def test_changed_source_ip_is_rejected(self):
        self.adapter._observe()
        self.world.source["NetworkSettings"]["Networks"]["owned-network"][
            "IPAddress"
        ] = "172.30.20.9"
        self.code("native_address_changed", self.adapter._observe)

    def test_prepare_requires_readonly_and_does_not_mutate_rules(self):
        self.use_client()
        self.code("native_not_readonly", self.adapter.prepare)
        self.adapter.readonly()
        self.client.rules[self.client.buckets[0]] = [
            {"ID": "old", "Status": "Enabled", "Expiration": {"Days": 1}}
        ]
        snapshot = self.adapter.prepare()
        self.assertEqual(
            snapshot["lifecycle"][self.client.buckets[0]]["disabled"][0]["Status"],
            "Disabled",
        )
        self.assertEqual(
            self.client.rules[self.client.buckets[0]][0]["Status"], "Enabled"
        )
        self.assertEqual(self.client.calls, [])

    def test_lifecycle_disable_restore_cas_and_existing_disabled_are_idempotent(self):
        self.use_client()
        self.adapter.readonly()
        bucket = self.client.buckets[0]
        original = [
            {
                "ID": "old",
                "Status": "Enabled",
                "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
            }
        ]
        self.client.rules[bucket] = copy.deepcopy(original)
        snapshot = self.adapter.prepare()
        self.adapter.disable_lifecycle(snapshot)
        self.adapter.disable_lifecycle(snapshot)
        self.assertEqual(self.client.rules[bucket][0]["Status"], "Disabled")
        self.adapter.restore_lifecycle(snapshot)
        self.adapter.restore_lifecycle(snapshot)
        self.assertEqual(self.client.rules[bucket], original)
        self.assertEqual(sum(call[0] == "lifecycle" for call in self.client.calls), 2)

    def test_rule_drift_is_never_overwritten(self):
        self.use_client()
        self.adapter.readonly()
        bucket = self.client.buckets[0]
        self.client.rules[bucket] = [{"Status": "Enabled", "Expiration": {"Days": 1}}]
        snapshot = self.adapter.prepare()
        self.client.rules[bucket][0]["Expiration"]["Days"] = 2
        self.code(
            "source_lifecycle_changed", lambda: self.adapter.disable_lifecycle(snapshot)
        )
        self.assertEqual(self.client.calls, [])

    def test_transition_rule_not_silently_dropped(self):
        self.client.rules[self.client.buckets[0]] = [
            {"Status": "Disabled", "Transition": {"Days": 1, "StorageClass": "WARM"}}
        ]
        self.code(
            "source_lifecycle_unsupported",
            lambda: self.adapter._lifecycle(self.client, self.client.buckets[0]),
        )

    def test_date_and_abort_incomplete_rules_roundtrip_without_deletion(self):
        self.use_client()
        self.adapter.readonly()
        bucket = self.client.buckets[0]
        self.client.rules[bucket] = [
            {
                "ID": "existing-date",
                "Status": "Enabled",
                "Expiration": {"Date": datetime(2030, 1, 1, tzinfo=timezone.utc)},
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
            }
        ]
        snapshot = self.adapter.prepare()
        self.adapter.disable_lifecycle(snapshot)
        self.adapter.restore_lifecycle(snapshot)
        self.assertEqual(
            self.client.rules[bucket][0]["Expiration"]["Date"],
            "2030-01-01T00:00:00+00:00",
        )
        self.assertEqual(
            self.client.rules[bucket][0]["AbortIncompleteMultipartUpload"],
            {"DaysAfterInitiation": 1},
        )

    def test_partial_bucket_visibility_is_not_whole_source_proof(self):
        self.use_client()
        self.client.buckets.append("unregistered-bucket")
        self.code("source_bucket_scope_changed", self.adapter.preview)

    def test_replication_permission_failure_is_unknown_not_absence(self):
        self.use_client()
        self.client.replication_error = "AccessDenied"
        self.code("source_replication_unobservable", self.adapter.preview)

    def test_replication_configuration_rejected(self):
        self.use_client()
        self.client.replication_error = None
        self.code("source_replication_configured", self.adapter.preview)

    def test_all_versions_including_null_are_headed(self):
        self.use_client()
        bucket = self.client.buckets[0]
        self.client.objects[bucket] = [
            {"Key": "a", "VersionId": "null"},
            {"Key": "a", "VersionId": "old"},
        ]
        self.client.heads["null"] = {}
        counts = self.adapter.preview()
        self.assertEqual(counts["versions"], 2)
        self.assertEqual(
            [call[1]["VersionId"] for call in self.client.calls], ["null", "old"]
        )

    def test_old_remote_storage_class_is_not_an_extra_fence_gate(self):
        self.use_client()
        bucket = self.client.buckets[0]
        self.client.objects[bucket] = [{"Key": "a", "VersionId": "old"}]
        self.client.heads["old"] = {"VersionId": "old", "StorageClass": "OLD_TIER"}
        self.assertEqual(self.adapter.preview()["versions"], 1)

    def test_restore_or_wrong_version_blocks_observation(self):
        self.use_client()
        bucket = self.client.buckets[0]
        self.client.objects[bucket] = [{"Key": "a", "VersionId": "old"}]
        for response, error in (
            (
                {"VersionId": "old", "Restore": 'ongoing-request="false"'},
                "source_restore_present",
            ),
            (
                {
                    "VersionId": "old",
                    "ResponseMetadata": {"HTTPHeaders": {"X-Amz-Restore": "malformed"}},
                },
                "source_restore_present",
            ),
            ({"VersionId": "other"}, "source_object_changed"),
        ):
            with self.subTest(response=response):
                self.client.heads["old"] = response
                self.code(error, self.adapter.preview)

    def test_repeated_version_page_token_rejected(self):
        self.use_client()
        self.client.list_object_versions = lambda **kw: {
            "Versions": [],
            "IsTruncated": True,
            "NextKeyMarker": "same",
            "NextVersionIdMarker": "same",
        }
        self.code("source_pagination_invalid", self.adapter.preview)

    def test_unversioned_pages_use_original_key_only_head(self):
        self.use_client()
        self.client.get_bucket_versioning = lambda **kw: {}
        self.client.list_objects_v2 = lambda **kw: {
            "Contents": [{"Key": "x"}],
            "IsTruncated": False,
        }
        self.adapter.preview()
        self.assertTrue(all("VersionId" not in call[1] for call in self.client.calls))

    def test_abort_only_actual_multipart_not_complete_objects(self):
        self.use_client()
        self.adapter.readonly()
        self.client.uploads[self.client.buckets[0]] = [{"Key": "x", "UploadId": "u"}]
        self.assertEqual(self.adapter.abort_uploads(), 1)
        self.assertEqual(
            [call[0] for call in self.client.calls], ["parts", "abort", "parts"]
        )

    def test_abort_missing_upload_allowed_other_failure_keeps_responsibility(self):
        self.use_client()
        self.adapter.readonly()
        self.client.uploads[self.client.buckets[0]] = [{"Key": "x", "UploadId": "u"}]

        def abort(**kw):
            raise ProviderError("NoSuchUpload")

        self.client.abort_multipart_upload = abort
        self.client.list_parts = abort
        self.assertEqual(self.adapter.abort_uploads(), 0)

        def denied(**kw):
            raise ProviderError("AccessDenied")

        self.client.list_parts = lambda **kw: {"Parts": []}
        self.client.abort_multipart_upload = denied
        self.code("source_upload_abort_failed", self.adapter.abort_uploads)

    def test_resume_aborts_saved_upload_ids_when_restart_empties_list_cache(self):
        self.use_client()
        self.adapter.readonly()
        bucket = self.client.buckets[0]
        self.client.uploads[bucket] = [
            {"Key": "with-part", "UploadId": "part-id"},
            {"Key": "empty", "UploadId": "empty-id"},
        ]
        snapshot = self.adapter.prepare()
        self.assertEqual(len(snapshot["known_uploads"]), 2)
        self.client.cache_missing = True
        resumed = native.DockerAdapter(self.world.config)
        resumed._docker = self.world.docker
        resumed._ready = lambda: None
        resumed._admin = lambda: None
        resumed._client = self.client_context
        resumed.disable_lifecycle(snapshot)
        self.assertEqual(resumed.abort_uploads(), 2)
        self.assertEqual(resumed.abort_uploads(), 0)
        barriers = [resumed.barrier(), resumed.barrier()]
        observed = resumed.verify(snapshot, barriers)
        self.assertEqual(observed["counts"]["known_uploads_verified"], 2)
        self.assertEqual(observed["counts"]["native_multipart_records"], 0)
        aborted = [value[1] for value in self.client.calls if value[0] == "abort"]
        self.assertEqual({row["UploadId"] for row in aborted}, {"part-id", "empty-id"})
        self.assertTrue(all(row["Bucket"] == bucket for row in aborted))

    def test_saved_and_current_uploads_are_merged_without_duplicate_abort(self):
        self.use_client()
        self.adapter.readonly()
        bucket = self.client.buckets[0]
        self.client.uploads[bucket] = [{"Key": "x", "UploadId": "first"}]
        snapshot = self.adapter.prepare()
        self.adapter.disable_lifecycle(snapshot)
        self.client.uploads[bucket].append({"Key": "y", "UploadId": "late"})
        self.assertEqual(self.adapter.abort_uploads(), 2)
        self.assertEqual(sum(call[0] == "abort" for call in self.client.calls), 2)

    def test_saved_upload_contract_is_strict_and_bounded_before_mutation(self):
        self.use_client()
        self.adapter.readonly()
        snapshot = self.adapter.prepare()
        good = dict(bucket=self.client.buckets[0], key="x", upload_id="u")
        bad_values = [
            None,
            [good, good],
            [{**good, "bucket": "outside"}],
            [{**good, "upload_id": ""}],
            [{**good, "key": "x" * 1025}],
            [{**good, "extra": "not-allowed"}],
        ]
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(FenceError):
                    self.adapter.disable_lifecycle({**snapshot, "known_uploads": value})
        missing = dict(snapshot)
        missing.pop("known_uploads")
        self.code(
            "native_snapshot_changed", lambda: self.adapter.disable_lifecycle(missing)
        )
        with patch.object(native, "_MAX_OUTPUT", 100):
            self.code(
                "source_budget_exceeded",
                lambda: self.adapter.disable_lifecycle(snapshot),
            )
        self.assertEqual(self.client.calls, [])

    def test_abort_ack_does_not_replace_exact_upload_absence_proof(self):
        self.use_client()
        self.adapter.readonly()
        bucket = self.client.buckets[0]
        self.client.uploads[bucket] = [{"Key": "x", "UploadId": "u"}]
        self.client.abort_multipart_upload = lambda **kw: {}
        self.code("source_uploads_remaining", self.adapter.abort_uploads)

        def denied(**kw):
            raise ProviderError("AccessDenied")

        self.client.list_parts = denied
        self.code("source_upload_unobservable", self.adapter.abort_uploads)

    def test_verify_rechecks_saved_ids_even_with_empty_sdk_cache(self):
        self.use_client()
        self.adapter.readonly()
        bucket = self.client.buckets[0]
        self.client.uploads[bucket] = [{"Key": "hidden", "UploadId": "u"}]
        snapshot = self.adapter.prepare()
        self.client.cache_missing = True
        barriers = [self.adapter.barrier(), self.adapter.barrier()]
        self.code(
            "source_uploads_remaining", lambda: self.adapter.verify(snapshot, barriers)
        )

    def test_native_residual_metadata_rejects_unmapped_late_upload(self):
        self.use_client()
        self.adapter.readonly()
        snapshot = self.adapter.prepare()
        barriers = [self.adapter.barrier(), self.adapter.barrier()]
        self.world.native_records = b"1\n"
        self.code(
            "source_multipart_metadata_remaining",
            lambda: self.adapter.verify(snapshot, barriers),
        )
        self.assertFalse(any(call[0] == "abort" for call in self.client.calls))

    def test_native_multipart_count_requires_one_bounded_integer(self):
        for value in (b"", b"0", b"0\n1\n", b"-1\n", b"100001\n", b"error\n"):
            with self.subTest(value=value):
                self.world.native_records = value
                with self.assertRaises(FenceError):
                    self.adapter._native_multipart_records()
        self.world.native_records = b"0\n"
        self.assertEqual(self.adapter._native_multipart_records(), 0)
        with patch.object(
            self.adapter, "_docker", side_effect=FenceError("native_command_failed")
        ):
            self.code(
                "native_multipart_unobservable", self.adapter._native_multipart_records
            )

    def test_fixed_native_script_counts_only_current_metadata_without_following_links(
        self,
    ):
        root = Path(self.temporary.name) / "source-system"
        root.mkdir()
        script = native._MULTIPART_RECORDS.replace("/data/.minio.sys", str(root))

        def run():
            return native._command(["/bin/bash", "-c", script])

        self.assertEqual(run(), b"0\n")
        path = root / "multipart" / "hash" / "upload"
        path.mkdir(parents=True)
        (path / "xl.meta").write_bytes(b"metadata")
        (path / "part.1").write_bytes(b"part")
        other = root / "unrelated"
        other.mkdir()
        (other / "xl.json").write_bytes(b"metadata")
        (root / "multipart" / "link").symlink_to(other, target_is_directory=True)
        self.assertEqual(run(), b"1\n")
        (path / "xl.json").write_bytes(b"metadata")
        self.assertEqual(run(), b"2\n")

    def test_fixed_native_script_rejects_symlink_roots_and_non_directory(self):
        root = Path(self.temporary.name) / "source-system"
        root.mkdir()
        path = root / "multipart"
        script = native._MULTIPART_RECORDS.replace("/data/.minio.sys", str(root))

        def run():
            return native._command(["/bin/bash", "-c", script])

        path.symlink_to(root, target_is_directory=True)
        self.code("native_command_failed", run)
        path.unlink()
        path.write_text("not a directory")
        self.code("native_command_failed", run)
        path.unlink()
        root.rmdir()
        root.symlink_to(Path(self.temporary.name), target_is_directory=True)
        self.code("native_command_failed", run)

    def test_fixed_native_script_propagates_listing_failure_and_line_cap(self):
        root = Path(self.temporary.name) / "source-system"
        (root / "multipart").mkdir(parents=True)
        script = native._MULTIPART_RECORDS.replace("/data/.minio.sys", str(root))
        for listing in ("(printf 'xl.meta\\n'; exit 1)", "/usr/bin/seq 100001"):
            with self.subTest(listing=listing):
                replaced = script.replace('/usr/bin/ls -1ARU -- "$path"', listing)
                self.code(
                    "native_command_failed",
                    lambda: native._command(["/bin/bash", "-c", replaced]),
                )

    def test_verify_requires_two_real_generations_and_closed_uploads(self):
        self.use_client()
        self.adapter.readonly()
        snapshot = self.adapter.prepare()
        self.adapter.disable_lifecycle(snapshot)
        first = self.adapter.barrier()
        self.code(
            "native_barrier_missing", lambda: self.adapter.verify(snapshot, [first])
        )
        second = self.adapter.barrier()
        observed = self.adapter.verify(snapshot, [first, second])
        self.assertTrue(
            all(type(value) is int for value in observed["counts"].values())
        )
        self.assertIn("observed_at", observed)
        self.client.uploads[self.client.buckets[0]] = [{"Key": "x", "UploadId": "u"}]
        self.code(
            "source_uploads_remaining",
            lambda: self.adapter.verify(snapshot, [first, second]),
        )

    def test_verify_rejects_later_gateway_or_source_generation(self):
        self.use_client()
        self.adapter.readonly()
        snapshot = self.adapter.prepare()
        barriers = [self.adapter.barrier(), self.adapter.barrier()]
        self.world.gateway["State"]["Pid"] += 1
        self.code(
            "native_gateway_generation_changed",
            lambda: self.adapter.verify(snapshot, barriers),
        )
        self.adapter.gateway_generation = None
        self.code(
            "native_generation_changed", lambda: self.adapter.verify(snapshot, barriers)
        )

    def test_writable_restarts_and_restored_rules_are_checked(self):
        self.use_client()
        self.adapter.readonly()
        snapshot = self.adapter.prepare()
        self.adapter.writable()
        self.adapter.verify_open(snapshot)
        self.assertEqual(sum(call[0] == "restart" for call in self.world.calls), 2)

    def test_release_before_prepare_verifies_open_without_snapshot(self):
        self.adapter.readonly()
        self.adapter.writable()
        self.adapter.verify_open(None)

    def test_verify_uses_last_two_receipts_after_resume(self):
        self.use_client()
        self.adapter.readonly()
        snapshot = self.adapter.prepare()
        old = [self.adapter.barrier(), self.adapter.barrier()]
        self.adapter.readonly()
        recent = [self.adapter.barrier(), self.adapter.barrier()]
        self.adapter.verify(snapshot, old + recent)
        self.code(
            "native_generation_changed",
            lambda: self.adapter.verify(snapshot, [old[-1], recent[-1]]),
        )

    def test_actual_sdk_factory_retains_one_attempt_and_closes(self):
        self.adapter._observe()
        with (
            patch.dict(
                os.environ,
                {
                    "AAP_SOURCE_FENCE_ACCESS_KEY": "unit-access",
                    "AAP_SOURCE_FENCE_SECRET_KEY": "unit-secret",
                },
            ),
            patch("boto3.client", return_value=self.client) as factory,
        ):
            with self.adapter._client():
                request = SimpleNamespace(
                    url=f"http://{self.world.ip}:9000/owned?versionId=a"
                )
                self.assertIsNone(self.client.hook(request))
                self.assertTrue(request.url.endswith("?versionId=a"))
                self.code(
                    "native_endpoint_changed",
                    lambda: self.client.hook(SimpleNamespace(url="http://other/owned")),
                )
            self.assertEqual(
                factory.call_args.kwargs["config"].retries, {"total_max_attempts": 1}
            )
            self.assertEqual(self.client.closed, 1)

    def test_sdk_body_error_is_safe_and_client_still_closed(self):
        self.adapter._observe()
        with (
            patch.dict(
                os.environ,
                {
                    "AAP_SOURCE_FENCE_ACCESS_KEY": "unit-access",
                    "AAP_SOURCE_FENCE_SECRET_KEY": "unit-secret",
                },
            ),
            patch("boto3.client", return_value=self.client),
        ):

            def fail():
                with self.adapter._client():
                    raise ValueError("do-not-expose-unit-secret")

            self.code("source_observation_failed", fail)
        self.assertEqual(self.client.closed, 1)

    def test_admin_requires_explicit_success_empty_arrays_and_disabled_site(self):
        query = native.DockerAdapter._admin.__get__(self.adapter)
        self.adapter._observe()
        with patch.dict(
            os.environ,
            {
                "AAP_SOURCE_FENCE_ACCESS_KEY": "unit-access",
                "AAP_SOURCE_FENCE_SECRET_KEY": "unit-secret",
            },
        ):
            for results, code in (
                ([b""], "native_response_invalid"),
                ([b'{"status":"success"}'], "source_admin_unobservable"),
                ([b'{"status":"success","tiers":[{}]}'], "source_tier_configured"),
                (
                    [b'{"status":"success","tiers":null}', b'{"enabled":true}'],
                    "source_site_replication_configured",
                ),
                (
                    [
                        b'{"status":"success","tiers":[]}',
                        b'{"enabled":false}',
                        b'{"status":"success","jobs":[{}]}',
                    ],
                    "source_batch_jobs_active",
                ),
            ):
                with (
                    self.subTest(results=results),
                    patch.object(native, "_command", side_effect=results),
                ):
                    self.code(code, query)
            with patch.object(
                native,
                "_command",
                side_effect=[
                    b'{"status":"success","tiers":null}',
                    b'{"enabled":false}',
                    b'{"status":"success","jobs":null}',
                ],
            ) as command:
                query()
                args, kwargs = command.call_args
                self.assertNotIn("unit-secret", " ".join(args[0]))
                self.assertIn("MC_HOST_fence", kwargs["environment"])
                self.assertFalse(Path(kwargs["environment"]["MC_CONFIG_DIR"]).exists())

    def test_native_command_failure_never_exposes_stderr(self):
        self.code(
            "native_command_failed",
            lambda: native._command(
                [
                    sys.executable,
                    "-c",
                    "import sys;sys.stderr.write('unit-secret');sys.exit(1)",
                ]
            ),
        )

    def test_native_command_output_is_bounded(self):
        self.code(
            "native_output_limit",
            lambda: native._command(
                [sys.executable, "-c", "import sys;sys.stdout.write('x'*3000000)"]
            ),
        )

    def test_native_command_stderr_is_also_bounded(self):
        self.code(
            "native_output_limit",
            lambda: native._command(
                [sys.executable, "-c", "import sys;sys.stderr.write('x'*3000000)"]
            ),
        )

    def test_budget_is_enforced_without_refreshing_it(self):
        budget = native._Budget()
        budget.versions = native._MAX_VERSIONS
        self.code("source_budget_exceeded", budget.version)
        budget = native._Budget()
        budget.pages = native._MAX_PAGES
        self.code("source_budget_exceeded", budget.page)


if __name__ == "__main__":
    unittest.main()
