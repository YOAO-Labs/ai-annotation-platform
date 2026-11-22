"""ADR-0050 §10.1 / §B.4 · smooth weighted round robin (SWRR) selection.

The only production routing policy (ADR-0050 D10). The pure function
is testable without Redis. The Redis acquire Lua mirrors this algorithm atomically
(see ``ledger.py``). Both must stay in lockstep — golden fixtures (§C.2) verify the
weight distribution and tie-break.

Algorithm (classic nginx SWRR):
1. For each eligible member: ``current_weight += configured_weight``.
2. Select the member with the largest ``current_weight``; ties broken by stable
   instance UUID (lexicographic) so the pick is deterministic across processes.
3. Selected member: ``current_weight -= total_eligible_weight``.
4. Members at max concurrency are excluded BEFORE weight advancement (they don't
   accrue weight while saturated — §10.1).
5. On membership/weight generation change, ``current_weight`` is reset for the new
   member set (the acquire Lua handles this via the generation check).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SWRRState:
    """Per-pool SWRR state (mirrors the Redis ``pool:state`` hash ``current_weight`` map).

    ``current_weights`` maps instance UUID hex → running weight. Reset when the
    eligible member set changes (generation bump).
    """

    current_weights: dict[str, int] = field(default_factory=dict)
    generation: int = 1


def select_swrr(
    candidates: list[tuple[str, int]],
    state: SWRRState,
    *,
    eligible_instance_ids: set[str] | None = None,
) -> str | None:
    """Pick one instance by smooth weighted round robin.

    Args:
        candidates: all configured members of the pool (instance hex, weight).
        state: per-pool SWRR state (mutated in place: current_weights advanced/selected).
        eligible_instance_ids: if provided, only these instances participate (others
            are excluded e.g. at max concurrency / circuit open). None = all eligible.

    Returns:
        Selected instance UUID hex, or None if no eligible candidate.

    The caller is responsible for resetting ``state.current_weights`` when the member
    set changes (generation bump); this function prunes stale entries lazily.
    """
    eligible = [
        (iid, w)
        for (iid, w) in candidates
        if eligible_instance_ids is None or iid in eligible_instance_ids
    ]
    if not eligible:
        return None

    live_ids = {iid for iid, _ in eligible}
    for stale in list(state.current_weights):
        if stale not in live_ids:
            del state.current_weights[stale]

    total_weight = sum(w for _, w in eligible)
    if total_weight <= 0:
        return min(iid for iid, _ in eligible)

    for iid, w in eligible:
        state.current_weights[iid] = state.current_weights.get(iid, 0) + w

    winner = max(
        eligible,
        key=lambda iw: (
            state.current_weights.get(iw[0], 0),
            _invert_uuid_for_min(iw[0]),
        ),
    )
    top_weight = state.current_weights[winner[0]]
    tied = sorted(
        iid for iid, _ in eligible if state.current_weights.get(iid, 0) == top_weight
    )
    winner_id = tied[0]

    state.current_weights[winner_id] = (
        state.current_weights.get(winner_id, 0) - total_weight
    )
    return winner_id


def _invert_uuid_for_min(uuid_hex: str) -> int:
    """Helper so that max(key=(weight, invert(uuid))) breaks ties toward smaller UUID.

    Maps the UUID's leading 64 bits to its bitwise-not value; smaller UUID → larger
    inverted value → wins under max(). Used only as a tie-break secondary sort key.
    """
    prefix = uuid_hex[:16].ljust(16, "0")
    try:
        val = int(prefix, 16)
    except ValueError:
        return 0
    return (~val) & ((1 << 64) - 1)


def expected_distribution(
    candidates: list[tuple[str, int]], draws: int
) -> dict[str, float]:
    """Theoretical per-member share of ``draws`` selections under SWRR.

    For golden-test assertions (§C.2): SWRR with integer weights reproduces the weight
    ratio exactly over one full cycle of sum(weights) draws. Returns expected fraction
    per instance.
    """
    total = sum(w for _, w in candidates)
    if total <= 0:
        n = len(candidates)
        return {iid: (1.0 / n if n else 0.0) for iid, _ in candidates}
    return {iid: w / total for iid, w in candidates}
