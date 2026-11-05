"""Private owner transport and explicit run identities for seed consumers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

import boto3
import httpx
from botocore.config import Config
from sqlalchemy import text


_ROOT = Path(__file__).resolve().parents[3]
_RETAINED_SEED_CLIENTS = []


def fixture_original_reader():
    """Use the launcher's read-only fixture credentials, never the writer."""
    prefix = "AAP_ASSETS_ADDITIONAL_ORIGINAL_"
    return boto3.client(
        "s3",
        endpoint_url=os.environ[prefix + "ENDPOINT"],
        aws_access_key_id=os.environ[prefix + "ACCESS_KEY"],
        aws_secret_access_key=os.environ[prefix + "SECRET_KEY"],
        region_name="us-east-1",
        config=Config(signature_version="s3v4"),
    )


async def _fixture_versions(engine, run):
    async with engine.connect() as db:
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT c.id,c.location_id,c.bucket,c.object_key,c.version_id,"
                        "c.sha256,c.size_bytes,c.status "
                        "FROM yoao_fixture_v1.fixture_run_resources AS r "
                        "JOIN public.managed_original_creations AS c ON c.id=r.creation_id "
                        "WHERE r.run_id=:run AND r.kind='creation' AND r.state='published'"
                    ),
                    {"run": run.run_id},
                )
            )
            .mappings()
            .all()
        )
    assert rows
    assert all(
        row["status"] == "verified"
        and row["version_id"]
        and row["bucket"] == os.environ["AAP_ASSETS_ADDITIONAL_ORIGINAL_BUCKET"]
        for row in rows
    )
    return rows


def _read_versions(client, rows):
    result = {}
    for row in rows:
        key = (row["bucket"], row["object_key"], row["version_id"])
        response = client.get_object(Bucket=key[0], Key=key[1], VersionId=key[2])
        assert response["VersionId"] == key[2]
        with closing(response["Body"]) as body:
            raw = body.read()
        assert len(raw) == row["size_bytes"]
        assert hashlib.sha256(raw).hexdigest() == row["sha256"]
        result[key] = raw
    return result


@dataclass(frozen=True)
class OwnedRun:
    suite_id: uuid.UUID
    run_id: uuid.UUID
    namespace: str

    def payload(self) -> dict[str, str]:
        return {
            "namespace": self.namespace,
            "suite_id": str(self.suite_id),
            "run_id": str(self.run_id),
        }

    def scope(self):
        from app.api.v1._test_seed import SeedOwnedRequest, _request_owned_scope

        return _request_owned_scope(SeedOwnedRequest(**self.payload()))


