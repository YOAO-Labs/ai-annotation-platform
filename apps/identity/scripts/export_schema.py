"""Generate the distributable Identity wire schema from the closed models."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pydantic import TypeAdapter  # noqa: E402
from aap_identity.contract import (  # noqa: E402
    AuthenticateRequest,
    CandidatesPage,
    FailureReply,
    PrincipalReply,
)
from aap_identity.key_contract import (  # noqa: E402
    KeyFailureReply,
    KeyReply,
    KeyRequest,
    NativeKeyCommand,
    NativeKeyFailureReply,
    NativeKeyReply,
)


def principal_shape(schema):
    schema["$defs"]["Principal"]["allOf"] = [
        {
            "oneOf": [
                {
                    "properties": {
                        "credential_type": {"const": "jwt"},
                        "generation": {
                            "type": "integer",
                            "minimum": 0,
                            "exclusiveMaximum": 2**63,
                        },
                        "scopes": {"type": "null"},
                        "expires_at": {"type": "string", "format": "date-time"},
                    }
                },
                {
                    "properties": {
                        "credential_type": {"const": "api_key"},
                        "generation": {"type": "null"},
                        "scopes": {"type": "array", "items": {"type": "string"}},
                    }
                },
            ]
        }
    ]
    return schema


def patch_shape(schema):
    patch = schema.get("$defs", {}).get("KeyPatch")
    if patch is not None:
        patch["properties"]["name"] = {
            "maxLength": 60,
            "minLength": 1,
            "title": "Name",
            "type": "string",
        }
        patch["properties"]["scopes"] = {
            "items": {"type": "string"},
            "title": "Scopes",
            "type": "array",
        }
    return schema


def render_schema(adapter, schema_id):
    schema = adapter.json_schema()
    if "Principal" in schema.get("$defs", {}):
        principal_shape(schema)
    if "KeyPatch" in schema.get("$defs", {}):
        patch_shape(schema)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = schema_id
    return schema


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    schemas = {
        "src/aap_identity/protocol.schema.json": render_schema(
            TypeAdapter(
                AuthenticateRequest | PrincipalReply | FailureReply | CandidatesPage
            ),
            "aap-identity/v1",
        ),
        "src/aap_identity/key_protocol.schema.json": render_schema(
            TypeAdapter(KeyRequest | KeyReply | KeyFailureReply),
            "aap-identity-keys/v1",
        ),
        "src/aap_identity/native_key_protocol.schema.json": render_schema(
            TypeAdapter(NativeKeyCommand | NativeKeyReply | NativeKeyFailureReply),
            "aap-identity-keys-native/v1",
        ),
    }
    for relative_path, schema in schemas.items():
        target = ROOT / relative_path
        if args.check:
            if not target.exists() or json.loads(target.read_text()) != schema:
                raise SystemExit(
                    f"Identity schema differs from its frozen models: {relative_path}"
                )
        else:
            target.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
