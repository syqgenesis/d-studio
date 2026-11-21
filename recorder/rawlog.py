"""Thread-safe JSONL event writer.

Every record gets ``type``, ``timestamp`` (wall ms), ``seq`` (monotonic) and
``osTime``; ``seq``/``osTime`` let downstream code dedupe, detect dropped
samples and recover ordering without trusting the wall clock.
"""
from __future__ import annotations

import json
import threading
from typing import Any, Dict

from .clock import os_time_ms, wall_ms


class EventLog:
    def __init__(self, path: str, flush_every: int = 64):
        self._fh = open(path, "w", encoding="utf-8", buffering=1 << 20)
        self._lock = threading.Lock()
        self._seq = 0
        self._pending = 0
        self._flush_every = flush_every
        self._closed = False
        self.late_events = 0   # emitted after close(): dropped instead of crashing a thread

    def emit(self, etype: str, **fields: Any) -> Dict[str, Any]:
        with self._lock:
            if self._closed:
                self.late_events += 1
                return {"type": etype, **fields}
            self._seq += 1
            rec: Dict[str, Any] = {"type": etype}
            rec.update(fields)
            rec["timestamp"] = fields.get("timestamp", wall_ms())
            rec["seq"] = self._seq
            rec["osTime"] = fields.get("osTime", os_time_ms())
            self._fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
            self._fh.write("\n")
            self._pending += 1
            if self._pending >= self._flush_every:
                self._fh.flush()
                self._pending = 0
            return rec

    def emit_record(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        return self.emit(rec["type"], **{k: v for k, v in rec.items() if k != "type"})

    @property
    def count(self) -> int:
        return self._seq

    @property
    def closed(self) -> bool:
        return self._closed

    def flush(self) -> None:
        """Push buffered events to the OS (the recorder calls it every ~10 s so a
        crash loses at most that much of the log, like raw/segments.json)."""
        with self._lock:
            if self._closed or not self._pending:
                return
            self._fh.flush()
            self._pending = 0

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._fh.flush()
            self._fh.close()
