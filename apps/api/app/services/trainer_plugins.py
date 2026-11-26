"""Validation and qualification binding for yoao-trainer/v1 plugin registration."""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

from training_contract.trainer_plugin import (
    TRAINER_PROTOCOL_VERSION,
    canonical_json_digest,
)

from app.schemas.trainer_plugin import (
    QualifyTrainerPluginRequest,
    RegisterTrainerPluginRequest,
    TrainerPluginRecord,
)

EXTENSION_MANIFEST_SCHEMA = "yoao-extension-manifest/v1"
PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9._-]{1,127}$")
FRAMEWORK_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
OCI_DIGEST_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
NUMERIC_HOST_RE = re.compile(
    r"^(?:[0-9]+|0[xX][0-9a-fA-F]+)(?:\.(?:[0-9]+|0[xX][0-9a-fA-F]+)){0,3}$"
)
TRAINER_CAPABILITIES = frozenset({"train", "evaluate", "preview", "export_weights"})
SHELL_EXECUTABLES = frozenset(
    {"sh", "bash", "dash", "zsh", "ash", "cmd", "powershell", "pwsh", "ksh", "fish"}
)
MIN_MEMORY_MB = 16
MAX_MEMORY_MB = 65536


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _validate_text(value: Any, *, field_name: str, max_length: int) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > max_length or _has_control_chars(value):
        raise ValueError(f"{field_name} must be 1..{max_length} printable characters")
    return cleaned


def _validate_optional_text(
    value: Any, *, field_name: str, max_length: int
) -> str | None:
    if value is None:
        return None
    return _validate_text(value, field_name=field_name, max_length=max_length)


def _is_forbidden_public_host(hostname: str) -> bool:
    lowered = hostname.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(lowered)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
    except ValueError:
        return bool(NUMERIC_HOST_RE.fullmatch(lowered))
    return (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_private
        or addr.is_multicast
        or addr.is_unspecified
        or addr.is_reserved
        or not addr.is_global
    )


def validate_homepage_url(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError("homepage must be a string")
    if (
        not value
        or value != value.strip()
        or len(value) > 255
        or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in value)
    ):
        raise ValueError("homepage must be a valid https:// URL up to 255 ASCII chars")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("homepage must be a valid https:// URL") from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc.endswith(":")
        or (port is not None and not 1 <= port <= 65535)
        or _is_forbidden_public_host(hostname)
    ):
        raise ValueError(
            "homepage must be a public https:// URL without credentials or private hosts"
        )
    return value


def validate_entrypoint(entrypoint: Any) -> list[str]:
    if not isinstance(entrypoint, list) or not entrypoint:
        raise ValueError("entrypoint must be a non-empty list of strings")
    if len(entrypoint) > 64:
        raise ValueError("entrypoint has too many arguments")
    if any(
        type(arg) is not str or not arg or any(ch in arg for ch in "\x00\r\n")
        for arg in entrypoint
    ):
        raise ValueError("entrypoint arguments must be non-empty strings")
    head = entrypoint[0]
    if any(ch.isspace() for ch in head) or ".." in head.split("/"):
        raise ValueError("entrypoint executable must be a safe command path or name")
    basename = head.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if basename.endswith(suffix):
            basename = basename[: -len(suffix)]
            break
    if not basename or basename in SHELL_EXECUTABLES:
        raise ValueError("entrypoint must not invoke a shell interpreter")
    if basename.startswith("python"):
        skip_next = False
        for arg in entrypoint[1:]:
            if skip_next:
                skip_next = False
                continue
            if arg == "--" or not arg.startswith("-"):
                break
            if arg == "-m" or (not arg.startswith("--") and arg.startswith("-m")):
                break
            if arg in {"-W", "-X"}:
                skip_next = True
                continue
            if arg == "-c" or (not arg.startswith("--") and "c" in arg[1:]):
                raise ValueError("entrypoint must not invoke python -c")
    return list(entrypoint)


