"""Two independently paired Linux sites using explicit, real replica grants.

The helper mutates only its Owner's containers, files and disposable API data.
SQL is used solely to observe actual transport receipts, never to seed them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
import uuid

from site_agent_recovery import rows
from verify_site_agent import ROOT, private_json, require, wait_until


def ui_handoff(owner, value, seconds, *, clock=time.monotonic, sleep=time.sleep):
    require(type(seconds) is int and 0 <= seconds <= 300, "ui_hold_out_of_bounds")
    if not seconds:
        return "disabled"
    ready = {
        **value,
        "schema": "aap.site-agent-ui/v1",
        "owner_nonce": owner.value["nonce"],
        "checkout": str(ROOT),
        "hold_seconds": seconds,
    }
    private_json(owner.state / "ui-ready.json", ready)
    deadline = clock() + seconds
    done = owner.state / "ui-done.json"
    while clock() < deadline:
        if done.exists():
            observed = done.lstat()
            require(
                stat.S_ISREG(observed.st_mode)
                and observed.st_uid == os.getuid()
                and not observed.st_mode & 0o077
                and observed.st_size <= 4096,
                "ui_done_not_private",
            )
            completed = json.loads(done.read_bytes())
            require(
                completed.get("owner_nonce") == owner.value["nonce"], "ui_owner_changed"
            )
            return "completed"
        sleep(min(0.2, max(0, deadline - clock())))
    return "timeout"


class Replicas:
    def __init__(
        self,
        *,
        owner,
        http,
        image,
        center,
        worker,
        agent,
        network,
        replica_network,
        replica_gateway,
        port,
        site_dir,
        source_root,
        dataset,
        source,
        location,
        files,
        node,
        record,
    ):
        self.owner, self.http, self.image = owner, http, image
        self.center, self.worker, self.agent, self.network = (
            center,
            worker,
            agent,
            network,
        )
        self.replica_network, self.replica_gateway, self.port = (
            replica_network,
            replica_gateway,
            port,
        )
        self.site_dir, self.source_root = site_dir, source_root
        self.dataset, self.source, self.location = dataset, source, location
        self.files, self.node, self.record = files, node, record
        self.identity_a = json.loads((site_dir / "identity.json").read_bytes())

    def items(self):
        return self.http.call(
            "GET", f"/api/v1/datasets/{self.dataset['id']}/items?limit=100"
        )["items"]

    def detail(self, item):
        return self.http.call(
            "GET", f"/api/v1/datasets/{self.dataset['id']}/items/{item['id']}"
        )

    def links(self):
        return self.http.call("GET", self.base)["items"]

    def content(self, *, headers=None, expected=200, peer=None):
        return (peer or self.http).call(
            "GET", self.path, binary=True, headers=headers, expected=expected
        )

    def verified(self, peer=None):
        body, headers = self.content(peer=peer)
        require(
            body == self.files["a.png"]
            and headers.get("X-Content-SHA256") == self.sha
            and headers.get("Accept-Ranges") == "bytes",
            "replica_full_sha_changed",
        )
        part, headers = self.content(
            peer=peer,
            expected=206,
            headers={"Range": "bytes=17-271", "If-Range": f'"sha256:{self.sha}"'},
        )
        require(
            part == body[17:272]
            and headers.get("Content-Range") == f"bytes 17-271/{len(body)}"
            and headers.get("X-Content-SHA256") == self.sha,
            "replica_range_changed",
        )

    def commands(self):
        return rows(
            "SELECT id, agent_id, state, closed, payload, result, received_bytes "
            "FROM site_agent_commands WHERE agent_id=ANY($1::uuid[]) ORDER BY created_at,id",
            [
                uuid.UUID(self.identity_a["agent_id"]),
                uuid.UUID(self.pairing["agent_id"]),
            ],
        )

    def start_second_site(self):
        owner, http = self.owner, self.http
        self.private = owner.state / "site-b"
        self.source_b = owner.state / "source-b"
        self.private.mkdir(mode=0o700)
        (self.source_b / "mounted/dataset/child").mkdir(parents=True, mode=0o700)
        for name, data in self.files.items():
            (self.source_b / "mounted/dataset" / name).write_bytes(data)
        shutil.copyfile(self.site_dir / "ca.pem", self.private / "ca.pem")
        shutil.copyfile(self.site_dir / "run.py", self.private / "run.py")
        site = http.call(
            "POST",
            "/api/v1/site-agents/sites",
            {
                "name": "Owned replica site B " + owner.value["nonce"][:8],
            },
            expected=201,
        )
        self.pairing = http.call(
            "POST",
            f"/api/v1/site-agents/sites/{site['id']}/agents",
            {
                "name": "Owned independent Linux Agent B",
            },
            expected=201,
        )
        private_json(
            self.private / "pair.json",
            {
                "agent_id": self.pairing["agent_id"],
                "pair_code": self.pairing["pair_code"],
            },
        )
        private_json(
            self.private / "config.json",
            {
                "center_url": f"https://{self.replica_gateway}:{self.port}",
                "state_dir": "/agent-private/state",
                "credential_file": "/agent-private/identity.json",
                "roots": {"images": {"path": "/source", "revision": 1}},
                "ca_file": "/agent-private/ca.pem",
            },
        )
        self.agent_b = owner.create(
            "container",
            "agent-b",
            image=self.image,
            options=[
                "--network",
                self.replica_network["id"],
                "--user",
                str(os.getuid()),
                "--entrypoint",
                "python",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--env",
                "PYTHONPATH=/code/agent:/code/fs:/code/wire",
                "--volume",
                str(ROOT / "apps/storage-agent") + ":/code/agent:ro",
                "--volume",
                str(ROOT / "apps/_shared/filesystem_read/src") + ":/code/fs:ro",
                "--volume",
                str(ROOT / "apps/_shared/site_agent_contract/src") + ":/code/wire:ro",
                "--volume",
                str(self.source_b) + ":/source:ro",
                "--volume",
                str(self.private) + ":/agent-private",
            ],
            command=["/agent-private/run.py"],
        )
        owner.action(self.agent_b, "start")
        require(
            owner.execute(
                self.agent_b,
                [
                    "python",
                    "-m",
                    "storage_agent",
                    "--config",
                    "/agent-private/config.json",
                    "--pairing-file",
                    "/agent-private/pair.json",
                ],
            ).stdout.strip()
            == b"agent_paired",
            "replica_pairing_failed",
        )
        wait_until(
            lambda: http.call(
                "GET", "/api/v1/site-agents/agents?agent_id=" + self.pairing["agent_id"]
            ),
            lambda value: value["items"] and value["items"][0]["state"] == "online",
        )
        self.location_b = http.call(
            "POST",
            "/api/v1/storage-locations/agent",
            {
                "name": "Owned replica images B",
                "agent_id": self.pairing["agent_id"],
                "root_handle": "images",
                "root_revision": 1,
                "relative_path": "mounted",
                "scope": "global",
                "cache_policy": "none",
            },
            expected=201,
        )
        require(
            http.call(
                "POST",
                f"/api/v1/storage-locations/{self.location_b['id']}/probe",
                {
                    "sample_file": "dataset/a.png",
                },
            )["ok"],
            "replica_probe_failed",
        )
        self.source_b_row = http.call(
            "POST",
            "/api/v1/dataset-sources",
            {
                "name": "Outbound source B",
                "dataset_id": self.dataset["id"],
                "location_id": self.location_b["id"],
                "relative_path": "dataset",
                "node_id": self.node,
                "cache_policy": "none",
            },
            expected=201,
        )
        self.check_isolation()
        self.record("second_independent_linux_agent_and_readonly_root_ready")

    def check_isolation(self):
        owner = self.owner
        agents = [owner.inspect(self.agent), owner.inspect(self.agent_b)]
        require(
            set(agents[0]["NetworkSettings"]["Networks"]).isdisjoint(
                agents[1]["NetworkSettings"]["Networks"]
            ),
            "sites_share_a_network",
        )
        for observed, source in zip(
            agents, (self.source_root, self.source_b), strict=True
        ):
            require(
                any(
                    mount["Source"] == str(source)
                    and mount["Destination"] == "/source"
                    and mount["RW"] is False
                    for mount in observed["Mounts"]
                ),
                "source_mount_not_readonly",
            )
        for resource in (self.center, self.worker):
            observed = owner.inspect(resource)
            require(
                not any(
                    mount["Source"] in {str(self.source_root), str(self.source_b)}
                    for mount in observed["Mounts"]
                ),
                "center_mounted_site_source",
            )
        canary = owner.create(
            "container",
            "canary-b",
            image=self.image,
            options=[
                "--network",
                self.replica_network["id"],
                "--entrypoint",
                "python",
            ],
            command=[
                "-c",
                "import socket; s=socket.socket(); s.bind(('0.0.0.0',8123)); s.listen();\nwhile True:\n c,_=s.accept(); c.close()",
            ],
        )
        owner.action(canary, "start")
        wait_until(
            lambda: (
                owner.execute(
                    canary,
                    [
                        "python",
                        "-c",
                        "import socket; socket.create_connection(('127.0.0.1',8123),timeout=2).close()",
                    ],
                    check=False,
                ).returncode
            ),
            lambda value: value == 0,
            seconds=10,
        )
        address = owner.inspect(canary)["NetworkSettings"]["Networks"][
            self.replica_network["name"]
        ]["IPAddress"]
        code = (
            "import socket\ntry:\n s=socket.create_connection(("
            + repr(address)
            + ",8123),timeout=2)\nexcept OSError:\n print('isolated')\nelse:\n s.close(); raise SystemExit(3)"
        )
        require(
            owner.execute(self.center, ["python", "-c", code]).stdout.strip()
            == b"isolated",
            "center_can_enter_replica_site",
        )
        self.record("center_inbound_second_site_blocked_and_sources_not_mounted")

    def register_second_source(self):
        scan = self.http.call(
            "POST",
            f"/api/v1/dataset-sources/{self.source_b_row['id']}/scans",
            expected=202,
        )
        scan = wait_until(
            lambda: self.http.call("GET", f"/api/v1/dataset-scans/{scan['id']}"),
            lambda value: value["status"] in {"completed", "failed", "cancelled"},
        )
        require(
            scan["status"] == "completed" and scan["complete"], "replica_scan_failed"
        )
        self.http.call(
            "POST", f"/api/v1/dataset-scans/{scan['id']}/registration", expected=202
        )

        def registration():
            return self.http.call(
                "GET", f"/api/v1/dataset-scans/{scan['id']}/registration"
            )

        ready = wait_until(
            registration, lambda value: value["status"] in {"completed", "failed"}
        )
        require(
            ready["status"] == "completed" and ready["counts"]["ready_count"] == 2,
            "replica_preflight_failed",
        )
        self.http.call(
            "POST", f"/api/v1/dataset-registrations/{ready['id']}/confirm", expected=202
        )
        result = wait_until(
            registration,
            lambda value: (
                value["phase"] == "registering"
                and value["status"] in {"completed", "failed"}
            ),
        )
        require(
            result["status"] == "completed"
            and result["counts"]["registered_count"] == 2
            and result["counts"]["original_copy_bytes"] == 0,
            "replica_registration_failed",
        )
        self.record("second_site_explicitly_registered_without_copy", result["counts"])

    def permissions(self):
        key = self.http.call(
            "POST",
            "/api/v1/me/api-keys",
            {
                "name": "Owned replica reader",
                "scopes": ["datasets:read"],
            },
            expected=201,
        )
        reader = type(self.http)(self.http.origin, self.site_dir / "ca.pem")
        reader.token = key["plaintext"]
        self.verified(reader)
        reader.call(
            "POST", self.base, {"replica_item_id": self.item_b["id"]}, expected=403
        )
        self.http.call("DELETE", f"/api/v1/me/api-keys/{key['id']}", expected=204)
        body, headers = self.content(peer=reader, expected=401)
        require(
            headers.get("X-Content-SHA256") is None and body != self.files["a.png"],
            "revoked_reader_received_content",
        )
        self.record("replica_route_preserves_current_scopes_and_reader_revocation")

    def rejected_content(self):
        # B has higher priority; with both candidates unavailable the last error
        # may describe A. Prove rejection, rather than pinning one candidate code.
        body, headers = self.content(expected={403, 409, 410, 422, 503, 504})
        require(
            headers.get("X-Content-SHA256") is None
            and headers.get_content_type() == "application/json"
            and isinstance(json.loads(body).get("detail"), (str, dict)),
            "invalid_replica_served_content",
        )

    def run(self, *, ui_hold_seconds=0):
        original = self.items()
        require(len(original) == 2, "primary_items_missing")
        self.item_a = next(item for item in original if item["file_name"] == "a.png")
        before = self.detail(self.item_a)
        self.base = f"/api/v1/dataset-items/{self.item_a['id']}/reference-replicas"
        self.sha = hashlib.sha256(self.files["a.png"]).hexdigest()
        self.path = (
            f"/api/v1/dataset-items/{self.item_a['id']}/content?sha256={self.sha}"
        )
        self.start_second_site()
        self.register_second_source()
        replicas = [
            item
            for item in self.items()
            if item["id"] not in {item["id"] for item in original}
        ]
        require(len(replicas) == 2, "physical_replicas_collapsed_into_primary")
        self.item_b = next(item for item in replicas if item["file_name"] == "a.png")
        require(not self.links(), "sha_alone_created_replica_grant")
        candidates = self.http.call(
            "GET",
            self.base.replace("reference-replicas", "reference-replica-candidates"),
        )
        require(
            any(row["item_id"] == self.item_b["id"] for row in candidates["items"]),
            "registered_same_content_candidate_missing",
        )
        self.record("matching_sha_does_not_automatically_link_physical_items")
        link = self.http.call(
            "POST",
            self.base,
            {"replica_item_id": self.item_b["id"], "priority": 0},
            expected=201,
        )
        require(
            link["item_id"] == self.item_a["id"]
            and link["replica_item_id"] == self.item_b["id"],
            "explicit_replica_identity_changed",
        )
        self.owner.action(self.agent, "kill")
        require(
            not self.owner.inspect(self.agent)["State"]["Running"],
            "primary_agent_still_running",
        )
        wait_until(
            lambda: self.http.call(
                "GET",
                "/api/v1/site-agents/agents?agent_id=" + self.identity_a["agent_id"],
            ),
            lambda value: value["items"][0]["state"] == "offline",
            seconds=75,
        )
        self.record("primary_agent_physically_stopped_and_observed_offline")
        commands = {row["id"] for row in self.commands()}
        self.verified()
        delivered = [row for row in self.commands() if row["id"] not in commands]
        require(
            delivered
            and all(
                str(row["agent_id"]) == self.pairing["agent_id"]
                and row["state"] == "applied"
                and row["closed"] is True
                for row in delivered
            ),
            "replica_transport_not_acknowledged",
        )
        after = self.detail(self.item_a)
        stable = (
            "id",
            "dataset_id",
            "file_name",
            "file_path",
            "content_hash",
            "original_identity_path",
        )
        require(
            all(before[key] == after[key] for key in stable),
            "replica_replaced_logical_identity",
        )
        self.record(
            "offline_primary_read_from_b_matches_full_sha_and_range",
            {
                "sha256": self.sha,
                "bytes": len(self.files["a.png"]),
                "applied_read_commands": len(delivered),
            },
        )
        status = ui_handoff(
            self.owner,
            {
                "api_origin": self.http.origin,
                "ca_path": str(self.site_dir / "ca.pem"),
                "private_identity_path": str(self.owner.state / "center/identity.json"),
                "dataset_id": self.dataset["id"],
                "item_a_id": self.item_a["id"],
                "item_b_id": self.item_b["id"],
                "source_a_id": self.source["id"],
                "source_b_id": self.source_b_row["id"],
                "location_a_id": self.location["id"],
                "location_b_id": self.location_b["id"],
            },
            ui_hold_seconds,
        )
        self.record("optional_owner_bound_ui_handoff", status)
        require(
            any(link["replica_item_id"] == self.item_b["id"] for link in self.links()),
            "ui_did_not_restore_replica_grant",
        )
        self.permissions()
        target = self.source_b / "mounted/dataset/a.png"
        corrupt = bytearray(target.read_bytes())
        corrupt[len(corrupt) // 2] ^= 0xFF
        target.write_bytes(corrupt)
        self.rejected_content()
        self.record("corrupt_b_rejected_without_mixed_or_partial_image")
        self.http.call(
            "POST",
            f"/api/v1/site-agents/agents/{self.pairing['agent_id']}/revoke",
            {"expected_revision": 1},
        )
        self.rejected_content()
        self.record("current_agent_revocation_cannot_be_bypassed_by_replica")
