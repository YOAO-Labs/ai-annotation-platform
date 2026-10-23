"""Shared selection semantics for inventories and accepted-path impact review."""

import fnmatch

RULE_KEYS = frozenset({"recursive", "include_globs", "exclude_globs"})


def locator(config):
    return {key: value for key, value in config.items() if key not in RULE_KEYS}


def excluded(config, path):
    return any(
        fnmatch.fnmatchcase(path, pattern)
        for pattern in config.get("exclude_globs", [])
    )


def descend(config, path):
    return config["recursive"] and not excluded(config, path)


def selected_file(config, path, backend):
    if not config["recursive"] and "/" in path:
        return False
    if excluded(config, path):
        return False
    if backend != "s3":
        parts = path.split("/")
        if any(
            excluded(config, "/".join(parts[:index])) for index in range(1, len(parts))
        ):
            return False
    return any(
        fnmatch.fnmatchcase(path, pattern) for pattern in config["include_globs"]
    )
