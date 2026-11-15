import csv
import http.client
import io
import json
import multiprocessing
import os
import socket
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from http.cookies import CookieError
from unittest.mock import Mock, patch

import demo
from remedy.artefacts import Artefact
from remedy.batch import _cell, run
from remedy.classify import classify
from remedy.limits import MAX_ARTEFACT_BYTES, MAX_RESPONSE_BYTES
from remedy.models import Case
from remedy.sample_data import DEMO_TODAY, RAWS, make_sources
from remedy.worker import ComputeTimeout, bounded_call


def spin_forever():
    while True:
        pass


class InputSecurityTests(unittest.TestCase):
    def test_large_unknown_value_rejected_before_any_pii_regex(self):
        raw = deepcopy(RAWS[0])
        raw["ignored"] = "a." * 30000
        with patch("remedy.models._reject_pii") as scanner:
            with self.assertRaises(ValueError):
                Case.from_dict(raw)
            scanner.assert_not_called()

    def test_input_budget_and_depth_apply_to_unknown_keys(self):
        nested = "x"
        for _ in range(10):
            nested = [nested]
        for value in ({str(n): "x" * 500 for n in range(20)}, nested):
            raw = deepcopy(RAWS[0])
            raw["ignored"] = value
            with self.assertRaises(ValueError):
                Case.from_dict(raw)

    def test_amplifying_collections_are_rejected(self):
        for kind in ("items", "categories", "attributes", "item_attributes", "events"):
            raw = deepcopy(RAWS[0])
            if kind == "items":
                raw["order"]["items"] *= 9
            elif kind == "categories":
                raw["mandate"]["constraints"]["allowed_categories"] = ["category"] * 9
            elif kind == "attributes":
                raw["mandate"]["constraints"]["required_attributes"] = {str(n): "x" for n in range(9)}
            elif kind == "item_attributes":
                raw["order"]["items"][0]["attributes"] = {str(n): "x" for n in range(9)}
            else:
                raw["events"] *= 17
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                Case.from_dict(raw)

    def test_artefact_and_page_have_byte_limits(self):
        with self.assertRaises(ValueError):
            Artefact("é" * MAX_ARTEFACT_BYTES)
        with self.assertRaises(ValueError):
            demo.page("Title", "x" * MAX_RESPONSE_BYTES)
        with self.assertRaises(ValueError):
            Artefact("small", facts=[{"value": "x" * MAX_ARTEFACT_BYTES}])

    def test_borrowed_order_or_card_or_principal_cannot_read_evidence(self):
        for index, mutation in ((5, "principal"), (6, "principal"), (6, "card"), (5, "order")):
            raw = deepcopy(RAWS[index])
            if mutation == "principal":
                raw["mandate"]["principal_ref"] = "different_principal"
            elif mutation == "card":
                raw["order"]["card_ref"] = "card_tok_8"
            else:
                raw["order"]["order_id"] = "order_7"
            sources = make_sources()
            with patch.object(sources.ledger, "charges") as ledger, patch.object(sources.carrier, "status") as carrier, patch.object(sources.merchant, "confirmation") as merchant:
                decision = classify(Case.from_dict(raw), sources, DEMO_TODAY)
                self.assertEqual("HOLD_ownership", decision.sub)
                self.assertEqual([], decision.evidence)
                ledger.assert_not_called()
                carrier.assert_not_called()
                merchant.assert_not_called()

    def test_missing_trusted_binding_fails_closed(self):
        sources = make_sources()
        sources.bindings.clear()
        self.assertEqual("HOLD_ownership", classify(Case.from_dict(RAWS[6]), sources, DEMO_TODAY).sub)

    def test_not_received_never_requests_return_of_delivered_item(self):
        raw = deepcopy(RAWS[0])
        raw["claim"]["reason"] = "not_received"
        decision = classify(Case.from_dict(raw), make_sources(), DEMO_TODAY)
        self.assertEqual(("HOLD", "HOLD_delivery"), (decision.route, decision.sub))

    def test_invalid_case_id_never_reaches_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "in.jsonl")
            target = os.path.join(directory, "out.csv")
            with open(source, "w", encoding="utf-8") as stream:
                for case_id in ("private@example.invalid", "x" * 70000):
                    raw = deepcopy(RAWS[0])
                    raw["case_id"] = case_id
                    stream.write(json.dumps(raw) + "\n")
                stream.write(json.dumps(RAWS[0]) + "\n")
            run(source, target, DEMO_TODAY)
            with open(target, encoding="utf-8") as stream:
                text = stream.read()
            rows = list(csv.DictReader(io.StringIO(text)))
            self.assertEqual(["line-1", "line-2", "sample-1"], [row["case_id"] for row in rows])
            self.assertNotIn("private@", text)
            self.assertLess(len(text), 1000)

    def test_csv_control_characters_and_formulas_are_neutralised(self):
        for value in ("\t=1+1", "\r=1+1", "\n@SUM(A1)", "  =1+1", "+1", "-1", "@x"):
            self.assertTrue(_cell(value).startswith("'"))
        self.assertLessEqual(len(_cell("x" * 10000)), 501)

    def test_cpu_deadline_kills_and_reaps_process(self):
        before = {process.pid for process in multiprocessing.active_children()}
        started = time.monotonic()
        with self.assertRaises(ComputeTimeout):
            bounded_call(spin_forever, timeout=0.2)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(before, {process.pid for process in multiprocessing.active_children()})

    def test_sessions_do_not_evict_active_visitors(self):
        app = demo.App(max_sessions=2)
        first, _ = app.acquire_session(None)
        second, _ = app.acquire_session(None)
        first.receipts["sample-6"] = "FAKE-1"
        for _ in range(100):
            app.acquire_session(None)
        self.assertEqual({first.session_id, second.session_id}, set(app.sessions))
        self.assertEqual("FAKE-1", app.acquire_session(first.session_id)[0].receipts["sample-6"])

    def test_receipt_storage_is_bounded_when_custom_cases_churn(self):
        app = demo.App()
        state, _ = app.acquire_session(None)
        for _ in range(30):
            case_id = app.add(state, Case.from_dict(deepcopy(RAWS[6])))
            state.receipts[case_id] = state.submitter.submit(case_id, "C", "fake")
            state.rejected.add(case_id)
        self.assertEqual(demo.MAX_CUSTOM_CASES, len(state.cases))
        self.assertEqual(demo.MAX_CUSTOM_CASES, len(state.submitter.receipts))
        self.assertEqual(demo.MAX_CUSTOM_CASES, len(state.submitter._by_case))
        self.assertEqual({}, state.results)