def validate_capabilities(capabilities: Any) -> list[str]:
    if not isinstance(capabilities, list) or not capabilities:
        raise ValueError("capabilities must be a non-empty list")
    seen: set[str] = set()
    validated: list[str] = []
    for item in capabilities:
        if type(item) is not str or item not in TRAINER_CAPABILITIES:
            raise ValueError(f"unsupported trainer capability: {item!r}")
        if item in seen:
            raise ValueError(f"duplicate trainer capability: {item!r}")
        seen.add(item)
        validated.append(item)
    return validated


def validate_resource(resource: Any, *, execution_mode: str) -> dict[str, Any]:
    if resource is None:
        if execution_mode == "container":
            raise ValueError("container execution_mode requires resource.memory_mb")
        return {}
    if not isinstance(resource, dict):
        raise ValueError("resource must be an object")
    memory_mb = resource.get("memory_mb")
    if execution_mode == "container":
        if type(memory_mb) is not int or not (
            MIN_MEMORY_MB <= memory_mb <= MAX_MEMORY_MB
        ):
            raise ValueError(
                "container resource.memory_mb must be an integer in 16..65536"
            )
    elif memory_mb is not None:
        if type(memory_mb) is not int or not (
            MIN_MEMORY_MB <= memory_mb <= MAX_MEMORY_MB
        ):
            raise ValueError("resource.memory_mb must be an integer in 16..65536")
    return dict(resource)


def entrypoint_digest(entrypoint: list[str]) -> str:
    return canonical_json_digest({"entrypoint": list(entrypoint)})


