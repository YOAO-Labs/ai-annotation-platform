"""Export the private HTTP contract without importing its server framework."""

from __future__ import annotations

from . import contract as c
from .transport import (
    InternalSourceReferenceRequest,
    MediaReferences,
    SourceReference,
    SourceReferences,
    SourceReferencesRequest,
)


def http_document() -> dict:
    paths = {}
    definitions = {}
    error = {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "ok", "error"],
        "properties": {
            "schema": {"const": c.MEDIA_SCHEMA},
            "ok": {"const": False},
            "error": c._Error.model_json_schema(),
        },
    }

    def model_ref(model):
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        for name, value in schema.pop("$defs", {}).items():
            if name in definitions and definitions[name] != value:
                raise ValueError("conflicting media HTTP schema")
            definitions[name] = value
        name = model.__name__
        if name in definitions and definitions[name] != schema:
            raise ValueError("conflicting media HTTP schema")
        definitions[name] = schema
        return {"$ref": "#/components/schemas/" + name}

    def add(path, request, response):
        paths[path] = {
            "post": {
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": model_ref(request)}},
                },
                "responses": {
                    "200": {
                        "description": "Accepted owner result",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["schema", "ok", "data"],
                                    "properties": {
                                        "schema": {"const": c.MEDIA_SCHEMA},
                                        "ok": {"const": True},
                                        "data": model_ref(response),
                                    },
                                }
                            }
                        },
                    },
                    "default": {
                        "description": "Media contract failure",
                        "content": {"application/json": {"schema": error}},
                    },
                },
            }
        }

    for name, operation in c.OPERATIONS.items():
        if operation.role != "worker":
            add("/v1/commands/" + name, operation.request, operation.result)
    for path, request, response in (
        ("source-reference", c.ReadSourceRequest, SourceReference),
        ("source-references", SourceReferencesRequest, SourceReferences),
        ("internal-source-reference", InternalSourceReferenceRequest, SourceReference),
        ("assets-reference", c.AssetsRequest, MediaReferences),
        ("internal-frame-reference", c.InternalFrameRequest, MediaReferences),
    ):
        add("/v1/" + path, request, response)
    return {
        "openapi": "3.1.0",
        "info": {"title": "Assets media control", "version": "1"},
        "security": [{"serviceToken": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {"serviceToken": {"type": "http", "scheme": "bearer"}},
            "schemas": definitions,
        },
        "x-media-schema": c.MEDIA_SCHEMA,
        "x-budgets": c.contract_document()["x-budgets"],
    }
