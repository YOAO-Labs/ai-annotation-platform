"""Saved target responsibilities, separate from source lineage and live bindings."""


def confirmation_counts(counts):
    """Global unknown history is coverage, never a selected-location dependency."""
    return {
        key: int(value) for key, value in counts.items() if key != "unlocated_targets"
    }
