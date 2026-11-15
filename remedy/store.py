"""Bounded, token-addressed on-disk workspace storage.

Layout: ``DATA_DIR/ws/<sha256(token)>/state.json``. Older layouts
(``DATA_DIR/<digest>/workspace.json``, ``DATA_DIR/<digest>/state.json`` and
``DATA_DIR/<digest>.json``) are still read. They move to the current layout on
the next write.

A deleted or expired workspace leaves a small tombstone in
``DATA_DIR/tombstones/<digest>``. The tombstone holds only the reason and the
date. A returning visitor then learns what happened instead of seeing a bare
"not found". The token itself is never written to disk.

Retention: a workspace that holds at least one case is kept for 90 days after
the last visit or write. An empty workspace is kept for 1 day.
"""
import hashlib
import json
import os
import secrets
import threading
import time
from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path


class MissingWorkspace(KeyError):
    pass


class CapacityError(RuntimeError):
    pass


class RateLimitError(RuntimeError):
    pass


MAX_WORKSPACES = 3000
MAX_CASES = 20
MAX_WORKSPACE_BYTES = 5 * 1024 * 1024
MAX_CONTACTS = 50
WORKSPACE_POST_LIMIT = 60
WORKSPACE_GET_LIMIT = 240
GET_LIMIT = 240            # anonymous GETs (landing page, unknown tokens)
GLOBAL_POST_LIMIT = 600    # all POSTs, per RATE_WINDOW
CREATE_LIMIT = 60          # new workspaces, per CREATE_WINDOW
CREATE_WINDOW = 3600
RATE_WINDOW = 60
WORKSPACE_TTL = timedelta(days=90)
EMPTY_WORKSPACE_TTL = timedelta(days=1)
TOMBSTONE_TTL = timedelta(days=365)
# New workspaces are refused once this fraction of the cap is used. The rest
# is kept free so existing users can still save into their workspaces.
CREATE_HEADROOM = 0.8
STATE_FILE = "state.json"
TEMP_PREFIX = ".workspace-"
TEMP_SUFFIX = ".tmp"
_RESERVED = {"ws", "tombstones"}


def _now():
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.replace(microsecond=0).isoformat()


