import html
import io
import json
import logging
import os
import re
import secrets
import select
import socket
import time
from collections import OrderedDict, deque
from copy import deepcopy
from decimal import InvalidOperation
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import BoundedSemaphore, Event, RLock, Thread, Timer
from urllib.parse import parse_qs, quote, urlparse

from remedy.artefacts import verify_packet
from remedy.limits import MAX_RESPONSE_BYTES
from remedy.models import plain
from remedy.sample_data import DEMO_TODAY, SAMPLE_CASES, STORIES, make_sources
from remedy.sources import FakeSubmitter
from remedy.worker import BusyError, ComputeTimeout, bounded_call, evaluate, evaluate_json, parse_case

BANNER = "Demo operated by AI agents. Made-up data. Do not enter real personal data. Nothing is charged."
MAX_BODY = 65536
MAX_HEADERS = 8192
REQUEST_TIMEOUT = 10
INTAKE_TIMEOUT = 2
MAX_PENDING = 64
MAX_PENDING_PER_PEER = 4
RATE_LIMIT = 120
GLOBAL_RATE_LIMIT = 240
MAX_THREADS = 32
MAX_SESSIONS = 64
MAX_CUSTOM_CASES = 4
SESSION_TTL = 1800
DEFAULT_HOST = "0.0.0.0"
SESSION_COOKIE = "remedy_session"
LOGGER = logging.getLogger("remedy.demo")


def esc(value):
    return html.escape(str(value), quote=True)


def page(title, body):
    result = ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
              f"<meta name='viewport' content='width=device-width'><title>{esc(title)}</title>"
              "<link rel='stylesheet' href='/static/style.css'></head><body>"
              f"<div class='banner'>{esc(BANNER)}</div><main><h1>{esc(title)}</h1>{body}</main></body></html>")
    if len(result.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValueError("page is too large")
    return result


class AllowListFilter(logging.Filter):
    allowed = ("case_id", "route", "sub", "reasons", "error_class")

    def filter(self, record):
        record.msg = " ".join(f"{key}={getattr(record, key)}" for key in self.allowed if hasattr(record, key))
        record.args = ()
        return bool(record.msg)


def configure_logging():
    if not LOGGER.handlers:
        handler = logging.StreamHandler()
        handler.addFilter(AllowListFilter())
        LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.INFO)
        LOGGER.propagate = False


def _origin_key(value):
    parsed = urlparse(value)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname is None or parsed.username is not None or
            parsed.password is not None or parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment):
        raise ValueError("invalid origin")
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme, parsed.hostname.lower(), port


class SessionState:
    def __init__(self, session_id):
        self.session_id = session_id
        self.cases = OrderedDict()
        self.results = {}  # Results are recomputed; amplified artefacts are never cached.
        self.rejected = set()
        self.receipts = {}
        self.rate = deque()
        self.submitter = FakeSubmitter()
        self.last_seen = time.monotonic()


