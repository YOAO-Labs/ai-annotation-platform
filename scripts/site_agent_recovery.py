"""Fault injection for the owned, real outbound HTTPS Agent fixture.

Only the disposable fixture's network and Agent are interrupted. Database
queries observe actual owner state; they never manufacture receipts or leases.
"""

from concurrent.futures import ThreadPoolExecutor
import asyncio
import hashlib
import json
import os
import sqlite3
import time
from urllib.parse import urlsplit, urlunsplit
import uuid


def require(value, code):
    if not value:
        raise ValueError(code)


def until(call, predicate, *, seconds=75):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = call()
        if predicate(value):
            return value
        time.sleep(0.1)
    raise ValueError("recovery_observation_timeout")


def rows(sql, *values):
    """Read-only observation on the already verified launcher test database."""
    require(os.environ.get("AAP_WORKTREE_MODE") == "test", "test_mode_required")
    url = urlsplit(os.environ["DATABASE_URL"])
    require(url.path.endswith("_test"), "test_database_required")
    dsn = urlunsplit(("postgresql", url.netloc, url.path, url.query, url.fragment))

    async def fetch():
        import asyncpg

        connection = await asyncpg.connect(dsn, timeout=3, command_timeout=3)
        try:
            async with connection.transaction(readonly=True):
                require(
                    await connection.fetchval("SELECT current_database()")
                    == url.path.removeprefix("/"),
                    "observation_database_changed",
                )
                return [dict(row) for row in await connection.fetch(sql, *values)]
        finally:
            await connection.close()

    return asyncio.run(fetch())


