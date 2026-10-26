"""Ownership facts for acceptance tests that must touch an actual provider.

These tests have to observe real bytes, versions and delete markers, so they
cannot be satisfied by an in-memory substitute. They may only write where this
run provably owns the provider. Two disposable environments qualify:

* the worktree launcher's owned ``test`` namespace: ``AAP_WORKTREE_MODE=test``
  with ``aap-wt-<identity>-test-<purpose>`` buckets and a per-checkout Redis;
* the GitHub Actions backend job, whose containerised Postgres, MinIO and Redis
  services exist only for that hosted runner and are destroyed with it.

A developer machine, a self-hosted runner or any non-loopback endpoint is
refused with an assertion: the acceptance case must always run where the bytes
are disposable, never silently skip its versioning or accounting evidence.
"""

import os
import re
from urllib.parse import urlsplit

from app.config import settings

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_WORKTREE_BUCKET = re.compile(
    r"aap-wt-[0-9a-f]{16}-test-(?P<purpose>[a-z][a-z0-9-]*)"
)


def _host_of(endpoint: str) -> str:
    """Return the host of ``host:port`` or ``scheme://host:port`` without I/O."""
    parsed = urlsplit(endpoint if "://" in endpoint else f"//{endpoint}")
    return parsed.hostname or ""


def _disposable_owner() -> str:
    """Return ``worktree`` or ``github``; refuse every other environment."""
    mode = os.environ.get("AAP_WORKTREE_MODE")
    if mode == "test":
        return "worktree"
    assert mode is None, (
        "Actual-provider acceptance runs only in the owned worktree test "
        f"namespace, never AAP_WORKTREE_MODE={mode!r}"
    )
    facts = (
        os.environ.get("GITHUB_ACTIONS"),
        os.environ.get("RUNNER_ENVIRONMENT"),
        os.environ.get("GITHUB_RUN_ID"),
    )
    assert facts[0] == "true" and facts[1] == "github-hosted", (
        "Actual-provider acceptance runs only on a disposable GitHub hosted "
        "runner (GITHUB_ACTIONS=true, RUNNER_ENVIRONMENT=github-hosted), not "
        "on a developer machine or self-hosted runner"
    )
    assert re.fullmatch(r"\d+", facts[2] or ""), (
        "Actual-provider acceptance requires the real hosted run identity "
        "(GITHUB_RUN_ID)"
    )
    return "github"


def _configured_buckets() -> frozenset[str]:
    """The application buckets this run's provider is expected to own."""
    return frozenset(
        {
            settings.minio_bucket,
            settings.minio_datasets_bucket,
            settings.minio_bug_reports_bucket,
            settings.minio_media_cache_bucket,
            settings.minio_audit_archive_bucket,
            settings.minio_import_bucket,
            settings.minio_export_bucket,
            settings.minio_avatars_bucket,
            settings.minio_training_bucket,
        }
    )


def owned_object_store() -> str:
    """Assert the configured MinIO belongs to this run; return its owner."""
    owner = _disposable_owner()
    host = _host_of(settings.minio_endpoint)
    assert host in _LOOPBACK_HOSTS, (
        "Actual-provider acceptance writes only to the loopback MinIO of the "
        f"disposable {owner} environment; refusing remote object store"
    )
    return owner


def owned_bucket(bucket: str, *, purpose: str) -> str:
    """Assert ``bucket`` is this run's own object-store namespace for ``purpose``."""
    owner = owned_object_store()
    if owner == "worktree":
        match = _WORKTREE_BUCKET.fullmatch(bucket)
        assert match is not None and match.group("purpose") == purpose, (
            f"{bucket!r} is not this checkout's owned test bucket for {purpose!r}"
        )
    else:
        assert bucket in _configured_buckets(), (
            f"{bucket!r} is not one of the job's configured application "
            f"buckets for {purpose!r}"
        )
    return bucket


def owned_redis() -> str:
    """Assert the configured Redis belongs to this run; return its owner."""
    owner = _disposable_owner()
    parsed = urlsplit(settings.redis_url)
    assert (
        parsed.scheme in {"redis", "rediss"}
        and parsed.hostname in _LOOPBACK_HOSTS
    ), (
        "Actual-provider acceptance uses only the loopback Redis of the "
        f"disposable {owner} environment"
    )
    return owner