def validate_registration_payload(
    body: RegisterTrainerPluginRequest,
) -> dict[str, Any]:
    manifest = body.manifest
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")
    if manifest.get("manifest_schema") != EXTENSION_MANIFEST_SCHEMA:
        raise ValueError("manifest_schema must be 'yoao-extension-manifest/v1'")
    if manifest.get("extension_point") != "trainer":
        raise ValueError("extension_point must be 'trainer'")
    if manifest.get("protocol_version") != TRAINER_PROTOCOL_VERSION:
        raise ValueError("protocol_version must be 'yoao-trainer/v1'")
    if "source_tier" in manifest:
        raise ValueError("manifest must not declare source_tier")

    plugin_id = manifest.get("plugin_id")
    if type(plugin_id) is not str or not PLUGIN_ID_RE.fullmatch(plugin_id):
        raise ValueError("plugin_id must match ^[a-z][a-z0-9._-]{1,127}$")

    version = _validate_text(
        manifest.get("version"), field_name="version", max_length=64
    )
    display_name = _validate_text(
        manifest.get("display_name"), field_name="display_name", max_length=64
    )
    description = _validate_optional_text(
        manifest.get("description"), field_name="description", max_length=280
    )
    homepage = validate_homepage_url(manifest.get("homepage"))
    publisher = _validate_optional_text(
        manifest.get("publisher"), field_name="publisher", max_length=128
    )
    license_name = _validate_optional_text(
        manifest.get("license"), field_name="license", max_length=64
    )
    entrypoint = validate_entrypoint(manifest.get("entrypoint"))
    capabilities = validate_capabilities(manifest.get("capabilities"))
    resource = validate_resource(
        manifest.get("resource"), execution_mode=body.execution_mode
    )

    extension = manifest.get("extension")
    if not isinstance(extension, dict):
        raise ValueError("manifest.extension must be an object")
    if "source_tier" in extension:
        raise ValueError("manifest.extension must not declare source_tier")

    snapshot_framework_id = body.capabilities_snapshot.framework_id
    if not FRAMEWORK_ID_RE.fullmatch(snapshot_framework_id):
        raise ValueError(
            "capabilities_snapshot.framework_id must match ^[a-z][a-z0-9_-]{0,63}$"
        )

    ext_framework_id = extension.get("framework_id")
    if ext_framework_id is not None:
        if type(ext_framework_id) is not str or not FRAMEWORK_ID_RE.fullmatch(
            ext_framework_id
        ):
            raise ValueError("manifest.extension.framework_id is invalid")
        if ext_framework_id != snapshot_framework_id:
            raise ValueError(
                "capabilities_snapshot.framework_id does not match manifest.extension.framework_id"
            )
    if (
        "framework_version" in extension
        and extension["framework_version"] is not None
        and extension["framework_version"]
        != body.capabilities_snapshot.framework_version
    ):
        raise ValueError(
            "capabilities_snapshot.framework_version does not match manifest.extension.framework_version"
        )
    if "requires_gpu" in extension and type(extension["requires_gpu"]) is not bool:
        raise ValueError("manifest.extension.requires_gpu must be a boolean")

    raw_artifact = manifest.get("artifact")
    if body.execution_mode == "container":
        if body.artifact is None or not OCI_DIGEST_RE.fullmatch(body.artifact):
            raise ValueError(
                "container execution_mode requires a digest-pinned artifact (@sha256:...)"
            )
        if raw_artifact is not None and raw_artifact != body.artifact:
            raise ValueError("manifest.artifact conflicts with request artifact")
        artifact: str | None = body.artifact
    else:
        if body.artifact is not None or raw_artifact is not None:
            raise ValueError("local_process execution_mode forbids artifact")
        artifact = None

    capabilities_digest = canonical_json_digest(body.capabilities_snapshot)
    stored_manifest = dict(manifest)
    if artifact is not None:
        stored_manifest["artifact"] = artifact
    else:
        stored_manifest.pop("artifact", None)

    return {
        "plugin_id": plugin_id,
        "version": version,
        "framework_id": snapshot_framework_id,
        "display_name": display_name,
        "description": description,
        "homepage": homepage,
        "publisher": publisher,
        "license": license_name,
        "source_tier": body.source_tier,
        "execution_mode": body.execution_mode,
        "protocol_version": TRAINER_PROTOCOL_VERSION,
        "artifact": artifact,
        "entrypoint": entrypoint,
        "resource": resource,
        "capabilities": capabilities,
        "manifest": stored_manifest,
        "capabilities_snapshot": body.capabilities_snapshot.model_dump(mode="json"),
        "capabilities_digest": capabilities_digest,
    }


def validate_qualification_report(
    record: TrainerPluginRecord, body: QualifyTrainerPluginRequest
) -> None:
    report = body.qualification_report
    if not isinstance(report, dict):
        raise ValueError("qualification_report must be a JSON object")
    actual_sha256 = canonical_json_digest(report)
    if actual_sha256 != body.qualification_report_sha256:
        raise ValueError(
            "qualification_report_sha256 does not match canonical digest of qualification_report"
        )
    if report.get("passed") is not True:
        raise ValueError("qualification_report.passed must be true")
    if report.get("plugin_id") != record.plugin_id:
        raise ValueError(
            "qualification_report.plugin_id does not match registered plugin"
        )
    if report.get("version") != record.version:
        raise ValueError(
            "qualification_report.version does not match registered plugin"
        )
    if report.get("execution_mode") != record.execution_mode:
        raise ValueError(
            "qualification_report.execution_mode does not match registered plugin"
        )
    if report.get("capabilities_digest") != record.capabilities_digest:
        raise ValueError(
            "qualification_report.capabilities_digest does not match registered plugin"
        )
    if record.execution_mode == "container":
        if report.get("artifact") != record.artifact:
            raise ValueError(
                "qualification_report.artifact does not match registered plugin artifact"
            )
    else:
        expected_ep_digest = entrypoint_digest(record.entrypoint)
        if report.get("entrypoint_digest") != expected_ep_digest:
            raise ValueError(
                "qualification_report.entrypoint_digest does not match registered plugin entrypoint"
            )
