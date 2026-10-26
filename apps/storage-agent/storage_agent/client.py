"""Fixed-origin HTTPS; redirects and unbounded retries never carry credentials."""

import hashlib
from functools import partial
import http.client
import ssl
import time
from urllib import error, request
from uuid import uuid4

from aap_site_agent_contract import (
    canonical_json,
    digest,
    parse_json,
    validate_ack,
    validate_command,
)

from .config import AgentError


class RetryableError(AgentError):
    pass


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class DeadlineConnection(http.client.HTTPSConnection):
    """Recheck expiry after TLS before writing request headers or content."""

    def __init__(self, *args, command_deadline=None, clock=time.time, **kwargs):
        super().__init__(*args, **kwargs)
        self.command_deadline = command_deadline
        self.clock = clock

    def remaining(self):
        if self.command_deadline is None:
            return self.timeout
        remaining = self.command_deadline - self.clock()
        if remaining <= 0:
            raise AgentError("command_expired")
        return min(self.timeout, remaining)

    def connect(self):
        self.timeout = self.remaining()
        super().connect()
        try:
            self.remaining()
        except BaseException:
            self.close()
            raise

    def send(self, data):
        if self.sock is None:
            self.connect()
        self.sock.settimeout(self.remaining())
        super().send(data)


class DeadlineHTTPSHandler(request.HTTPSHandler):
    def https_open(self, req):
        connection = partial(
            DeadlineConnection,
            command_deadline=getattr(req, "agent_deadline", None),
            clock=getattr(req, "agent_clock", time.time),
        )
        return self.do_open(connection, req, context=self._context)


class Bandwidth:
    """One bounded chunk burst, then aggregate sustained byte-rate pacing."""

    def __init__(self, rate, *, clock=time.monotonic, sleep=time.sleep):
        self.rate = rate
        self.clock = clock
        self.sleep = sleep
        self.available_at = clock()

    def consume(self, size):
        now = self.clock()
        if self.available_at > now:
            self.sleep(self.available_at - now)
            now = self.clock()
        self.available_at = max(now, self.available_at) + size / self.rate


