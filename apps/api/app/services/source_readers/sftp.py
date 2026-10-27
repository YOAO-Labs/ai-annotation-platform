"""Strict snapshots inside a deployment-attested, read-only SFTP jail.

SFTP cannot prove chroot or provide atomic nofollow. The entire account namespace
must be an authorized location; subdirectories are filters, not security roots.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import hashlib
import json
import socket
import stat
import time

import paramiko

from app.config import settings
from app.core.crypto import (
    decrypt_secret,
    ConnectorCryptoError,
    ConnectorCryptoNotConfigured,
)
from app.services import connector_guard
from app.services.storage_access import close_registered, closing_registered
from app.services.filesystem_storage import (
    FilesystemStorageError as StorageError,
    relative_path,
    MAX_ENTRIES,
)
from aap_assets_source.connectors.sftp import load_sftp_private_key


def qualified_root(config):
    if config.get("base_path") != "/":
        raise StorageError("sftp_root_not_qualified")
    for handle, root in settings.storage_sftp_roots.items():
        if (config.get("host"), config.get("port", 22), config.get("username")) == (
            root.host,
            root.port,
            root.username,
        ):
            return handle, root.model_dump()
    raise StorageError("sftp_root_not_qualified")


def observation(info, path):
    fields = {
        name: getattr(info, "st_" + name, None)
        for name in ("mode", "size", "mtime", "uid", "gid")
    }
    if any(type(value) is not int or value < 0 for value in fields.values()):
        raise StorageError("sftp_invalid_attributes")
    return {"protocol": "sftp", "path": path, **fields}


def checked_path(sftp, path, *, directory):
    """Reject observed links; server jail, not this check, provides the boundary."""
    path = relative_path(path)
    components = path.split("/") if path else []
    result = None
    for index in range(len(components) + 1):
        remote = "/" + "/".join(components[:index])
        info = sftp.lstat(remote)
        mode = info.st_mode
        if mode is None:
            raise StorageError("sftp_invalid_attributes")
        if stat.S_ISLNK(mode):
            raise StorageError("symlink_rejected")
        if index < len(components) or directory:
            if not stat.S_ISDIR(mode):
                raise StorageError("not_a_directory")
        elif not stat.S_ISREG(mode):
            raise StorageError("not_a_regular_file")
        result = observation(info, remote)
    return result


@dataclass(frozen=True, repr=False)
class SftpAccess:
    config: dict
    secret_enc: bytes
    allowlist: tuple[str, ...]
    handle: str
    root: dict

    @property
    def anchor(self):
        return {
            "kind": "sftp",
            "root_handle": self.handle,
            "qualification": hashlib.sha256(
                json.dumps(self.root, sort_keys=True).encode()
            ).hexdigest(),
        }

    @contextmanager
    def client(self):
        client = paramiko.SSHClient()
        connection = None
        try:
            addresses = connector_guard.resolve_allowed_ips(
                self.root["host"], list(self.allowlist)
            )
            try:
                secret = decrypt_secret(self.secret_enc)
            except (ConnectorCryptoError, ConnectorCryptoNotConfigured):
                raise StorageError("connection_unavailable") from None
            entry = paramiko.hostkeys.HostKeyEntry.from_line(
                "qualified " + self.root["host_key"]
            )
            host = self.root["host"]
            port = self.root["port"]
            server = host if port == 22 else f"[{host}]:{port}"
            client.get_host_keys().add(server, entry.key.get_name(), entry.key)
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            try:
                key = load_sftp_private_key(secret)
            except (ValueError, TypeError):
                raise StorageError("connection_unavailable") from None
            address = addresses[0]
            connection = socket.socket(
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
            )
            connection.settimeout(2)
            connection.connect((address, port))
            client.connect(
                hostname=host,
                port=port,
                username=self.root["username"],
                password=secret.get("password") if key is None else None,
                pkey=key,
                allow_agent=False,
                look_for_keys=False,
                timeout=2,
                banner_timeout=2,
                auth_timeout=2,
                channel_timeout=2,
                sock=connection,
            )
            with closing_registered(client.open_sftp()) as sftp:
                sftp.get_channel().settimeout(2)
                yield sftp
        except paramiko.BadHostKeyException as exc:
            if getattr(exc, "storage_operation_unsettled", False):
                raise
            raise StorageError("sftp_host_key_mismatch") from None
        except paramiko.AuthenticationException as exc:
            if getattr(exc, "storage_operation_unsettled", False):
                raise
            failure = StorageError("connection_unavailable")
            failure.storage_fault_category = "access_denied"
            raise failure from None
        except connector_guard.ConnectorHostDenied as exc:
            if getattr(exc, "storage_operation_unsettled", False):
                raise
            raise StorageError("connector_host_denied") from None
        except (paramiko.SSHException, EOFError) as exc:
            if getattr(exc, "storage_operation_unsettled", False):
                raise
            raise StorageError("content_source_unavailable") from None
        except OSError as exc:
            if getattr(exc, "storage_operation_unsettled", False):
                raise
            raise StorageError(
                "path_unavailable"
                if exc.errno == errno.ENOENT
                else "permission_denied"
                if exc.errno in {errno.EACCES, errno.EPERM}
                else "content_source_unavailable"
            ) from None
        finally:
            try:
                close_registered(client)
            finally:
                if connection is not None:
                    close_registered(connection)

    def require_anchor(self, expected):
        if expected is not None and expected != self.anchor:
            raise StorageError("root_identity_changed")

    def scan_directory(self, path, *, expected_anchor, expected_identity=None):
        self.require_anchor(expected_anchor)
        path = relative_path(path)
        identity = ["sftp", path]
        if expected_identity is not None and identity != expected_identity:
            raise StorageError("directory_identity_changed")
        with self.client() as sftp:
            deadline = time.monotonic() + 2
            before = checked_path(sftp, path, directory=True)
            entries, names = [], set()
            with closing_registered(
                sftp.listdir_iter("/" + path, read_aheads=1)
            ) as iterator:
                for info in iterator:
                    if len(entries) >= MAX_ENTRIES:
                        raise StorageError("directory_too_large")
                    if time.monotonic() > deadline:
                        raise StorageError("directory_budget_exceeded")
                    name = relative_path(info.filename)
                    if not name or "/" in name or name in names:
                        raise StorageError("sftp_invalid_listing")
                    names.add(name)
                    remote = "/" + "/".join(p for p in [path, name] if p)
                    observed = observation(info, remote)
                    mode = observed["mode"]
                    kind = (
                        "directory"
                        if stat.S_ISDIR(mode)
                        else "file"
                        if stat.S_ISREG(mode)
                        else "symlink"
                        if stat.S_ISLNK(mode)
                        else "special"
                    )
                    entries.append({"name": name, "kind": kind, **observed})
            if checked_path(sftp, path, directory=True) != before:
                raise StorageError("directory_changed")
            entries.sort(key=lambda e: e["name"])
            digest = hashlib.sha256(
                json.dumps([before, entries], sort_keys=True).encode()
            ).hexdigest()
            if time.monotonic() > deadline:
                raise StorageError("directory_budget_exceeded")
            return {"identity": identity, "digest": digest, "entries": entries}

    def browse_directory(
        self, path, *, expected_anchor, limit, after=None, snapshot=None
    ):
        manifest = self.scan_directory(path, expected_anchor=expected_anchor)
        if snapshot is not None and snapshot != manifest["digest"]:
            raise StorageError("directory_changed")
        names = [
            e["name"]
            for e in manifest["entries"]
            if e["kind"] == "directory" and (after is None or e["name"] > after)
        ]
        return names[:limit], len(names) > limit, manifest["digest"]

    @contextmanager
    def content_file(
        self, base, path, *, expected_anchor=None, expected=None, **kwargs
    ):
        self.require_anchor(expected_anchor)
        base, path = relative_path(base), relative_path(path)
        if not path:
            raise StorageError("invalid_relative_path")
        relative = relative_path("/".join(p for p in [base, path] if p))
        with self.client() as sftp:
            before = checked_path(sftp, relative, directory=False)
            if expected and any(
                before.get(k) != v for k, v in expected.items() if k != "kind"
            ):
                raise StorageError("source_changed")
            with closing_registered(sftp.open("/" + relative, "rb", bufsize=0)) as body:
                body.settimeout(2)
                if observation(body.stat(), "/" + relative) != before:
                    raise StorageError("source_changed")
                yield body, before
                if observation(body.stat(), "/" + relative) != before:
                    raise StorageError("source_changed")
            if checked_path(sftp, relative, directory=False) != before:
                raise StorageError("source_changed")

    def probe(self, sample_file=None):
        capabilities = {
            name: "unsupported"
            for name in ("range", "write", "delete", "presign", "conditional_read")
        }
        capabilities.update(list="unknown", read="unknown")
        try:
            self.scan_directory("", expected_anchor=self.anchor)
            capabilities["list"] = "verified"
            if sample_file is not None:
                with self.content_file(
                    "", sample_file, expected_anchor=self.anchor
                ) as (body, _):
                    body.read(16)
                capabilities["read"] = "verified"
            return {
                "ok": True,
                "code": "ok",
                "capabilities": capabilities,
                "anchor": self.anchor,
            }
        except StorageError as exc:
            if getattr(exc, "storage_operation_unsettled", False):
                raise
            return {
                "ok": False,
                "code": exc.code,
                "capabilities": capabilities,
                "anchor": self.anchor,
            }