class Recovery:
    def __init__(
        self,
        *,
        owner,
        http,
        agent,
        network,
        site_dir,
        source_root,
        source,
        dataset,
        location,
        files,
        record,
    ):
        self.owner, self.http, self.agent, self.network = owner, http, agent, network
        self.site_dir, self.source_root = site_dir, source_root
        self.source, self.dataset, self.location = source, dataset, location
        self.files, self.record = files, record
        self.identity = json.loads((site_dir / "identity.json").read_bytes())
        self.agent_id = uuid.UUID(self.identity["agent_id"])
        self.rate = 64 * 1024
        self.pool = ThreadPoolExecutor(max_workers=3)

    def close(self):
        # HTTP requests have finite timeouts; do not leave observers running
        # while the fixture's owned containers and networks are removed.
        self.pool.shutdown(wait=True, cancel_futures=True)

    def items(self):
        return self.http.call(
            "GET", "/api/v1/datasets/" + self.dataset["id"] + "/items"
        )["items"]

    def content(self, item):
        expected = next(
            data
            for name, data in self.files.items()
            if name.rsplit("/", 1)[-1] == item["file_name"]
        )
        path = "/api/v1/dataset-items/" + item["id"] + "/content?sha256="
        return path + hashlib.sha256(expected).hexdigest(), expected

    def commands(self):
        return rows(
            "SELECT id, state, payload, result, result_digest, received_bytes, chunks, "
            "closed, expires_at, error_code, operation_id FROM site_agent_commands "
            "WHERE agent_id=$1 ORDER BY created_at, id",
            self.agent_id,
        )

    def command(self, command_id):
        return next(row for row in self.commands() if row["id"] == command_id)

    @staticmethod
    def document(value):
        return json.loads(value) if isinstance(value, str) else value

    def new_command(self, before, operation, **conditions):
        def choose():
            for row in self.commands():
                payload = self.document(row["payload"])
                if row["id"] not in before and payload["operation"] == operation:
                    if all(row.get(key) == value for key, value in conditions.items()):
                        return row
            return None

        return until(choose, bool)

    def stop_agent(self, *, disconnected=False):
        self.owner.inspect(self.agent)
        if disconnected:
            self.owner.inspect(self.network)
            from verify_site_agent import run

            run(
                [
                    "docker",
                    "network",
                    "disconnect",
                    self.network["id"],
                    self.agent["id"],
                ]
            )
        self.owner.action(self.agent, "kill")

    def start_agent(self, *, disconnected=False):
        self.owner.inspect(self.agent)
        if disconnected:
            self.owner.inspect(self.network)
            from verify_site_agent import run

            run(["docker", "network", "connect", self.network["id"], self.agent["id"]])
        self.owner.action(self.agent, "start")

    def agent_http(self):
        peer = type(self.http)(self.http.origin, self.site_dir / "ca.pem")
        peer.token = self.identity["token"]
        return peer

    def poll(self):
        with sqlite3.connect(self.site_dir / "state/journal.sqlite3") as journal:
            session = journal.execute("SELECT session_id FROM poll_session").fetchone()[
                0
            ]
        return self.agent_http().call(
            "POST",
            "/api/v1/site-agents/commands/poll",
            {
                "protocol_version": 1,
                "agent_id": str(self.agent_id),
                "credential_revision": self.identity["credential_revision"],
                "session_id": session,
                "roots": [{"root_handle": "images", "root_revision": 1}],
                "limits": {
                    "max_concurrency": 1,
                    "bandwidth_bytes_per_second": 8 * 1024 * 1024,
                    "max_bytes": 64 * 1024 * 1024,
                },
            },
        )["command"]

    def read_restart(self, item):
        path, expected = self.content(item)
        before = {row["id"] for row in self.commands()}
        started = time.monotonic()
        request = self.pool.submit(self.http.call, "GET", path, binary=True)
        row = self.new_command(before, "read")
        until(
            lambda: self.command(row["id"]),
            lambda current: 0 < current["received_bytes"] < len(expected),
        )
        self.stop_agent(disconnected=True)
        self.start_agent(disconnected=True)
        received, headers = request.result(timeout=70)
        elapsed = time.monotonic() - started
        require(received == expected, "restart_content_changed")
        require(
            headers["X-Content-SHA256"] == hashlib.sha256(expected).hexdigest(),
            "restart_sha_changed",
        )
        current = self.command(row["id"])
        require(
            current["state"] == "applied"
            and current["closed"] is True
            and current["received_bytes"] == len(expected),
            "restart_receipt_not_applied",
        )
        require(
            self.document(current["payload"])["bandwidth_bytes_per_second"] == self.rate
            and elapsed >= (len(expected) - self.rate) / self.rate,
            "central_bandwidth_not_enforced",
        )
        # Exactly the recorded immutable result may be replayed; no byte or
        # business counters are incremented by a repeated result request.
        ack = self.agent_http().call(
            "POST",
            f"/api/v1/site-agents/commands/{row['id']}/result",
            self.document(current["result"]),
        )
        require(
            ack["state"] == "applied"
            and ack["result_digest"] == current["result_digest"],
            "repeat_result_changed",
        )
        self.record(
            "interrupted_chunk_upload_replayed_after_agent_restart",
            {
                "size_bytes": len(expected),
                "seconds": round(elapsed, 3),
                "central_bytes_per_second": self.rate,
            },
        )
        return current

    def lost_poll_after_expiry(self, item):
        path, _ = self.content(item)
        self.stop_agent()
        before = {row["id"] for row in self.commands()}
        request = self.pool.submit(
            self.http.call, "GET", path, expected=504, binary=True
        )

        def queued_read():
            if request.done():
                request.result()  # Surface the actual HTTP failure, not a timeout.
                raise ValueError("read_finished_without_lost_poll_grant")
            return next(
                (
                    row
                    for row in self.commands()
                    if row["id"] not in before
                    and self.document(row["payload"])["operation"] == "read"
                    and row["state"] in {"queued", "leased"}
                ),
                None,
            )

        until(queued_read, bool)
        command = self.poll()
        require(
            command is not None and command["operation"] == "read",
            "lost_poll_not_leased",
        )
        command_id = uuid.UUID(command["command_id"])
        require(command_id not in before, "lost_poll_wrong_command")
        # Deliberately discard this real poll response before the Agent can
        # journal it, then let the original unextended grant expire.
        request.result(timeout=70)
        current = self.command(command_id)
        require(
            current["state"] == "unsettled" and current["result"] is None,
            "lost_poll_responsibility_not_retained",
        )
        self.start_agent()
        current = until(
            lambda: self.command(command_id),
            lambda value: value["state"] == "rejected" and value["closed"] is True,
        )
        result = self.document(current["result"])
        require(
            current["expires_at"].timestamp() == command["expires_at"]
            and current["received_bytes"] == 0
            and result["error_code"] == "command_expired",
            "expired_command_reexecuted",
        )
        require(
            not rows(
                "SELECT id FROM storage_operations WHERE id=$1", current["operation_id"]
            ),
            "expired_operation_not_closed",
        )
        self.record("lost_poll_response_after_expiry_closed_without_source_io")

    def scan_restart(self, original_ids):
        bulk = self.source_root / "mounted/dataset/bulk"
        bulk.mkdir()
        for index in range(400):
            (bulk / (f"marker-{index:04}-" + "x" * 130 + ".txt")).write_bytes(b"")
        before = {row["id"] for row in self.commands()}
        scan = self.http.call(
            "POST",
            "/api/v1/dataset-sources/" + self.source["id"] + "/scans",
            expected=202,
        )

        def active_bulk():
            for row in self.commands():
                payload = self.document(row["payload"])
                if (
                    row["id"] not in before
                    and row["state"] == "leased"
                    and payload["operation"] == "scan"
                    and payload["arguments"]["path"] == "dataset/bulk"
                ):
                    return row
            return None

        command = until(active_bulk, bool)
        self.stop_agent(disconnected=True)
        items = self.items()
        require(
            {item["id"] for item in items} == original_ids, "partial_scan_changed_items"
        )
        require(
            all(item["availability"]["state"] != "missing" for item in items),
            "partial_scan_marked_unseen_missing",
        )
        until(
            lambda: self.command(command["id"]),
            lambda value: value["error_code"] == "agent_command_expired",
        )
        self.start_agent(disconnected=True)
        until(
            lambda: self.command(command["id"]), lambda value: value["closed"] is True
        )
        # Existing resume supersedes the old attempt, retaining and validating
        # its directory checkpoints. No fixture SQL changes a business row.
        self.http.call(
            "POST", "/api/v1/dataset-scans/" + scan["id"] + "/resume", expected=202
        )
        completed = until(
            lambda: self.http.call("GET", "/api/v1/dataset-scans/" + scan["id"]),
            lambda value: value["status"] in {"completed", "failed", "cancelled"},
        )
        require(
            completed["complete"] and completed["status"] == "completed",
            "scan_resume_failed",
        )
        registration = self.http.call(
            "POST",
            "/api/v1/dataset-scans/" + scan["id"] + "/registration",
            expected=202,
        )

        def get():
            return self.http.call(
                "GET", "/api/v1/dataset-scans/" + scan["id"] + "/registration"
            )

        until(get, lambda value: value["status"] in {"completed", "failed"})
        self.http.call(
            "POST",
            "/api/v1/dataset-registrations/" + registration["id"] + "/confirm",
            expected=202,
        )
        final = until(
            get,
            lambda value: (
                value["phase"] == "registering"
                and value["status"] in {"completed", "failed"}
            ),
        )
        require(final["status"] == "completed", "resumed_registration_failed")
        require(
            {item["id"] for item in self.items()} == original_ids,
            "resumed_registration_duplicated_items",
        )
        self.record(
            "scan_network_interruption_and_agent_restart_resumed_without_duplicates",
            completed["counts"],
        )

    def offline_metadata(self, original_ids, item):
        self.stop_agent()

        # Expire a real heartbeat, rather than manufacturing an offline row.
        def offline_source():
            page = self.http.call(
                "GET",
                "/api/v1/dataset-sources?dataset_id=" + self.dataset["id"],
            )
            return next(row for row in page["items"] if row["id"] == self.source["id"])

        source = until(
            offline_source,
            lambda row: row["agent_status"]["state"] == "offline",
            seconds=70,
        )
        require(source["agent_status"]["last_seen"] is not None, "heartbeat_missing")
        items = self.items()
        require({row["id"] for row in items} == original_ids, "offline_metadata_lost")
        require(
            all(row["availability"]["reason_code"] == "agent_offline" for row in items),
            "offline_availability_reason_missing",
        )
        page = self.http.call(
            "GET",
            "/api/v1/datasets/"
            + self.dataset["id"]
            + "/items?search=a.png&availability=unavailable&limit=1",
        )
        require(
            page["total"] == 1 and page["items"][0]["id"] == item["id"],
            "offline_search_or_count_changed",
        )
        path, _ = self.content(item)
        response = self.http.call("GET", path, expected=409)
        require(
            response.get("detail", {}).get("code") == "agent_offline",
            "offline_read_not_rejected_promptly",
        )
        self.record(
            "actual_heartbeat_expiry_preserves_metadata_search_and_availability"
        )
        self.start_agent()
        until(offline_source, lambda row: row["agent_status"]["state"] == "online")

    def revoked(self, applied, item):
        self.stop_agent()
        self.http.call(
            "POST",
            f"/api/v1/site-agents/agents/{self.agent_id}/revoke",
            {"expected_revision": self.identity["credential_revision"]},
        )
        peer = self.agent_http()
        peer.call(
            "POST",
            "/api/v1/site-agents/commands/poll",
            {
                "protocol_version": 1,
                "agent_id": str(self.agent_id),
                "credential_revision": self.identity["credential_revision"],
                "session_id": str(uuid.uuid4()),
                "roots": [],
                "limits": {
                    "max_concurrency": 1,
                    "bandwidth_bytes_per_second": self.rate,
                    "max_bytes": 64 * 1024 * 1024,
                },
            },
            expected=403,
        )
        path, _ = self.content(item)
        response = self.http.call("GET", path, expected=409)
        require(
            response.get("detail", {}).get("code") == "agent_revoked",
            "revoked_read_reason_missing",
        )
        ack = peer.call(
            "POST",
            f"/api/v1/site-agents/commands/{applied['id']}/result",
            self.document(applied["result"]),
        )
        require(ack["state"] == "applied", "revoked_identity_lost_closed_history")
        self.record("revoked_agent_denied_new_poll_and_original_read")

    def run(self):
        try:
            items = self.items()
            original_ids = {item["id"] for item in items}
            item = next(item for item in items if item["file_name"] == "a.png")
            applied = self.read_restart(item)
            self.lost_poll_after_expiry(item)
            self.scan_restart(original_ids)
            self.offline_metadata(original_ids, item)
            self.revoked(applied, item)
        finally:
            self.close()
