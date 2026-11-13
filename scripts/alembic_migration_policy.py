"""Report the complete linear forward-upgrade path, failing closed on ambiguity.

Beta migration qualification executes real upgrades only. Irreversible markers
and historical downgrade implementations do not determine whether an upgrade
chain is supported; database downgrade qualification is deferred to stable.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


class UnsupportedChain(RuntimeError):
    """The revision graph cannot be expressed as the supported linear segments."""


@dataclass(frozen=True)
class Revision:
    revision: str
    down_revision: str | tuple[str, ...] | None


@dataclass(frozen=True)
class ChainPolicy:
    head: str
    upgrade_path: tuple[str, ...]


def _single_parent(revision: Revision) -> str | None:
    parent = revision.down_revision
    if parent is None:
        return None
    if isinstance(parent, (list, tuple)):
        if len(parent) != 1:
            raise UnsupportedChain(
                f"revision {revision.revision} has a merged down_revision "
                f"({len(parent)} parents); branch graphs are not supported"
            )
        return parent[0]
    return parent


def classify_chain(revisions: dict[str, Revision], head: str) -> ChainPolicy:
    """Return every revision in base-to-head upgrade order."""
    if head not in revisions:
        raise UnsupportedChain(f"head {head!r} is not in the revision map")

    order: list[Revision] = []
    seen: set[str] = set()
    current: str | None = head
    while current is not None:
        if current in seen:
            raise UnsupportedChain(f"cycle detected at revision {current!r}")
        seen.add(current)
        revision = revisions.get(current)
        if revision is None:
            raise UnsupportedChain(
                f"revision {current!r} is referenced but missing from the graph"
            )
        order.append(revision)
        current = _single_parent(revision)

    orphans = sorted(set(revisions) - seen)
    if orphans:
        raise UnsupportedChain(
            "revision graph has unreachable/branch revisions: " + ", ".join(orphans)
        )

    return ChainPolicy(
        head=head,
        upgrade_path=tuple(revision.revision for revision in reversed(order)),
    )


def require_single_head(heads: list[str]) -> str:
    if len(heads) != 1:
        raise UnsupportedChain(
            f"expected exactly one head, found {len(heads)}: {', '.join(sorted(heads))}"
        )
    return heads[0]


def load_revisions(config: Config) -> tuple[dict[str, Revision], str]:
    script = ScriptDirectory.from_config(config)
    head = require_single_head(list(script.get_heads()))
    revisions: dict[str, Revision] = {}
    for revision in script.walk_revisions():  # head -> base
        revisions[revision.revision] = Revision(
            revision=revision.revision,
            down_revision=revision.down_revision,
        )
    return revisions, head


def build_config() -> Config:
    script_dir = Path(__file__).resolve().parent.parent / "apps" / "api"
    config = Config(str(script_dir / "alembic.ini"))
    config.set_main_option("script_location", str(script_dir / "alembic"))
    return config


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in arguments
    try:
        policy = classify_chain(*load_revisions(build_config()))
    except UnsupportedChain as error:
        print(f"alembic-migration-policy: {error}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(asdict(policy), ensure_ascii=False, sort_keys=True))
    else:
        print(policy.head)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