class App:
    def __init__(self, public_origin=None, max_sessions=MAX_SESSIONS):
        self.sources = make_sources()
        self.sessions = OrderedDict()
        self.lock = RLock()
        self.max_sessions = max_sessions
        self.global_rate = deque()
        self.public_origin = public_origin.rstrip("/") if public_origin else None
        self.public_origin_key = _origin_key(self.public_origin) if self.public_origin else None

    def acquire_session(self, token):
        with self.lock:
            now = time.monotonic()
            for key, state in list(self.sessions.items()):
                if now - state.last_seen > SESSION_TTL:
                    del self.sessions[key]
            if token and token in self.sessions:
                state = self.sessions[token]
                state.last_seen = now
                return state, False
            state = SessionState(secrets.token_urlsafe(24))
            if len(self.sessions) < self.max_sessions:
                self.sessions[state.session_id] = state
            # An unregistered state is refused by HTTP admission. Never evict
            # another visitor to accommodate cookie churn.
            return state, True

    def _allow(self, bucket, limit):
        now = time.monotonic()
        with self.lock:
            while bucket and bucket[0] <= now - 60:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True

    def allowed_global(self):
        return self._allow(self.global_rate, GLOBAL_RATE_LIMIT)

    def allowed(self, state):
        return self._allow(state.rate, RATE_LIMIT)

    def add(self, state, case):
        with self.lock:
            case.case_id = "custom-" + secrets.token_urlsafe(18)
            state.cases[case.case_id] = case
            while len(state.cases) > MAX_CUSTOM_CASES:
                old, _ = state.cases.popitem(last=False)
                state.results.pop(old, None)
                state.rejected.discard(old)
                state.receipts.pop(old, None)
                state.submitter.forget(old)
            return case.case_id

    def result(self, state, case_id):
        with self.lock:
            case = state.cases.get(case_id)
            if case is None and case_id in SAMPLE_CASES:
                case = SAMPLE_CASES[case_id]
            if case is None:
                return None
            raw = deepcopy(case.to_dict())
        return bounded_call(evaluate, raw, self.sources, DEMO_TODAY)


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Buffer bounded requests without allocating a thread to idle sockets."""
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, server_address, handler_class, max_workers=MAX_THREADS):
        self.max_workers = max_workers
        self._slots = BoundedSemaphore(max_workers)
        self._intake_lock = RLock()
        self._pending = {}
        self._inputs = {}
        self._stop_intake = Event()
        super().__init__(server_address, handler_class)
        self._intake_thread = Thread(target=self._intake, daemon=True)
        self._intake_thread.start()

    def process_request(self, request, client_address):
        with self._intake_lock:
            peer_count = sum(entry[0][0] == client_address[0] for entry in self._pending.values())
            if len(self._pending) >= MAX_PENDING or peer_count >= MAX_PENDING_PER_PEER:
                self.shutdown_request(request)
                return
            request.setblocking(False)
            self._pending[request] = [client_address, time.monotonic(), bytearray()]

    def _ready(self, data):
        end = data.find(b"\r\n\r\n")
        if end < 0:
            if len(data) > MAX_HEADERS:
                raise ValueError("headers too large")
            return False
        end += 4
        if end > MAX_HEADERS:
            raise ValueError("headers too large")
        lines = bytes(data[:end]).split(b"\r\n")
        lengths = []
        for line in lines[1:]:
            if not line:
                continue
            if line[:1] in (b" ", b"\t") or b":" not in line:
                raise ValueError("invalid header")
            key, value = line.split(b":", 1)
            if key.lower() == b"transfer-encoding":
                raise ValueError("transfer encoding is not supported")
            if key.lower() == b"content-length":
                lengths.append(value.strip())
        if len(lengths) > 1:
            raise ValueError("duplicate content length")
        try:
            length = int(lengths[0]) if lengths else 0
        except ValueError:
            return True  # Handler sends a clean 400.
        if length < 0 or length > MAX_BODY:
            return True  # Handler sends 400/413 without reading a body.
        return len(data) >= end + length

    def _intake(self):
        while not self._stop_intake.wait(0.01):
            with self._intake_lock:
                sockets = list(self._pending)
            if not sockets:
                continue
            try:
                readable, _, _ = select.select(sockets, [], [], 0.05)
            except (OSError, ValueError):
                continue
            with self._intake_lock:
                for request, entry in list(self._pending.items()):
                    address, started, data = entry
                    ready = False
                    close = time.monotonic() - started > INTAKE_TIMEOUT
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
                        except (OSError, ValueError):
                            close = True
                    if not close and not ready:
                        continue
                    del self._pending[request]
                    if close or not self._slots.acquire(blocking=False):
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
        with self._intake_lock:
            return self._inputs.pop(request)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._intake_lock:
                self._inputs.pop(request, None)
            self._slots.release()

    def server_close(self):
        self._stop_intake.set()
        self._intake_thread.join(1)
        with self._intake_lock:
            for request in self._pending:
                self.shutdown_request(request)
            self._pending.clear()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "RemedyDemo"

    def setup(self):
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT)
        self.rfile.close()
        self.rfile = io.BytesIO(self.server.take_input(self.connection))
        self._session_state = None
        self._set_session_cookie = False

    def _expire_connection(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def handle(self):
        deadline = Timer(REQUEST_TIMEOUT, self._expire_connection)
        deadline.daemon = True
        deadline.start()
        try:
            super().handle()
        finally:
            deadline.cancel()

    def log_message(self, format, *args):
        return

    @property
    def app(self):
        return self.server.app

    @property
    def session(self):
        if self._session_state is None:
            token = None
            try:
                cookies = SimpleCookie(self.headers.get("Cookie", ""))
                morsel = cookies.get(SESSION_COOKIE)
                token = morsel.value if morsel else None
            except CookieError:
                pass
            state, fresh = self.app.acquire_session(token)
            with self.app.lock:
                if state.session_id not in self.app.sessions:
                    raise BusyError("session capacity reached")
            self._session_state, self._set_session_cookie = state, fresh
        return self._session_state

    def _cookie(self):
        # Response handling must never acquire or parse a session again.
        if not self._set_session_cookie or self._session_state is None:
            return None
        value = f"{SESSION_COOKIE}={self._session_state.session_id}; Path=/; HttpOnly; SameSite=Strict"
        if self.app.public_origin_key and self.app.public_origin_key[0] == "https":
            value += "; Secure"
        return value

    def _headers(self, status, content_type, length, location=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        if location is not None:
            self.send_header("Location", location)
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        cookie = self._cookie()
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.close_connection = True

    def send_data(self, status, data, content_type="text/html; charset=utf-8"):
        raw = data.encode("utf-8") if isinstance(data, str) else data
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("response is too large")
        self._headers(status, content_type, len(raw))
        self.wfile.write(raw)

    def send_json(self, status, value):
        self.send_data(status, json.dumps(plain(value), sort_keys=True), "application/json; charset=utf-8")

    def redirect(self, location):
        self._headers(303, "text/html; charset=utf-8", 0, location)

    def body(self):
        value = self.headers.get("Content-Length")
        if value is None:
            self.send_data(411, page("Length required", "<p>A Content-Length header is required.</p>"))
            return None
        try:
            length = int(value)
        except ValueError:
            length = -1
        if length < 0:
            self.send_data(400, page("Bad request", "<p>Invalid request.</p>"))
            return None
        if length > MAX_BODY:
            self.send_data(413, page("Too large", "<p>The request body exceeds 64 KB.</p>"))
            return None
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("incomplete body")
        return raw

    def _valid_host(self):
        host = self.headers.get("Host", "")
        if not host or any(c.isspace() or c in "/@," for c in host):
            return False
        try:
            parsed = urlparse("http://" + host)
            parsed.port
            return parsed.hostname is not None
        except ValueError:
            return False

    def _valid_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return False
        try:
            supplied = _origin_key(origin)
            expected = self.app.public_origin_key or _origin_key(f"http://{self.headers.get('Host', '')}")
            return supplied == expected
        except ValueError:
            return False

    def _dispatch(self, method):
        try:
            if not self._valid_host():
                return self.send_data(400, page("Bad request", "<p>Invalid request host.</p>"))
            if not self.app.allowed_global():
                return self.send_data(429, page("Too many requests", "<p>Please try again later.</p>"))
            if method == "POST" and not self._valid_origin():
                return self.send_data(403, page("Forbidden", "<p>Cross-origin request rejected.</p>"))
            path = urlparse(self.path).path
            if not (method == "GET" and path in {"/healthz", "/static/style.css", "/", "/new"}):
                if not self.app.allowed(self.session):
                    return self.send_data(429, page("Too many requests", "<p>Please try again later.</p>"))
            return self._get() if method == "GET" else self._post()
        except (BusyError, ComputeTimeout):
            return self.send_data(503, page("Busy", "<p>Please try again later.</p>"))
        except (ValueError, TypeError, KeyError, InvalidOperation, RecursionError):
            LOGGER.info("request rejected", extra={"case_id": "unknown", "error_class": "invalid_input"})
            return self.send_data(400, page("Bad request", "<p>The submitted data is invalid.</p>"))
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return
        except Exception as exc:
            LOGGER.error("request failed", extra={"case_id": "unknown", "error_class": type(exc).__name__})
            return self.send_data(500, page("Error", "<p>The request could not be completed.</p>"))

    def do_GET(self):
        return self._dispatch("GET")

    def do_POST(self):
        return self._dispatch("POST")

    def _get(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self.send_data(200, "ok", "text/plain; charset=utf-8")
        if path == "/static/style.css":
            try:
                with open(os.path.join(os.path.dirname(__file__), "static", "style.css"), "rb") as source:
                    return self.send_data(200, source.read(MAX_RESPONSE_BYTES + 1), "text/css; charset=utf-8")
            except OSError:
                return self.send_data(404, b"", "text/plain")
        if path == "/":
            cards = [f"<li><p>{esc(story)}</p><a class='button' href='/case/{esc(case_id)}'>Run</a></li>" for case_id, story in STORIES.items()]
            return self.send_data(200, page("Remedy demo", "<p>Try a made-up case and simulate its submission.</p><ul class='cards'>" + "".join(cards) + "</ul><p><a href='/new'>Try your own made-up case</a></p>"))
        if path == "/new":
            sample = json.dumps(SAMPLE_CASES["sample-6"].to_dict(), indent=2)
            body = ("<p>Use made-up, pseudonymous data only. Personal data is rejected.</p>"
                    "<form method='post' action='/classify'><label>Case JSON<textarea name='case_json' rows='24'>" +
                    esc(sample) + "</textarea></label><button>Classify</button></form>")
            return self.send_data(200, page("Try a made-up case", body))
        match = re.fullmatch(r"/case/([^/]+)", path)
        if match:
            result = self.app.result(self.session, match.group(1))
            if not result:
                return self.send_data(404, page("Not found", "<p>Unknown case.</p>"))
            case, decision, artefact = result
            dev = "".join(f"<li>{esc(d.field)}: expected {esc(d.expected)}, actual {esc(d.actual)}</li>" for d in decision.deviations) or "<li>None</li>"
            evidence = esc(json.dumps(plain([e.to_dict() for e in decision.evidence]), indent=2))
            controls = ""
            if decision.route != "HOLD" and case.case_id not in self.session.rejected:
                if decision.route == "C":
                    checks = "".join(f"<label><input type='checkbox' name='fact_{n}' value='yes'> Confirm {esc(f['field'])}: {esc(json.dumps(f['value'], sort_keys=True))}</label>" for n, f in enumerate(artefact.facts or []))
                else:
                    checks = "<label><input type='checkbox' name='artefact_confirmed' value='yes'> Confirm the generated request and its facts</label>"
                controls = (f"<form method='post' action='/case/{esc(case.case_id)}/confirm'>{checks}"
                            f"<input type='hidden' name='packet_hash' value='{esc(artefact.packet_hash)}'><button>Confirm and send (simulated)</button></form>"
                            f"<form method='post' action='/case/{esc(case.case_id)}/reject'><button class='secondary'>Reject</button></form>")
            body = (f"<p class='route'>Route {esc(decision.route)} — {esc(decision.sub)}</p>"
                    f"<p>Reasons: {esc(', '.join(decision.reasons))}</p><h2>Mandate deviations</h2><ul>{dev}</ul>"
                    f"<h2>Evidence</h2><pre>{evidence}</pre><h2>Generated artefact</h2>"
                    f"<pre>{esc(artefact.text)}</pre>{controls}<p><a href='/'>Back to samples</a></p>")
            return self.send_data(200, page(f"Case {case.case_id}", body))
        return self.send_data(404, page("Not found", "<p>Unknown page.</p>"))

    def _form(self, raw):
        return {key: values[-1] for key, values in parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=200).items()}

    def _post(self):
        path = urlparse(self.path).path
        raw = self.body()
        if raw is None:
            return
        if path == "/classify":
            case = bounded_call(parse_case, self._form(raw).get("case_json", ""))
            case_id = self.app.add(self.session, case)
            return self.redirect("/case/" + quote(case_id, safe=""))
        if path == "/api/classify":
            _, decision, artefact = bounded_call(evaluate_json, raw.decode("utf-8"), self.app.sources, DEMO_TODAY)
            return self.send_json(200, {"decision": decision.to_dict(), "artefact": artefact.to_dict()})
        action = re.fullmatch(r"/case/([^/]+)/(confirm|reject)", path)
        if not action:
            return self.send_data(404, page("Not found", "<p>Unknown page.</p>"))
        case_id, verb = action.groups()
        result = self.app.result(self.session, case_id)
        if not result:
            return self.send_data(404, page("Not found", "<p>Unknown case.</p>"))
        _, decision, artefact = result
        fields = self._form(raw)
        with self.app.lock:
            # A concurrent custom-case eviction must not recreate orphan state.
            if case_id not in SAMPLE_CASES and case_id not in self.session.cases:
                return self.send_data(404, page("Not found", "<p>Unknown case.</p>"))
            if verb == "reject":
                if case_id in self.session.receipts:
                    return self.send_data(409, page("Already submitted", "<p>This case was already submitted.</p>"))
                self.session.rejected.add(case_id)
                return self.send_data(200, page("Rejected", "<p>The frozen result was rejected. It cannot be submitted.</p>"))
            if decision.route == "HOLD" or case_id in self.session.rejected:
                return self.send_data(409, page("Unavailable", "<p>This case cannot be submitted.</p>"))
            if not verify_packet(artefact.facts or [], fields.get("packet_hash")) or fields.get("packet_hash") != artefact.packet_hash:
                return self.send_data(400, page("Bad request", "<p>The frozen packet did not match.</p>"))
            if decision.route == "C":
                if any(fields.get(f"fact_{n}") != "yes" for n in range(len(artefact.facts or []))):
                    return self.send_data(400, page("Bad request", "<p>Every frozen fact must be confirmed.</p>"))
            elif fields.get("artefact_confirmed") != "yes":
                return self.send_data(400, page("Bad request", "<p>The generated request must be confirmed.</p>"))
            receipt = self.session.receipts.get(case_id)
            if receipt is None:
                receipt = self.session.submitter.submit(case_id, decision.route, artefact.text, artefact.packet_hash)
                self.session.receipts[case_id] = receipt
        return self.send_data(200, page("Submitted", f"<p class='success'>Simulated receipt: {esc(receipt)}</p>"))


def make_server(host="127.0.0.1", port=8080, public_origin=None, max_workers=MAX_THREADS):
    configure_logging()
    server = BoundedThreadingHTTPServer((host, port), Handler, max_workers=max_workers)
    server.app = App(public_origin=public_origin)
    return server


def main():
    server = make_server(os.environ.get("HOST", DEFAULT_HOST), int(os.environ.get("PORT", "8080")),
                         public_origin=os.environ.get("PUBLIC_ORIGIN"))
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
