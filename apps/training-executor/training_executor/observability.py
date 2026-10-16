"""Optional bounded observation transport; it never owns cancellation or leases."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

import httpx
import psutil
from training_contract.observability import (
    LOG_LIMIT,
    METRICS,
    PHASES,
    REQUEST_BYTES,
    SAMPLE_LIMIT,
    SUMMARY_BYTES,
    encoded,
    eta_basis,
    measurement,
    utc_now,
    validate_sample,
)

from .files import atomic_json


class ObservationSpool:
    """One serialized SQLite connection and durable, immutable pending requests."""

    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA page_size=4096")
        self.db.execute("PRAGMA max_page_count=2048")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        for name in ("telemetry", "logs"):
            self.db.execute(
                f"CREATE TABLE IF NOT EXISTS {name} (sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
            )
        if self.get("stream_id") is None:
            self.set("stream_id", str(uuid.uuid4()))
        self.db.commit()

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute(
                "SELECT value FROM metadata WHERE key=?", (key,)
            ).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute(
            "INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, encoded(value).decode()),
        )

    def append(self, stream, payload):
        if stream not in {"telemetry", "logs"}:
            raise ValueError("Unknown observation stream")
        with self.lock:
            self._reserve()
            with self.db:
                sequence = self.get(stream + "_sequence", 0) + 1
                item = {**payload, "sequence": sequence}
                keep = SAMPLE_LIMIT if stream == "telemetry" else LOG_LIMIT
                self.db.execute(
                    f"DELETE FROM {stream} WHERE sequence<=?", (sequence - keep,)
                )
                self.db.execute(
                    f"INSERT INTO {stream} VALUES (?,?)",
                    (sequence, encoded(item).decode()),
                )
                self.set(stream + "_sequence", sequence)
                return item

    def _reserve(self):
        # Leave room for the durable outbox before SQLITE_FULL. A shortened
        # window is explicitly reported through ACK/earliest-sequence gaps.
        maximum = self.db.execute("PRAGMA max_page_count").fetchone()[0]
        reserve = min(128, maximum // 4)
        while True:
            used = (
                self.db.execute("PRAGMA page_count").fetchone()[0]
                - self.db.execute("PRAGMA freelist_count").fetchone()[0]
            )
            if used <= maximum - reserve:
                return
            removed = 0
            with self.db:
                for stream in ("telemetry", "logs"):
                    removed += self.db.execute(
                        f"DELETE FROM {stream} WHERE sequence IN (SELECT sequence FROM {stream} WHERE sequence < (SELECT MAX(sequence) FROM {stream}) ORDER BY sequence LIMIT 64)"
                    ).rowcount
            if not removed:
                raise sqlite3.OperationalError("Observation spool quota is unavailable")

    def recent(self, stream, limit):
        with self.lock:
            return [
                json.loads(row[0])
                for row in self.db.execute(
                    f"SELECT payload FROM (SELECT sequence,payload FROM {stream} ORDER BY sequence DESC LIMIT ?) ORDER BY sequence",
                    (limit,),
                )
            ]

    def seal(self, fence, producer_boot_id, summary):
        with self.lock, self.db:
            pending = self.get("pending")
            if pending is not None:
                return pending
            self._reserve()
            body = {
                **fence,
                "stream_id": self.get("stream_id"),
                "request_id": str(uuid.uuid4()),
                "producer_boot_id": producer_boot_id,
                "telemetry": [],
                "logs": [],
                "skipped": {},
                "summary": summary,
            }
            for stream, limit in (("telemetry", 12), ("logs", 32)):
                acknowledged = self.get(stream + "_ack", 0)
                earliest = self.db.execute(
                    f"SELECT MIN(sequence) FROM {stream}"
                ).fetchone()[0]
                if earliest is not None and earliest > acknowledged + 1:
                    body["skipped"][stream] = {
                        "after": acknowledged,
                        "through": earliest - 1,
                    }
                candidates = [
                    json.loads(row[0])
                    for row in self.db.execute(
                        f"SELECT payload FROM {stream} WHERE sequence>? ORDER BY sequence LIMIT ?",
                        (acknowledged, limit),
                    )
                ]
                for item in candidates:
                    body[stream].append(item)
                    if len(encoded(body)) > REQUEST_BYTES - 4096:
                        body[stream].pop()
                        break
            if not body["telemetry"] and not body["logs"] and not body["skipped"]:
                return None
            if len(encoded(body)) > REQUEST_BYTES:
                raise ValueError("Observation request exceeds its bound")
            self.set("pending", body)
            return body

    def acknowledge(self, body, response):
        with self.lock, self.db:
            if (
                response.get("stream_id") != body["stream_id"]
                or response.get("request_id") != body["request_id"]
            ):
                raise ValueError(
                    "Observation ACK identity differs from the sealed batch"
                )
            if self.get("pending", {}).get("request_id") != body["request_id"]:
                raise ValueError("Observation ACK does not own the pending request")
            for stream in ("telemetry", "logs"):
                sent = max(
                    [
                        self.get(stream + "_ack", 0),
                        body["skipped"].get(stream, {}).get("through", 0),
                        *[item["sequence"] for item in body[stream]],
                    ]
                )
                if response.get(stream + "_cursor") != sent:
                    raise ValueError(
                        "Observation ACK cursor differs from the sealed batch"
                    )
                self.set(stream + "_ack", sent)
            self.set("pending", None)

    def close(self):
        with self.lock:
            self.db.close()


class NativeProgress:
    """Callback-owned receipt. No thread/CUDA context is created by observation."""

    def __init__(self, spec, output, *, clock=time.monotonic):
        self.spec, self.output, self.clock = spec, output, clock
        self.phase_name = "preparing"
        self.phase_start = self.clock()
        self.durations = {}
        self.cycles = []
        self.cycle_start = None
        self.completed_epoch = 0
        self.last_write = float("-inf")
        self.batch_start = self.clock()
        self.batch_images = 0
        self.throughput = measurement(reason="callback_unavailable")
        self.closing_epochs = None

    def phase(self, name):
        if name not in PHASES:
            raise ValueError("Unknown observed native phase")
        now = self.clock()
        if name != self.phase_name:
            self.durations[self.phase_name] = (
                self.durations.get(self.phase_name, 0) + now - self.phase_start
            )
            self.phase_name, self.phase_start = name, now
        self.write(force=True)

    def started(self, *, closing_epochs=None):
        if type(closing_epochs) is int and closing_epochs >= 0:
            self.closing_epochs = min(closing_epochs, self.spec["parameters"]["epochs"])
        self.cycle_start = self.clock()
        self.batch_start = self.clock()
        self.phase("training")

    def batch(self, images):
        if type(images) is int and images > 0:
            self.batch_images += images
        now = self.clock()
        if now - self.batch_start >= 5:
            self.throughput = measurement(self.batch_images / (now - self.batch_start))
            self.batch_images, self.batch_start = 0, now
        self.write()

    def epoch(self, epoch, *, raw_epoch_seconds=None):
        now = self.clock()
        total = self.spec["parameters"]["epochs"]
        close = self.closing_epochs or 0
        if self.cycle_start is not None:
            self.cycles.append(
                {
                    "epoch": epoch,
                    "seconds": now - self.cycle_start,
                    "comparable": bool(self.cycles),
                    "closing": bool(close and epoch > total - close),
                }
            )
            self.cycles = self.cycles[-20:]
        self.cycle_start, self.completed_epoch = now, epoch
        self.raw_epoch_seconds = raw_epoch_seconds
        self.write(force=True)

    def write(self, *, force=False):
        now = self.clock()
        if not force and now - self.last_write < 5:
            return
        # Observation failure must not interrupt a native callback or training.
        try:
            allocated = reserved = measurement(reason="cuda_uninitialized")
            torch = sys.modules.get("torch")
            if torch is not None and torch.cuda.is_initialized():
                allocated = measurement(torch.cuda.memory_allocated())
                reserved = measurement(torch.cuda.memory_reserved())
            total = self.spec["parameters"]["epochs"]
            progress = {
                "completed_epoch": self.completed_epoch,
                "total_epochs": total,
                "cycles": self.cycles,
                "phase_durations": self.durations,
                "eta": eta_basis(
                    self.cycles,
                    completed_epoch=self.completed_epoch,
                    total_epochs=total,
                    close_mosaic=self.closing_epochs or 0,
                )
                if self.closing_epochs is not None
                else {
                    "available": False,
                    "reason": "unobserved_augmentation_regime",
                    "sample_count": 0,
                },
            }
            raw = getattr(self, "raw_epoch_seconds", None)
            if type(raw) in {int, float} and raw >= 0:
                progress["native_raw"] = {"epoch_seconds": raw}
            atomic_json(
                self.output / "progress.json",
                {
                    "phase": self.phase_name,
                    "phase_elapsed_seconds": now - self.phase_start,
                    "captured_monotonic": now,
                    "captured_at": utc_now(),
                    "progress": progress,
                    "allocator": {
                        "cuda_allocated_bytes": allocated,
                        "cuda_reserved_bytes": reserved,
                    },
                    "images_per_second": self.throughput,
                },
            )
            self.last_write = now
        except Exception:
            pass


class ProcessSampler:
    def __init__(self, gpu_uuid):
        self.gpu_uuid = gpu_uuid
        self.identity = None
        self.previous = {}

    def attach(self, pid, created_at):
        self.identity = (pid, created_at)
        self.previous.clear()

    def sample(self):
        values = {name: measurement(reason="callback_unavailable") for name in METRICS}
        values["process_gpu_utilization"] = measurement(
            reason="pid_namespace_unresolved"
        )
        reason = (
            "recovery_no_process" if self.identity is None else "process_unavailable"
        )
        try:
            if self.identity is not None:
                root = psutil.Process(self.identity[0])
                if root.create_time() != self.identity[1]:
                    reason = "pid_reused"
                else:
                    now, rss, cpu, baseline = time.monotonic(), 0, 0, False
                    seen = {}
                    for process in [root, *root.children(recursive=True)]:
                        with process.oneshot():
                            identity = (process.pid, process.create_time())
                            # An unrelated process/parallel preview cannot enter this tree.
                            if os.getpgid(process.pid) != root.pid:
                                continue
                            rss += process.memory_info().rss
                            times = process.cpu_times()
                            current = times.user + times.system
                        previous = self.previous.get(identity)
                        if previous and now > previous[0]:
                            cpu += max(0, current - previous[1]) / (now - previous[0])
                            baseline = True
                        seen[identity] = (now, current)
                    self.previous = seen
                    values["rss_bytes"] = measurement(rss)
                    values["cpu_cores"] = (
                        measurement(cpu)
                        if baseline
                        else measurement(reason="baseline_pending")
                    )
        except psutil.AccessDenied:
            reason = "permission_denied"
        except (psutil.NoSuchProcess, ProcessLookupError, PermissionError):
            pass
        if values["rss_bytes"]["value"] is None:
            values["rss_bytes"] = measurement(reason=reason)
            values["cpu_cores"] = measurement(reason=reason)
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--id=" + self.gpu_uuid,
                    "--query-gpu=uuid,utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            )
            parts = [item.strip() for item in result.stdout.strip().split(",")]
            if len(parts) != 4 or parts[0] != self.gpu_uuid:
                raise ValueError("Device identity mismatch")
            for key, value in zip(
                ("gpu_utilization", "gpu_used_mib", "gpu_total_mib"),
                parts[1:],
                strict=True,
            ):
                values[key] = measurement(float(value))
                if (
                    key == "gpu_utilization"
                    and values[key]["value"] is not None
                    and values[key]["value"] > 100
                ):
                    values[key] = measurement(reason="invalid_value")
        except (OSError, ValueError, subprocess.SubprocessError):
            for key in ("gpu_utilization", "gpu_used_mib", "gpu_total_mib"):
                values[key] = measurement(reason="device_unavailable")
        return values


class ObservationSession:
    def __init__(self, supervisor, journal, directory):
        self.supervisor, self.journal, self.directory = supervisor, journal, directory
        self.sampler = ProcessSampler(supervisor.runtime["gpu_uuid"])
        self.spool = ObservationSpool(directory / "observations.sqlite")
        self.done = threading.Event()
        self.phase_name, self.phase_started = "preparing", time.monotonic()
        recent = self.spool.recent("telemetry", 1)
        self.last_sample = recent[0] if recent else None
        self.peaks = self.spool.get("peaks", {})
        self.threads = []

    def start(self):
        for name, target in (
            ("training-observation-sampler", self.collect),
            ("training-observation-transport", self.transport),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            self.threads.append(thread)
            thread.start()

    def phase(self, name):
        self.phase_name, self.phase_started = name, time.monotonic()

    def log(self, text):
        self.spool.append(
            "logs",
            {
                "text": text,
                "captured_at": utc_now(),
                "producer_boot_id": self.supervisor.boot,
            },
        )

    def summary(self):
        with self.spool.lock:
            return {
                "protocol": 1,
                "peaks": dict(self.peaks),
                "latest": self.last_sample,
            }

    def collect(self):
        while not self.done.is_set():
            try:
                metrics = self.sampler.sample()
                now = time.monotonic()
                sample = {
                    "captured_at": utc_now(),
                    "producer_boot_id": self.supervisor.boot,
                    "phase": self.phase_name,
                    "phase_elapsed_seconds": now - self.phase_started,
                    "metrics": metrics,
                    "progress": {},
                }
                if self.sampler.identity is not None and self.phase_name != "uploading":
                    path = self.directory / "progress.json"
                    if path.is_file() and path.stat().st_size <= 16 * 1024:
                        progress = json.loads(path.read_text())
                        age = now - progress["captured_monotonic"]
                        if age >= 0:
                            sample.update(
                                phase=progress["phase"],
                                phase_elapsed_seconds=progress["phase_elapsed_seconds"]
                                + age,
                                progress=progress["progress"],
                                callback_age_seconds=age,
                            )
                            for key in ("cuda_allocated_bytes", "cuda_reserved_bytes"):
                                metrics[key] = (
                                    progress["allocator"][key]
                                    if age <= 10
                                    else measurement(reason="callback_stale")
                                )
                            metrics["images_per_second"] = (
                                progress["images_per_second"]
                                if age <= 10
                                else measurement(reason="callback_stale")
                            )
                validate_sample(sample)
                with self.spool.lock, self.spool.db:
                    self.last_sample = self.spool.append("telemetry", sample)
                    for key, item in metrics.items():
                        if item["value"] is not None:
                            self.peaks[key] = max(self.peaks.get(key, 0), item["value"])
                    self.spool.set("peaks", self.peaks)
            except Exception:
                pass
            self.done.wait(5)

    def transport(self):
        with httpx.Client(
            base_url=self.supervisor.client.base_url,
            headers=self.supervisor.client.headers,
            timeout=3,
            trust_env=False,
        ) as transport:
            while not self.done.is_set():
                try:
                    body = self.spool.seal(
                        self.supervisor.fence(self.journal),
                        self.supervisor.boot,
                        self.summary(),
                    )
                    if body:
                        response = transport.post(
                            self.supervisor.endpoint(self.journal, "observations"),
                            json=body,
                        )
                        if response.status_code in {403, 404}:
                            return
                        if response.status_code == 409:
                            detail = response.json().get("detail")
                            if isinstance(detail, dict) and detail.get("code") in {
                                "stale_attempt",
                                "observation_stream_conflict",
                                "observation_request_conflict",
                                "observation_cursor_conflict",
                                "observation_cursor_retired",
                                "observation_content_conflict",
                                "observation_cursor_gap",
                            }:
                                return
                        response.raise_for_status()
                        self.spool.acknowledge(body, response.json())
                except Exception:
                    pass
                self.done.wait(1)

    def freeze(self, redaction):
        path = self.directory / "observability.json"
        if path.exists():
            return
        with self.spool.lock:
            self._freeze(path, redaction)

    def _freeze(self, path, redaction):
        body = {
            "protocol": 1,
            "run_id": self.journal["assignment"]["run_id"],
            "attempt_id": self.journal["assignment"]["attempt_id"],
            "config_digest": self.journal["assignment"]["config_digest"],
            "runtime": self.journal["claim"]["runtime"],
            "stream_id": self.spool.get("stream_id"),
            "collection_cutoff": utc_now(),
            "metric_definitions": METRICS,
            "summary": self.summary(),
            "samples": self.spool.recent("telemetry", 120),
            "redaction": redaction,
        }
        progress = self.directory / "progress.json"
        if (
            self.sampler.identity is not None
            and progress.is_file()
            and progress.stat().st_size <= 16 * 1024
        ):
            body["native_progress"] = json.loads(progress.read_text())
        while len(encoded(body)) > SUMMARY_BYTES and body["samples"]:
            body["samples"].pop(0)
        if len(encoded(body)) > SUMMARY_BYTES:
            raise ValueError("Observation summary exceeds its bound")
        atomic_json(path, body)

    def stop(self):
        self.done.set()
        for thread in self.threads:
            thread.join(timeout=6)
        if any(thread.is_alive() for thread in self.threads):
            raise RuntimeError("Observation transport did not stop")
        self.spool.close()
