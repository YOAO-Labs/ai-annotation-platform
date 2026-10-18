"""Image HTTP contract snapshot without importing the server framework."""

from __future__ import annotations

from . import contract as c
from .transport import (
    ImageAssetReferences,
    ImageAssetReferencesRequest,
    ImageGenerationReference,
    ImageGenerationReferenceRequest,
    ImageSourceReference,
    ImageSourceReferences,
    ImageSourceReferencesRequest,
    InternalImageSourceReferenceRequest,
)


def http_document() -> dict:
    paths = {}
    definitions = {}
    error = {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "ok", "error"],
        "properties": {
            "schema": {"const": c.IMAGE_SCHEMA},
            "ok": {"const": False},
            "error": c._Error.model_json_schema(),
        },
    }

    def model_ref(model):
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        for name, value in schema.pop("$defs", {}).items():
            if name in definitions and definitions[name] != value:
                raise ValueError("conflicting image HTTP schema")
            definitions[name] = value
        name = model.__name__
        if name in definitions and definitions[name] != schema:
            raise ValueError("conflicting image HTTP schema")
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
                        "description": "Accepted image owner result",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["schema", "ok", "data"],
                                    "properties": {
                                        "schema": {"const": c.IMAGE_SCHEMA},
                                        "ok": {"const": True},
                                        "data": model_ref(response),
                                    },
                                }
                            }
                        },
                    },
                    "default": {
                        "description": "Image contract failure",
                        "content": {"application/json": {"schema": error}},
                    },
                },
            }
        }

    for name, operation in c.OPERATIONS.items():
        if operation.role not in {"worker", "cleanup"}:
            add("/v1/images/commands/" + name, operation.request, operation.result)
    for path, request, response in (
        ("source-reference", c.ReadImageSourceRequest, ImageSourceReference),
        ("raw-source-reference", c.ReadImageSourceRequest, ImageSourceReference),
        ("source-references", ImageSourceReferencesRequest, ImageSourceReferences),
        (
            "internal-source-reference",
            InternalImageSourceReferenceRequest,
            ImageSourceReference,
        ),
        (
            "generation-reference",
            ImageGenerationReferenceRequest,
            ImageGenerationReference,
        ),
        ("asset-references", ImageAssetReferencesRequest, ImageAssetReferences),
    ):
        add("/v1/images/" + path, request, response)
    return {
        "openapi": "3.1.0",
        "info": {"title": "Assets image control", "version": "1"},
        "security": [{"serviceToken": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {"serviceToken": {"type": "http", "scheme": "bearer"}},
            "schemas": definitions,
        },
        "x-image-schema": c.IMAGE_SCHEMA,
        "x-budgets": c.contract_document()["x-budgets"],
    }
