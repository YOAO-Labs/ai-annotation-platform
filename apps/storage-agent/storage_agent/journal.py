"""Single-process durable results; receiving bytes is not a business ACK."""

from contextlib import contextmanager
import fcntl
import json
import os
import sqlite3
import stat
import time
from uuid import UUID, uuid4

from aap_site_agent_contract import (
    canonical_json,
    digest,
    validate_ack,
    validate_result,
)

from .config import AgentError, private_directory


class Journal:
    def __init__(self, directory, *, max_pending=16):
        self.directory = private_directory(directory)
        self.stages = private_directory(self.directory / "stages")
        self.max_pending = max_pending
        self.lock_fd = None
        self.connection = None

    def __enter__(self):
        try:
            self.lock_fd = os.open(
                self.directory / "agent.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            info = os.fstat(self.lock_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise AgentError("private_journal_required")
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path = self.directory / "journal.sqlite3"
            fd = os.open(
                path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
            )
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077
                ):
                    raise AgentError("private_journal_required")
            finally:
                os.close(fd)
            self.connection = sqlite3.connect(path)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA journal_mode=DELETE")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA max_page_count=8192")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise AgentError("journal_version_incompatible")
            self.connection.execute("""CREATE TABLE IF NOT EXISTS commands (
                command_id TEXT PRIMARY KEY, command_digest TEXT NOT NULL,
                command TEXT, result TEXT, result_digest TEXT, stage TEXT,
                state TEXT NOT NULL, ack TEXT, updated REAL NOT NULL)""")
            self.connection.execute("""CREATE TABLE IF NOT EXISTS poll_session (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                agent_id TEXT NOT NULL, credential_revision INTEGER NOT NULL,
                session_id TEXT NOT NULL)""")
            self.connection.execute("PRAGMA user_version=1")
            self.connection.commit()
            if self.connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                self._corrupt()
            return self
        except BaseException:
            self.close()
            raise

    def _corrupt(self):
        raise AgentError("journal_corrupt")

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def transaction(self):
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def get(self, command_id):
        row = self.connection.execute(
            "SELECT * FROM commands WHERE command_id=?", (command_id,)
        ).fetchone()
        if row is None:
            return None
        value = dict(row)
        for field in ("command", "result", "ack"):
            value[field] = (
                json.loads(value[field]) if value[field] is not None else None
            )
        return value

    def poll_session(self, credentials):
        """Keep lost-delivery recovery bound to the exclusively locked journal."""
        with self.transaction():
            row = self.connection.execute("SELECT * FROM poll_session").fetchone()
            if row is not None and (row["agent_id"], row["credential_revision"]) == (
                credentials.agent_id,
                credentials.credential_revision,
            ):
                try:
                    return str(UUID(row["session_id"]))
                except (ValueError, TypeError):
                    self._corrupt()
            session = str(uuid4())
            self.connection.execute(
                "INSERT OR REPLACE INTO poll_session VALUES (1, ?, ?, ?)",
                (credentials.agent_id, credentials.credential_revision, session),
            )
        return session

    def accept(self, command):
        command_id = command["command_id"]
        command_digest = digest(command)
        with self.transaction():
            prior = self.get(command_id)
            if prior:
                if prior["command_digest"] != command_digest:
                    raise AgentError("command_replay_conflict")
                return prior
            count = self.connection.execute(
                "SELECT count(*) FROM commands WHERE state NOT IN ('applied','rejected')"
            ).fetchone()[0]
            if count >= self.max_pending:
                raise AgentError("journal_capacity_exceeded")
            self.connection.execute(
                "INSERT INTO commands(command_id,command_digest,command,state,updated) VALUES (?,?,?,'accepted',?)",
                (
                    command_id,
                    command_digest,
                    canonical_json(command).decode(),
                    time.time(),
                ),
            )
        return self.get(command_id)

    def complete(self, result, *, stage=None):
        result = validate_result(result)
        command_id = result["command_id"]
        if stage is not None and stage != command_id + ".bin":
            raise AgentError("invalid_stage_name")
        with self.transaction():
            prior = self.get(command_id)
            if prior is None or prior["command_digest"] != result["command_digest"]:
                raise AgentError("result_identity_mismatch")
            result_digest = digest(result)
            if prior["result_digest"] is not None:
                if prior["result_digest"] != result_digest or prior["stage"] != stage:
                    raise AgentError("result_replay_conflict")
                return
            self.connection.execute(
                "UPDATE commands SET result=?,result_digest=?,stage=?,state='completed',updated=? WHERE command_id=?",
                (
                    canonical_json(result).decode(),
                    result_digest,
                    stage,
                    time.time(),
                    command_id,
                ),
            )

    def acknowledge(self, value):
        ack = validate_ack(value)
        stage = None
        with self.transaction():
            prior = self.get(ack["command_id"])
            if prior is None or any(
                prior[field] != ack[field]
                for field in ("command_digest", "result_digest")
            ):
                raise AgentError("ack_identity_mismatch")
            if prior["state"] in {"applied", "rejected"}:
                if prior["state"] != ack["state"]:
                    raise AgentError("ack_state_regression")
                return
            if ack["state"] == "applied" and not prior["result"]["closed"]:
                raise AgentError("ack_close_mismatch")
            # A rejected lease is not proof that its remote physical I/O closed.
            unsettled = not prior["result"]["closed"]
            terminal = ack["state"] in {"applied", "rejected"} and not unsettled
            stage = prior["stage"] if terminal else None
            self.connection.execute(
                "UPDATE commands SET state=?,ack=?,command=?,result=?,stage=?,updated=? WHERE command_id=?",
                (
                    "unsettled" if unsettled else ack["state"],
                    canonical_json(ack).decode(),
                    None if terminal else canonical_json(prior["command"]).decode(),
                    None if terminal else canonical_json(prior["result"]).decode(),
                    None if terminal else prior["stage"],
                    time.time(),
                    ack["command_id"],
                ),
            )
            self.connection.execute("""DELETE FROM commands WHERE state IN ('applied','rejected') AND command_id NOT IN (
                SELECT command_id FROM commands WHERE state IN ('applied','rejected') ORDER BY updated DESC LIMIT 256)""")
        # The ACK is durable before bytes can be removed.
        if stage is not None:
            (self.stages / stage).unlink(missing_ok=True)
            self.sync_stages()

    def pending(self):
        return [
            self.get(row[0])
            for row in self.connection.execute(
                "SELECT command_id FROM commands WHERE state NOT IN ('applied','rejected') ORDER BY updated"
            ).fetchall()
        ]

    def sync_stages(self):
        fd = os.open(self.stages, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def stage_usage(self):
        total = 0
        for path in self.stages.iterdir():
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise AgentError("unsafe_stage_file")
            total += info.st_size
        return total

    def remove_orphans(self):
        keep = {
            row[0]
            for row in self.connection.execute(
                "SELECT stage FROM commands WHERE stage IS NOT NULL"
            )
        }
        for path in self.stages.iterdir():
            if path.name not in keep:
                path.unlink()
        self.sync_stages()
