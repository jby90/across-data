"""Bounded process-local cache; no secrets, disk persistence or source decisions."""

import copy
import hashlib
import json
import time
from collections import OrderedDict

MAX_ENTRIES = 128
TTL_SECONDS = 1800
_entries = OrderedDict()


def cache_key(index, dimension, title, query, top_k, config, scope, protocol):
    payload = [
        protocol,
        scope,
        dimension,
        title,
        query,
        top_k,
        config,
        sorted((c.__dict__ for c in index.candidates), key=lambda c: c["clause_id"]),
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def get(key):
    entry = _entries.get(key)
    if not entry:
        return None
    created, value = entry
    if time.monotonic() - created > TTL_SECONDS:
        del _entries[key]
        return None
    _entries.move_to_end(key)
    return copy.deepcopy(value)


def put(key, value):
    _entries[key] = (time.monotonic(), copy.deepcopy(value))
    _entries.move_to_end(key)
    while len(_entries) > MAX_ENTRIES:
        _entries.popitem(last=False)
