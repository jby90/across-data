"""Bounded process-local PDF artifacts. Authorization and source holds run BEFORE this cache.

No original archive, report snapshot or approval is written. Restart discards artifacts.
Frozen content, task identity, tenant, dimension and live source-update notices form the key.
"""

import hashlib
import json
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future

from app.analysis.report_exporter import _view


class ArtifactCache:
    def __init__(self, max_bytes=8 * 1024 * 1024, max_entries=32, ttl=900, clock=time.monotonic):
        self.max_bytes, self.max_entries, self.ttl, self.clock = max_bytes, max_entries, ttl, clock
        self._lock = threading.Lock()
        self._items = OrderedDict()
        self._pending = {}
        self._bytes = 0

    def get_or_build(self, key, build):
        with self._lock:
            now = self.clock()
            for old, (expires, data) in list(self._items.items()):
                if expires <= now:
                    self._items.pop(old)
                    self._bytes -= len(data)
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key][1], True
            pending = self._pending.get(key)
            owner = pending is None
            if owner:
                pending = self._pending[key] = Future()
        if not owner:
            return pending.result(), True
        try:
            data = build()
            with self._lock:
                if len(data) <= self.max_bytes:
                    self._items[key] = (self.clock() + self.ttl, data)
                    self._bytes += len(data)
                    while self._bytes > self.max_bytes or len(self._items) > self.max_entries:
                        _, (_, evicted) = self._items.popitem(last=False)
                        self._bytes -= len(evicted)
                self._pending.pop(key)
                pending.set_result(data)
            return data, False
        except BaseException as exc:
            with self._lock:
                self._pending.pop(key)
                pending.set_exception(exc)
            raise


PDF_CACHE = ArtifactCache()


def cached_pdf(task, report, dimension, build):
    task_view, report_view = _view(task, report, dimension)
    snapshot = {
        "org_id": str(task.org_id), "report_id": str(report.id), "dimension": dimension,
        "task": vars(task_view), "report": vars(report_view),
    }
    key = hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, default=str,
    ).encode()).hexdigest()
    return PDF_CACHE.get_or_build(key, lambda: build(task, report, dimension))
