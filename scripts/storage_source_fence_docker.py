"""Owned Docker/MinIO ingress and process fence; not an application cutover API."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from storage_migration_inventory import PURPOSES
from storage_source_fence_state import FenceError

MINIO_IMAGE = "minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e"
NGINX_IMAGE = (
    "nginx@sha256:dc5069ad14f19660b141b21236140b91656bf89bbc3e2417c70ae650cd66104c"
)
OWNER_LABEL = "aap.source-fence.owner"
CHECKOUT_LABEL = "aap.source-fence.checkout"
ROLE_LABEL = "aap.source-fence.role"
_MINIO_ID = "sha256:69b2ec208575b69597784255eec6fa6a2985ee9e1a47f4411a51f7f5fdd193a9"
_NGINX_ID = "sha256:1ae82dcc4a34bcd976195b3c4c5a6b7e569505527a1e2a28c2101e032159a5c7"
_SCHEMA = "aap.storage-source-fence-config/v1"
_SNAPSHOT = "aap.storage-source-fence-docker-snapshot/v1"
_FIELDS = {
    "schema",
    "owner",
    "checkout",
    "source_container",
    "gateway_container",
    "network_id",
    "gateway_frontend_network_id",
    "source_volume_name",
    "gateway_config_directory",
    "public_endpoint",
    "purpose_buckets",
}
_ROOT = Path(__file__).resolve().parents[1]
_MAX_OUTPUT = 2 * 1024**2
_MAX_PAGES = 10000
_MAX_VERSIONS = 100000
_MAX_RULES = 1000


def _require(value, code):
    if not value:
        raise FenceError(code)


def _safe(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except FenceError:
            raise
        except Exception:
            raise FenceError("native_operation_failed") from None

    return call


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _json_data(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_data(item) for item in value]
    return value


def _command(arguments, *, environment=None):
    """Bound both child lifetime and captured output; never expose native errors."""
    try:
        child = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
    except Exception:
        raise FenceError("native_command_unavailable") from None
    output, total = bytearray(), 0
    try:
        deadline = time.monotonic() + 25
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ, True)
            selector.register(child.stderr, selectors.EVENT_READ, False)
            while selector.get_map():
                _require(time.monotonic() < deadline, "native_command_timeout")
                for key, _ in selector.select(
                    min(0.1, max(0, deadline - time.monotonic()))
                ):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    _require(total <= _MAX_OUTPUT, "native_output_limit")
                    if key.data:
                        output.extend(chunk)
        try:
            child.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise FenceError("native_command_timeout") from None
        _require(child.returncode == 0, "native_command_failed")
        return bytes(output)
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.wait(timeout=3)
        child.stdout.close()
        child.stderr.close()


def _decode(data):
    try:
        return json.loads(data)
    except (ValueError, UnicodeError):
        raise FenceError("native_response_invalid") from None


def gateway_config(source_ip: str, readonly: bool) -> bytes:
    """Preserve the original request URI and signed Host on the sole S3 route."""
    try:
        _require(
            isinstance(source_ip, str) and ipaddress.ip_address(source_ip).version == 4,
            "native_address_invalid",
        )
    except ValueError:
        raise FenceError("native_address_invalid") from None
    _require(type(readonly) is bool, "native_config_invalid")
    restriction = (
        "if ($request_method !~ ^(GET|HEAD)$) { return 405; }" if readonly else ""
    )
    return f"""worker_processes 1;
