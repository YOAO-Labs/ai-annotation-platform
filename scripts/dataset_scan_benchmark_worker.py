"""Observability-only bootstrap for the disposable directory scan benchmark."""

from collections import Counter
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))


class ObservedEntry:
    def __init__(self, entry, counts):
        self.entry, self.counts = entry, counts

    def __getattr__(self, name):
        return getattr(self.entry, name)

    def stat(self, **kwargs):
        self.counts["entry_stat"] += 1
        return self.entry.stat(**kwargs)


class ObservedIterator:
    def __init__(self, iterator, counts):
        self.iterator, self.counts = iterator, counts

    def __iter__(self):
        return self

    def __next__(self):
        entry = next(self.iterator)
        self.counts["entries_yielded"] += 1
        return ObservedEntry(entry, self.counts)

    def close(self):
        return self.iterator.close()


class ObservedOS:
    def __init__(self, counts):
        self.counts = counts

    def __getattr__(self, name):
        return getattr(os, name)

    def scandir(self, descriptor):
        self.counts["directory_enumerations"] += 1
        return ObservedIterator(os.scandir(descriptor), self.counts)


def install(directory, *, sync=False):
    import stat
    from celery.signals import task_prerun, task_postrun
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from app.services import filesystem_storage as fs, dataset_scans as scans

    counts, sql, traversals, publications = Counter(), Counter(), [], []
    active = False
    task_started = 0
    names = {"app.workers.dataset_scan.run_dataset_scan"}
    if sync:
        names.update(
            {
                "app.workers.dataset_registration.run_dataset_registration",
                "app.workers.dataset_source_sync.advance_source_sync",
            }
        )
    original_scan, original_open, original_apply = (
        fs.scan_directory,
        fs._open_beneath,
        scans.apply_directory,
    )

    @task_prerun.connect(weak=False)
    def begin(sender=None, **kwargs):
        nonlocal active, task_started
        active = sender.name in names
        task_started = time.monotonic()
        counts.clear()
        sql.clear()
        traversals.clear()
        publications.clear()

    @event.listens_for(Engine, "before_cursor_execute")
    def before(connection, cursor, statement, parameters, context, many):
        if active:
            context.scan_benchmark_started = time.monotonic()

    @event.listens_for(Engine, "after_cursor_execute")
    def after(connection, cursor, statement, parameters, context, many):
        if active:
            sql["statements"] += 1
            sql["seconds"] += time.monotonic() - context.scan_benchmark_started

    fs.os = ObservedOS(counts)

    def opened(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            counts["regular_file_opens"] += 1
        return descriptor

    def scanned(*args, **kwargs):
        started = time.monotonic()
        try:
            return original_scan(*args, **kwargs)
        finally:
            if active:
                traversals.append(time.monotonic() - started)

    async def applied(*args, **kwargs):
        started = time.monotonic()
        try:
            return await original_apply(*args, **kwargs)
        finally:
            if active:
                publications.append(
                    {
                        "phase": args[4]["phase"],
                        "path_digest": args[4]["path_digest"],
                        "seconds": time.monotonic() - started,
                    }
                )

    fs._open_beneath, fs.scan_directory, scans.apply_directory = (
        opened,
        scanned,
        applied,
    )

    if sync:
        from app.services import reference_storage
        from dataset_read_benchmark_api import ObservedBody

        original_content = reference_storage.content_file

        class SyncBody(ObservedBody):
            def __init__(self, body, category):
                super().__init__(body, counts)
                self.category = category

            def read(self, *args):
                value = super().read(*args)
                counts[self.category + "_bytes"] += len(value)
                return value

        @contextmanager
        def content(*args, **kwargs):
            with original_content(*args, **kwargs) as (body, observation):
                if active:
                    category = (
                        "added" if Path(args[2]).name.startswith("new-") else "baseline"
                    )
                    counts["source_content_sessions"] += 1
                    counts[category + "_sessions"] += 1
                    body = SyncBody(body, category)
                yield body, observation

        reference_storage.content_file = content

    @task_postrun.connect(weak=False)
    def finish(sender=None, task_id=None, **kwargs):
        nonlocal active
        if not active:
            return
        active = False
        for key in [
            "regular_file_opens",
            "directory_enumerations",
            "entry_stat",
            "entries_yielded",
        ]:
            counts.setdefault(key, 0)
        values = sorted(traversals)
        data = {
            "task_name": sender.name,
            "started_monotonic": task_started,
            "settled_monotonic": time.monotonic(),
            "filesystem": dict(counts),
            "sql": dict(sql),
            "directory_scan_seconds": {
                "count": len(values),
                "p50": statistics.median(values) if values else 0,
                "p95": values[max(0, math.ceil(len(values) * 0.95) - 1)]
                if values
                else 0,
                "max": max(values, default=0),
            },
            "directory_application_attempts": publications,
        }
        temporary = directory / f"{task_id}.pending"
        temporary.write_text(json.dumps(data))
        temporary.replace(directory / f"{task_id}.json")


if __name__ == "__main__":
    from benchmark_dataset_scan import verify_worker_environment

    verify_worker_environment()
    metrics, node, queue, *mode = sys.argv[1:]
    if mode not in ([], ["sync"]):
        raise ValueError("Unknown measurement mode")
    install(Path(metrics), sync=bool(mode))
    from app.workers.celery_app import celery_app

    celery_app.worker_main(
        [
            "worker",
            "--pool=solo",
            "--concurrency=1",
            "--loglevel=warning",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
            "-Q",
            queue,
            "--hostname",
            node,
        ]
    )
