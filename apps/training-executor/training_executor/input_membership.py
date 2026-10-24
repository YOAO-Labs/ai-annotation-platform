"""Observe existing native datasets without replacing loaders or sampling algorithms."""

from pathlib import Path

from training_contract.input_membership import (
    contract_for,
    declared_contract,
    member_records,
    observed_members,
    receipt_for,
    verify_receipt,
)
from training_contract.observability import InputMismatchError

from .files import atomic_json


class InputMembership:
    def __init__(self, spec, output, manifest):
        self.config = spec["frozen_config"]
        if contract_for(manifest) != declared_contract(self.config):
            raise InputMismatchError("Frozen input contract differs from its manifest")
        self.root = Path(spec["data_yaml"]).resolve().parent
        self.records = member_records(manifest)
        self.run_id, self.attempt_id = spec["run_id"], spec["attempt_id"]
        self.output = output
        self.members = {}

    def observe(self, split, native_paths):
        try:
            paths = []
            for value in native_paths:
                if not isinstance(value, (str, Path)):
                    raise ValueError("Native input path is invalid")
                path = Path(value)
                if not path.is_absolute():
                    path = self.root / path
                paths.append(path.resolve().relative_to(self.root).as_posix())
            observed = observed_members(self.records, split, paths)
            if split in self.members and self.members[split] != observed:
                raise ValueError("Native input members changed during execution")
            self.members[split] = observed
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise InputMismatchError(str(exc)) from exc

    def complete(self):
        try:
            proof = receipt_for(self.config, self.run_id, self.attempt_id, self.members)
        except (ValueError, KeyError, TypeError) as exc:
            raise InputMismatchError("Native input observation is incomplete") from exc
        atomic_json(self.output / "input-membership.json", proof)
        return proof


def membership_for(spec, output, current=None, *, manifest=None):
    try:
        if declared_contract(spec["frozen_config"]) is None:
            return None
        if current is not None:
            return current
        if manifest is None:
            from .adapters import validate_dataset

            manifest = validate_dataset(spec)
        return InputMembership(spec, output, manifest)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise InputMismatchError(str(exc)) from exc


def verified_local_receipt(config, run_id, attempt_id, path):
    """Keep the existing execution manifest as the only uploaded receipt artifact."""
    import json

    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Training input receipt exceeds the report limit")
        proof = json.loads(path.read_text())
    except OSError as exc:
        raise ValueError("Training input receipt is missing or unreadable") from exc
    verify_receipt(proof, config, run_id, attempt_id)
    return proof
