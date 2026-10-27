"""Wire values for durable managed-original allocation and physical evidence.

The Assets owner never accepts a caller-supplied storage target: ``begin``
resolves the current datasets binding, reserves the immutable locator and
returns it; the caller performs one conditional write (or one pinned adoption
read) and reports version-pinned evidence through ``record``. Publication of the
business item is only possible once the owner holds a verified creation row.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Protocol, TypeVar
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    model_validator,
)

MANAGED_ORIGINAL_SCHEMA = "aap-managed-original/v1"

ManagedOriginalOrigin = Literal["direct", "zip", "connector", "directory_copy"]
ManagedOriginalMode = Literal["write", "adopt"]
ManagedOriginalOutcome = Literal["verified", "failed", "conflict", "unknown"]
ManagedOriginalPhysicalOutcome = Literal["not_written", "written", "unknown"]
ManagedOriginalAuthorityKind = Literal[
    "dataset_import",
    "dataset_upload",
    "dataset_source_ingest",
    "operator_item",
]

MANAGED_ORIGINAL_FAILURES = frozenset(
    {
        "invalid_request",
        "creation_missing",
        "creation_scope_mismatch",
        "creation_state_conflict",
        "creation_configuration_changed",
        "creation_target_unavailable",
        "creation_key_conflict",
        "creation_budget_exceeded",
        "creation_byte_budget_exceeded",
        "creation_evidence_mismatch",
        "creation_missing",
        "creation_terminal",
        "operation_unsettled",
        "resource_budget_exceeded",
        "storage_unavailable",
        "invalid_response",
    }
)

_SHA256 = r"^[0-9a-f]{64}$"
_MD5 = r"^[0-9a-f]{32}$"

Digest = Annotated[str, Field(strict=True, pattern=_SHA256)]
Md5 = Annotated[str, Field(strict=True, pattern=_MD5)]
ByteCount = Annotated[int, Field(strict=True, ge=0, le=9_223_372_036_854_775_807)]
Text = Annotated[str, Field(strict=True, min_length=1, max_length=1000)]
SafeCode = Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9_]{0,79}$")]


class ManagedOriginalFailure(Exception):
    def __init__(self, code: str):
        if code not in MANAGED_ORIGINAL_FAILURES:
            raise ValueError("Unknown managed original failure")
        super().__init__(code)
        self.code = code


class _Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class _Staging(_Value):
    bucket: Text
    key: Text
    etag: StrictStr | None = None
    version_id: StrictStr | None = None


class ManagedOriginalAuthority(_Value):
    kind: ManagedOriginalAuthorityKind
    job_id: UUID | None = None
    operation_id: UUID | None = None
    batch_id: UUID | None = None
    entry_index: Annotated[int, Field(strict=True, ge=0, le=2_147_483_647)] | None = (
        None
    )
    publication_id: UUID | None = None
    item_id: UUID | None = None

    @model_validator(mode="after")
    def check_scope(self):
        if self.kind == "dataset_import":
            if (
                self.job_id is None
                or self.entry_index is None
                or self.publication_id is None
            ):
                raise ValueError(
                    "dataset import authority needs job, entry and publication"
                )
            if any(
                value is not None
                for value in (self.operation_id, self.batch_id, self.item_id)
            ):
                raise ValueError("dataset import authority has unrelated fields")
        elif self.kind == "dataset_upload":
            if self.item_id is None or self.publication_id is None:
                raise ValueError("dataset upload authority needs item and publication")
            if any(
                value is not None
                for value in (
                    self.job_id,
                    self.operation_id,
                    self.batch_id,
                    self.entry_index,
                )
            ):
                raise ValueError("dataset upload authority has unrelated fields")
        elif self.kind == "dataset_source_ingest":
            if (
                self.operation_id is None
                or self.batch_id is None
                or self.entry_index is None
                or self.publication_id is None
            ):
                raise ValueError(
                    "ingest authority needs operation, batch, entry and publication"
                )
            if self.job_id is not None or self.item_id is not None:
                raise ValueError("ingest authority has unrelated fields")
        else:
            if (
                self.operation_id is None
                or self.item_id is None
                or self.publication_id is None
            ):
                raise ValueError(
                    "operator authority needs operation, item and publication"
                )
            if any(
                value is not None
                for value in (self.job_id, self.batch_id, self.entry_index)
            ):
                raise ValueError("operator authority has unrelated fields")
        return self

    def jsonb(self) -> dict[str, str | int]:
        return {
            key: str(value) if isinstance(value, UUID) else value
            for key, value in self.model_dump(exclude_none=True).items()
        }


class ManagedOriginalMaterial(_Value):
    creation_id: UUID
    dataset_id: UUID
    location_id: UUID
    connection_id: UUID | None = None
    connection_revision: Annotated[int, Field(strict=True, ge=1)] | None = None
    bucket: Text
    object_key: Text
    key_prefix: StrictStr
    endpoint_url: StrictStr
    storage_identity: Digest
    config_revision: Annotated[int, Field(strict=True, ge=1)]
    binding_generation: Annotated[int, Field(strict=True, ge=1)]
    max_bytes: ByteCount
    content_type: StrictStr
    origin: ManagedOriginalOrigin
    mode: ManagedOriginalMode
    staging: _Staging | None = None
    publication_id: UUID | None = None
    file_path: StrictStr
    destination: dict[str, Any] | None = None
    status: StrictStr | None = None
    sha256: Digest | None = None
    size_bytes: ByteCount | None = None
    etag: StrictStr | None = None
    version_id: StrictStr | None = None

    @model_validator(mode="after")
    def check_target(self):
        if self.mode == "adopt" and self.origin in {"direct", "zip"}:
            raise ValueError("upload and ZIP origin never adopt pre-existing bytes")
        if self.origin == "direct" and self.staging is None:
            raise ValueError("direct origin requires its staging observation")
        if self.key_prefix and not self.object_key.startswith(self.key_prefix):
            raise ValueError("object_key does not start with its prefix")
        if self.file_path != self.object_key[len(self.key_prefix) :]:
            raise ValueError("file_path does not match the allocated locator")
        if (self.connection_id is None) != (self.connection_revision is None):
            raise ValueError("connection identity and revision must be paired")
        if self.connection_id is not None and not self.destination:
            raise ValueError("connection target requires its encrypted credentials")
        return self


class ManagedOriginalProof(_Value):
    outcome: ManagedOriginalOutcome
    mode: ManagedOriginalMode
    physical_outcome: ManagedOriginalPhysicalOutcome = "unknown"
    sha256: Digest | None = None
    md5: Md5 | None = None
    size_bytes: ByteCount | None = None
    etag: StrictStr | None = None
    version_id: StrictStr | None = None
    request_id: StrictStr | None = None
    prepared: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    observed: dict[str, Any] | None = None
    error_code: SafeCode | None = None

    @model_validator(mode="after")
    def check_outcome(self):
        if self.outcome == "verified":
            if (
                self.sha256 is None
                or self.md5 is None
                or self.size_bytes is None
                or not self.etag
                or self.verification is None
                or self.error_code is not None
            ):
                raise ValueError("verified proof needs content and readback evidence")
            if (
                self.verification.get("sha256") != self.sha256
                or self.verification.get("md5") != self.md5
                or self.verification.get("size_bytes") != self.size_bytes
                or self.verification.get("etag") != self.etag
                or self.verification.get("version_id") != self.version_id
            ):
                raise ValueError("readback evidence does not match the saved version")
            if self.physical_outcome != "written":
                raise ValueError("verified proof must record a written outcome")
        elif self.error_code is None:
            raise ValueError("terminal failure needs a safe error code")
        return self

    @classmethod
    def verified(
        cls,
        *,
        mode: ManagedOriginalMode,
        sha256: str,
        md5: str,
        size_bytes: int,
        etag: str,
        version_id: str | None,
        request_id: str | None,
        prepared: dict[str, Any],
        verification: dict[str, Any],
        observed: dict[str, Any] | None = None,
    ) -> ManagedOriginalProof:
        verification = {
            **verification,
            "sha256": sha256,
            "md5": md5,
            "size_bytes": size_bytes,
            "etag": etag,
            "version_id": version_id,
        }
        return cls(
            outcome="verified",
            mode=mode,
            physical_outcome="written",
            sha256=sha256,
            md5=md5,
            size_bytes=size_bytes,
            etag=etag,
            version_id=version_id,
            request_id=request_id,
            prepared=prepared,
            verification=verification,
            observed=observed,
        )


class ManagedOriginalOperation(_Value):
    """One independently durable physical permit bound to its creation."""

    operation_id: UUID
    mode: Literal["write", "read"]


ManagedOriginalStage = Literal[
    "allocate", "open", "prepare", "write", "verify", "close", "adopt"
]


class ManagedOriginalFailureEvidence(_Value):
    """Settlement evidence the owner needs to decide about its permit.

    ``physical_settled`` is only true when every handle that could still write
    has been drained and closed; an unknown close keeps the permit.
    """

    physical_settled: StrictBool
    physical_outcome: ManagedOriginalPhysicalOutcome
    stage: ManagedOriginalStage
    receipt: dict[str, Any] | None = None
    observation: dict[str, Any] | None = None
    sha256: Digest | None = None
    md5: Md5 | None = None
    size_bytes: ByteCount | None = None
    physical_size_bytes: ByteCount | None = None
    current_head_matches: StrictBool | None = None
    returned_version: StrictStr | None = None

    @model_validator(mode="after")
    def check_settlement(self):
        if self.physical_settled and self.physical_outcome == "unknown":
            raise ValueError("settled failures record their observed outcome")
        if not self.physical_settled and self.physical_outcome != "unknown":
            raise ValueError("unsettled failures have an unknown outcome")
        if self.physical_outcome == "written" and self.receipt is None:
            raise ValueError("written evidence needs the observed receipt")
        if self.current_head_matches is not None and self.receipt is None:
            raise ValueError("head evidence needs the observed receipt")
        return self

    @classmethod
    def build(
        cls,
        *,
        physical_settled: bool,
        physical_outcome: str,
        stage: str,
        receipt: dict[str, Any] | None = None,
        observation: dict[str, Any] | None = None,
        sha256: str | None = None,
        md5: str | None = None,
        size_bytes: int | None = None,
        physical_size_bytes: int | None = None,
        current_head_matches: bool | None = None,
        returned_version: str | None = None,
    ) -> dict[str, Any]:
        return cls(
            physical_settled=physical_settled,
            physical_outcome=physical_outcome,
            stage=stage,
            receipt=receipt,
            observation=observation,
            sha256=sha256,
            md5=md5,
            size_bytes=size_bytes,
            physical_size_bytes=physical_size_bytes,
            current_head_matches=current_head_matches,
            returned_version=returned_version,
        ).model_dump(mode="json", exclude_none=True)


class _Failure(_Value):
    code: StrictStr


class _Reply(_Value):
    schema_version: Literal["aap-managed-original/v1"] = Field(alias="schema")
    ok: StrictBool
    data: Any = None
    error: _Failure | None = None

    @model_validator(mode="after")
    def check_branch(self):
        if self.ok:
            if self.data is None or "error" in self.model_fields_set:
                raise ValueError("successful reply contains only data")
        elif self.error is None or "data" in self.model_fields_set:
            raise ValueError("failed reply contains only its stable error")
        return self


_Target = TypeVar("_Target", bound=BaseModel)


def decode_managed_original_reply(payload: object, model: type[_Target]) -> _Target:
    reply = _Reply.model_validate(payload)
    if reply.schema_version != MANAGED_ORIGINAL_SCHEMA:
        raise ValueError("Unexpected managed original contract")
    if not reply.ok:
        assert reply.error is not None
        raise ManagedOriginalFailure(reply.error.code)
    return model.model_validate(reply.data)


class ManagedOriginalDataPort(Protocol):
    async def allocate(self, request: dict[str, Any]) -> ManagedOriginalMaterial: ...

    async def open_operation(self, creation_id: UUID) -> ManagedOriginalOperation: ...

    async def prepared(self, creation_id: UUID, prepared: dict[str, Any]) -> None: ...

    async def writing(self, creation_id: UUID) -> None: ...

    async def written(self, creation_id: UUID, receipt: dict[str, Any]) -> None: ...

    async def verified(
        self, creation_id: UUID, verification: dict[str, Any]
    ) -> None: ...

    async def fail(
        self,
        creation_id: UUID,
        outcome: str,
        error_code: str,
        evidence: dict[str, Any],
    ) -> None: ...

    async def close_operation(
        self, creation_id: UUID, operation_id: UUID, physical_settled: bool
    ) -> None: ...


def observed_current_head_match(
    verification: dict[str, Any] | None, *, size_bytes: int
) -> bool | None:
    """Observed current-head equality frozen by the pinned readback.

    Only a version-pinned write can be attributed to today's HEAD. The readback
    observation is captured before any handle closes, so ``None`` (unversioned,
    missing observation, or unreadable) never claims a match.
    """
    if not isinstance(verification, dict):
        return None
    version_id = verification.get("version_id")
    observation = verification.get("observation")
    if version_id in (None, "", "null") or not isinstance(observation, dict):
        return None
    return (
        observation.get("version_id") == version_id
        and observation.get("size") == size_bytes
    )


def require_managed_original_ok(payload: object) -> None:
    """Validate a reply that has no caller-facing data (proof recording)."""
    reply = _Reply.model_validate(payload)
    if reply.schema_version != MANAGED_ORIGINAL_SCHEMA:
        raise ValueError("Unexpected managed original contract")
    if not reply.ok:
        assert reply.error is not None
        raise ManagedOriginalFailure(reply.error.code)