class SeedOwner:
    """One private Python owner, never a product request fallback."""

    def __init__(self, process, errors):
        self.process = process
        self.errors = errors
        self.lock = asyncio.Lock()
        self.failure = None
        self.last_operation_id = None

    async def _exchange(self, envelope):
        self.process.stdin.write((json.dumps(envelope) + "\n").encode())
        await self.process.stdin.drain()
        line = await self.process.stdout.readline()
        if not line:
            await self.process.wait()
            self.errors.seek(0)
            matches = re.findall(
                rb"^Fixture setup failed: (\w+)$", self.errors.read(), re.MULTILINE
            )
            kind = matches[-1].decode() if matches else f"exit {self.process.returncode}"
            raise RuntimeError(f"Fixture owner process failed: {kind}")
        result = json.loads(line)
        if result.get("operation_id") != envelope["operation_id"]:
            raise RuntimeError("Fixture owner returned a different operation identity")
        return result

    async def _request(self, payload):
        operation_id = str(uuid.uuid4())
        self.last_operation_id = operation_id
        async with self.lock:
            if self.failure is not None:
                raise self.failure
            result = None
            try:
                result = await self._exchange({
                    "action": "execute", "operation_id": operation_id, "payload": payload
                })
                if result.get("state") == "pending":
                    raise RuntimeError("Fixture operation outcome is pending")
                if (
                    ("owner_error" in result and (
                        not isinstance(result["owner_error"], str) or not result["owner_error"].isidentifier()
                    ))
                    or ("owner_error" not in result and (
                        type(result.get("status")) is not int
                        or not isinstance(result.get("body"), str)
                    ))
                    or ("journal_error" in result and (
                        not isinstance(result["journal_error"], str) or not result["journal_error"].isidentifier()
                    ))
                ):
                    raise RuntimeError("Fixture owner returned an invalid operation result")
                if result.get("owner_error") not in {
                    "OperationOutcomeUnresolved", "OperationIdentityConflict"
                }:
                    ack = await self._exchange({"action": "ack", "operation_id": operation_id})
                    if ack.get("state") != "acknowledged":
                        raise RuntimeError("Fixture operation receipt was not acknowledged")
            except BaseException as error:
                if result is not None:
                    error.fixture_response = result
                    error.add_note("Fixture operation response was received before transport failure")
                error.fixture_operation_id = operation_id
                error.add_note(f"Fixture operation ID: {operation_id}")
                self.failure = error
                raise
        causes = []
        if "owner_error" in result:
            causes.append(RuntimeError(f"Fixture operation failed: {result['owner_error']}"))
        elif result.get("journal_error"):
            causes.append(RuntimeError(f"Fixture operation returned HTTP {result['status']}"))
        if result.get("journal_error"):
            causes.append(RuntimeError(f"Fixture evidence journal failed: {result['journal_error']}"))
        if causes:
            error = causes[0] if len(causes) == 1 else ExceptionGroup(
                "Fixture operation and evidence failed", causes
            )
            error.add_note(f"Fixture operation ID: {operation_id}")
            raise error
        return result

    async def recover_operation(self, operation_id):
        """Query the original authority without re-executing the operation."""
        async with self.lock:
            return await self._exchange({"action": "recover", "operation_id": str(operation_id)})

    async def acknowledge_operation(self, operation_id):
        async with self.lock:
            result = await self._exchange({"action": "ack", "operation_id": str(operation_id)})
            if result.get("state") != "acknowledged":
                raise RuntimeError("Fixture operation outcome is not completed")

    async def prepare_suite(self, suite_id: uuid.UUID, family: str):
        result = await self._request(
            {
                "operation": "prepare_fixture_suite",
                "data": {"suite_id": str(suite_id), "family": family},
            }
        )
        assert result["status"] == 200
        assert json.loads(result["body"]) == {
            "suite_id": str(suite_id),
            "family": family,
        }

    async def shutdown_authority(self, suite_id: uuid.UUID | None = None) -> str:
        payload = {"operation": "owner_shutdown"}
        if suite_id is not None:
            payload["suite_id"] = str(suite_id)
        result = await self._request(payload)
        if result["status"] != 200:
            raise RuntimeError(f"Fixture authority shutdown failed: {result['status']}")
        state = json.loads(result["body"])["state"]
        if state not in {"stopped", "shared"}:
            raise RuntimeError("Fixture authority returned an invalid shutdown state")
        return state

    async def post(self, path: str, *, json: dict | None = None, headers=None):
        result = await self._request({"path": path, "data": json, "headers": headers})
        return httpx.Response(
            result["status"],
            text=result["body"],
            request=httpx.Request("POST", "http://fixture.local" + path),
        )


@dataclass(frozen=True)
class OwnedSuite:
    owner: SeedOwner
    suite_id: uuid.UUID

    def new_run(self, namespace: str) -> OwnedRun:
        return OwnedRun(self.suite_id, uuid.uuid4(), namespace)

    async def finish(self):
        response = await self.owner.post(
            "/api/v1/__test/seed/cleanup", json={"suite_id": str(self.suite_id)}
        )
        assert response.status_code == 200, response.text


@asynccontextmanager
async def private_seed_owner():
    """Launch the private owner process without passing its DSN to product HTTP."""
    errors = tempfile.TemporaryFile()
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(_ROOT / "apps/web/e2e/fixtures/seed-request.py"),
            cwd=_ROOT / "apps/api",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=errors,
            limit=16 * 1024 * 1024,
        )
    except BaseException:
        errors.close()
        raise
    owner = SeedOwner(process, errors)
    failure = None
    try:
        yield owner
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if owner.failure is not None and process.returncode is None:
            # A pending response is not physical settlement; retain this client
            # and use a different client to recover the same authority.
            _RETAINED_SEED_CLIENTS.append(owner)
        else:
            try:
                if process.returncode is None:
                    process.stdin.close()
                    await process.wait()
                if process.returncode:
                    raise RuntimeError(
                        f"Fixture owner process exited with status {process.returncode}"
                    )
            except BaseException as shutdown_failure:
                if failure is not None:
                    raise BaseExceptionGroup(
                        "Fixture operation and owner shutdown failed",
                        [failure, shutdown_failure],
                    ) from None
                raise
            finally:
                errors.close()


@asynccontextmanager
async def private_owned_suite():
    """Prepare one fresh suite and finalize its exact registered child runs."""
    async with private_seed_owner() as owner:
        suite = OwnedSuite(owner, uuid.uuid4())
        await owner.prepare_suite(suite.suite_id, "owned")
        failure = None
        try:
            yield suite
        except BaseException as exc:
            failure = exc
            raise
        finally:
            if owner.failure is not None:
                if failure is None:
                    raise owner.failure
            else:
                try:
                    await suite.finish()
                    await owner.shutdown_authority(suite.suite_id)
                except BaseException as cleanup_failure:
                    if failure is not None:
                        raise BaseExceptionGroup(
                            "Fixture operation and suite cleanup failed",
                            [failure, cleanup_failure],
                        ) from None
                    raise
