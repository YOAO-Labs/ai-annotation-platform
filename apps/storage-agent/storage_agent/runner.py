"""Only bounded, read-only I/O; business state and authorization stay central."""

from dataclasses import replace
import hashlib
import os
import stat
import time

from aap_filesystem_read import (
    FilesystemStorageError,
    browse_directory,
    content_file,
    probe_directory,
    scan_directory,
)
from aap_filesystem_read.stage import stage_content
from aap_site_agent_contract import (
    ProtocolError,
    digest,
    validate_command,
    validate_result,
)

from .config import AgentError


class Runner:
    def __init__(self, config, credentials, journal, *, clock=time.time):
        self.config = config
        self.credentials = credentials
        self.journal = journal
        self.clock = clock

    def execute(self, value):
        command = validate_command(value)
        if (
            command["agent_id"] != self.credentials.agent_id
            or command["credential_revision"] != self.credentials.credential_revision
        ):
            raise AgentError("agent_identity_mismatch")
        existing = self.journal.accept(command)
        if existing["state"] in {"applied", "rejected"}:
            raise AgentError("command_already_acknowledged")
        if existing["result"] is not None:
            return existing["result"]
        result = {
            "protocol_version": 1,
            "command_id": command["command_id"],
            "command_digest": digest(command),
            "status": "completed",
            "result": {},
            "error_code": None,
            "closed": True,
        }
        stage_name = None
        try:
            now = self.clock()
            if now >= self.credentials.expires_at:
                raise AgentError("credential_expired")
            if (
                now >= command["expires_at"]
                or command["expires_at"] > self.credentials.expires_at
            ):
                raise AgentError("command_expired")
            if now + 5 < command["issued_at"]:
                raise AgentError("clock_out_of_sync")
            root = self.config.roots.get(command["root_handle"])
            if root is None:
                raise AgentError("root_not_configured")
            if root.binding_revision != command["root_revision"]:
                raise AgentError("root_revision_changed")
            args = command["arguments"]
            spec = replace(root, relative_base=args["relative_path"])
            anchor = args["expected_anchor"]
            operation = command["operation"]
            if operation == "probe":
                result["result"] = probe_directory(
                    spec, args["sample_file"], expected_anchor=anchor
                )
            elif operation == "browse":
                names, more, snapshot = browse_directory(
                    spec,
                    args["path"],
                    expected_anchor=anchor,
                    limit=args["limit"],
                    after=args["after"],
                    snapshot=args["snapshot"],
                )
                result["result"] = {
                    "names": names,
                    "has_more": more,
                    "snapshot": snapshot,
                }
            elif operation == "scan":
                result["result"] = scan_directory(
                    spec,
                    args["path"],
                    expected_anchor=anchor,
                    expected_identity=args["expected_identity"],
                )
            elif operation == "read":
                result["result"], stage_name = self._read(command, spec, args)
            # No successful local I/O can extend a central grant.
            if self.clock() >= command["expires_at"]:
                raise AgentError("command_expired")
        except (AgentError, FilesystemStorageError, OSError) as exc:
            if stage_name is not None:
                (self.journal.stages / stage_name).unlink(missing_ok=True)
                self.journal.sync_stages()
                stage_name = None
            result.update(
                status="failed",
                result={},
                error_code=getattr(exc, "code", "local_io_unavailable"),
                closed=not getattr(exc, "storage_operation_unsettled", False),
            )
        try:
            result = validate_result(result)
        except ProtocolError as exc:
            if stage_name is not None:
                (self.journal.stages / stage_name).unlink(missing_ok=True)
                self.journal.sync_stages()
                stage_name = None
            # A locally valid manifest can exceed the bounded wire envelope.
            # Persist a closed failure instead of re-executing forever on restart.
            result = validate_result(
                {
                    **result,
                    "status": "failed",
                    "result": {},
                    "error_code": exc.code,
                }
            )
        self.journal.complete(result, stage=stage_name)
        return result

    def _read(self, command, spec, args):
        remaining = min(
            command["timeout_seconds"], command["expires_at"] - self.clock(), 10
        )
        if remaining <= 0:
            raise AgentError("command_expired")
        # A temporary verified stage and the durable outgoing file can coexist.
        metadata_size = sum(
            path.stat().st_size
            for path in self.journal.directory.iterdir()
            if path.is_file()
        )
        available = (
            self.config.max_state_bytes - self.journal.stage_usage() - metadata_size
        )
        max_bytes = min(
            command["max_bytes"],
            self.config.public_limits()["max_bytes"],
            available // 2,
        )
        if max_bytes <= 0 or (
            args["expected_size"] is not None and args["expected_size"] > max_bytes
        ):
            raise AgentError("stage_capacity_exceeded")
        stage = stage_content(
            lambda: content_file(
                spec,
                "",
                args["path"],
                expected_anchor=args["expected_anchor"],
                expected=args["expected"],
                expected_source_identity=args["expected_source_identity"],
            ),
            expected_sha256=args["expected_sha256"],
            expected_size=args["expected_size"],
            max_bytes=max_bytes,
            timeout_seconds=remaining,
            directory=self.journal.stages,
        )
        name = command["command_id"] + ".bin"
        temporary = self.journal.stages / (name + ".partial")
        final = self.journal.stages / name
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            try:
                with os.fdopen(fd, "wb", closefd=False) as output:
                    stage.file.seek(0)
                    while data := stage.file.read(256 * 1024):
                        if self.clock() >= command["expires_at"]:
                            raise AgentError("command_expired")
                        output.write(data)
                    output.flush()
                    os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, final)
            self.journal.sync_stages()
            return {
                "sha256": stage.sha256,
                "size_bytes": stage.size,
                "observation": stage.observation,
            }, name
        except BaseException:
            temporary.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            raise
        finally:
            stage.close()

    def outgoing(self, result):
        """Revalidate a persisted stage before sending any of its bytes."""
        row = self.journal.get(result["command_id"])
        if row is None or row["result_digest"] != digest(result):
            raise AgentError("result_identity_mismatch")
        if row["stage"] is None:
            return None
        path = self.journal.stages / row["stage"]
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        body = os.fdopen(fd, "rb")
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size != result["result"]["size_bytes"]
            ):
                raise AgentError("stage_content_mismatch")
            checksum = hashlib.sha256()
            size = 0
            while data := body.read(256 * 1024):
                checksum.update(data)
                size += len(data)
                if size > result["result"]["size_bytes"]:
                    raise AgentError("stage_content_mismatch")
            if (
                size != result["result"]["size_bytes"]
                or checksum.hexdigest() != result["result"]["sha256"]
            ):
                raise AgentError("stage_content_mismatch")
            body.seek(0)
            return body
        except BaseException:
            body.close()
            raise
