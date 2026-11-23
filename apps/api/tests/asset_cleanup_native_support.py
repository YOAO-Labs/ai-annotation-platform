"""Saved-output fixtures for the standalone cleanup worker's real Source signer."""

from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
import json
import time
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services.dataset_assets import ensure_legacy_bindings


@dataclass(frozen=True)
class _Member:
    output_id: uuid.UUID
    operation_id: uuid.UUID
    prefix: str
    etag: str
    version_id: str | None
    recorded_sha256: str
    body: bytes

    def object_request(self, bucket: str) -> dict[str, str]:
        request = {"Bucket": bucket, "Key": self.prefix + "manifest.json"}
        if self.version_id is not None:
            request["VersionId"] = self.version_id
        return request


class NativeCleanupProbe:
    def __init__(self, engine, client, bucket: str, endpoint: str, fixture: dict):
        self.sessions = async_sessionmaker(engine)
        self.client = client
        self.bucket = bucket
        self.endpoint = endpoint
        self.asset_id = fixture["sibling_asset_id"]
        self.source_identity = fixture["sibling_source_key"]

    async def _seed(
        self, db, binding, generation: int, body: bytes, expected: bytes
    ) -> _Member:
        output_id, operation_id, job_id, lease_token = (uuid.uuid4() for _ in range(4))
        prefix = f"image-pyramids/objects/{output_id}/"
        put = self.client.put_object(
            Bucket=self.bucket, Key=prefix + "manifest.json", Body=body, IfNoneMatch="*"
        )
        digest = sha256(expected).hexdigest()
        version = put.get("VersionId")
        storage_ref = {
            "schema": 2,
            "purpose": "media-cache",
            "location_id": str(binding.location_id),
            "config_revision": binding.config_revision,
            "binding_generation": binding.generation,
            "endpoint_url": self.endpoint,
            "bucket": self.bucket,
            "key_prefix": "",
            "prefix": prefix,
            "storage_identity": sha256(
                json.dumps([self.endpoint, self.bucket]).encode()
            ).hexdigest(),
        }
        values = {
            "asset": self.asset_id,
            "generation": generation,
            "job": job_id,
            "lease": lease_token,
            "snapshot": sha256(self.source_identity.encode()).hexdigest(),
            "source": self.source_identity,
            "output": output_id,
            "ref": json.dumps(storage_ref),
            "gate": binding.gate_revision,
            "operation": operation_id,
            "permit": uuid.uuid4(),
            "sha256": digest,
            "size": len(body),
            "etag": put["ETag"],
            "version": version,
        }
        assert len(expected) == len(body)
        for statement in (
            """
            INSERT INTO public.image_pyramid_generations (
                asset_id,generation,job_id,job_generation,source_generation,source_snapshot,
                source_identity,status,normalization_version,policy
            ) VALUES (
                :asset,:generation,:job,1,1,:snapshot,:source,'failed','cleanup-runtime-v1',
                jsonb_build_object('gc_grace_hours',0,'url_expiry_seconds',1)
            )
            """,
            """
            INSERT INTO public.image_pyramid_outputs (id,asset_id,generation,lease_token,storage_ref)
            VALUES (:output,:asset,:generation,:lease,CAST(:ref AS jsonb))
            """,
            """
            INSERT INTO yoao_assets_v1.image_pyramid_native_attempts (
                output_id,job_id,job_generation,lease_token,asset_id,generation,
                source_generation,source_snapshot,profile_version,gate_revision
            ) VALUES (:output,:job,1,:lease,:asset,:generation,1,:snapshot,'cleanup-runtime-v1',:gate)
            """,
            """
            INSERT INTO yoao_assets_v1.image_pyramid_native_members (
                operation_id,output_id,relative_key,sha256,size_bytes,state,operation_permit,etag,version_id
            ) VALUES (:operation,:output,'manifest.json',:sha256,:size,'written',:permit,:etag,:version)
            """,
        ):
            await db.execute(text(statement), values)
        return _Member(
            output_id, operation_id, prefix, put["ETag"], version, digest, body
        )

    async def seed(self) -> None:
        """Create fixtures whose immutable receipts live until verifier retirement."""
        async with self.sessions.begin() as db:
            await ensure_legacy_bindings(db)
            await db.execute(
                text("""
                INSERT INTO public.storage_maintenance_gates (location_id,state,revision)
                SELECT location_id,'active',1 FROM public.storage_bindings WHERE purpose='media-cache'
                ON CONFLICT (location_id) DO NOTHING
            """)
            )
            binding = (
                await db.execute(
                    text("""
                SELECT b.location_id,b.bucket,b.generation,l.config_revision,l.config,g.revision AS gate_revision
                FROM public.storage_bindings b
                JOIN public.storage_locations l ON l.id=b.location_id
                JOIN public.storage_maintenance_gates g ON g.location_id=b.location_id
                WHERE b.purpose='media-cache' AND g.state='active' AND l.connection_id IS NULL
            """)
                )
            ).one()
            scheme = "https" if binding.config["use_ssl"] else "http"
            if (
                binding.bucket != self.bucket
                or f"{scheme}://{binding.config['endpoint']}" != self.endpoint
            ):
                raise RuntimeError(
                    "Native cleanup storage must be the verified private builtin location"
                )
            self.verified = await self._seed(
                db, binding, 2, b"verified member", b"verified member"
            )
            self.rejected = await self._seed(
                db, binding, 3, b"retained member", b"declared member"
            )

    @property
    def rejected_object(self) -> dict[str, str]:
        return self.rejected.object_request(self.bucket)

    async def _wait_page_states(self, member: _Member, *expected: str) -> list[str]:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            async with self.sessions() as db:
                states = list(
                    (
                        await db.scalars(
                            text("""
                    SELECT state FROM yoao_assets_v1.native_pyramid_retirement_pages
                    WHERE output_id=:output ORDER BY prepared_at,page_id
                """),
                            {"output": member.output_id},
                        )
                    ).all()
                )
            if set(expected).issubset(states):
                return states
            await asyncio.sleep(0.2)
        raise RuntimeError(
            f"Native retirement {member.output_id} did not reach {expected}: {states}"
        )

    def _assert_no_versions(self, member: _Member) -> None:
        page = self.client.list_object_versions(
            Bucket=self.bucket, Prefix=member.prefix
        )
        assert not page.get("Versions") and not page.get("DeleteMarkers")

    async def exercise(self, worker_log_contains) -> dict:
        await self._wait_page_states(self.verified, "members_absent", "prefix_absent")
        self._assert_no_versions(self.verified)
        await self._wait_page_states(self.rejected, "closed_without_ack")
        failure = (
            f"Native retirement member failed output_id={self.rejected.output_id} "
            f"operation_id={self.rejected.operation_id} code=native_member_identity_mismatch"
        )
        assert await asyncio.to_thread(worker_log_contains, failure)
        response = self.client.get_object(**self.rejected_object)
        with closing(response["Body"]) as stream:
            retained = stream.read()
        assert retained == self.rejected.body
        assert response["ETag"] == self.rejected.etag
        assert response.get("VersionId") == self.rejected.version_id
        async with self.sessions() as db:
            state = (
                await db.execute(
                    text("""
                SELECT closed_at,after_operation_id FROM yoao_assets_v1.native_pyramid_retirements
                WHERE output_id=:output
            """),
                    {"output": self.rejected.output_id},
                )
            ).one()
        assert state.closed_at is None and state.after_operation_id is None
        return {
            "source_signed_exact_delete": {
                "output_id": str(self.verified.output_id),
                "operation_id": str(self.verified.operation_id),
                "key": self.verified.object_request(self.bucket)["Key"],
                "etag": self.verified.etag,
                "version_id": self.verified.version_id,
                "sha256": self.verified.recorded_sha256,
                "outcome": "prefix_absent",
            },
            "legacy_principal_native_delete_denied": True,
            "mismatch": {
                "output_id": str(self.rejected.output_id),
                "operation_id": str(self.rejected.operation_id),
                "key": self.rejected_object["Key"],
                "size_bytes": len(retained),
                "etag": self.rejected.etag,
                "version_id": self.rejected.version_id,
                "recorded_sha256": self.rejected.recorded_sha256,
                "retained_sha256": sha256(retained).hexdigest(),
                "outcome": "closed_without_ack",
            },
        }
