"""Choose the audience's S3 origin before signing, using the admitted client."""

from contextvars import ContextVar
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
import weakref

from app.config import settings

Audience = Literal["browser", "internal", "ml"]
_target: ContextVar[tuple[object, str] | None] = ContextVar(
    "storage_presigning_target", default=None
)


def install(client):
    """Install once at client construction; physical requests keep their origin."""
    reference = weakref.ref(client)

    def before_sign(request, **_kwargs):
        target = _target.get()
        if (
            target is None
            or target[0] is not reference()
            or not request.context.get("is_presign_request")
        ):
            return
        origin = urlsplit(target[1])
        current = urlsplit(request.url)
        internal = urlsplit(reference().meta.endpoint_url)
        if current.netloc != internal.netloc:
            raise ValueError("Audience signing requires path-style S3 addressing")
        request.url = urlunsplit(
            (origin.scheme, origin.netloc, current.path, current.query, "")
        )

    client.meta.events.register(
        "before-sign.s3", before_sign, unique_id="aap-storage-presigning"
    )


def _absolute(value):
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(ord(char) <= 32 for char in value)
        ):
            raise ValueError
        parsed.port
        return parsed
    except ValueError:
        raise ValueError("Invalid object storage access endpoint") from None


def generate_url(client, operation, *, params, expires_in, audience: Audience):
    """Sign the effective S3 request; proxy prefixes are transport routes only.

    Relative browser routes restore the internal Host and strip their prefix.
    Absolute browser routes preserve the public Host and strip any prefix.
    This function neither creates a client nor performs network I/O.
    """
    origin = None
    prefix = ""
    relative = False
    if audience == "browser":
        public = settings.minio_public_url.rstrip("/")
        if public.startswith("/"):
            route = urlsplit(public)
            if route.netloc or route.query or route.fragment:
                raise ValueError("Invalid object storage proxy route")
            prefix, relative = route.path, True
            origin = client.meta.endpoint_url
        elif public:
            route = _absolute(public)
            origin = urlunsplit((route.scheme, route.netloc, "", "", ""))
            prefix = route.path
    elif audience == "ml":
        if settings.ml_backend_storage_host:
            internal = _absolute(client.meta.endpoint_url)
            route = _absolute(f"{internal.scheme}://{settings.ml_backend_storage_host}")
            if route.path not in {"", "/"}:
                raise ValueError("ML storage host must contain only host and port")
            origin = urlunsplit((route.scheme, route.netloc, "", "", ""))
    elif audience != "internal":
        raise ValueError("Unknown object storage URL audience")

    token = _target.set((client, origin) if origin else None)
    try:
        signed = client.generate_presigned_url(
            operation, Params=params, ExpiresIn=expires_in
        )
    finally:
        _target.reset(token)
    if not prefix:
        return signed
    parsed = urlsplit(signed)
    return urlunsplit(
        (
            "" if relative else parsed.scheme,
            "" if relative else parsed.netloc,
            prefix + parsed.path,
            parsed.query,
            "",
        )
    )
