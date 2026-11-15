"""Bounded request intake: incomplete requests never occupy handler threads."""
import select
import socket
import threading
import time
from http.server import ThreadingHTTPServer

MAX_BODY = 1024 * 1024
MAX_HEADERS = 8192
MAX_THREADS = 32
MAX_PENDING = 64
INTAKE_TIMEOUT = 2
REQUEST_TIMEOUT = 10


class LimitedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, handler, max_workers=MAX_THREADS):
        self.max_workers = max_workers
        self._slots = threading.BoundedSemaphore(max_workers)
        self._lock = threading.RLock()
        self._pending = {}
        self._inputs = {}
        self._stop_intake = threading.Event()
        super().__init__(address, handler)
        self._thread = threading.Thread(target=self._intake, daemon=True)
        self._thread.start()

    def process_request(self, request, address):
        with self._lock:
            # All visitors may share the TLS proxy's address. Admission is
            # bounded globally, never by an untrusted or shared peer address.
            if len(self._pending) >= MAX_PENDING:
                self.shutdown_request(request)
                return
            request.setblocking(False)
            self._pending[request] = [address, time.monotonic(), bytearray()]

    def _ready(self, data):
        end = data.find(b"\r\n\r\n")
        if end < 0:
            if len(data) > MAX_HEADERS:
                raise ValueError("headers too large")
            return False
        end += 4
        if end > MAX_HEADERS:
            raise ValueError("headers too large")
        lengths = []
        for line in bytes(data[:end]).split(b"\r\n")[1:]:
            if not line:
                continue
            if line[:1] in (b" ", b"\t") or b":" not in line:
                raise ValueError("invalid header")
            key, value = line.split(b":", 1)
            if key.lower() == b"transfer-encoding":
                raise ValueError("unsupported transfer encoding")
            if key.lower() == b"content-length":
                lengths.append(value.strip())
        if len(lengths) > 1:
            raise ValueError("duplicate content length")
        try:
            length = int(lengths[0]) if lengths else 0
        except ValueError:
            return True
        if length < 0 or length > MAX_BODY:
            return True
        return len(data) >= end + length

    def _intake(self):
        while not self._stop_intake.wait(0.01):
            with self._lock:
                sockets = list(self._pending)
            if not sockets:
                continue
            try:
                readable, _, _ = select.select(sockets, [], [], 0.05)
            except (ValueError, OSError):
                continue
            with self._lock:
                for request, (address, started, data) in list(self._pending.items()):
                    close = time.monotonic() - started >= INTAKE_TIMEOUT
                    ready = False
                    if request in readable and not close:
                        try:
                            chunk = request.recv(min(16384, MAX_BODY + MAX_HEADERS + 1 - len(data)))
                            if not chunk:
                                close = True
                            else:
                                data.extend(chunk)
                                if len(data) > MAX_BODY + MAX_HEADERS:
                                    close = True
                                else:
                                    ready = self._ready(data)
                        except BlockingIOError:
                            continue
                        except (ValueError, OSError):
                            close = True
                    if not close and not ready:
                        continue
                    del self._pending[request]
                    if close or not self._slots.acquire(False):
                        self.shutdown_request(request)
                        continue
                    request.settimeout(REQUEST_TIMEOUT)
                    self._inputs[request] = bytes(data)
                    try:
                        super().process_request(request, address)
                    except Exception:
                        self._inputs.pop(request, None)
                        self._slots.release()
                        self.shutdown_request(request)

    def take_input(self, request):
        with self._lock:
            return self._inputs.pop(request)

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            with self._lock:
                self._inputs.pop(request, None)
            self._slots.release()

    def server_close(self):
        self._stop_intake.set()
        self._thread.join(1)
        with self._lock:
            for request in self._pending:
                self.shutdown_request(request)
            self._pending.clear()
        if hasattr(self, "app"):
            self.app.stop.set()
        super().server_close()
