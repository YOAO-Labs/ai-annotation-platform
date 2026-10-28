"""Administrator-side local plugin installation and crash recovery.

The Registry is authoritative; the private state directory is required to prove
local ownership and to replay the same reservation after a lost response.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_annotation._plugin_docker import ContainerConfig, Docker
from ai_annotation._plugin_receipt import ReceiptStore, _check_private
from ai_annotation.errors import AAPError
from ai_annotation.plugin_models import (
    PluginActivateRequest, PluginInstallationResult, PluginLaunchReceipt,
    PluginManifest, PluginPrepareRequest, PluginResourceBinding,
)
from ai_annotation.plugins import PluginInstallations

_DATA_TARGET = "/var/lib/aap-libreyolo"


class LocalPluginConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    url: str
    manifest: PluginManifest
    gpu_resource_id: str = Field(min_length=1)
    physical_gpu_uuid: str = Field(min_length=1)
    network: str = Field(min_length=1)
    host_port: int = Field(ge=1, le=65535)
    cpus: float = Field(gt=0, le=256, allow_inf_nan=False)
    gpu_verify_keyring: dict[str, str] = Field(min_length=1)
    image_hosts: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_config(self):
        url = urlsplit(self.url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("url must be an HTTP(S) endpoint without credentials, query or fragment")
        if self.manifest.resource.device != "cuda":
            raise ValueError("local managed installation requires CUDA")
        if self.manifest.plugin_id != "libreyolo" or self.manifest.entrypoint != ["aap-libreyolo", "serve", "--host", "0.0.0.0", "--port", "8000"]:
            raise ValueError("local installer requires the approved LibreYOLO entrypoint")
        if not re.fullmatch(r"GPU-[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}", self.physical_gpu_uuid):
            raise ValueError("one physical NVIDIA GPU UUID is required")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", self.network) or self.network == "host":
            raise ValueError("an explicit non-host Docker network is required")
        if any(c in self.gpu_resource_id for c in "\n\r"):
            raise ValueError("invalid GPU resource identity")
        if any(not key or not value or "\n" in key or "\n" in value for key, value in self.gpu_verify_keyring.items()):
            raise ValueError("verification keyring must contain nonempty public keys")
        for key in self.gpu_verify_keyring.values():
            try:
                decoded = base64.b64decode(key + "=" * (-len(key) % 4), altchars=b"-_", validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("invalid GPU verification public key") from exc
            if len(decoded) != 32:
                raise ValueError("GPU verification public keys must be Ed25519 public keys")
        if any(not host or any(c in host for c in ",/ :\n\r") for host in self.image_hosts):
            raise ValueError("image_hosts must contain hostnames only")
        for origin in self.manifest.allowed_asset_sources:
            source = urlsplit(origin)
            if (source.scheme not in {"http", "https"} or not source.hostname or source.username or source.password
                    or source.path not in {"", "/"} or source.query or source.fragment or "," in origin):
                raise ValueError("asset sources must be exact HTTP(S) origins without credentials")
        return self


class LocalPluginDeployment:
    def __init__(self, plugins: PluginInstallations, api_origin: str, state_dir: Path, docker: Docker | None = None):
        self.plugins = plugins
        self.api_origin = api_origin.rstrip("/")
        self.store = ReceiptStore(state_dir)
        self.docker = docker or Docker()

    def _load(self) -> dict:
        receipt = self.store.load()
        if not receipt or receipt.get("version") != 1 or receipt.get("api_origin") != self.api_origin:
            raise AAPError("Missing installation receipt or different platform endpoint")
        return receipt

    def _request(self, receipt: dict) -> PluginPrepareRequest:
        config = LocalPluginConfig.model_validate(receipt["config"])
        return PluginPrepareRequest(
            installation_id=receipt["installation_id"], name=config.name, url=config.url,
            auth_token=receipt["token"], manifest=config.manifest,
            resource=PluginResourceBinding(gpu_resource_id=config.gpu_resource_id,
                physical_gpu_uuid=config.physical_gpu_uuid, logical_device="cuda:0"),
        )

    def _verify(self, receipt: dict, result: PluginInstallationResult) -> None:
        config = LocalPluginConfig.model_validate(receipt["config"])
        binding = PluginResourceBinding(gpu_resource_id=config.gpu_resource_id,
            physical_gpu_uuid=config.physical_gpu_uuid, logical_device="cuda:0")
        installation = result.installation
        if (installation.installation_id != UUID(receipt["installation_id"]) or
            installation.manifest != config.manifest or installation.resource != binding or
            (receipt.get("registry_id") and receipt["registry_id"] != str(result.registry_id))):
            raise AAPError("Registry installation does not match the local ownership receipt")

    def prepare(self, config: LocalPluginConfig) -> PluginInstallationResult:
        with self.store.locked(create=True):
            receipt = self.store.load()
            body = config.model_dump(mode="json")
            if receipt is None:
                receipt = {"version": 1, "api_origin": self.api_origin,
                    "installation_id": str(uuid4()), "token": secrets.token_urlsafe(48),
                    "owner": secrets.token_hex(32), "config": body}
                self.store.save(receipt)
            elif receipt.get("api_origin") != self.api_origin or receipt.get("config") != body:
                raise AAPError("Installation receipt already belongs to a different configuration")
            if "token" not in receipt:
                raise AAPError("Retired installation cannot be prepared again")
            result = self.plugins.prepare(self._request(receipt))
            self._verify(receipt, result)
            receipt["registry_id"] = str(result.registry_id)
            self.store.save(receipt)
            return result

    def status(self) -> PluginInstallationResult:
        with self.store.locked():
            receipt = self._load()
            result = self.plugins.get(receipt["installation_id"])
            self._verify(receipt, result)
            return result

    @staticmethod
    def _private_file(path: Path, content: str) -> None:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            _check_private(path, directory=False)
            if path.read_text(encoding="utf-8") != content:
                raise AAPError("Existing deployment file does not match the installation receipt")
            return
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    def _container(self, receipt: dict, result: PluginInstallationResult, *, provision: bool = True) -> ContainerConfig:
        config = LocalPluginConfig.model_validate(receipt["config"])
        data = self.store.directory / "data"
        data.mkdir(mode=0o700, exist_ok=True)
        _check_private(data, directory=True)
        if provision:
            self._private_file(data / "token", receipt["token"])
            self._private_file(data / "gpu-keyring.json", json.dumps(config.gpu_verify_keyring, sort_keys=True))
        env = {
            "AAP_PLUGIN_TOKEN_FILE": f"{_DATA_TARGET}/token",
            "AAP_PLUGIN_DEVICE": "cuda:0",
            "AAP_PLUGIN_REGISTRY_ID": str(result.registry_id),
            "AAP_PLUGIN_GPU_RESOURCE_ID": config.gpu_resource_id,
            "AAP_PLUGIN_GPU_UUID": config.physical_gpu_uuid,
            "AAP_PLUGIN_GPU_KEYRING_FILE": f"{_DATA_TARGET}/gpu-keyring.json",
            "AAP_PLUGIN_ASSET_ORIGINS": ",".join(config.manifest.allowed_asset_sources),
            "AAP_PLUGIN_IMAGE_HOSTS": ",".join(config.image_hosts),
            "AAP_PLUGIN_DATA_DIR": _DATA_TARGET,
        }
        if any("\n" in value or "\r" in value for value in env.values()):
            raise AAPError("Invalid newline in plugin configuration")
        env_file = self.store.directory / "container.env"
        self._private_file(env_file, "".join(f"{key}={value}\n" for key, value in env.items()))
        return ContainerConfig(name="aap-plugin-" + receipt["installation_id"],
            installation_id=receipt["installation_id"], owner_hash=hashlib.sha256(receipt["owner"].encode()).hexdigest(),
            image=config.manifest.artifact, gpu_uuid=config.physical_gpu_uuid, network=config.network,
            host_port=config.host_port, container_port=8000, memory_mb=config.manifest.resource.memory_mb,
            cpus=config.cpus, env_file=env_file, data_dir=data, data_target=_DATA_TARGET)

    def prepare_assets(self) -> PluginInstallationResult:
        with self.store.locked():
            receipt = self._load()
            result = self.plugins.get(receipt["installation_id"])
            self._verify(receipt, result)
            if result.installation.status != "prepared":
                raise AAPError("Assets can only be prepared for a prepared installation")
            self.docker.prepare_assets(self._container(receipt, result))
            return result

    def install(self) -> PluginInstallationResult:
        with self.store.locked():
            receipt = self._load()
            result = self.plugins.get(receipt["installation_id"])
            self._verify(receipt, result)
            if result.installation.status not in {"prepared", "active"}:
                raise AAPError("Only a prepared or active installation can be started")
            container = self._container(receipt, result)
            if result.installation.status == "active":
                self.docker.verify_running(container)
            else:
                inspected = self.docker.ensure_started(container)
                evidence = PluginLaunchReceipt(
                    installation_id=result.installation.installation_id,
                    registry_id=result.registry_id, artifact=result.installation.manifest.artifact,
                    image_id=inspected["image_id"], container_id=inspected["id"],
                    resource=result.installation.resource,
                )
                result = self.plugins.activate(receipt["installation_id"], PluginActivateRequest(
                    expected_revision=result.installation.revision, receipt=evidence))
                self._verify(receipt, result)
                if result.installation.status != "active":
                    raise AAPError("Registry did not activate the installation")
            return result

    def disable(self) -> PluginInstallationResult:
        with self.store.locked():
            receipt = self._load()
            result = self.plugins.get(receipt["installation_id"])
            self._verify(receipt, result)
            if result.installation.status == "active":
                result = self.plugins.disable(receipt["installation_id"], result.installation.revision)
                self._verify(receipt, result)
            if result.installation.status != "disabled":
                raise AAPError("Installation is not disabled")
            container = self._container(receipt, result)
            if not self.docker.exists(container) and receipt.get("quiesced_revision") == result.installation.revision:
                return result
            proof = self.plugins.quiesce(receipt["installation_id"], result.installation.revision)
            self._verify(receipt, proof)
            if not proof.safe_to_stop or proof.installation.status != "disabled":
                raise AAPError("Plugin quiescence was not proven; container remains running")
            receipt["quiesced_revision"] = proof.installation.revision
            self.store.save(receipt)
            self.docker.stop_and_remove(container)
            result = proof
            return result

    def uninstall(self) -> PluginInstallationResult:
        with self.store.locked():
            receipt = self._load()
            result = self.plugins.get(receipt["installation_id"])
            self._verify(receipt, result)
            if result.installation.status == "active":
                result = self.plugins.disable(receipt["installation_id"], result.installation.revision)
                self._verify(receipt, result)
            if result.installation.status != "retired":
                result = self.plugins.retire(receipt["installation_id"], result.installation.revision)
                self._verify(receipt, result)
            if result.installation.status != "retired":
                raise AAPError("Registry did not prove retirement")
            self.docker.stop_and_remove(self._container(receipt, result, provision=False))
            token_file = self.store.directory / "data" / "token"
            if token_file.exists():
                _check_private(token_file, directory=False)
                token_file.unlink()
            receipt.pop("token", None)
            self.store.save(receipt)
            return result