class Client:
    def __init__(
        self, config, credentials, *, opener=None, clock=time.time, sleep=time.sleep
    ):
        self.config = config
        self.credentials = credentials
        self.clock = clock
        self.sleep = sleep
        self.session_id = str(uuid4())
        self.bandwidth = Bandwidth(config.bandwidth_bytes_per_second, sleep=sleep)
        context = ssl.create_default_context(cafile=config.ca_file)
        # Environment proxy settings cannot redirect the independent identity.
        self.opener = opener or request.build_opener(
            request.ProxyHandler({}),
            NoRedirect(),
            DeadlineHTTPSHandler(context=context),
        )

    def _post(self, path, value=None, *, data=None, headers=None, deadline=None):
        if not path.startswith("/api/v1/site-agents/") or "?" in path or "#" in path:
            raise AgentError("invalid_center_endpoint")
        if self.clock() >= self.credentials.expires_at:
            raise AgentError("credential_expired")
        payload = canonical_json(value) if data is None else data
        headers = {
            **(headers or {}),
            "Content-Type": "application/json"
            if data is None
            else "application/octet-stream",
            "Authorization": "Bearer " + self.credentials.token,
            "X-Agent-Protocol": "1",
        }
        req = request.Request(
            self.config.center_url + path, payload, headers, method="POST"
        )
        req.agent_deadline = deadline
        req.agent_clock = self.clock
        timeout = 25 if deadline is None else min(25, deadline - self.clock())
        if timeout <= 0:
            raise AgentError("command_expired")
        try:
            with self.opener.open(req, timeout=timeout) as response:
                if response.status != 200:
                    raise AgentError("unexpected_center_status")
                raw = response.read(2 * 1024 * 1024 + 1)
                return parse_json(raw, max_bytes=2 * 1024 * 1024)
        except error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status == 429 or 500 <= status <= 599:
                raise RetryableError("center_temporarily_unavailable") from None
            # Do not print response bodies or exception URLs: they may hold credentials.
            raise AgentError(
                {
                    401: "agent_unauthorized",
                    403: "agent_revoked",
                    409: "agent_fence_rejected",
                    426: "agent_protocol_incompatible",
                }.get(status, "center_request_rejected")
            ) from None
        except (error.URLError, TimeoutError, OSError):
            raise RetryableError("center_unreachable") from None

    def poll(self):
        response = self._post(
            "/api/v1/site-agents/commands/poll",
            {
                "protocol_version": 1,
                "agent_id": self.credentials.agent_id,
                "credential_revision": self.credentials.credential_revision,
                "session_id": self.session_id,
                "roots": [
                    {"root_handle": handle, "root_revision": spec.binding_revision}
                    for handle, spec in sorted(self.config.roots.items())
                ],
                "limits": self.config.public_limits(),
            },
        )
        if (
            not isinstance(response, dict)
            or set(response) != {"protocol_version", "command"}
            or type(response["protocol_version"]) is not int
            or response["protocol_version"] != 1
        ):
            raise AgentError("invalid_poll_response")
        return (
            validate_command(response["command"])
            if response["command"] is not None
            else None
        )

    def submit(self, runner, result):
        command_id = result["command_id"]
        row = runner.journal.get(command_id)
        if row is None:
            raise AgentError("result_identity_mismatch")
        command = row["command"]
        # Expiry can stop a byte replay, never renew the original authorization.
        body = runner.outgoing(result) if self.clock() < command["expires_at"] else None
        if body is not None:
            try:
                offset = 0
                while data := body.read(
                    min(256 * 1024, self.config.bandwidth_bytes_per_second)
                ):
                    self.bandwidth.consume(len(data))
                    if self.clock() >= command["expires_at"]:
                        raise AgentError("command_expired")
                    receipt = self._post(
                        "/api/v1/site-agents/commands/" + command_id + "/chunks",
                        data=data,
                        deadline=command["expires_at"],
                        headers={
                            "X-Command-Digest": result["command_digest"],
                            "X-Chunk-Offset": str(offset),
                            "X-Chunk-SHA256": hashlib.sha256(data).hexdigest(),
                        },
                    )
                    if (
                        not isinstance(receipt, dict)
                        or set(receipt)
                        != {"command_id", "command_digest", "offset", "sha256"}
                        or receipt
                        != {
                            "command_id": command_id,
                            "command_digest": result["command_digest"],
                            "offset": offset + len(data),
                            "sha256": hashlib.sha256(data).hexdigest(),
                        }
                        or type(receipt["offset"]) is not int
                    ):
                        raise AgentError("chunk_ack_mismatch")
                    offset += len(data)
            finally:
                body.close()
        ack = validate_ack(
            self._post("/api/v1/site-agents/commands/" + command_id + "/result", result)
        )
        if any(
            ack[key] != value
            for key, value in {
                "command_id": command_id,
                "command_digest": result["command_digest"],
                "result_digest": digest(result),
            }.items()
        ):
            raise AgentError("ack_identity_mismatch")
        runner.journal.acknowledge(ack)
        if not result["closed"]:
            raise AgentError("source_close_unconfirmed")
        return ack

    def step(self, runner, *, drain=False):
        # Replay immutable completed results before claiming additional work.
        for row in runner.journal.pending():
            result = row["result"] or runner.execute(row["command"])
            self.submit(runner, result)
        if drain:
            return False
        command = self.poll()
        if command is None:
            return False
        self.submit(runner, runner.execute(command))
        return True

    def run(self, runner, *, once=False, drain=False):
        failures = 0
        while True:
            try:
                did_work = self.step(runner, drain=drain)
                failures = 0
                if once or drain:
                    return
                if not did_work:
                    self.sleep(1)
            except RetryableError:
                failures += 1
                if failures >= 5:
                    raise
                self.sleep(min(2**failures, 16))
