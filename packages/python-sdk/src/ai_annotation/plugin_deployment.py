"""Administrator-side local plugin installation and crash recovery.

The Registry is authoritative; the private state directory is required to prove
local ownership and to replay the same reservation after a lost response.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import replace
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_annotation._plugin_docker import (
    ContainerConfig,
    Docker,
    _IMAGE,
    _QUALIFICATION_TARGET,
)
from ai_annotation._plugin_receipt import ReceiptStore, _check_private
from ai_annotation.errors import AAPError, APIStatusError, InstallError
from ai_annotation.plugin_models import (
    PluginActivateRequest,
    PluginInstallationResult,
    PluginLaunchReceipt,
    PluginManifest,
    PluginPrepareRequest,
    PluginQualification,
    PluginResourceBinding,
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
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "url must be an HTTP(S) endpoint without credentials, query or fragment"
            )
        if self.manifest.resource.device != "cuda":
            raise ValueError("local managed installation requires CUDA")
        if (
            self.manifest.plugin_id != "libreyolo"
            or self.manifest.entrypoint[1:]
            != ["serve", "--host", "0.0.0.0", "--port", "8000"]
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]*", self.manifest.entrypoint[0]
            )
        ):
            raise ValueError(
                "local installer requires an approved console script with fixed LibreYOLO serving arguments"
            )
        if not _IMAGE.fullmatch(self.manifest.artifact):
            raise ValueError("image must be a safe digest-pinned OCI reference")
        if not re.fullmatch(
            r"GPU-[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}",
            self.physical_gpu_uuid,
        ):
            raise ValueError("one physical NVIDIA GPU UUID is required")
        if (
            not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", self.network)
            or self.network == "host"
        ):
            raise ValueError("an explicit non-host Docker network is required")
        if any(c in self.gpu_resource_id for c in "\n\r"):
            raise ValueError("invalid GPU resource identity")
        if any(
            not key or not value or "\n" in key or "\n" in value
            for key, value in self.gpu_verify_keyring.items()
        ):
            raise ValueError("verification keyring must contain nonempty public keys")
        for key in self.gpu_verify_keyring.values():
            try:
                decoded = base64.b64decode(
                    key + "=" * (-len(key) % 4), altchars=b"-_", validate=True
                )
            except (ValueError, binascii.Error) as exc:
                raise ValueError("invalid GPU verification public key") from exc
            if len(decoded) != 32:
                raise ValueError(
                    "GPU verification public keys must be Ed25519 public keys"
                )
        if any(
            not host or any(c in host for c in ",/ :\n\r") for host in self.image_hosts
        ):
            raise ValueError("image_hosts must contain hostnames only")
        for origin in self.manifest.allowed_asset_sources:
            source = urlsplit(origin)
            if (
                source.scheme not in {"http", "https"}
                or not source.hostname
                or source.username
                or source.password
                or source.path not in {"", "/"}
                or source.query
                or source.fragment
                or "," in origin
            ):
                raise ValueError(
                    "asset sources must be exact HTTP(S) origins without credentials"
                )
        return self


class LocalPluginDeployment:
    def __init__(
        self,
        plugins: PluginInstallations,
        api_origin: str,
        state_dir: Path,
        docker: Docker | None = None,
    ):
        self.plugins = plugins
        self.api_origin = api_origin.rstrip("/")
        self.store = ReceiptStore(state_dir)
        self.docker = docker or Docker()

    def _load(self) -> dict:
        receipt = self.store.load()
        if (
            not receipt
            or receipt.get("version") != 1
            or receipt.get("api_origin") != self.api_origin
        ):
            raise AAPError(
                "Missing installation receipt or different platform endpoint"
            )
        return receipt

    def _request(self, receipt: dict) -> PluginPrepareRequest:
        config = LocalPluginConfig.model_validate(receipt["config"])
        return PluginPrepareRequest(
            installation_id=receipt["installation_id"],
            name=config.name,
            url=config.url,
            auth_token=receipt["token"],
            manifest=config.manifest,
            resource=PluginResourceBinding(
                gpu_resource_id=config.gpu_resource_id,
                physical_gpu_uuid=config.physical_gpu_uuid,
                logical_device="cuda:0",
            ),
        )

    def _verify(self, receipt: dict, result: PluginInstallationResult) -> None:
        config = LocalPluginConfig.model_validate(receipt["config"])
        binding = PluginResourceBinding(
            gpu_resource_id=config.gpu_resource_id,
            physical_gpu_uuid=config.physical_gpu_uuid,
            logical_device="cuda:0",
        )
        installation = result.installation
        if (
            installation.installation_id != UUID(receipt["installation_id"])
            or installation.manifest != config.manifest
            or installation.resource != binding
            or (
                receipt.get("registry_id")
                and receipt["registry_id"] != str(result.registry_id)
            )
        ):
            raise AAPError(
                "Registry installation does not match the local ownership receipt"
            )

    def prepare(self, config: LocalPluginConfig) -> PluginInstallationResult:
        with self.store.locked(create=True):
            receipt = self.store.load()
            body = config.model_dump(mode="json")
            if receipt is None:
                receipt = {
                    "version": 1,
                    "api_origin": self.api_origin,
                    "installation_id": str(uuid4()),
                    "token": secrets.token_urlsafe(48),
                    "owner": secrets.token_hex(32),
                    "config": body,
                }
                self.store.save(receipt)
            elif (
                receipt.get("api_origin") != self.api_origin
                or receipt.get("config") != body
            ):
                raise AAPError(
                    "Installation receipt already belongs to a different configuration"
                )
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
            fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            _check_private(path, directory=False)
            if path.read_text(encoding="utf-8") != content:
                raise AAPError(
                    "Existing deployment file does not match the installation receipt"
                )
            return
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    def _container(
        self,
        receipt: dict,
        result: PluginInstallationResult,
        *,
        provision: bool = True,
        with_qualification: bool = True,
    ) -> ContainerConfig:
        config = LocalPluginConfig.model_validate(receipt["config"])
        data = self.store.directory / "data"
        data.mkdir(mode=0o700, exist_ok=True)
        _check_private(data, directory=True)
        if provision:
            self._private_file(data / "token", receipt["token"])
            self._private_file(
                data / "gpu-keyring.json",
                json.dumps(config.gpu_verify_keyring, sort_keys=True),
            )
        env = {
            "AAP_PLUGIN_TOKEN_FILE": f"{_DATA_TARGET}/token",
            "AAP_ROUTE_CONTROL_TOKEN_FILE": f"{_DATA_TARGET}/token",
            "AAP_PLUGIN_DEVICE": "cuda:0",
            "AAP_PLUGIN_REGISTRY_ID": str(result.registry_id),
            "AAP_PLUGIN_GPU_RESOURCE_ID": config.gpu_resource_id,
            "AAP_PLUGIN_GPU_UUID": config.physical_gpu_uuid,
            "AAP_PLUGIN_GPU_KEYRING_FILE": f"{_DATA_TARGET}/gpu-keyring.json",
            "AAP_PLUGIN_ASSET_ORIGINS": ",".join(config.manifest.allowed_asset_sources),
            "AAP_PLUGIN_IMAGE_HOSTS": ",".join(config.image_hosts),
            "AAP_PLUGIN_DATA_DIR": _DATA_TARGET,
            "OMP_NUM_THREADS": str(max(1, int(config.cpus))),
            "MKL_NUM_THREADS": str(max(1, int(config.cpus))),
            "OPENBLAS_NUM_THREADS": str(max(1, int(config.cpus))),
        }
        qualification = receipt.get("qualification") if with_qualification else None
        qualification_file = (
            self.store.directory / "qualification.json" if qualification else None
        )
        if qualification:
            env.update(
                {
                    "AAP_PLUGIN_QUALIFICATION_FILE": _QUALIFICATION_TARGET,
                    "AAP_PLUGIN_QUALIFICATION_SHA256": qualification["sha256"],
                    "AAP_PLUGIN_OCI_ARTIFACT": config.manifest.artifact,
                    "AAP_PLUGIN_IMAGE_ID": qualification["image_id"],
                }
            )
        if any("\n" in value or "\r" in value for value in env.values()):
            raise AAPError("Invalid newline in plugin configuration")
        env_file = self.store.directory / (
            "container.env" if qualification else "prepare.env"
        )
        if provision:
            self._private_file(
                env_file, "".join(f"{key}={value}\n" for key, value in env.items())
            )
        elif env_file.exists():
            _check_private(env_file, directory=False)
        token = receipt.get("container_candidate_token")
        name = (
            "aap-plugin-"
            + receipt["installation_id"]
            + ("-" + token[:16] if token else "")
        )
        return ContainerConfig(
            name=name,
            installation_id=receipt["installation_id"],
            owner_hash=hashlib.sha256(receipt["owner"].encode()).hexdigest(),
            image=config.manifest.artifact,
            entrypoint=tuple(config.manifest.entrypoint),
            gpu_uuid=config.physical_gpu_uuid,
            network=config.network,
            host_port=config.host_port,
            container_port=8000,
            memory_mb=config.manifest.resource.memory_mb,
            cpus=config.cpus,
            env_file=env_file,
            data_dir=data,
            data_target=_DATA_TARGET,
            qualification_file=qualification_file,
            candidate_token=token,
        )

    def prepare_assets(self) -> PluginInstallationResult:
        with self.store.locked():
            receipt = self._load()
            result = self.plugins.get(receipt["installation_id"])
            self._verify(receipt, result)
            if result.installation.status != "prepared":
                raise AAPError(
                    "Assets can only be prepared for a prepared installation"
                )
            self.docker.prepare_assets(
                self._container(receipt, result, with_qualification=False)
            )
            return result

    def _qualification(
        self, receipt: dict, result: PluginInstallationResult, source: Path | None
    ) -> PluginQualification:
        try:
            from aap_backend_runtime.lifecycle_evidence import (
                validate_managed_lifecycle_evidence,
            )
            from training_contract.inference_runtime import LIBRE_CUDA_RUNTIME
        except ModuleNotFoundError as exc:
            raise InstallError(
                "Plugin deployment dependencies are missing; install 'ai-annotation-sdk[plugins]' from the private wheel index"
            ) from exc

        saved = receipt.get("qualification")
        path = self.store.directory / "qualification.json"
        if source is None:
            if not saved:
                raise AAPError("A GPU lifecycle qualification report is required")
            source = path
            _check_private(source, directory=False)
        try:
            report = json.loads(source.read_text(encoding="utf-8"))
            canonical = json.dumps(
                report,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
        except (OSError, ValueError, TypeError) as exc:
            raise AAPError(
                "Unable to read a valid GPU lifecycle qualification report"
            ) from exc
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if saved and saved["sha256"] != digest:
            raise AAPError("Qualification report differs from the installation receipt")
        try:
            report = validate_managed_lifecycle_evidence(report)
        except (TypeError, ValueError) as exc:
            raise AAPError("Invalid GPU lifecycle qualification report") from exc
        if not report["passed"]:
            raise AAPError("GPU lifecycle qualification did not pass")
        image_id = self.docker.image_id(
            self._container(receipt, result, with_qualification=False)
        )
        if saved and saved["image_id"] != image_id:
            raise AAPError("Qualified image ID differs from the local image")
        item = result.installation
        resource = item.resource
        deployment = report["deployment"]
        gpu = report["gpu"]
        versions = deployment["runtime_versions"]
        required = {
            "plugin_id": item.manifest.plugin_id,
            "plugin_version": item.manifest.version,
            "artifact": item.manifest.artifact,
            "asset_manifest_sha256": item.manifest.asset_manifest_sha256,
            "registry_id": str(result.registry_id),
            "gpu_resource_id": resource.gpu_resource_id,
            "driver_version": gpu["driver_version"],
        }
        if any(
            entry.framework == "libreyolo"
            for entry in item.manifest.managed_model_compatibility
        ):
            required.update(
                {
                    key: str(value).lower() if isinstance(value, bool) else str(value)
                    for key, value in LIBRE_CUDA_RUNTIME.items()
                }
            )
        if (
            report["backend_name"] != item.manifest.plugin_id
            or deployment["image_id"] != image_id
            or gpu["uuid"] != resource.physical_gpu_uuid
            or deployment["pool_topology"]
            != {
                "registry_id": str(result.registry_id),
                "gpu_resource_id": resource.gpu_resource_id,
                "pools": {"models": {"cap": 1}},
            }
            or any(versions.get(key) != value for key, value in required.items())
            or (
                "cuda_build" in required
                and gpu["runtime_version"] != required["cuda_build"]
            )
        ):
            raise AAPError(
                "GPU lifecycle qualification differs from the reserved installation"
            )
        self._private_file(path, canonical)
        if not saved:
            receipt["qualification"] = {"sha256": digest, "image_id": image_id}
            self.store.save(receipt)
        return PluginQualification(report=report, sha256=digest)

    def install(
        self, qualification_path: Path | None = None
    ) -> PluginInstallationResult:
        with self.store.locked():
            receipt = self._load()
            result = self.plugins.get(receipt["installation_id"])
            self._verify(receipt, result)
            if receipt.get("restart_candidate") or receipt.get("restart_intent"):
                raise AAPError("Finish the pending restart with plugins restart")
            if result.installation.status not in {"prepared", "active"}:
                raise AAPError("Only a prepared or active installation can be started")
            qualification = self._qualification(receipt, result, qualification_path)
            container = self._container(receipt, result)
            if result.installation.status == "active":
                inspected = self.docker.verify_running(container)
                self._verify_process(
                    receipt.get("launch"), receipt.get("started_at"), inspected
                )
            else:
                # Record intent before Docker can create a container; a crash must
                # not turn a possible launch into a cancellable reservation.
                receipt["launch_attempted"] = True
                self.store.save(receipt)
                inspected = (
                    self.docker.verify_running(container)
                    if receipt.get("launch")
                    else self.docker.ensure_started(container)
                )
                if receipt.get("launch"):
                    self._verify_process(
                        receipt["launch"], receipt.get("started_at"), inspected
                    )
                if inspected["image_id"] != receipt["qualification"]["image_id"]:
                    raise AAPError("Container image changed after qualification")
                evidence = PluginLaunchReceipt(
                    installation_id=result.installation.installation_id,
                    registry_id=result.registry_id,
                    artifact=result.installation.manifest.artifact,
                    image_id=inspected["image_id"],
                    container_id=inspected["id"],
                    resource=result.installation.resource,
                )
                receipt["launch"] = evidence.model_dump(mode="json")
                receipt["started_at"] = inspected["started_at"]
                self.store.save(receipt)
                self.docker.wait_ready(container, inspected["id"])
                result = self.plugins.activate(
                    receipt["installation_id"],
                    PluginActivateRequest(
                        expected_revision=result.installation.revision,
                        receipt=evidence,
                        qualification=qualification,
                    ),
                )
                self._verify(receipt, result)
                if result.installation.status != "active":
                    raise AAPError("Registry did not activate the installation")
                self._verify_process(
                    receipt["launch"],
                    receipt["started_at"],
                    self.docker.verify_running(container),
                )
            return result

    @staticmethod
    def _verify_process(
        launch: dict | None, started_at: str | None, inspected: dict
    ) -> None:
        if (
            not launch
            or not started_at
            or launch.get("container_id") != inspected["id"]
            or launch.get("image_id") != inspected["image_id"]
            or started_at != inspected["started_at"]
        ):
            raise AAPError(
                "Container process identity is not the recorded activation; unexpected exits or external restarts require owner recovery"
            )

    def restart(self) -> PluginInstallationResult:
        """Replace a proven-quiescent owned launch, retaining its data and history."""
        with self.store.locked():
            receipt = self._load()
            result = self.plugins.get(receipt["installation_id"])
            self._verify(receipt, result)
            candidate = receipt.get("restart_candidate")
            if result.installation.status == "active" and candidate:
                container = self._candidate_container(receipt, result, candidate)
                inspected = self.docker.verify_running(container)
                self._verify_process(
                    candidate.get("launch"), candidate.get("started_at"), inspected
                )
                return self._finish_restart(receipt, result)
            intent = receipt.get("restart_intent")
            if result.installation.status == "active":
                self._bind_active_process(receipt, result)
                if intent is None:
                    intent = {
                        "launch": receipt["launch"],
                        "started_at": receipt["started_at"],
                        "revision": result.installation.revision,
                        "registry_id": str(result.registry_id),
                    }
                    receipt["restart_intent"] = intent
                    self.store.save(receipt)
                self._verify_restart_intent(receipt, result, intent, active=True)
                result = self._disable(receipt, result)
            elif (
                result.installation.status == "disabled"
                and candidate is None
                and intent is not None
            ):
                self._verify_restart_intent(receipt, result, intent, active=False)
                if not receipt.get("quiescence"):
                    result = self._disable(receipt, result)
            if result.installation.status != "disabled":
                raise AAPError(
                    "Restart requires an active or proven disabled installation"
                )
            proof = receipt.get("quiescence")
            if (
                not proof
                or proof.get("launch") != receipt.get("launch")
                or not receipt.get("launch")
                or (
                    candidate is None
                    and proof.get("revision") != result.installation.revision
                )
                or (
                    candidate is not None
                    and proof.get("revision", 0) > result.installation.revision
                )
            ):
                raise AAPError(
                    "Restart requires the matching committed local quiescence proof"
                )
            qualification = self._qualification(receipt, result, None)
            container = self._container(receipt, result)
            if candidate is None:
                self.docker.stop_and_remove(
                    container,
                    proof["launch"]["container_id"],
                    proof.get("started_at", receipt.get("started_at")),
                )
                candidate = {
                    "proof": proof,
                    "previous_started_at": receipt.get("started_at"),
                    "token": secrets.token_hex(32),
                }
                receipt["restart_candidate"] = candidate
                self.store.save(receipt)
            if candidate.get("proof") != proof:
                raise AAPError(
                    "Candidate does not match the committed quiescence proof"
                )
            container = self._candidate_container(receipt, result, candidate)
            if "container_id" not in candidate:
                created = self.docker.create_candidate(container)
                candidate["container_id"] = created["id"]
                if created["id"] == proof["launch"]["container_id"]:
                    raise AAPError("Candidate must be a new container")
                self.store.save(receipt)
            inspected = self.docker.start_candidate(
                container,
                candidate["container_id"],
                may_start=not candidate.get("started_at"),
            )
            if inspected["image_id"] != receipt["qualification"]["image_id"]:
                raise AAPError("Container image changed after qualification")
            evidence = PluginLaunchReceipt(
                installation_id=result.installation.installation_id,
                registry_id=result.registry_id,
                artifact=result.installation.manifest.artifact,
                image_id=inspected["image_id"],
                container_id=inspected["id"],
                resource=result.installation.resource,
            )
            launch = evidence.model_dump(mode="json")
            if candidate.get("launch"):
                self._verify_process(
                    candidate["launch"], candidate.get("started_at"), inspected
                )
                if candidate["launch"] != launch:
                    raise AAPError(
                        "Candidate launch differs from the recorded identity"
                    )
            else:
                candidate["launch"] = launch
                candidate["started_at"] = inspected["started_at"]
                self.store.save(receipt)
            self.docker.wait_ready(container, inspected["id"])
            result = self.plugins.activate(
                receipt["installation_id"],
                PluginActivateRequest(
                    expected_revision=result.installation.revision,
                    receipt=evidence,
                    qualification=qualification,
                ),
            )
            self._verify(receipt, result)
            if result.installation.status != "active":
                raise AAPError("Registry did not activate the installation")
            self._verify_process(
                launch, candidate["started_at"], self.docker.verify_running(container)
            )
            return self._finish_restart(receipt, result)

    @staticmethod
    def _verify_restart_intent(
        receipt: dict, result: PluginInstallationResult, intent: dict, *, active: bool
    ) -> None:
        revision = intent.get("revision")
        if (
            type(revision) is not int
            or not intent.get("started_at")
            or intent.get("launch") != receipt.get("launch")
            or intent.get("started_at") != receipt.get("started_at")
            or intent.get("registry_id") != str(result.registry_id)
            or result.installation.revision
            not in ({revision} if active else {revision + 1, revision + 2})
        ):
            raise AAPError(
                "Pending restart intent does not match the original launch and revision"
            )

    def _candidate_container(
        self, receipt: dict, result: PluginInstallationResult, candidate: dict
    ) -> ContainerConfig:
        token = candidate["token"]
        container = self._container(receipt, result)
        return replace(
            container,
            name="aap-plugin-" + receipt["installation_id"] + "-" + token[:16],
            candidate_token=token,
        )

    def _finish_restart(
        self, receipt: dict, result: PluginInstallationResult
    ) -> PluginInstallationResult:
        candidate = receipt.pop("restart_candidate")
        receipt.setdefault("launch_history", []).append(
            {
                "launch": receipt["launch"],
                "started_at": candidate["previous_started_at"],
                "quiescence": candidate["proof"],
            }
        )
        receipt["launch"] = candidate["launch"]
        receipt["started_at"] = candidate["started_at"]
        receipt["container_candidate_token"] = candidate["token"]
        receipt.pop("quiescence", None)
        receipt.pop("restart_intent", None)
        self.store.save(receipt)
        return result

    def _save_proof(self, receipt: dict, proof) -> None:
        self._verify(receipt, proof)
        launch = proof.launch.model_dump(mode="json")
        item = proof.installation
        if (
            not proof.safe_to_stop
            or item.status != "disabled"
            or launch["installation_id"] != str(item.installation_id)
            or launch["registry_id"] != str(proof.registry_id)
            or launch["artifact"] != item.manifest.artifact
            or launch["resource"] != item.resource.model_dump(mode="json")
            or (receipt.get("launch") is not None and receipt["launch"] != launch)
        ):
            raise AAPError("Plugin quiescence was not proven for the local launch")
        receipt["quiescence"] = {
            "revision": item.revision,
            "launch": launch,
            "started_at": receipt.get("started_at"),
        }
        self.store.save(receipt)

    def _bind_active_process(
        self, receipt: dict, result: PluginInstallationResult
    ) -> None:
        inspected = self.docker.verify_running(
            self._container(receipt, result, provision=False)
        )
        launch = receipt.get("launch")
        started_at = receipt.get("started_at")
        if started_at is None and launch:
            # Older receipts predate start-time recording. Persist the observed
            # process before requesting a proof; the owner still authenticates
            # the original activation boot when it establishes quiescence.
            self._verify_process(launch, inspected["started_at"], inspected)
            receipt["started_at"] = inspected["started_at"]
            self.store.save(receipt)
        else:
            self._verify_process(launch, started_at, inspected)

    def _disable(
        self, receipt: dict, result: PluginInstallationResult
    ) -> PluginInstallationResult:
        if receipt.get("restart_candidate"):
            raise AAPError(
                "Finish the pending restart before disabling or uninstalling"
            )
        if result.installation.status == "active":
            self._bind_active_process(receipt, result)
            result = self.plugins.disable(
                receipt["installation_id"], result.installation.revision
            )
            self._verify(receipt, result)
        if result.installation.status != "disabled":
            raise AAPError("Installation is not disabled")
        container = self._container(receipt, result, provision=False)
        saved = receipt.get("quiescence")
        if (
            not self.docker.exists(container)
            and saved
            and saved["revision"] == result.installation.revision
        ):
            return result
        proof = self.plugins.quiesce(
            receipt["installation_id"], result.installation.revision
        )
        self._save_proof(receipt, proof)
        self.docker.stop_and_remove(
            container, proof.launch.container_id, receipt["quiescence"]["started_at"]
        )
        return proof

    def disable(self) -> PluginInstallationResult:
        with self.store.locked():
            receipt = self._load()
            result = self.plugins.get(receipt["installation_id"])
            self._verify(receipt, result)
            return self._disable(receipt, result)

    def uninstall(self) -> PluginInstallationResult:
        with self.store.locked():
            receipt = self._load()
            result = self.plugins.get(receipt["installation_id"])
            self._verify(receipt, result)
            if receipt.get("restart_candidate") or receipt.get("restart_intent"):
                raise AAPError(
                    "Finish the pending restart before disabling or uninstalling"
                )
            if result.installation.status == "active":
                self._bind_active_process(receipt, result)
                result = self.plugins.disable(
                    receipt["installation_id"], result.installation.revision
                )
                self._verify(receipt, result)
            if result.installation.status != "retired":
                try:
                    proof = self.plugins.quiesce(
                        receipt["installation_id"], result.installation.revision
                    )
                except APIStatusError as exc:
                    if (
                        result.installation.status != "prepared"
                        or exc.status_code != 409
                        or not isinstance(exc.detail, dict)
                        or exc.detail.get("error_code") != "plugin_launch_unrecorded"
                    ):
                        raise
                    if (
                        receipt.get("launch_attempted")
                        or receipt.get("launch")
                        or self.docker.exists(
                            self._container(receipt, result, provision=False)
                        )
                    ):
                        raise AAPError(
                            "Unrecorded local launch prevents cancellation"
                        ) from exc
                    result = self.plugins.cancel_prepared(
                        receipt["installation_id"], result.installation.revision
                    )
                    self._verify(receipt, result)
                else:
                    self._save_proof(receipt, proof)
                    result = self.plugins.retire(
                        receipt["installation_id"], proof.installation.revision
                    )
                    self._verify(receipt, result)
            if result.installation.status != "retired":
                raise AAPError("Registry did not prove retirement")
            container = self._container(receipt, result, provision=False)
            saved = receipt.get("quiescence")
            if saved and saved["revision"] + 1 == result.installation.revision:
                self.docker.stop_and_remove(
                    container,
                    saved["launch"]["container_id"],
                    saved.get("started_at", receipt.get("started_at")),
                )
            elif self.docker.exists(container):
                raise AAPError("No matching quiescence proof for the local container")
            token_file = self.store.directory / "data" / "token"
            if token_file.exists():
                _check_private(token_file, directory=False)
                token_file.unlink()
            receipt.pop("token", None)
            self.store.save(receipt)
            return result
