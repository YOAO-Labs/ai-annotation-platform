"""Frozen HTTP shape, without importing the server framework or exposing workers."""

from . import contract as c
from .client import HTTP_OPERATIONS


def http_document():
    definitions = {}

    def model_ref(model):
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        for name, definition in schema.pop("$defs", {}).items():
            if name in definitions and definitions[name] != definition:
                raise ValueError("conflicting input HTTP definition")
            definitions[name] = definition
        name = model.__name__
        if name in definitions and definitions[name] != schema:
            raise ValueError("conflicting input HTTP model")
        definitions[name] = schema
        return {"$ref": "#/components/schemas/" + name}

    paths = {}
    for operation, (request, result) in HTTP_OPERATIONS.items():
        success = {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema", "ok", "data"],
            "properties": {
                "schema": {"const": c.INPUT_SCHEMA},
                "ok": {"const": True},
                "data": model_ref(result),
            },
        }
        failure = {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema", "ok", "error"],
            "properties": {
                "schema": {"const": c.INPUT_SCHEMA},
                "ok": {"const": False},
                "error": model_ref(c._Error),
            },
        }
        paths["/v1/inputs/" + operation] = {
            "post": {
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": model_ref(request)}},
                },
                "responses": {
                    "200": {
                        "description": "Accepted input result",
                        "content": {"application/json": {"schema": success}},
                    },
                    "default": {
                        "description": "Input contract failure",
                        "content": {"application/json": {"schema": failure}},
                    },
                },
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": "Accepted image input control", "version": "1"},
        "security": [{"serviceToken": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {"serviceToken": {"type": "http", "scheme": "bearer"}},
            "schemas": definitions,
        },
        "x-input-schema": c.INPUT_SCHEMA,
        "x-budgets": c.contract_document()["x-budgets"],
    }
