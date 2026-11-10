"""Opt-in, test-only diagnostics for the isolated importer acceptance image.

The fixture mounts this file only when AAP_IMPORT_CAPTURE_DIAGNOSTICS=1.
Only typed codes and operation names are emitted, never SDK messages, locators,
credentials, request parameters, or response bodies.
"""

from __future__ import annotations

import re

from botocore.client import BaseClient
from botocore.exceptions import ClientError

from yoao_assets_source_import.__main__ import main
from yoao_assets_source_import.workflow import ImportWorkflow


def _token(value: object) -> str:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_]{1,80}", value):
        return value
    return "unavailable"


_DESTINATION_MESSAGES = {
    "destination target is not configured": "target_not_configured",
    "destination credentials are malformed": "credentials_malformed",
    "destination credentials are unavailable": "credentials_unavailable",
    "destination target scope changed": "target_scope_changed",
    "destination transfer timed out": "transfer_timed_out",
    "destination returned an invalid write receipt": "invalid_write_receipt",
}


_original_api_call = BaseClient._make_api_call


def _diagnostic_api_call(self, operation, params):
    try:
        return _original_api_call(self, operation, params)
    except Exception as exc:
        code = "unavailable"
        status = "unavailable"
        if isinstance(exc, ClientError):
            response = exc.response
            code = _token(response.get("Error", {}).get("Code"))
            observed_status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if type(observed_status) is int:
                status = str(observed_status)
        print(
            f"IMPORT_DIAGNOSTIC sdk_operation={_token(operation)} "
            f"exception={_token(type(exc).__name__)} code={code} status={status}",
            flush=True,
        )
        raise


_original_fail = ImportWorkflow._fail_original


async def _diagnostic_fail(self, target, stage, code, exc):
    typed_code = getattr(exc, "code", None)
    if typed_code is None and len(exc.args) == 1:
        typed_code = exc.args[0]
    if isinstance(typed_code, str):
        typed_code = _DESTINATION_MESSAGES.get(typed_code, typed_code)
    print(
        f"IMPORT_DIAGNOSTIC stage={_token(stage)} category={_token(code)} "
        f"exception={_token(type(exc).__name__)} code={_token(typed_code)} "
        f"outcome={_token(getattr(exc, 'outcome', None))} "
        f"allocated={target is not None}",
        flush=True,
    )
    return await _original_fail(self, target, stage, code, exc)


BaseClient._make_api_call = _diagnostic_api_call
ImportWorkflow._fail_original = _diagnostic_fail
main()