def _fsync_dir(path):
    """Persist directory entries (renames, unlinks) across power loss."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _is_temp(path):
    return path.name.startswith(TEMP_PREFIX) and path.name.endswith(TEMP_SUFFIX)


def _entries(directory):
    try:
        return list(directory.iterdir())
    except OSError:
        return []


class Store:
    def __init__(self, data_dir, cap_mb=200):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ws = self.root / "ws"
        self.ws.mkdir(mode=0o700, exist_ok=True)
        self.tombstones = self.root / "tombstones"
        self.tombstones.mkdir(mode=0o700, exist_ok=True)
        try:
            self.cap = int(cap_mb) * 1024 * 1024
        except (TypeError, ValueError):
            raise ValueError("APP_DATA_CAP_MB must be an integer") from None
        if self.cap < 0:
            raise ValueError("APP_DATA_CAP_MB must not be negative")

        # One lock covers mutations and sweeping. Callers must not run
        # expensive compute inside update callbacks; the app computes first and
        # then performs a short compare-and-set under this lock.
        self._lock = threading.RLock()
        self._workspace_posts = {}
        self._workspace_gets = {}
        self._get_posts = deque()
        self._global_posts = deque()
        self._creations = deque()
        # One process per DATA_DIR: at startup no write is in flight, so any
        # temporary file is an orphan from a crash and may hold a full copy of
        # a workspace (possibly a deleted one). Remove it.
        self._remove_orphan_temps()
        self._bytes = self._usage()

    @staticmethod
    def digest(token):
        if not isinstance(token, str):
            raise MissingWorkspace()
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    # ----- paths -------------------------------------------------------

    def _candidates(self, digest):
        return (
            self.ws / digest / STATE_FILE,
            self.root / digest / "workspace.json",
            self.root / digest / STATE_FILE,
            self.root / (digest + ".json"),
        )

    def _path_for_digest(self, digest):
        for path in self._candidates(digest):
            try:
                if path.is_file():
                    return path
            except OSError:
                continue
        return None

    def _file(self, token):
        digest = self.digest(token)
        return self._path_for_digest(digest) or self._candidates(digest)[0]

    def _index_locked(self):
        found = {}
        for entry in _entries(self.ws):
            try:
                if entry.is_dir() and (entry / STATE_FILE).is_file():
                    found.setdefault(entry.name, entry / STATE_FILE)
            except OSError:
                continue
        for entry in _entries(self.root):
            if entry.name in _RESERVED:
                continue
            try:
                if entry.is_dir():
                    for name in ("workspace.json", STATE_FILE):
                        candidate = entry / name
                        if candidate.is_file():
                            found.setdefault(entry.name, candidate)
                            break
                elif entry.is_file() and entry.name.endswith(".json"):
                    found.setdefault(entry.stem, entry)
            except OSError:
                continue
        return found

    def _temps(self):
        temps = []
        for parent in (self.ws, self.root):
            for entry in _entries(parent):
                if parent is self.root and entry.name in _RESERVED:
                    continue
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                temps.extend(child for child in _entries(entry) if _is_temp(child))
        return temps

    def _remove_orphan_temps(self):
        for temp in self._temps():
            try:
                temp.unlink()
            except OSError:
                pass

    def _usage(self):
        total = 0
        for path in list(self._index_locked().values()) + self._temps():
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def _workspace_count_locked(self):
        return len(self._index_locked())

    def exists(self, token):
        with self._lock:
            return self._path_for_digest(self.digest(token)) is not None

    def _require(self, token):
        path = self._path_for_digest(self.digest(token))
        if path is None:
            raise MissingWorkspace(token)
        return path

    # ----- rate limits -------------------------------------------------

    def _prune_bucket(self, bucket, now, window=RATE_WINDOW):
        cutoff = now - window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

    def _prune_map(self, buckets, now):
        for key, bucket in list(buckets.items()):
            self._prune_bucket(bucket, now)
            if not bucket:
                del buckets[key]

    def _limit(self, bucket, limit, now=None):
        now = time.monotonic() if now is None else now
        self._prune_bucket(bucket, now)
        if len(bucket) >= limit:
            raise RateLimitError()
        bucket.append(now)

    def _keyed_limit(self, buckets, key, limit, now):
        self._prune_map(buckets, now)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = deque()
            buckets[key] = bucket
        try:
            self._limit(bucket, limit, now)
        except Exception:
            if not bucket:
                buckets.pop(key, None)
            raise

    def allow_get(self, token=None):
        """Admit a GET.

        A request for an existing workspace uses that workspace's own budget.
        A scraper on the landing page or on random tokens therefore cannot
        lock owners out of viewing, exporting or deleting their data. A
        bucket is created only for a workspace that exists.
        """
        with self._lock:
            now = time.monotonic()
            if token is not None and isinstance(token, str):
                digest = self.digest(token)
                if self._path_for_digest(digest) is not None:
                    self._keyed_limit(self._workspace_gets, digest, WORKSPACE_GET_LIMIT, now)
                    return
            self._limit(self._get_posts, GET_LIMIT, now)

    def allow_post(self, token):
        with self._lock:
            now = time.monotonic()
            self._limit(self._global_posts, GLOBAL_POST_LIMIT, now)
            if token is None:
                return
            # An unknown token is rejected before a bucket is allocated, so
            # random-token traffic cannot grow memory without bound.
            self._require(token)
            self._keyed_limit(self._workspace_posts, self.digest(token), WORKSPACE_POST_LIMIT, now)

    # ----- writing -----------------------------------------------------

    def _encode(self, value):
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_WORKSPACE_BYTES:
            raise CapacityError("workspace size limit reached")
        return raw

    def _reserve_locked(self, new_size, old_size=0):
        # A rewrite that does not grow the stored data is always allowed, so a
        # full store can still be recovered from.
        if old_size and new_size <= old_size:
            return
        # Atomic replacement briefly needs room for the old file and the whole
        # temporary file.
        if self._bytes + new_size > self.cap:
            raise CapacityError("storage capacity reached")

    def _write_locked(self, token, value, creating=False):
        raw = self._encode(value)
        digest = self.digest(token)
        directory = self.ws / digest
        target = directory / STATE_FILE
        existing = self._path_for_digest(digest)
        old_size = 0
        if existing is not None:
            try:
                old_size = existing.stat().st_size
            except OSError:
                old_size = 0
        elif not creating:
            raise MissingWorkspace(token)

        self._reserve_locked(len(raw), old_size)
        made_directory = False
        temporary = directory / (TEMP_PREFIX + secrets.token_hex(8) + TEMP_SUFFIX)
        try:
            if not directory.exists():
                directory.mkdir(mode=0o700)
                made_directory = True
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except Exception:
            try:
                temporary.unlink()
            except OSError:
                pass
            if made_directory:
                try:
                    directory.rmdir()
                except OSError:
                    pass
            raise
        _fsync_dir(directory)
        if made_directory:
            _fsync_dir(self.ws)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        if existing is not None and existing != target:
            # Migrate from a legacy layout: remove the old copy.
            try:
                existing.unlink()
            except OSError:
                pass
            if existing.parent != self.root:
                try:
                    existing.parent.rmdir()
                except OSError:
                    pass
            _fsync_dir(existing.parent)
        self._bytes = max(0, self._bytes + len(raw) - old_size)

    def create(self, peer=None):
        # ``peer`` is accepted for call-site compatibility but is not used.
        # Behind the TLS proxy every visitor shares one peer address. Creation
        # is bounded globally instead: a per-hour budget, a workspace count,
        # and storage headroom reserved for existing users.
        with self._lock:
            if self._workspace_count_locked() >= MAX_WORKSPACES:
                raise CapacityError("workspace limit reached")
            if self._bytes >= self.cap * CREATE_HEADROOM:
                raise CapacityError("storage nearly full; new workspaces are paused")
            now = time.monotonic()
            self._prune_bucket(self._creations, now, CREATE_WINDOW)
            if len(self._creations) >= CREATE_LIMIT:
                raise RateLimitError()
            moment = _iso(_now())
            for _ in range(20):
                token = secrets.token_urlsafe(24)
                digest = self.digest(token)
                if (self._path_for_digest(digest) is None and not (self.ws / digest).exists()
                        and not (self.tombstones / digest).exists()):
                    self._write_locked(token, {"cases": {}, "created": moment, "updated": moment}, creating=True)
                    self._creations.append(now)
                    return token
            raise CapacityError("unable to allocate workspace")

    # ----- reading -----------------------------------------------------

    def _read_locked(self, token, touch=True):
        path = self._require(token)
        try:
            with open(path, "rb") as stream:
                raw = stream.read(MAX_WORKSPACE_BYTES + 1)
            if len(raw) > MAX_WORKSPACE_BYTES:
                raise ValueError("workspace is too large")
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            raise MissingWorkspace(token) from None
        if not isinstance(value, dict) or not isinstance(value.get("cases"), dict):
            raise MissingWorkspace(token)
        if touch:
            try:
                os.utime(path, None)
            except OSError:
                # A full or read-only metadata device must not hide data or
                # block export and deletion.
                pass
        return value

    def read(self, token):
        """Return the workspace, with the retention fields this visit sets."""
        with self._lock:
            value = deepcopy(self._read_locked(token))
        now = _now()
        ttl = WORKSPACE_TTL if value["cases"] else EMPTY_WORKSPACE_TTL
        value["last_seen"] = _iso(now)
        value["keep_until"] = _iso(now + ttl)
        return value

    def gone(self, token):
        """Return 'deleted' or 'expired' for a removed workspace, else None."""
        try:
            digest = self.digest(token)
        except MissingWorkspace:
            return None
        with self._lock:
            if self._path_for_digest(digest) is not None:
                return None
            path = self.tombstones / digest
            try:
                with open(path, "rb") as stream:
                    data = json.loads(stream.read(4096).decode("utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return None
        reason = data.get("reason") if isinstance(data, dict) else None
        return reason if reason in {"deleted", "expired"} else None

    # ----- case mutations ----------------------------------------------

    @staticmethod
    def _stamp(state):
        state["updated"] = _iso(_now())

    def put_case(self, token, case_id, value):
        with self._lock:
            state = self._read_locked(token, touch=False)
            cases = state["cases"]
            if case_id not in cases and len(cases) >= MAX_CASES:
                raise CapacityError("case limit reached")
            supplied = deepcopy(value)
            contacts = deepcopy(supplied.get("selfserve", {}).get("contacts", []))
            cases[case_id] = {
                "input": supplied,
                "contacts": contacts,
                "confirmed_hash": None,
                "confirmed_on": None,
            }
            self._stamp(state)
            self._write_locked(token, state)

    def update_case(self, token, case_id, update):
        """Apply a short callback under the store lock. Never compute inside it."""
        with self._lock:
            state = self._read_locked(token, touch=False)
            if case_id not in state["cases"]:
                raise KeyError(case_id)
            update(state["cases"][case_id])
            self._stamp(state)
            self._write_locked(token, state)

    def add_contact(self, token, case_id, row):
        def append(entry):
            contacts = entry.setdefault("contacts", [])
            if len(contacts) >= MAX_CONTACTS:
                raise CapacityError("contact limit reached")
            contacts.append(deepcopy(row))
            entry["confirmed_hash"] = None
            entry["confirmed_on"] = None
        self.update_case(token, case_id, append)

    def delete_case(self, token, case_id):
        with self._lock:
            state = self._read_locked(token, touch=False)
            if case_id not in state["cases"]:
                raise KeyError(case_id)
            del state["cases"][case_id]
            self._stamp(state)
            self._write_locked(token, state)

    # ----- removal -----------------------------------------------------

    def _purge_locked(self, digest):
        removed = False
        for path in self._candidates(digest):
            try:
                if not path.is_file():
                    continue
                size = path.stat().st_size
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                continue
            removed = True
            self._bytes = max(0, self._bytes - size)
        for directory in (self.ws / digest, self.root / digest):
            try:
                if not directory.is_dir():
                    continue
            except OSError:
                continue
            for child in _entries(directory):
                if _is_temp(child):
                    try:
                        child.unlink()
                    except OSError:
                        pass
            try:
                directory.rmdir()
            except OSError:
                pass
            _fsync_dir(directory.parent)
        self._workspace_posts.pop(digest, None)
        self._workspace_gets.pop(digest, None)
        return removed

    def _tombstone_locked(self, digest, reason):
        path = self.tombstones / digest
        try:
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"reason": reason, "at": _iso(_now())}, stream)
        except OSError:
            # A missing tombstone only makes the message less specific. It
            # never keeps deleted data.
            pass

    def delete(self, token):
        with self._lock:
            digest = self.digest(token)
            if not self._purge_locked(digest):
                raise MissingWorkspace(token)
            self._tombstone_locked(digest, "deleted")

    def _is_empty(self, path):
        try:
            with open(path, "rb") as stream:
                raw = stream.read(MAX_WORKSPACE_BYTES + 1)
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        return isinstance(value, dict) and value.get("cases") == {}

    def sweep(self, now=None):
        if now is None:
            now = _now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("sweep time must include a timezone")
        cutoff = now - WORKSPACE_TTL
        empty_cutoff = now - EMPTY_WORKSPACE_TTL
        removed = 0
        with self._lock:
            for digest, path in list(self._index_locked().items()):
                try:
                    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                except OSError:
                    continue
                if modified >= empty_cutoff:
                    continue
                if modified >= cutoff and not self._is_empty(path):
                    continue
                if self._purge_locked(digest):
                    self._tombstone_locked(digest, "expired")
                    removed += 1
            tomb_cutoff = now - TOMBSTONE_TTL
            for entry in _entries(self.tombstones):
                try:
                    if datetime.fromtimestamp(entry.stat().st_mtime, timezone.utc) < tomb_cutoff:
                        entry.unlink()
                except OSError:
                    continue
        return removed
