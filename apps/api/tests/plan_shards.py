"""Build the four-shard pytest plan from a complete run, without contacting CI."""

import argparse
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import re
import sys
from xml.etree import ElementTree

from tests.pytest_sharding import shard_index_for_nodeid


_UUID4 = re.compile(
    r"\b[0-9a-f]{8}-?[0-9a-f]{4}-?4[0-9a-f]{3}-?[89ab][0-9a-f]{3}-?[0-9a-f]{12}\b"
)


def _parameter_shape(key):
    classname, name = key
    function, bracket, params = name.partition("[")
    return classname, function + bracket + _UUID4.sub("<uuid4>", params)


def module_times(collection, reports):
    expected = set()
    modules = {}
    for nodeid in collection.read_text().splitlines():
        head, bracket, params = nodeid.partition("[")
        module, *names = head.split("::")
        if names and module.startswith("tests/") and module.endswith(".py"):
            classname = ".".join([module[:-3].replace("/", "."), *names[:-1]])
            modules[classname] = module
            expected.add((classname, names[-1] + bracket + params))
    seconds = Counter()
    observed = set()
    seen = {}
    for index, report in enumerate(reports):
        cases = set()
        for case in ElementTree.parse(report).iter("testcase"):
            classname, name = case.get("classname", ""), case.attrib["name"]
            if classname not in modules:
                raise ValueError(
                    f"{report}: no collected class for {classname}::{name}"
                )
            module = modules[classname]
            if seen.setdefault(module, index) != index:
                raise ValueError(f"{module} occurs in more than one report")
            cases.add((classname, name))
            seconds[module] += Decimal(case.attrib["time"])
        if not cases:
            raise ValueError(f"{report}: no test cases")
        observed.update(cases)
    missing, extra = expected - observed, observed - expected
    if missing or extra:
        left = Counter(map(_parameter_shape, missing))
        right = Counter(map(_parameter_shape, extra))
        if left != right or any(n != 1 for n in left.values()):
            raise ValueError(
                f"JUnit cases differ from collection: {len(missing)} missing, {len(extra)} extra"
            )
        print(
            f"{len(missing)} parameter IDs differ only by generated UUIDs",
            file=sys.stderr,
        )
    return seconds


def allocate(seconds, count, longest):
    heavy = sorted(seconds, key=lambda module: (-seconds[module], module))[:longest]
    loads = [Decimal(0)] * count
    for module in seconds.keys() - set(heavy):
        loads[shard_index_for_nodeid(module, count)] += seconds[module]
    moves = [[] for _ in range(count)]
    for module in heavy:
        target = min(range(count), key=loads.__getitem__)
        loads[target] += seconds[module]
        if target != shard_index_for_nodeid(module, count):
            moves[target].append(module)
    return moves, loads


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="URL of the measured attempt")
    parser.add_argument(
        "--collection",
        required=True,
        type=Path,
        help="pytest -o addopts= --collect-only -q output from the measured revision",
    )
    parser.add_argument(
        "--longest",
        required=True,
        type=int,
        help="number of slow modules to redistribute",
    )
    parser.add_argument(
        "reports",
        nargs=4,
        type=Path,
        metavar="JUNIT",
        help="four pytest-results.xml files, in shard order",
    )
    args = parser.parse_args()
    if args.longest < 1:
        parser.error("--longest must be positive")
    times = module_times(args.collection, args.reports)
    moves, loads = allocate(times, len(args.reports), args.longest)
    plan = {"source": args.source, "longest": args.longest, "shards": moves}
    print(json.dumps(plan, indent=2))
    print("Estimated JUnit seconds:", *(round(s) for s in loads), file=sys.stderr)
