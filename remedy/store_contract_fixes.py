"""Final storage-contract fixes applied after the publication-review patches."""
import json
import os
import shutil
from datetime import datetime, timedelta, timezone

WORKSPACE_TTL = timedelta(days=90)
EMPTY_WORKSPACE_TTL = timedelta(days=1)
TOMBSTONE_TTL = timedelta(days=365)


def _utc(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("sweep time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _write_tombstone(store, digest, reason, at):
    store.tombstones.mkdir(parents=True, exist_ok=True)
    target = store.tombstones / digest
    temporary = store.tombstones / ("." + digest + ".tmp")
    payload = json.dumps({"reason": reason, "at": _utc(at).isoformat()}, sort_keys=True).encode("utf-8")
    with open(temporary, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    try:
        from .store import _fsync_dir
        _fsync_dir(store.tombstones)
    except (ImportError, OSError):
        pass


def _read_tombstone(store, token):
    path = store.tombstones / store.digest(token)
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError, TypeError):
        return None
    reason = value.get("reason") if isinstance(value, dict) else None
    return reason if reason in {"deleted", "expired"} else None


def _activity_time(state_file, state):
    values = []
    for name in ("created", "updated", "last_seen"):
        raw = state.get(name)
        if not isinstance(raw, str):
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            values.append(parsed.astimezone(timezone.utc))
    try:
        values.append(datetime.fromtimestamp(state_file.stat().st_mtime, timezone.utc))
    except OSError:
        pass
    return max(values) if values else datetime.fromtimestamp(0, timezone.utc)


def install(module):
    """Repair the public Store contract without disturbing legacy migration code."""
    store_type = module.Store
    original_delete = store_type.delete
    original_sweep = store_type.sweep

    def delete(self, token):
        digest = self.digest(token)
        original_delete(self, token)
        with self._lock:
            _write_tombstone(self, digest, "deleted", datetime.now(timezone.utc))
            self._workspace_posts.pop(digest, None)

    def gone(self, token):
        return _read_tombstone(self, token)

    def sweep(self, at=None):
        # Preserve the existing startup/daily sweep, including its legacy-layout
        # handling. The explicit clock is the deterministic public API used by
        # retention tests and maintenance callers.
        if at is None:
            return original_sweep(self)
        current = _utc(at)
        removed = 0
        with self._lock:
            for directory in list(self.ws.iterdir()):
                if not directory.is_dir():
                    continue
                state_file = directory / "state.json"
                try:
                    with open(state_file, encoding="utf-8") as stream:
                        state = json.load(stream)
                except (OSError, ValueError, TypeError):
                    continue
                cases = state.get("cases", {}) if isinstance(state, dict) else {}
                ttl = WORKSPACE_TTL if cases else EMPTY_WORKSPACE_TTL
                if current <= _activity_time(state_file, state) + ttl:
                    continue
                digest = directory.name
                shutil.rmtree(directory)
                _write_tombstone(self, digest, "expired", current)
                self._workspace_posts.pop(digest, None)
                removed += 1

            cutoff = current - TOMBSTONE_TTL
            for path in list(self.tombstones.iterdir()):
                if not path.is_file() or path.name.startswith("."):
                    continue
                try:
                    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                    if modified < cutoff:
                        path.unlink()
                except OSError:
                    pass

            # A sweep is allowed to scan storage and is the appropriate point
            # to reconcile the cached byte count after removals.
            self._bytes = self._usage()
        return removed

    store_type.delete = delete
    store_type.gone = gone
    store_type.sweep = sweep