class HttpSecurityTests(unittest.TestCase):
    def setUp(self):
        self.server = demo.make_server("127.0.0.1", 0, max_workers=2)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        supplied = dict(headers or {})
        if method == "POST":
            supplied.setdefault("Origin", f"http://127.0.0.1:{self.port}")
        connection.request(method, path, body, supplied)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    def test_referrer_policy_and_null_origin(self):
        response, _ = self.request("/new")
        self.assertEqual("same-origin", response.getheader("Referrer-Policy"))
        response, _ = self.request("/classify", "POST", b"", {"Origin": "null"})
        self.assertEqual(403, response.status)
        response, _ = self.request("/classify", "POST", "case_json=" + __import__("urllib.parse", fromlist=["quote"]).quote(json.dumps(RAWS[5])))
        self.assertEqual(303, response.status)
        self.assertEqual("same-origin", response.getheader("Referrer-Policy"))

    def test_cookie_error_returns_clean_response(self):
        with patch("demo.SimpleCookie", side_effect=CookieError("malformed")):
            response, _ = self.request("/missing", headers={"Cookie": "bad"})
        self.assertEqual(404, response.status)
        self.assertIn(demo.SESSION_COOKIE, response.getheader("Set-Cookie"))
        handler = object.__new__(demo.Handler)
        handler._set_session_cookie = False
        handler._session_state = None
        self.assertIsNone(handler._cookie())

    def test_cookie_free_requests_hit_global_limit_before_session_creation(self):
        with patch("demo.GLOBAL_RATE_LIMIT", 3):
            for _ in range(3):
                self.assertEqual(404, self.request("/missing")[0].status)
            count = len(self.server.app.sessions)
            self.assertEqual(429, self.request("/missing")[0].status)
            self.assertEqual(count, len(self.server.app.sessions))

    def test_public_gets_do_not_create_sessions(self):
        for path in ("/healthz", "/static/style.css", "/", "/new"):
            response, _ = self.request(path)
            self.assertEqual(200, response.status)
            self.assertIsNone(response.getheader("Set-Cookie"))
        self.assertFalse(self.server.app.sessions)

    def test_session_capacity_has_clean_response_and_preserves_existing_state(self):
        self.server.app.max_sessions = 1
        response, _ = self.request("/missing")
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        self.assertEqual(503, self.request("/missing")[0].status)
        self.assertEqual(404, self.request("/missing", headers={"Cookie": cookie})[0].status)

    def test_idle_and_partial_connections_do_not_consume_worker_slots(self):
        sockets = []
        try:
            for _ in range(3):
                connection = socket.create_connection(("127.0.0.1", self.port), timeout=2)
                sockets.append(connection)
            sockets[0].sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n")
            sockets[1].sendall(b"POST /classify HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\nx")
            response, body = self.request("/healthz")
            self.assertEqual((200, b"ok"), (response.status, body))
            time.sleep(demo.INTAKE_TIMEOUT + 0.2)
            for connection in sockets:
                try:
                    self.assertEqual(b"", connection.recv(1))
                except ConnectionResetError:
                    pass
        finally:
            for connection in sockets:
                connection.close()

    def test_expensive_input_rejected_and_health_remains_available(self):
        raw = deepcopy(RAWS[0])
        raw["ignored"] = "a." * 30000
        response, _ = self.request("/api/classify", "POST", json.dumps(raw))
        self.assertEqual(400, response.status)
        self.assertEqual(200, self.request("/healthz")[0].status)


if __name__ == "__main__":
    unittest.main()
