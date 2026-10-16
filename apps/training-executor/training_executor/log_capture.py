"""Drain a native pipe without network backpressure; persist only bounded redacted text."""

from __future__ import annotations

import os
import threading

from training_contract.observability import (
    LOG_BYTES,
    REDACTION_REVISION,
    RedactedLines,
    utf8_chunks,
)


class LogCapture:
    def __init__(self, directory, *, secrets=(), emit=None):
        self.directory, self.emit = directory, emit
        self.redactor = RedactedLines(secrets)
        self.head = directory / ".log-head"
        self.tail = directory / ".log-tail"
        self.older = directory / ".log-tail-old"
        self.omission = directory / ".log-middle-omitted"
        self.half = LOG_BYTES // 2 - 128
        self.segment = self.half // 2
        self.recovered = self.head.exists()
        self.total_bytes = sum(
            path.stat().st_size
            for path in (self.head, self.tail, self.older)
            if path.exists()
        )
        self.dropped_chunks = 0
        self.thread = None
        self.error = None
        self.head_complete = self.tail.exists() and self.tail.stat().st_size > 0
        self.truncated = self.omission.exists()
        for path in (self.head, self.tail):
            if not path.exists():
                path.touch(mode=0o600)

    def write(self, data, *, final=False):
        for line in self.redactor.feed(data, final=final):
            for chunk in utf8_chunks(line):
                content = chunk.encode()
                self.total_bytes += len(content)
                target = self.head
                if (
                    self.head_complete
                    or self.head.stat().st_size + len(content) > self.half
                ):
                    self.head_complete = True
                    target = self.tail
                    if self.tail.stat().st_size + len(content) > self.segment:
                        self.truncated = self.truncated or self.older.exists()
                        if self.truncated:
                            self.omission.touch(mode=0o600)
                        os.replace(self.tail, self.older)
                        self.tail.touch(mode=0o600)
                with target.open("ab") as output:
                    output.write(content)
                if self.emit:
                    try:
                        self.emit(chunk)
                    except Exception:
                        self.dropped_chunks += 1

    def start(self, pipe):
        def drain():
            try:
                while True:
                    chunk = os.read(pipe.fileno(), 4096)
                    if not chunk:
                        break
                    try:
                        self.write(chunk)
                    except Exception as exc:
                        # Keep draining on a failed optional spool/disk write;
                        # native stdout must never block behind observation.
                        self.error = type(exc).__name__
                self.write(b"", final=True)
            except Exception as exc:
                self.error = type(exc).__name__
            finally:
                pipe.close()

        self.thread = threading.Thread(
            target=drain, name="training-log-drain", daemon=True
        )
        self.thread.start()

    def finish(self):
        if self.thread:
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                raise RuntimeError("Native log pipe has not reached EOF")
        else:
            self.write(b"", final=True)
        path = self.directory / "train.log"
        with path.open("wb") as target:
            target.write(self.head.read_bytes())
            if self.truncated:
                target.write(b"\n[bounded native log: middle content omitted]\n")
            if self.older.exists():
                target.write(self.older.read_bytes())
            target.write(self.tail.read_bytes())
            target.flush()
            os.fsync(target.fileno())
        path.chmod(0o600)
        return {
            "revision": REDACTION_REVISION,
            "dropped_lines": self.redactor.dropped_lines,
            "dropped_chunks": self.dropped_chunks,
            "captured_redacted_bytes": self.total_bytes,
            "artifact_bytes": path.stat().st_size,
            "truncated": self.truncated,
            "drain_error": self.error,
            "recovered_segments": self.recovered,
            "pre_restart_drop_counts_available": not self.recovered,
        }
