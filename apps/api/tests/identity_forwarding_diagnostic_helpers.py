"""Test-only tracing of the original host-forwarded Identity DB topology."""

from __future__ import annotations

import json

from tests.identity_module_assembly_helpers import IdentityModuleStack


TRACE_PREFIX = "AAP_IDENTITY_ROUTE_TRACE "

# Instrument installed packages without mounting source or changing the adapters.
OWNER_TRACE = r"""
import contextvars, json, re, sys, time, traceback, urllib.parse
import asyncpg
from aap_identity.contract import IdentityUnavailable
import aap_identity_runtime.api as api

path_context = contextvars.ContextVar("identity_rpc_path", default=None)
connections = {}
def emit(phase, started, error=None, **fields):
    print("AAP_IDENTITY_ROUTE_TRACE " + json.dumps(dict(
        phase=phase, rpc=path_context.get(), seconds=round(time.monotonic()-started,6),
        error=type(error).__name__ if error else None,
        sqlstate=getattr(error,"sqlstate",None), **fields)), flush=True)

original_connect = asyncpg.connect
async def connect(*args, **kwargs):
    started = time.monotonic()
    url = urllib.parse.urlsplit(args[0] if args else kwargs.get("dsn", ""))
    metadata = dict(role=url.username, database=url.path.lstrip("/"), host=url.hostname)
    try:
        connection = await original_connect(*args, **kwargs)
    except BaseException as error:
        emit("connect", started, error, **metadata)
        raise
    metadata["pid"] = connection.get_server_pid()
    connections[id(connection)] = metadata
    emit("connect", started, **metadata)
    return connection
asyncpg.connect = connect

def trace_query(method):
    original = getattr(asyncpg.Connection, method)
    async def query(connection, sql, *args, **kwargs):
        started = time.monotonic()
        match = re.search(r"\b(aap_[a-z0-9_]+\.[a-z0-9_]+)\s*\(", str(sql))
        metadata = dict(connections.get(id(connection), {}))
        metadata["function"] = match.group(1) if match else "transaction_or_catalog"
        try:
            result = await original(connection, sql, *args, **kwargs)
        except BaseException as error:
            emit("query", started, error, **metadata)
            raise
        emit("query", started, **metadata)
        return result
    setattr(asyncpg.Connection, method, query)
for method in ("fetch", "fetchval", "execute"):
    trace_query(method)

original_close = asyncpg.Connection.close
async def close(connection, *args, **kwargs):
    started = time.monotonic()
    metadata = connections.pop(id(connection), {})
    try:
        result = await original_close(connection, *args, **kwargs)
    except BaseException as error:
        emit("close", started, error, **metadata)
        raise
    emit("close", started, **metadata)
    return result
asyncpg.Connection.close = close

original_unavailable = IdentityUnavailable.__init__
def unavailable(self, *args, **kwargs):
    error = sys.exception()
    causes = []
    for _ in range(6):
        if error is None:
            break
        causes.append(dict(type=type(error).__name__, sqlstate=getattr(error,"sqlstate",None)))
        error = error.__context__
    frames = [dict(file=f.filename.rsplit("/",1)[-1],line=f.lineno,function=f.name)
              for f in traceback.extract_stack(limit=6)[:-1]]
    emit("unavailable",time.monotonic(),causes=causes,frames=frames)
    original_unavailable(self,*args,**kwargs)
IdentityUnavailable.__init__ = unavailable

original_create_app = api.create_app
def create_app(*args, **kwargs):
    app = original_create_app(*args, **kwargs)
    async def traced(scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/v1/identity/"):
            return await app(scope, receive, send)
        token = path_context.set(scope["path"])
        started, status = time.monotonic(), None
        async def capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
        try:
            await app(scope, receive, capture)
        except BaseException as error:
            emit("owner_http", started, error, status=status)
            raise
        else:
            emit("owner_http", started, status=status)
        finally:
            path_context.reset(token)
    return traced
api.create_app = create_app
from aap_identity_runtime.__main__ import main
main()
"""

API_TRACE = r"""
import json, time
import httpx
original_send = httpx.AsyncClient.send
async def send(client, request, *args, **kwargs):
    if not request.url.path.startswith("/v1/identity/"):
        return await original_send(client, request, *args, **kwargs)
    started = time.monotonic()
    def emit(error=None, status=None):
        print("AAP_IDENTITY_ROUTE_TRACE " + json.dumps(dict(
            phase="api_http", rpc=request.url.path, status=status,
            seconds=round(time.monotonic()-started,6),
            error=type(error).__name__ if error else None)),flush=True)
    try:
        response = await original_send(client, request, *args, **kwargs)
    except BaseException as error:
        emit(error)
        raise
    emit(status=response.status_code)
    return response
httpx.AsyncClient.send = send
import uvicorn
uvicorn.run("app.main:app",host="0.0.0.0",port=8000,access_log=False,timeout_graceful_shutdown=3)
"""


class ForwardedIdentityStack(IdentityModuleStack):
    """Keep existing owned assembly checks and restore only the original DB route."""

    def __init__(self, *args, trace=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.trace_enabled = trace
        port = self.test_engine.url.port
        assert port and self.test_engine.url.host in {"127.0.0.1", "localhost"}
        self._database_urls = {
            kind: url.set(host="host.docker.internal", port=port)
            for kind, url in self._database_urls.items()
        }
        self._api_database_url = self._api_database_url.set(
            host="host.docker.internal", port=port
        )

    def _write_override_file(self):
        super()._write_override_file()
        if not self.trace_enabled:
            return
        value = self._override_file.read_text()
        owner = "  identity-admission:\n    image: ${AAP_IDENTITY_RUNTIME_IMAGE:?}\n"
        api = '    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--timeout-graceful-shutdown", "3"]'
        assert value.count(owner) == value.count(api) == 1
        value = value.replace(
            owner,
            owner + "    command: " + json.dumps(["python", "-c", OWNER_TRACE]) + "\n",
        ).replace(api, "    command: " + json.dumps(["python", "-c", API_TRACE]))
        self._override_file.write_text(value)

    def trace_records(self):
        records = []
        for service, details in self._verify_project_ownership().items():
            if service not in {"identity-admission", "identity-api"}:
                continue
            output = self._docker(
                ["logs", details["Id"]], operation="sanitized RPC trace"
            )
            for line in output.splitlines():
                if line.startswith(TRACE_PREFIX):
                    records.append(
                        {"service": service, **json.loads(line[len(TRACE_PREFIX) :])}
                    )
        return records
