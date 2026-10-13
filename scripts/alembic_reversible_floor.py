"""Report the newest revision automation may downgrade to — fail-closed.

The CI round-trip check (upgrade -> downgrade -> upgrade) must never execute the
``downgrade()`` of a migration marked ``IRREVERSIBLE = True`` (for example
``0174_project_role_conversion``: the employee cutover cannot be losslessly
mapped back to a single global role).

``classify_chain`` walks the revision graph from the single head towards base
and returns the highest revision whose *entire* ancestry is reversible.  That
value is the revision immediately below the unique irreversible revision.  The
caller must start the reversible round trip *at that revision*
(``alembic upgrade <floor>`` then ``alembic downgrade base``) — it must **not**
``alembic stamp <floor>``: stamp only rewrites the version marker and never
executes the real schema/data rollback.

The policy is deliberately fail-closed.  A chain with multiple heads, a merge
revision, an orphan/branch revision, an unknown parent, a cycle, more than one
irreversible revision, an irreversible revision at the base, or any reversible
revision above the unique irreversible revision raises ``UnsupportedChain``.

The last shape is not impossible to validate: ``head -> <irreversible>`` is
itself a valid reversible segment.  This runner simply does not implement
post-irreversible suffix validation yet, so it refuses the shape rather than
silently validating only the prefix below the irreversible revision.  When every
migration is reversible the floor is the current head, keeping the round trip
identical to a plain ``downgrade base``.

The frozen stdout contract (a single line with the floor revision) is preserved
for existing callers; ``--json`` additionally emits the full policy.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import load_python_file


class UnsupportedChain(RuntimeError):
    """The revision graph cannot be expressed as one reversible segment."""


@dataclass(frozen=True)
class Revision:
    revision: str
    down_revision: str | tuple[str, ...] | None
    irreversible: bool


@dataclass(frozen=True)
class ChainPolicy:
    head: str
    reversible_floor: str
    irreversible: tuple[str, ...]
    reversible_segment: tuple[str, ...]


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
    """Return the reversible floor for a single-head, single-parent chain."""
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

    irreversible = tuple(r.revision for r in order if r.irreversible)
    if len(irreversible) > 1:
        raise UnsupportedChain(
            "multiple irreversible revisions "
            f"({', '.join(irreversible)}); no single reversible segment reaches "
            "base without executing one of their downgrades"
        )

    if not irreversible:
        floor = head
    else:
        if order[0].revision != irreversible[0]:
            suffix = [r.revision for r in order if r.revision != irreversible[0]]
            raise UnsupportedChain(
                "reversible revisions sit above irreversible "
                f"{irreversible[0]!r} ({', '.join(suffix[:5])}...); post-irreversible "
                "suffix validation is not implemented, so this chain shape is "
                "unsupported"
            )
        parent = _single_parent(order[0])
        if parent is None:
            raise UnsupportedChain(
                f"irreversible revision {order[0].revision!r} sits at base; there "
                "is no reversible segment to validate"
            )
        floor = parent

    floor_index = [r.revision for r in order].index(floor)
    segment = tuple(r.revision for r in order[floor_index:])
    return ChainPolicy(
        head=head,
        reversible_floor=floor,
        irreversible=irreversible,
        reversible_segment=segment,
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
        directory, filename = os.path.split(revision.path)
        loaded = load_python_file(directory, filename)
        module = getattr(loaded, "module", loaded)
        revisions[revision.revision] = Revision(
            revision=revision.revision,
            down_revision=revision.down_revision,
            irreversible=bool(getattr(module, "IRREVERSIBLE", False)),
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
        print(f"alembic-reversible-floor: {error}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(asdict(policy), ensure_ascii=False, sort_keys=True))
    else:
        print(policy.reversible_floor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