pid /var/run/nginx.pid;
events {{ worker_connections 1024; }}
http {{
    access_log off;
    error_log /dev/null crit;
    client_max_body_size 0;
    server {{
        listen 80;
        server_name _;
        location ~ ^/(minio|api)(/|$) {{ return 403; }}
        location / {{
            {restriction}
            proxy_pass http://{source_ip}:9000;
            proxy_http_version 1.1;
            proxy_set_header Host $http_host;
            proxy_set_header Connection "";
            proxy_request_buffering off;
            proxy_buffering off;
            proxy_ignore_client_abort on;
            proxy_read_timeout 300s;
            proxy_send_timeout 300s;
        }}
    }}
}}
""".encode()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise FenceError("native_endpoint_changed")


class _Budget:
    def __init__(self):
        self.pages = self.versions = 0
        self.started = time.monotonic()

    def page(self):
        self.pages += 1
        self.check()

    def version(self):
        self.versions += 1
        self.check()

    def check(self):
        _require(
            self.pages <= _MAX_PAGES
            and self.versions <= _MAX_VERSIONS
            and time.monotonic() - self.started <= 600,
            "source_budget_exceeded",
        )


class DockerAdapter:
    @_safe
    def __init__(self, config: dict):
        _require(
            isinstance(config, dict)
            and set(config) == _FIELDS
            and config.get("schema") == _SCHEMA,
            "native_config_invalid",
        )
        for name, length in (
            ("owner", 32),
            ("source_container", 64),
            ("gateway_container", 64),
            ("network_id", 64),
            ("gateway_frontend_network_id", 64),
        ):
            _require(
                isinstance(config.get(name), str)
                and re.fullmatch(f"[0-9a-f]{{{length}}}", config[name]),
                "native_config_invalid",
            )
        _require(
            config["source_container"] != config["gateway_container"],
            "native_config_invalid",
        )
        _require(
            config["network_id"] != config["gateway_frontend_network_id"],
            "native_config_invalid",
        )
        _require(
            config["checkout"] == str(_ROOT) and _ROOT.resolve() == _ROOT,
            "native_checkout_changed",
        )
        _require(
            isinstance(config["source_volume_name"], str)
            and re.fullmatch(
                r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", config["source_volume_name"]
            ),
            "native_config_invalid",
        )
        buckets = config["purpose_buckets"]
        _require(
            isinstance(buckets, dict)
            and set(buckets) == PURPOSES
            and all(
                isinstance(bucket, str)
                and re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
                and bucket not in {"minio", "api"}
                for bucket in buckets.values()
            ),
            "native_config_invalid",
        )
        try:
            endpoint = urlsplit(config["public_endpoint"])
            valid = (
                endpoint.scheme == "http"
                and endpoint.hostname in {"127.0.0.1", "localhost"}
                and endpoint.port
                and endpoint.path in {"", "/"}
                and not (
                    endpoint.username
                    or endpoint.password
                    or endpoint.query
                    or endpoint.fragment
                )
            )
        except (ValueError, TypeError, AttributeError):
            valid = False
        _require(valid, "native_endpoint_unsupported")
        self.config = json.loads(json.dumps(config))
        self.public_port = str(endpoint.port)
        _require(
            isinstance(config["gateway_config_directory"], str),
            "native_config_path_invalid",
        )
        self.directory = Path(config["gateway_config_directory"])
        self.source_ip = None
        self.gateway_generation = None
        self._files()

    def close(self):
        """Every public method owns and closes its own SDK client."""

    def _files(self):
        try:
            _require(
                self.directory.is_absolute()
                and self.directory.resolve() == self.directory,
                "native_config_path_invalid",
            )
            directory = self.directory.lstat()
            _require(
                stat.S_ISDIR(directory.st_mode)
                and stat.S_IMODE(directory.st_mode) == 0o700
                and directory.st_uid == os.getuid(),
                "native_config_permissions",
            )
            _require(
                {p.name for p in self.directory.iterdir()} == {"nginx.conf"},
                "native_config_path_invalid",
            )
            path = self.directory / "nginx.conf"
            item = path.lstat()
            _require(
                stat.S_ISREG(item.st_mode)
                and stat.S_IMODE(item.st_mode) == 0o644
                and item.st_uid == os.getuid()
                and item.st_nlink == 1
                and item.st_size <= 16384,
                "native_config_permissions",
            )
            return path.read_bytes()
        except OSError:
            raise FenceError("native_config_path_invalid") from None

    def _docker(self, *arguments):
        return _command(["docker", *arguments])

    def _inspect(self, kind, identity):
        value = _decode(self._docker(kind, "inspect", identity))
        _require(
            isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict),
            "native_response_invalid",
        )
        return value[0]

    def _labels(self, item, role, *, volume=False):
        labels = item.get("Labels") if volume else item.get("Config", {}).get("Labels")
        _require(
            isinstance(labels, dict)
            and labels.get(OWNER_LABEL) == self.config["owner"]
            and labels.get(CHECKOUT_LABEL) == self.config["checkout"]
            and labels.get(ROLE_LABEL) == role,
            "native_owner_changed",
        )

    def _image(self, container, expected, reference):
        _require(container.get("Image") == expected, "native_image_changed")
        image = self._inspect("image", expected)
        digest = reference.split("@", 1)[1]
        _require(
            image.get("Id") == expected
            and any(
                isinstance(value, str) and value.endswith("@" + digest)
                for value in image.get("RepoDigests", [])
            ),
            "native_image_changed",
        )

    def _observe(self, *, stopped=False):
        source = self._inspect("container", self.config["source_container"])
        gateway = self._inspect("container", self.config["gateway_container"])
        network = self._inspect("network", self.config["network_id"])
        frontend = self._inspect("network", self.config["gateway_frontend_network_id"])
        volume = self._inspect("volume", self.config["source_volume_name"])
        for value, field, role in (
            (source, "source_container", "source"),
            (gateway, "gateway_container", "gateway"),
        ):
            _require(value.get("Id") == self.config[field], "native_identity_changed")
            self._labels(value, role)
            host = value.get("HostConfig", {})
            _require(
                not host.get("Privileged")
                and host.get("PidMode", "") == ""
                and host.get("NetworkMode") not in {"host", "none"},
                "native_topology_changed",
            )
            nets = value.get("NetworkSettings", {}).get("Networks", {})
            expected_networks = {self.config["network_id"]}
            if role == "gateway":
                expected_networks.add(self.config["gateway_frontend_network_id"])
                _require(
                    (host.get("Sysctls") or {}).get("net.ipv4.ip_forward") == "0",
                    "native_topology_changed",
                )
            _require(
                len(nets) == len(expected_networks)
                and {net.get("NetworkID") for net in nets.values()}
                == expected_networks,
                "native_topology_changed",
            )
        self._image(source, _MINIO_ID, MINIO_IMAGE)
        self._image(gateway, _NGINX_ID, NGINX_IMAGE)
        _require(
            source.get("Config", {}).get("Entrypoint")
            == ["/usr/bin/docker-entrypoint.sh"]
            and source.get("Config", {}).get("Cmd")
            == ["server", "/data", "--console-address", ":9001"],
            "native_source_changed",
        )
        members = set(network.get("Containers", {}))
        expected = {self.config["source_container"], self.config["gateway_container"]}
        exited = (
            source.get("State", {}).get("Running") is False
            and source["State"].get("Pid") == 0
        )
        _require(
            network.get("Id") == self.config["network_id"]
            and network.get("Internal") is True
            and (
                members == expected
                or (
                    stopped and exited and members == {self.config["gateway_container"]}
                )
            ),
            "native_topology_changed",
        )
        self._labels({"Config": {"Labels": network.get("Labels")}}, "network")
        _require(
            frontend.get("Id") == self.config["gateway_frontend_network_id"]
            and frontend.get("Driver") == "bridge"
            and frontend.get("Internal") is False
            and set(frontend.get("Containers", {}))
            == {self.config["gateway_container"]},
            "native_topology_changed",
        )
        self._labels({"Config": {"Labels": frontend.get("Labels")}}, "frontend")
        self._labels(volume, "data", volume=True)
        _require(
            volume.get("Name") == self.config["source_volume_name"]
            and volume.get("Driver") == "local"
            and not volume.get("Options"),
            "native_mount_changed",
        )
        source_mount = source.get("Mounts", [])
        _require(
            len(source_mount) == 1
            and source_mount[0].get("Type") == "volume"
            and source_mount[0].get("Name") == self.config["source_volume_name"]
            and source_mount[0].get("Destination") == "/data"
            and source_mount[0].get("RW") is True,
            "native_mount_changed",
        )
        gateway_mount = gateway.get("Mounts", [])
        _require(
            len(gateway_mount) == 1
            and gateway_mount[0].get("Type") == "bind"
            and gateway_mount[0].get("Source") == str(self.directory)
            and gateway_mount[0].get("Destination") == "/etc/nginx"
            and gateway_mount[0].get("RW") is False,
            "native_mount_changed",
        )
        _require(
            not any((source.get("HostConfig", {}).get("PortBindings") or {}).values())
            and not any(
                (source.get("NetworkSettings", {}).get("Ports") or {}).values()
            ),
            "native_topology_changed",
        )
        ports = gateway.get("NetworkSettings", {}).get("Ports", {})
        _require(
            set(ports) == {"80/tcp"}
            and ports["80/tcp"]
            == [{"HostIp": "127.0.0.1", "HostPort": self.public_port}],
            "native_endpoint_changed",
        )
        _require(
            gateway.get("Config", {}).get("Entrypoint") == ["nginx"]
            and gateway.get("Config", {}).get("Cmd") == ["-g", "daemon off;"],
            "native_gateway_changed",
        )
        _require(
            gateway.get("State", {}).get("Running") is True
            and gateway["State"].get("Pid", 0) > 0,
            "native_gateway_not_running",
        )
        running = source.get("State", {}).get("Running")
        _require(
            (running is True and source["State"].get("Pid", 0) > 0)
            or (stopped and running is False and source["State"].get("Pid") == 0),
            "native_source_not_running",
        )
        content = self._files()
        ip = next(iter(source["NetworkSettings"]["Networks"].values())).get("IPAddress")
        if stopped and exited and not ip:
            match = re.search(rb"proxy_pass http://([0-9.]+):9000;", content)
            _require(match is not None, "native_config_changed")
            ip = match[1].decode("ascii")
        gateway_config(ip, True)
        _require(self.source_ip in {None, ip}, "native_address_changed")
        self.source_ip = ip
        _require(
            content in {gateway_config(ip, True), gateway_config(ip, False)},
            "native_config_changed",
        )
        if self.gateway_generation is not None:
            _require(
                self._generation(gateway) == self.gateway_generation,
                "native_gateway_generation_changed",
            )
        return source, gateway, content

    @staticmethod
    def _generation(container):
        state = container["State"]
        return {"started_at": state["StartedAt"], "pid": state["Pid"]}

    @contextmanager
    def _client(self):
        self._observe()
        access = os.environ.get("AAP_SOURCE_FENCE_ACCESS_KEY")
        secret = os.environ.get("AAP_SOURCE_FENCE_SECRET_KEY")
        _require(bool(access) and bool(secret), "native_credentials_missing")
        import boto3
        from botocore.config import Config

        endpoint = f"http://{self.source_ip}:9000"
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access,
            aws_secret_access_key=secret,
            region_name="us-east-1",
            config=Config(
                connect_timeout=2,
                read_timeout=2,
                retries={"total_max_attempts": 1},
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                proxies={},
            ),
        )

        def guard(request, **kwargs):
            value = urlsplit(request.url)
            _require(
                value.scheme == "http"
                and value.netloc == f"{self.source_ip}:9000"
                and not value.fragment
                and not value.username,
                "native_endpoint_changed",
            )

        client.meta.events.register("before-send.s3", guard)
        try:
            yield client
        except FenceError:
            raise
        except Exception:
            raise FenceError("source_observation_failed") from None
        finally:
            try:
                client.close()
            except Exception:
                raise FenceError("source_client_unsettled") from None

    def _admin(self):
        access = os.environ.get("AAP_SOURCE_FENCE_ACCESS_KEY")
        secret = os.environ.get("AAP_SOURCE_FENCE_SECRET_KEY")
        _require(bool(access) and bool(secret), "native_credentials_missing")
        with tempfile.TemporaryDirectory(prefix="aap-source-fence-mc-") as directory:
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("MC_HOST_")
            }
            environment.update(
                MC_CONFIG_DIR=directory,
                MC_HOST_fence=f"http://{quote(access, safe='')}:{quote(secret, safe='')}@{self.source_ip}:9000",
                NO_PROXY="*",
                no_proxy="*",
            )
            environment.pop("MC_DEBUG", None)

            def query(*args):
                return _decode(
                    _command(["mc", "--json", *args, "fence"], environment=environment)
                )

            tiers = query("ilm", "tier", "ls")
            _require(
                isinstance(tiers, dict)
                and tiers.get("status") == "success"
                and "tiers" in tiers,
                "source_admin_unobservable",
            )
            _require(tiers["tiers"] in (None, []), "source_tier_configured")
            site = query("admin", "replicate", "info")
            _require(
                isinstance(site, dict) and type(site.get("enabled")) is bool,
                "source_admin_unobservable",
            )
            _require(
                site["enabled"] is False and site.get("sites") in (None, [], {}),
                "source_site_replication_configured",
            )
            jobs = query("batch", "list")
            _require(
                isinstance(jobs, dict)
                and jobs.get("status") == "success"
                and "jobs" in jobs,
                "source_admin_unobservable",
            )
            _require(jobs["jobs"] in (None, []), "source_batch_jobs_active")

    @staticmethod
    def _error_code(exc):
        return getattr(exc, "response", {}).get("Error", {}).get("Code")

    def _lifecycle(self, client, bucket):
        try:
            response = client.get_bucket_lifecycle_configuration(Bucket=bucket)
        except Exception as exc:
            if self._error_code(exc) == "NoSuchLifecycleConfiguration":
                return None
            raise
        rules = _json_data(response.get("Rules"))
        _require(
            isinstance(rules, list) and 0 < len(rules) <= _MAX_RULES,
            "source_lifecycle_invalid",
        )
        allowed = {
            "ID",
            "Filter",
            "Prefix",
            "Status",
            "Expiration",
            "NoncurrentVersionExpiration",
            "AbortIncompleteMultipartUpload",
        }
        _require(
            all(
                isinstance(rule, dict)
                and set(rule) <= allowed
                and rule.get("Status") in {"Enabled", "Disabled"}
                for rule in rules
            ),
            "source_lifecycle_unsupported",
        )
        return rules

    def _buckets(self, client, budget):
        buckets, token = set(), None
        seen = set()
        while True:
            budget.page()
            response = client.list_buckets(
                **({"ContinuationToken": token} if token else {})
            )
            rows = response.get("Buckets")
            _require(
                isinstance(rows, list) and len(rows) <= 1000, "source_scope_invalid"
            )
            for row in rows:
                name = row.get("Name")
                _require(
                    isinstance(name, str) and name not in buckets,
                    "source_scope_invalid",
                )
                buckets.add(name)
            token = response.get("ContinuationToken")
            if not token:
                break
            _require(
                isinstance(token, str) and token not in seen,
                "source_pagination_invalid",
            )
            seen.add(token)
        _require(
            buckets == set(self.config["purpose_buckets"].values()),
            "source_bucket_scope_changed",
        )
        return sorted(buckets)

    def _versions(self, client, bucket, budget, counts):
        versioning = client.get_bucket_versioning(Bucket=bucket).get("Status")
        _require(
            versioning in {None, "Enabled", "Suspended"}, "source_versioning_invalid"
        )
        cursor, seen = {}, set()
        while True:
            budget.page()
            if versioning:
                page = client.list_object_versions(
                    Bucket=bucket, MaxKeys=1000, **cursor
                )
                values = page.get("Versions", [])
                markers = page.get("DeleteMarkers", [])
                _require(isinstance(markers, list), "source_pagination_invalid")
                counts["delete_markers"] += len(markers)
                for _ in markers:
                    budget.version()
            else:
                page = client.list_objects_v2(Bucket=bucket, MaxKeys=1000, **cursor)
                values = page.get("Contents", [])
            _require(isinstance(values, list), "source_pagination_invalid")
            for item in values:
                budget.version()
                key = item.get("Key")
                _require(isinstance(key, str) and key != "", "source_object_invalid")
                arguments = {"Bucket": bucket, "Key": key}
                if versioning:
                    version = item.get("VersionId")
                    _require(
                        isinstance(version, str) and version != "",
                        "source_object_invalid",
                    )
                    arguments["VersionId"] = version
                head = client.head_object(**arguments)
                if versioning:
                    _require(
                        head.get("VersionId") == version
                        or (version == "null" and head.get("VersionId") is None),
                        "source_object_changed",
                    )
                headers = head.get("ResponseMetadata", {}).get("HTTPHeaders", {})
                _require(
                    "Restore" not in head
                    and not any(key.lower() == "x-amz-restore" for key in headers),
                    "source_restore_present",
                )
                counts["versions"] += 1
            truncated = page.get("IsTruncated")
            _require(type(truncated) is bool, "source_pagination_invalid")
            if not truncated:
                return
            cursor = (
                {
                    "KeyMarker": page.get("NextKeyMarker"),
                    "VersionIdMarker": page.get("NextVersionIdMarker"),
                }
                if versioning
                else {"ContinuationToken": page.get("NextContinuationToken")}
            )
            _require(
                isinstance(next(iter(cursor.values())), str)
                and next(iter(cursor.values())) != "",
                "source_pagination_invalid",
            )
            marker = json.dumps(cursor, sort_keys=True)
            _require(marker not in seen, "source_pagination_invalid")
            seen.add(marker)
            cursor = {key: value for key, value in cursor.items() if value is not None}

    def _uploads(self, client, bucket, budget):
        cursor, seen = {}, set()
        while True:
            budget.page()
            page = client.list_multipart_uploads(
                Bucket=bucket, MaxUploads=1000, **cursor
            )
            values = page.get("Uploads", [])
            _require(isinstance(values, list), "source_pagination_invalid")
            for value in values:
                _require(
                    isinstance(value.get("Key"), str)
                    and isinstance(value.get("UploadId"), str),
                    "source_upload_invalid",
                )
                yield value
            _require(type(page.get("IsTruncated")) is bool, "source_pagination_invalid")
            if not page["IsTruncated"]:
                return
            cursor = {
                "KeyMarker": page.get("NextKeyMarker"),
                "UploadIdMarker": page.get("NextUploadIdMarker"),
            }
            _require(
                all(isinstance(value, str) and value for value in cursor.values()),
                "source_pagination_invalid",
            )
            marker = json.dumps(cursor, sort_keys=True)
            _require(marker not in seen, "source_pagination_invalid")
            seen.add(marker)

    def _scan(self, client):
        budget = _Budget()
        self._admin()
        buckets = self._buckets(client, budget)
        counts = dict(
            buckets=len(buckets),
            versions=0,
            delete_markers=0,
            multipart_uploads=0,
            lifecycle_rules=0,
        )
        lifecycle = {}
        for bucket in buckets:
            try:
                client.get_bucket_replication(Bucket=bucket)
            except Exception as exc:
                _require(
                    self._error_code(exc)
                    in {
                        "ReplicationConfigurationNotFoundError",
                        "NoSuchReplicationConfiguration",
                    },
                    "source_replication_unobservable",
                )
            else:
                raise FenceError("source_replication_configured")
            rules = self._lifecycle(client, bucket)
            lifecycle[bucket] = {
                "original": rules,
                "disabled": [{**rule, "Status": "Disabled"} for rule in rules]
                if rules
                else None,
            }
            counts["lifecycle_rules"] += len(rules or [])
            _require(counts["lifecycle_rules"] <= _MAX_RULES, "source_budget_exceeded")
            self._versions(client, bucket, budget, counts)
            counts["multipart_uploads"] += sum(
                1 for _ in self._uploads(client, bucket, budget)
            )
        return lifecycle, counts

    @_safe
    def preview(self):
        with self._client() as client:
            _, counts = self._scan(client)
        self._observe()
        return counts

    @_safe
    def prepare(self):
        _, _, content = self._observe()
        _require(content == gateway_config(self.source_ip, True), "native_not_readonly")
        with self._client() as client:
            lifecycle, counts = self._scan(client)
        self._observe()
        snapshot = dict(
            schema=_SNAPSHOT,
            source_ip=self.source_ip,
            purpose_buckets=self.config["purpose_buckets"],
            readonly_sha256=_digest(gateway_config(self.source_ip, True)),
            writable_sha256=_digest(gateway_config(self.source_ip, False)),
            lifecycle=lifecycle,
            counts=counts,
        )
        _require(
            len(json.dumps(snapshot).encode()) <= 2 * 1024**2, "source_budget_exceeded"
        )
        return snapshot

    def _snapshot(self, snapshot):
        _require(
            isinstance(snapshot, dict)
            and snapshot.get("schema") == _SNAPSHOT
            and snapshot.get("source_ip") == self.source_ip
            and snapshot.get("purpose_buckets") == self.config["purpose_buckets"]
            and snapshot.get("readonly_sha256")
            == _digest(gateway_config(self.source_ip, True))
            and snapshot.get("writable_sha256")
            == _digest(gateway_config(self.source_ip, False))
            and set(snapshot.get("lifecycle", {}))
            == set(self.config["purpose_buckets"].values()),
            "native_snapshot_changed",
        )

    def _lifecycle_change(self, snapshot, *, restore):
        self._observe()
        self._snapshot(snapshot)
        with self._client() as client:
            for bucket, saved in snapshot["lifecycle"].items():
                current = self._lifecycle(client, bucket)
                original, disabled = saved["original"], saved["disabled"]
                _require(
                    current == original or current == disabled,
                    "source_lifecycle_changed",
                )
                desired = original if restore else disabled
                if current != desired:
                    _require(desired is not None, "source_lifecycle_changed")
                    client.put_bucket_lifecycle_configuration(
                        Bucket=bucket, LifecycleConfiguration={"Rules": desired}
                    )
                _require(
                    self._lifecycle(client, bucket) == desired,
                    "source_lifecycle_changed",
                )
        self._observe()

    @_safe
    def disable_lifecycle(self, snapshot):
        self._lifecycle_change(snapshot, restore=False)

    @_safe
    def restore_lifecycle(self, snapshot):
        self._lifecycle_change(snapshot, restore=True)

    def _gateway(self, readonly):
        _, before, _ = self._observe()
        data = gateway_config(self.source_ip, readonly)
        fd, name = tempfile.mkstemp(prefix=".nginx-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o644)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.directory / "nginx.conf")
            directory = os.open(self.directory, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self._docker("restart", "--time", "5", self.config["gateway_container"])
        self.gateway_generation = None
        _, after, content = self._observe()
        _require(
            content == data
            and before["State"]["StartedAt"] != after["State"]["StartedAt"],
            "native_gateway_unsettled",
        )
        self.gateway_generation = self._generation(after)

    @_safe
    def readonly(self):
        self._gateway(True)

    @_safe
    def writable(self):
        self._gateway(False)

    def _ready(self):
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with opener.open(
                    f"http://{self.source_ip}:9000/minio/health/ready", timeout=2
                ) as response:
                    if response.status == 200:
                        return
            except FenceError:
                raise
            except Exception:
                pass
            time.sleep(0.1)
        raise FenceError("native_source_not_ready")

    @_safe
    def barrier(self):
        source, _, content = self._observe(stopped=True)
        _require(content == gateway_config(self.source_ip, True), "native_not_readonly")
        if source["State"]["Running"]:
            self._docker("stop", "--time", "5", self.config["source_container"])
        stopped, _, _ = self._observe(stopped=True)
        _require(
            stopped["State"].get("Running") is False
            and stopped["State"].get("Pid") == 0,
            "native_source_unsettled",
        )
        finished = stopped["State"].get("FinishedAt")
        _require(
            isinstance(finished, str) and not finished.startswith("0001-"),
            "native_source_unsettled",
        )
        self._docker("start", self.config["source_container"])
        self._ready()
        current, gateway, content = self._observe()
        _require(
            current["State"]["StartedAt"] != source["State"]["StartedAt"]
            and content == gateway_config(self.source_ip, True),
            "native_source_unsettled",
        )
        return dict(
            source_container=self.config["source_container"],
            stopped_at=finished,
            stopped_pid=0,
            started_at=current["State"]["StartedAt"],
            pid=current["State"]["Pid"],
            gateway=self._generation(gateway),
            readonly_sha256=_digest(content),
        )

    @_safe
    def abort_uploads(self):
        budget, count = _Budget(), 0
        _, _, content = self._observe()
        _require(content == gateway_config(self.source_ip, True), "native_not_readonly")
        with self._client() as client:
            for bucket in self._buckets(client, budget):
                for upload in self._uploads(client, bucket, budget):
                    try:
                        client.abort_multipart_upload(
                            Bucket=bucket,
                            Key=upload["Key"],
                            UploadId=upload["UploadId"],
                        )
                    except Exception as exc:
                        _require(
                            self._error_code(exc) == "NoSuchUpload",
                            "source_upload_abort_failed",
                        )
                    count += 1
                    _require(count <= _MAX_VERSIONS, "source_budget_exceeded")
        self._observe()
        return count

    @_safe
    def verify(self, snapshot, barriers):
        current, gateway, content = self._observe()
        self._snapshot(snapshot)
        _require(
            isinstance(barriers, list)
            and 2 <= len(barriers) <= 128
            and all(isinstance(value, dict) for value in barriers[-2:]),
            "native_barrier_missing",
        )
        last = barriers[-1]
        previous = barriers[-2]
        _require(
            last.get("source_container") == self.config["source_container"]
            and previous.get("source_container") == self.config["source_container"]
            and last.get("stopped_pid") == previous.get("stopped_pid") == 0
            and last.get("started_at") == current["State"]["StartedAt"]
            and last.get("pid") == current["State"]["Pid"]
            and previous.get("started_at") != last.get("started_at")
            and last.get("gateway")
            == previous.get("gateway")
            == self._generation(gateway)
            and last.get("readonly_sha256")
            == previous.get("readonly_sha256")
            == _digest(content),
            "native_generation_changed",
        )
        _require(content == gateway_config(self.source_ip, True), "native_not_readonly")
        with self._client() as client:
            lifecycle, counts = self._scan(client)
            for bucket, saved in snapshot["lifecycle"].items():
                _require(
                    lifecycle[bucket]["original"] == saved["disabled"],
                    "source_lifecycle_changed",
                )
        _require(counts["multipart_uploads"] == 0, "source_uploads_remaining")
        after, after_gateway, after_content = self._observe()
        _require(
            after["State"]["StartedAt"] == last["started_at"]
            and after["State"]["Pid"] == last["pid"]
            and self._generation(after_gateway) == last["gateway"]
            and after_content == content,
            "native_generation_changed",
        )
        return {"observed_at": _stamp(), "counts": counts}

    @_safe
    def verify_open(self, snapshot=None):
        _, _, content = self._observe()
        _require(
            content == gateway_config(self.source_ip, False), "native_not_writable"
        )
        if snapshot is not None:
            self._snapshot(snapshot)
            with self._client() as client:
                for bucket, saved in snapshot["lifecycle"].items():
                    _require(
                        self._lifecycle(client, bucket) == saved["original"],
                        "source_lifecycle_changed",
                    )
        self._observe()
