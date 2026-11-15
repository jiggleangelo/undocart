import http.client
import json
import re
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from demo import (App, BANNER, DEFAULT_HOST, Handler, MAX_THREADS, RATE_LIMIT,
                  REQUEST_TIMEOUT, SESSION_COOKIE, make_server)
from remedy.sample_data import RAWS


class DemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(2)

    def setUp(self):
        self.jar = {}

    def request(self, method, path, body=None, headers=None, jar=None, add_origin=True, port=None):
        jar = self.jar if jar is None else jar
        target_port = self.port if port is None else port
        request_headers = dict(headers or {})
        if jar:
            request_headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in jar.items())
        if method == "POST" and add_origin and "Origin" not in request_headers:
            request_headers["Origin"] = f"http://127.0.0.1:{target_port}"
        connection = http.client.HTTPConnection("127.0.0.1", target_port, timeout=3)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse(); data = response.read(); connection.close()
        cookie = response.getheader("Set-Cookie")
        if cookie:
            name, value = cookie.split(";", 1)[0].split("=", 1)
            jar[name] = value
        return response, data

    def state_for(self, jar=None):
        jar = self.jar if jar is None else jar
        return self.server.app.acquire_session(jar[SESSION_COOKIE])[0]

    def confirmation_fields(self, case_id, jar=None):
        state = self.state_for(jar)
        _, decision, artefact = self.server.app.result(state, case_id)
        fields = {"packet_hash": artefact.packet_hash}
        if decision.route == "C":
            fields.update({f"fact_{n}": "yes" for n in range(len(artefact.facts))})
        else:
            fields["artefact_confirmed"] = "yes"
        return fields

    def test_health_and_banner(self):
        response, body = self.request("GET", "/healthz")
        self.assertEqual((200, b"ok"), (response.status, body))
        for path in ("/", "/new", "/case/sample-6", "/missing"):
            response, body = self.request("GET", path)
            self.assertIn(BANNER.encode(), body)

    def test_route_c_confirmation(self):
        response, body = self.request("GET", "/case/sample-6")
        self.assertIn(b"Route C", body)
        fields = self.confirmation_fields("sample-6")
        response, body = self.request("POST", "/case/sample-6/confirm", urlencode(fields),
                                      {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(200, response.status); self.assertIn(b"FAKE-", body)
        fields.pop("fact_0")
        response, _ = self.request("POST", "/case/sample-6/confirm", urlencode(fields),
                                   {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(400, response.status)

    def test_size_escape_and_api(self):
        response, _ = self.request("POST", "/api/classify", b"x" * 70_000,
                                   {"Content-Type": "application/json"})
        self.assertEqual(413, response.status)
        raw = json.loads(json.dumps(RAWS[7])); raw["claim"]["note"] = "<script>alert(1)</script>"
        form = urlencode({"case_json": json.dumps(raw)})
        response, _ = self.request("POST", "/classify", form,
                                   {"Content-Type": "application/x-www-form-urlencoded"})
        location = response.getheader("Location")
        _, body = self.request("GET", location)
        self.assertNotIn(b"<script>alert(1)</script>", body)
        response, body = self.request("POST", "/api/classify", json.dumps(RAWS[0]),
                                      {"Content-Type": "application/json"})
        self.assertEqual(200, response.status)
        self.assertIn("decision", json.loads(body))

    def test_missing_origin_is_rejected(self):
        response, _ = self.request("POST", "/api/classify", json.dumps(RAWS[0]),
                                   {"Content-Type": "application/json"}, add_origin=False)
        self.assertEqual(403, response.status)

    def test_rejection_receipts_and_custom_cases_are_per_visitor(self):
        first, second = {}, {}
        self.request("GET", "/case/sample-6", jar=first)
        response, _ = self.request("POST", "/case/sample-6/reject", b"", jar=first)
        self.assertEqual(200, response.status)
        response, body = self.request("GET", "/case/sample-6", jar=second)
        self.assertEqual(200, response.status)
        self.assertIn(b"Confirm and send", body)

        fields = self.confirmation_fields("sample-6", second)
        response, body = self.request("POST", "/case/sample-6/confirm", urlencode(fields),
                                      {"Content-Type": "application/x-www-form-urlencoded"}, jar=second)
        self.assertIn(b"FAKE-1", body)

        raw = json.loads(json.dumps(RAWS[7]))
        response, _ = self.request("POST", "/classify", urlencode({"case_json": json.dumps(raw)}),
                                   {"Content-Type": "application/x-www-form-urlencoded"}, jar=first)
        location = response.getheader("Location")
        self.assertRegex(location, r"^/case/custom-[A-Za-z0-9_-]{20,}$")
        response, _ = self.request("GET", location, jar=second)
        self.assertEqual(404, response.status)
        response, _ = self.request("GET", location, jar=first)
        self.assertEqual(200, response.status)

    def test_sessions_and_rate_buckets_are_bounded_and_independent(self):
        app = App(max_sessions=3)
        states = [app.acquire_session(None)[0] for _ in range(10)]
        self.assertEqual(3, len(app.sessions))
        first, second = states[-1], states[-2]
        for _ in range(RATE_LIMIT):
            self.assertTrue(app.allowed(first))
        self.assertFalse(app.allowed(first))
        self.assertTrue(app.allowed(second))
        self.assertFalse(hasattr(app, "rate"))

    def test_public_host_and_proxy_rewritten_host(self):
        server = make_server("127.0.0.1", 0, public_origin="https://demo.example")
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response, _ = self.request("GET", "/", headers={"Host": "demo.example"}, jar={}, port=port)
            self.assertEqual(200, response.status)
            response, _ = self.request("POST", "/api/classify", json.dumps(RAWS[0]),
                                       {"Content-Type": "application/json", "Host": "internal:8080",
                                        "Origin": "https://demo.example"}, jar={}, port=port)
            self.assertEqual(200, response.status)
            self.assertIn("Secure", response.getheader("Set-Cookie"))
        finally:
            server.shutdown(); server.server_close(); thread.join(2)
        self.assertEqual("0.0.0.0", DEFAULT_HOST)

    def test_server_caps_threads_and_handler_uses_absolute_deadline(self):
        server = make_server("127.0.0.1", 0)
        try:
            self.assertEqual(MAX_THREADS, server.max_workers)
        finally:
            server.server_close()
        handler = object.__new__(Handler)
        handler.connection = Mock()
        timer = Mock()
        with patch("demo.Timer", return_value=timer) as timer_class, \
                patch.object(BaseHTTPRequestHandler, "handle", return_value=None):
            Handler.handle(handler)
        timer_class.assert_called_once_with(REQUEST_TIMEOUT, handler._expire_connection)
        timer.start.assert_called_once_with()
        timer.cancel.assert_called_once_with()
        self.assertTrue(timer.daemon)


if __name__ == "__main__": unittest.main()
