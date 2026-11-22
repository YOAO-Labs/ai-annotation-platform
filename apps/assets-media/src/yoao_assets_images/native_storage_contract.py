"""Portable private context and finite signed-request values for Native pyramids.

The Assets Source process is the only producer of these values. This module
imports portable image values and the standard library; it never imports the
Source runtime, the API, an ORM or a storage SDK.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, quote, urlsplit
from uuid import UUID

from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    StrictStr,
    field_validator,
    model_validator,
)

from .contract import (
    Count,
    Digest,
    NativePyramidSavedTarget,
    Positive,
    Revision,
    Value,
)

NATIVE_WRITE_REFERENCE_PATH = "/v1/native-pyramids/write-reference"
NATIVE_VERIFY_REFERENCE_PATH = "/v1/native-pyramids/verify-reference"
NATIVE_READ_REFERENCES_PATH = "/v1/native-pyramids/read-references"
NATIVE_RETIREMENT_REFERENCES_PATH = "/v1/native-pyramids/retirement-references"
NATIVE_RETIREMENT_PREFIX_REFERENCE_PATH = (
    "/v1/native-pyramids/retirement-prefix-reference"
)

NATIVE_MEMBER_MAX = 100
NATIVE_URL_EXPIRY_MAX = 604_800
NATIVE_URL_MAX_LENGTH = 8192
NATIVE_HEADER_MAX = 8

_RELATIVE_KEY_PATTERN = (
    r"^(manifest[.]json|overview[.]webp|"
    r"tiles/(0|[1-9][0-9]*)/(0|[1-9][0-9]*)/(0|[1-9][0-9]*)[.]webp)$"
)
_QUOTED_ETAG_PATTERN = r'^"[^"]+"$'
_HEADER_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
_AMZ_DATE_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
_SIGNATURE_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")

_NATIVE_AUTH_QUERY = frozenset(
    {
        "X-Amz-Algorithm",
        "X-Amz-Credential",
        "X-Amz-Date",
        "X-Amz-Expires",
        "X-Amz-SignedHeaders",
        "X-Amz-Signature",
    }
)
_NATIVE_ALLOWED_HEADERS = frozenset(
    {
        "content-length",
        "content-type",
        "if-match",
        "if-none-match",
        "x-amz-meta-sha256",
    }
)

RelativeKey = Annotated[
    StrictStr,
    Field(min_length=1, max_length=2048, pattern=_RELATIVE_KEY_PATTERN),
]
QuotedEtag = Annotated[
    StrictStr,
    Field(min_length=3, max_length=1024, pattern=_QUOTED_ETAG_PATTERN),
]
VersionId = Annotated[StrictStr, Field(min_length=1, max_length=1024)]


def _quote(value: str, *, safe: str = "-_.~") -> str:
    return quote(value, safe=safe, encoding="utf-8", errors="strict")


def _bounded_http_url(value: str) -> str:
    if len(value) > NATIVE_URL_MAX_LENGTH or _CONTROL_CHARACTERS.search(value):
        raise ValueError("signed reference URL is not bounded")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("signed reference URL is not HTTP(S)")
    try:
        parsed.port
    except ValueError:
        raise ValueError("signed reference URL has an invalid port") from None
    return value


def _origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme, (parsed.hostname or "").casefold(), port


def _parse_query(value: str) -> dict[str, str]:
    pairs = parse_qsl(value, keep_blank_values=True)
    names = [name for name, _ in pairs]
    if len(names) != len(set(names)):
        raise ValueError("signed reference repeats a query parameter")
    return dict(pairs)


def _require_saved_target(output_id, storage_ref: NativePyramidSavedTarget) -> str:
    expected = storage_ref.key_prefix + "image-pyramids/objects/" + str(output_id) + "/"
    if storage_ref.prefix != expected:
        raise ValueError("saved target escaped its output prefix")
    return expected


def member_content_type(relative_key: str) -> str:
    """Return the only admitted content type for one reserved pyramid member."""
    if relative_key == "manifest.json":
        return "application/json"
    if relative_key.endswith(".webp"):
        return "image/webp"
    raise ValueError("unreserved native pyramid member")


def _require_member_scope(
    output_id, storage_ref: NativePyramidSavedTarget, member: "NativePyramidIoMember"
) -> str:
    prefix = _require_saved_target(output_id, storage_ref)
    if not member.relative_key:
        raise ValueError("native member has no relative key")
    key = prefix + member.relative_key
    if len(key.encode("utf-8")) > 1024:
        raise ValueError("native member key is too long")
    return key


class NativePyramidIoMember(Value):
    """One exact reserved member receipt, with a nullable recorded version."""

    operation_id: UUID
    relative_key: RelativeKey
    sha256: Digest
    size_bytes: Count
    etag: QuotedEtag | None = None
    version_id: VersionId | None = None

    @model_validator(mode="after")
    def recorded_version_is_explicit(self):
        if self.version_id is not None and not self.version_id:
            raise ValueError("recorded native member version is empty")
        return self


class NativePyramidIoDestination(Value):
    """Registered connection snapshot captured privately for Source only."""

    model_config = ConfigDict(extra="ignore")

    config: dict[str, Any]
    secret_enc: StrictStr = Field(repr=False)


class NativePyramidIoContext(Value):
    """Private capture returned only by the three Source-only SQL capabilities."""

    mode: Literal[
        "put",
        "verify",
        "read",
        "retirement_members",
        "retirement_prefix",
    ]
    scope_id: UUID
    scope_token: UUID
    output_id: UUID
    storage_ref: NativePyramidSavedTarget
    valid_until: AwareDatetime | None = None
    url_expiry_seconds: Positive
    members: list[NativePyramidIoMember] = Field(max_length=NATIVE_MEMBER_MAX)
    destination: NativePyramidIoDestination | None = Field(default=None, repr=False)
    host_allowlist: list[StrictStr] | None = None

    @model_validator(mode="after")
    def exact_private_scope(self):
        _require_saved_target(self.output_id, self.storage_ref)
        if self.host_allowlist is not None and len(self.host_allowlist) > 256:
            raise ValueError("native host allowlist is too large")
        operations = [member.operation_id for member in self.members]
        keys = [member.relative_key for member in self.members]
        if len(operations) != len(set(operations)):
            raise ValueError("native context repeats a member operation")
        if len(keys) != len(set(keys)):
            raise ValueError("native context repeats a member key")
        for member in self.members:
            if self.mode == "put":
                if member.etag is not None or member.version_id is not None:
                    raise ValueError("an unwritten member has no recorded receipt")
            elif member.etag is None:
                raise ValueError("a recorded native member has no quoted ETag")
        if self.mode in {"put", "verify"}:
            if len(self.members) != 1:
                raise ValueError("a write context binds exactly one member")
            if self.scope_id != self.members[0].operation_id:
                raise ValueError("write scope is not its member operation")
            if self.valid_until is None:
                raise ValueError("a write context requires its live lease")
        elif self.mode == "read":
            if not 1 <= len(self.members) <= NATIVE_MEMBER_MAX:
                raise ValueError("a read context requires members")
        elif self.mode == "retirement_members":
            if not 1 <= len(self.members) <= NATIVE_MEMBER_MAX:
                raise ValueError("a member retirement page requires members")
            if self.valid_until is None:
                raise ValueError("a retirement page requires its live lease")
        elif self.mode == "retirement_prefix":
            if self.members:
                raise ValueError("a terminal retirement page has no members")
            if self.valid_until is None:
                raise ValueError("a retirement page requires its live lease")
        return self


class NativePyramidWriteIoRequest(Value):
    operation_id: UUID
    job_id: UUID
    job_generation: Revision
    lease_token: UUID


class NativePyramidReadIoRequest(Value):
    operation_id: UUID
    token: UUID
    relative_keys: list[RelativeKey] = Field(min_length=1, max_length=NATIVE_MEMBER_MAX)
    purpose: Literal["inspect", "publish"]
    expires_in: Positive | None = None

    @model_validator(mode="after")
    def exact_purpose_expiry(self):
        if len(self.relative_keys) != len(set(self.relative_keys)):
            raise ValueError("duplicate native relative keys")
        if self.purpose == "inspect" and self.expires_in is not None:
            raise ValueError("an inspection request cannot request public expiry")
        return self


class NativePyramidRetirementIoRequest(Value):
    output_id: UUID
    lease_token: UUID
    page_id: UUID


class NativePyramidRetirementPrefixIoRequest(NativePyramidRetirementIoRequest):
    kind: Literal["objects", "versions", "uploads"]
    key_marker: Annotated[StrictStr, Field(min_length=1, max_length=1024)] | None = None
    upload_id_marker: (
        Annotated[StrictStr, Field(min_length=1, max_length=1024)] | None
    ) = None

    @model_validator(mode="after")
    def multipart_cursor_only(self):
        markers = (self.key_marker, self.upload_id_marker)
        if (self.key_marker is None) != (self.upload_id_marker is None):
            raise ValueError("multipart cursor requires both markers")
        if self.kind != "uploads" and any(value is not None for value in markers):
            raise ValueError("only multipart probes accept a cursor")
        if any(
            ord(character) < 32 or ord(character) == 127
            for value in markers
            if value is not None
            for character in value
        ):
            raise ValueError("multipart cursor contains a control character")
        return self


class NativePyramidSignedRequest(Value):
    """One finite signed S3 request plus its exact admitted headers."""

    method: Literal["GET", "HEAD", "PUT", "DELETE"]
    url: StrictStr
    headers: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    expires_at: AwareDatetime

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        return _bounded_http_url(value)

    @field_validator("headers")
    @classmethod
    def admitted_headers(cls, value):
        if len(value) > NATIVE_HEADER_MAX:
            raise ValueError("signed request carries too many headers")
        for name, item in value.items():
            if not re.fullmatch(_HEADER_NAME_PATTERN, name):
                raise ValueError("signed request header is not admitted")
            if name not in _NATIVE_ALLOWED_HEADERS:
                raise ValueError("signed request header is not admitted")
            if len(item) > 2048 or _CONTROL_CHARACTERS.search(item):
                raise ValueError("signed request header value is not bounded")
        return value


class NativePyramidSignedMember(Value):
    member: NativePyramidIoMember
    put: NativePyramidSignedRequest | None = None
    head: NativePyramidSignedRequest | None = None
    get: NativePyramidSignedRequest | None = None
    delete: NativePyramidSignedRequest | None = None
    public_url: StrictStr | None = None
    public_expires_at: AwareDatetime | None = None

    @field_validator("public_url")
    @classmethod
    def valid_public_url(cls, value):
        return value if value is None else _bounded_http_url(value)

    @model_validator(mode="after")
    def paired_public_lifetime(self):
        if (self.public_url is None) != (self.public_expires_at is None):
            raise ValueError("a public reference needs its exact lifetime")
        if all(
            value is None
            for value in (self.put, self.head, self.get, self.delete, self.public_url)
        ):
            raise ValueError("a signed member carries no request")
        return self


class NativePyramidIoReferences(Value):
    mode: Literal["put", "verify", "read", "retirement_members"]
    scope_id: UUID
    scope_token: UUID
    output_id: UUID
    storage_ref: NativePyramidSavedTarget
    members: list[NativePyramidSignedMember] = Field(max_length=NATIVE_MEMBER_MAX)
    expires_in: Positive | None = None

    @model_validator(mode="after")
    def exact_signed_scope(self):
        _require_saved_target(self.output_id, self.storage_ref)
        operations = [row.member.operation_id for row in self.members]
        keys = [row.member.relative_key for row in self.members]
        if len(operations) != len(set(operations)):
            raise ValueError("signed references repeat a member operation")
        if len(keys) != len(set(keys)):
            raise ValueError("signed references repeat a member key")
        if self.mode in {"put", "verify"}:
            if len(self.members) != 1:
                raise ValueError("a write reference binds exactly one member")
            member = self.members[0].member
            if self.scope_id != member.operation_id:
                raise ValueError("write reference is not its member operation")
            if self.mode == "put":
                if member.etag is not None or member.version_id is not None:
                    raise ValueError("a put reference has a recorded receipt")
            elif member.etag is None:
                raise ValueError("a verify reference has no recorded receipt")
            if self.expires_in is not None:
                raise ValueError("a write reference advertises no public lifetime")
        elif self.mode == "read":
            if not 1 <= len(self.members) <= NATIVE_MEMBER_MAX:
                raise ValueError("a read reference requires members")
        else:
            if not 1 <= len(self.members) <= NATIVE_MEMBER_MAX:
                raise ValueError("a member retirement page requires members")
            if self.expires_in is not None:
                raise ValueError("a retirement reference has no public lifetime")
        return self


class NativePyramidPrefixIoReference(Value):
    scope_id: UUID
    scope_token: UUID
    output_id: UUID
    storage_ref: NativePyramidSavedTarget
    kind: Literal["objects", "versions", "uploads"]
    request: NativePyramidSignedRequest

    @model_validator(mode="after")
    def exact_terminal_scope(self):
        _require_saved_target(self.output_id, self.storage_ref)
        if self.request.method != "GET":
            raise ValueError("a prefix listing is a GET")
        return self


def _require_signed_headers(
    signed: NativePyramidSignedRequest, expected: dict[str, str]
) -> set[str]:
    if dict(signed.headers) != expected:
        raise ValueError("signed request headers escaped their admission")
    names = {"host", *expected}
    return names


def _require_amz_query(
    query: dict[str, str],
    *,
    expected_signed_headers: set[str],
    expires_at: datetime,
    now: datetime,
) -> int:
    algorithm = query.get("X-Amz-Algorithm")
    credential = query.get("X-Amz-Credential", "")
    credential_parts = credential.split("/")
    amz_date = query.get("X-Amz-Date", "")
    expires_raw = query.get("X-Amz-Expires", "")
    signed_headers_raw = query.get("X-Amz-SignedHeaders", "")
    signature = query.get("X-Amz-Signature", "")
    if algorithm != "AWS4-HMAC-SHA256" or not _AMZ_DATE_PATTERN.fullmatch(amz_date):
        raise ValueError("signed request is not AWS SigV4")
    if (
        len(credential_parts) != 5
        or not credential_parts[0]
        or not credential_parts[1].isdigit()
        or credential_parts[1] != amz_date[:8]
        or not credential_parts[2]
        or credential_parts[3] != "s3"
        or credential_parts[4] != "aws4_request"
    ):
        raise ValueError("signed request credential is not an S3 scope")
    if not expires_raw.isdigit():
        raise ValueError("signed request has no bounded expiry")
    expires_in = int(expires_raw)
    if not 1 <= expires_in <= NATIVE_URL_EXPIRY_MAX:
        raise ValueError("signed request expiry is not bounded")
    signed_names = signed_headers_raw.split(";")
    if (
        not signed_headers_raw
        or len(signed_names) != len(set(signed_names))
        or signed_names != sorted(signed_names)
        or set(signed_names) != expected_signed_headers
    ):
        raise ValueError("signed request headers are not the admitted headers")
    if not _SIGNATURE_PATTERN.fullmatch(signature):
        raise ValueError("signed request has no signature")
    try:
        issued_at = datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        raise ValueError("signed request date is invalid") from None
    expected_expiry = issued_at + timedelta(seconds=expires_in)
    if expires_at != expected_expiry:
        raise ValueError("signed request expiry disagrees with its signature")
    if expires_at <= now:
        raise ValueError("signed request has expired")
    return expires_in


def _require_member_request(
    signed: NativePyramidSignedRequest | None,
    *,
    method: Literal["GET", "HEAD", "PUT", "DELETE"],
    storage_ref: NativePyramidSavedTarget,
    key: str,
    version_id: str | None,
    expected_headers: dict[str, str],
    public: bool,
    now: datetime,
) -> int:
    if not isinstance(signed, NativePyramidSignedRequest):
        raise ValueError("signed member request is missing")
    if signed.method != method:
        raise ValueError("signed member request changed its method")
    parsed = urlsplit(signed.url)
    _bounded_http_url(signed.url)
    if not public and _origin(signed.url) != _origin(storage_ref.endpoint_url):
        raise ValueError("signed member request escaped the saved endpoint")
    expected_path = "/" + _quote(storage_ref.bucket) + "/" + _quote(key, safe="/-_.~")
    if parsed.path != expected_path:
        raise ValueError("signed member request escaped its exact key")
    query = _parse_query(parsed.query)
    if not _NATIVE_AUTH_QUERY <= set(query):
        raise ValueError("signed member request has no complete credential")
    resources = set(query) - _NATIVE_AUTH_QUERY
    if version_id is None:
        if resources:
            raise ValueError("signed member request invented a version")
    elif resources != {"versionId"} or query.get("versionId") != version_id:
        raise ValueError("signed member request changed its recorded version")
    if not public:
        names = _require_signed_headers(signed, expected_headers)
        return _require_amz_query(
            query,
            expected_signed_headers=names,
            expires_at=signed.expires_at,
            now=now,
        )
    if signed.headers:
        raise ValueError("a public reference carries no request headers")
    _require_amz_query(
        query,
        expected_signed_headers={"host"},
        expires_at=signed.expires_at,
        now=now,
    )
    return 0


def _require_signed_member(
    row: NativePyramidSignedMember,
    *,
    mode: str,
    storage_ref: NativePyramidSavedTarget,
    output_id,
    now: datetime,
    expires_in: int | None = None,
) -> None:
    key = _require_member_scope(output_id, storage_ref, row.member)
    version = row.member.version_id
    if mode == "put":
        if row.member.etag is not None or row.member.version_id is not None:
            raise ValueError("a put reference has a recorded receipt")
        conditional: dict[str, str] = {}
    else:
        if row.member.etag is None:
            raise ValueError("a conditional reference has no recorded ETag")
        conditional = {"if-match": row.member.etag}
    if mode == "put":
        if any(
            value is not None
            for value in (row.head, row.get, row.delete, row.public_url)
        ):
            raise ValueError("a put reference carries another operation")
        expected = {
            "content-length": str(row.member.size_bytes),
            "content-type": member_content_type(row.member.relative_key),
            "if-none-match": "*",
            "x-amz-meta-sha256": row.member.sha256,
        }
        _require_member_request(
            row.put,
            method="PUT",
            storage_ref=storage_ref,
            key=key,
            version_id=None,
            expected_headers=expected,
            public=False,
            now=now,
        )
        return
    if mode == "verify":
        if any(
            value is not None
            for value in (row.put, row.head, row.delete, row.public_url)
        ):
            raise ValueError("a verify reference carries another operation")
        if row.get is None:
            raise ValueError("a verify reference has no conditional read")
        _require_member_request(
            row.get,
            method="GET",
            storage_ref=storage_ref,
            key=key,
            version_id=version,
            expected_headers=conditional,
            public=False,
            now=now,
        )
        return
    if mode == "retirement_members":
        if row.public_url is not None or row.put is not None:
            raise ValueError("a retirement reference is not a publication")
        if row.head is None or row.get is None or row.delete is None:
            raise ValueError("a retirement reference is incomplete")
        for method, signed in (
            ("HEAD", row.head),
            ("GET", row.get),
            ("DELETE", row.delete),
        ):
            _require_member_request(
                signed,
                method=method,
                storage_ref=storage_ref,
                key=key,
                version_id=version,
                expected_headers=conditional,
                public=False,
                now=now,
            )
        return
    if row.public_url is None:
        if any(value is not None for value in (row.put, row.delete, row.public_url)):
            raise ValueError("an inspection reference carries another operation")
        if row.head is None or row.get is None:
            raise ValueError("an inspection reference is incomplete")
        for method, signed in (("HEAD", row.head), ("GET", row.get)):
            _require_member_request(
                signed,
                method=method,
                storage_ref=storage_ref,
                key=key,
                version_id=version,
                expected_headers=conditional,
                public=False,
                now=now,
            )
        return
    if any(value is not None for value in (row.put, row.head, row.get, row.delete)):
        raise ValueError("a public reference carries a private operation")
    if row.public_expires_at is None:
        raise ValueError("a public reference has no exact lifetime")
    parsed = urlsplit(row.public_url)
    _bounded_http_url(row.public_url)
    expected_path = "/" + _quote(storage_ref.bucket) + "/" + _quote(key, safe="/-_.~")
    if parsed.path != expected_path:
        raise ValueError("a public reference escaped its exact key")
    query = _parse_query(parsed.query)
    if not _NATIVE_AUTH_QUERY <= set(query):
        raise ValueError("a public reference has no complete credential")
    resources = set(query) - _NATIVE_AUTH_QUERY
    if version is None:
        if resources:
            raise ValueError("a public reference invented a version")
    elif resources != {"versionId"} or query.get("versionId") != version:
        raise ValueError("a public reference changed its recorded version")
    seconds = _require_amz_query(
        query,
        expected_signed_headers={"host"},
        expires_at=row.public_expires_at,
        now=now,
    )
    if expires_in is not None and seconds != expires_in:
        raise ValueError("a public reference changed its requested lifetime")


def require_write_reference_scope(
    request: NativePyramidWriteIoRequest,
    result: NativePyramidIoReferences,
    mode: Literal["put", "verify"],
) -> None:
    if not isinstance(result, NativePyramidIoReferences):
        raise ValueError("write reference is not an io references value")
    if result.mode != mode:
        raise ValueError("write reference changed its mode")
    if (
        result.scope_id != request.operation_id
        or result.scope_token != request.lease_token
    ):
        raise ValueError("write reference escaped its operation")
    if len(result.members) != 1:
        raise ValueError("write reference did not bind exactly one member")
    if result.members[0].member.operation_id != request.operation_id:
        raise ValueError("write reference changed its member operation")
    if result.expires_in is not None:
        raise ValueError("write reference advertised a public lifetime")
    now = datetime.now(timezone.utc)
    for row in result.members:
        _require_signed_member(
            row,
            mode=mode,
            storage_ref=result.storage_ref,
            output_id=result.output_id,
            now=now,
        )


def require_read_reference_scope(
    request: NativePyramidReadIoRequest,
    result: NativePyramidIoReferences,
) -> None:
    if not isinstance(result, NativePyramidIoReferences):
        raise ValueError("read reference is not an io references value")
    if result.mode != "read":
        raise ValueError("read reference changed its mode")
    if result.scope_id != request.operation_id or result.scope_token != request.token:
        raise ValueError("read reference escaped its operation")
    actual = [row.member.relative_key for row in result.members]
    if len(actual) != len(request.relative_keys) or set(actual) != set(
        request.relative_keys
    ):
        raise ValueError("read reference changed its requested members")
    if request.purpose == "inspect":
        if result.expires_in is not None:
            raise ValueError("inspection reference advertised a public lifetime")
        for row in result.members:
            if row.public_url is not None:
                raise ValueError("inspection reference carried a public URL")
    else:
        if (
            result.expires_in is None
            or result.expires_in < 1
            or result.expires_in > NATIVE_URL_EXPIRY_MAX
        ):
            raise ValueError("public reference has no bounded lifetime")
        if request.expires_in is not None and result.expires_in != request.expires_in:
            raise ValueError("public reference changed its requested lifetime")
        for row in result.members:
            if row.public_url is None:
                raise ValueError("public reference has no URL")
    now = datetime.now(timezone.utc)
    for row in result.members:
        _require_signed_member(
            row,
            mode="read",
            storage_ref=result.storage_ref,
            output_id=result.output_id,
            now=now,
            expires_in=result.expires_in,
        )


def require_retirement_reference_scope(
    request: NativePyramidRetirementIoRequest,
    result: NativePyramidIoReferences,
) -> None:
    if not isinstance(result, NativePyramidIoReferences):
        raise ValueError("retirement reference is not an io references value")
    if result.mode != "retirement_members":
        raise ValueError("retirement reference changed its mode")
    if (
        result.scope_id != request.page_id
        or result.scope_token != request.lease_token
        or result.output_id != request.output_id
    ):
        raise ValueError("retirement reference escaped its prepared page")
    if not result.members:
        raise ValueError("retirement reference has no members")
    if result.expires_in is not None:
        raise ValueError("retirement reference advertised a public lifetime")
    now = datetime.now(timezone.utc)
    for row in result.members:
        _require_signed_member(
            row,
            mode="retirement_members",
            storage_ref=result.storage_ref,
            output_id=result.output_id,
            now=now,
        )


def prefix_listing_query(
    prefix: str,
    kind: str,
    *,
    key_marker: str | None = None,
    upload_id_marker: str | None = None,
) -> list[tuple[str, str]]:
    """Bound exact-prefix probes and the provider-compatible multipart scan."""
    if kind == "objects":
        return [("list-type", "2"), ("max-keys", "1"), ("prefix", prefix)]
    if kind == "versions":
        return [("versions", ""), ("max-keys", "1"), ("prefix", prefix)]
    if kind == "uploads":
        query = [("uploads", ""), ("max-uploads", "100")]
        if key_marker is not None:
            query.extend(
                (
                    ("key-marker", key_marker),
                    ("upload-id-marker", upload_id_marker),
                )
            )
        return query
    raise ValueError("unsupported native listing kind")


def require_retirement_prefix_reference_scope(
    request: NativePyramidRetirementPrefixIoRequest,
    result: NativePyramidPrefixIoReference,
) -> None:
    if not isinstance(result, NativePyramidPrefixIoReference):
        raise ValueError("prefix reference is not a prefix value")
    if (
        result.scope_id != request.page_id
        or result.scope_token != request.lease_token
        or result.output_id != request.output_id
    ):
        raise ValueError("prefix reference escaped its prepared page")
    if result.kind != request.kind:
        raise ValueError("prefix reference changed its listing")
    expected = prefix_listing_query(
        result.storage_ref.prefix,
        result.kind,
        key_marker=request.key_marker,
        upload_id_marker=request.upload_id_marker,
    )
    signed = result.request
    parsed = urlsplit(signed.url)
    _bounded_http_url(signed.url)
    if _origin(signed.url) != _origin(result.storage_ref.endpoint_url):
        raise ValueError("prefix reference escaped the saved endpoint")
    expected_path = "/" + _quote(result.storage_ref.bucket)
    if parsed.path != expected_path:
        raise ValueError("prefix reference escaped its exact bucket")
    query = _parse_query(parsed.query)
    if not _NATIVE_AUTH_QUERY <= set(query):
        raise ValueError("prefix reference has no complete credential")
    resources = set(query) - _NATIVE_AUTH_QUERY
    if resources != {name for name, _ in expected}:
        raise ValueError("prefix reference changed its listing query")
    if any(query.get(name) != value for name, value in expected):
        raise ValueError("prefix reference changed its listing values")
    if signed.headers:
        raise ValueError("a prefix reference carries no request headers")
    _require_amz_query(
        query,
        expected_signed_headers={"host"},
        expires_at=signed.expires_at,
        now=datetime.now(timezone.utc),
    )
