"""Regression coverage for the self-serve outside review; legacy tests unchanged."""
import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from app import Handler, make_server, render_form
from remedy import models
from remedy.httpserver import INTAKE_TIMEOUT, REQUEST_TIMEOUT
from remedy.lint import lint
from remedy.models import Case
from remedy.sample_data import RAWS
from remedy.selfserve import check, normalised, parse_form, parse_json, utc_today
from remedy.store import CapacityError, MissingWorkspace, RateLimitError, Store

TODAY = date(2026, 11, 15)
BASE_FIELDS = {
    "asked_item": "Shoes", "merchant_name": "Shop", "order_label": "Order-A",
    "item_title": "Shoes", "unit_price": "80", "total": "80", "currency": "USD",
    "quantity": "1", "ordered_quantity": "1", "order_date": "2026-10-01",
    "return_days": "30", "problem_type": "wrong_item",
}


def payment(reason="charged_twice"):
    fields = dict(BASE_FIELDS, problem_type=reason, status="placed")
    fields["charges"] = ("80, 2026-10-02, settled\n80, 2026-10-03, settled"
                         if reason == "charged_twice" else "95, 2026-10-02, settled")
    return parse_form(fields)


def contact(day="2026-11-02", response="none"):
    return {"date": day, "channel": "chat", "response": response}


class AdapterReviewTests(unittest.TestCase):
    def test_all_payment_issuer_routes_require_contact_and_full_wait(self):
        for reason in ("charged_twice", "wrong_amount"):
            with self.subTest(reason=reason):
                case = payment(reason)
                self.assertEqual("Ask the merchant", check(case, TODAY)["route"])
                case._selfserve["contacts"] = [contact("2026-11-11")]
                result = check(case, TODAY)
                self.assertEqual("Wait until 2026-11-18", result["route"])
                self.assertEqual(3, result["deadlines"][0]["days_remaining"])
                self.assertEqual("Wait until 2026-11-18", check(case, date(2026, 11, 17))["route"])
                self.assertEqual("Talk to your card issuer", check(case, date(2026, 11, 18))["route"])
                case._selfserve["contacts"][-1]["response"] = "resolved"
                self.assertNotEqual("Talk to your card issuer", check(case, date(2026, 11, 18))["route"])

    def test_contact_dates_and_computed_date_pass_unchanged_lint(self):
        case = payment()
        case._selfserve["contacts"] = [contact("2026-10-15"), contact("2026-11-02"), contact("2026-11-11")]
        result = check(case, TODAY)
        self.assertIn("2026-11-18", result["artefact"].text)
        result = check(case, date(2026, 11, 18))
        for day in ("2026-10-15", "2026-11-02", "2026-11-11"):
            self.assertIn(day, result["artefact"].text)
        self.assertNotIn("verified", result["artefact"].text.lower())
        self.assertNotIn("guarantee", result["artefact"].text.lower())
        with self.assertRaises(ValueError):
            lint("mystery 987654321", "C", case, result["decision"].evidence)

    def test_full_log_and_timeline_are_frozen_and_confirmable(self):
        case = payment()
        case._selfserve["contacts"] = [contact("2026-10-15"), contact("2026-11-02")]
        first = check(case, TODAY)["artefact"]
        self.assertTrue({"timeline", "merchant_contact_log"}.issubset({f["field"] for f in first.facts}))
        case._selfserve["contacts"][0]["channel"] = "phone"
        second = check(case, TODAY)["artefact"]
        self.assertNotEqual(first.packet_hash, second.packet_hash)
        case.events[0].at += timedelta(days=1)
        self.assertNotEqual(second.packet_hash, check(case, TODAY)["artefact"].packet_hash)

    def test_blank_and_human_status_normalization(self):
        for status in ("", "Delivered", "delivered", "placed"):
            with self.subTest(status=status):
                case = parse_form(dict(BASE_FIELDS, status=status, delivered_date="2026-11-10"))
                self.assertEqual("delivered", case.order.status)
                self.assertEqual("Return or cancel now", check(case, TODAY)["route"])
        case = parse_form(dict(BASE_FIELDS, status="in transit"))
        self.assertEqual("in_transit", case.order.status)
        form = render_form("x" * 32)
        self.assertIn("<select name='status'>", form)
        self.assertNotIn("<input type='text' name='status'", form)

    def test_form_cancellation_can_progress_without_inventing_shipment_fact(self):
        fields = dict(BASE_FIELDS, problem_type="cancelled_but_charged", cancellation_date="2026-10-03",
                      charges="80, 2026-10-02, settled", cancellation_before_shipment="yes")
        case = parse_form(fields)
        self.assertEqual("Ask the merchant", check(case, TODAY)["route"])
        case._selfserve["contacts"] = [contact()]
        self.assertEqual("Talk to your card issuer", check(case, TODAY)["route"])
        fields["cancellation_before_shipment"] = ""
        self.assertEqual("Missing info", check(parse_form(fields), TODAY)["route"])
        form = render_form("x" * 32, normalised(case))
        self.assertIn("name='cancellation_date' value='2026-10-03'", form)
        self.assertIn("name='cancellation_before_shipment'", form)

    def test_backdated_contact_cannot_shorten_wait(self):
        case = payment()
        case._selfserve["contacts"] = [contact("2026-11-14"), contact("2026-10-02")]
        self.assertEqual("Wait until 2026-11-21", check(case, TODAY)["route"])

    def test_utc_used_for_default_dates(self):
        instant = datetime(2026, 11, 2, 0, 1, tzinfo=timezone.utc)
        with patch("remedy.selfserve.datetime") as clock:
            clock.now.return_value = instant
            self.assertEqual(date(2026, 11, 2), utc_today())
            clock.now.assert_called_once_with(timezone.utc)
        with patch("remedy.selfserve.utc_today", return_value=date(2026, 11, 2)):
            fields = dict(BASE_FIELDS)
            fields.pop("order_date")
            self.assertEqual(date(2026, 11, 2), parse_form(fields).order.ordered_at.date())

    def test_privacy_guard_is_never_monkeypatched_during_concurrent_parsing(self):
        original_guard = models._reject_pii
        raw = deepcopy(RAWS[0])
        raw["order"]["order_id"] = "Amazon-123-4567890-1234567"
        text = json.dumps(raw)

        def selfserve():
            parse_json(text)
            self.assertIs(original_guard, models._reject_pii)

        def legacy():
            with self.assertRaises(ValueError):
                Case.from_dict(raw)
            self.assertIs(original_guard, models._reject_pii)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(selfserve if n % 2 else legacy) for n in range(100)]
            for future in futures:
                future.result()

    def test_all_fifty_contacts_survive_normalisation_and_sheet_generation(self):
        case = payment()
        case._selfserve["contacts"] = [contact() for _ in range(50)]
        parsed = parse_json(json.dumps(normalised(case)))
        self.assertEqual(50, len(parsed._selfserve["contacts"]))
        self.assertEqual("Talk to your card issuer", check(parsed, TODAY)["route"])

    def test_mandate_deviation_and_description_never_create_issuer_ground(self):
        for reason in ("wrong_item", "not_as_described"):
            case = parse_form(dict(BASE_FIELDS, problem_type=reason, asked_size="10", actual_size="11", final_sale="yes"))
            case._selfserve["contacts"] = [contact()]
            self.assertNotEqual("Talk to your card issuer", check(case, TODAY)["route"])


class StorageReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_random_workspace_posts_never_allocate_buckets(self):
        with patch.object(self.store, "_limit"):
            for n in range(2000):
                with self.assertRaises(MissingWorkspace):
                    self.store.allow_post(f"{n:032d}")
        self.assertEqual({}, self.store._workspace_posts)
        token = self.store.create()
        self.store.allow_post(token)
        self.assertEqual(1, len(self.store._workspace_posts))
        with patch("remedy.store.time.monotonic", return_value=time.monotonic() + 61):
            other = self.store.create("other")
            self.store.allow_post(other)
        self.assertEqual({self.store.digest(other)}, set(self.store._workspace_posts))

    def test_full_storage_does_not_block_reads_or_deletions(self):
        token = self.store.create()
        self.store.put_case(token, "one", normalised(payment()))
        self.store.cap = 0
        with patch.object(self.store, "_write_locked", side_effect=AssertionError("GET must not write JSON")), \
                patch.object(self.store, "_usage", side_effect=AssertionError("GET must not scan disk")):
            self.assertIn("one", self.store.read(token)["cases"])
        self.store.delete_case(token, "one")
        self.assertEqual({}, self.store.read(token)["cases"])
        self.store.delete(token)
        self.assertFalse(self.store.exists(token))

    def test_metadata_touch_failure_does_not_block_recovery(self):
        token = self.store.create()
        os.utime(self.store._file(token), (0, 0))
        with patch("remedy.store.os.utime", side_effect=OSError("disk full")):
            self.assertEqual({}, self.store.read(token)["cases"])

    def test_creation_has_no_peer_limit_and_failed_create_leaves_no_directory(self):
        tokens = [self.store.create("peer-one") for _ in range(11)]
        self.assertEqual(11, len(set(tokens)))
        self.store.create("peer-two")
        before = set(self.store.ws.iterdir())
        self.store.cap = 0
        with self.assertRaises(CapacityError):
            self.store.create("peer-three")
        self.assertEqual(before, set(self.store.ws.iterdir()))

    def test_deleted_and_swept_workspaces_leave_no_lock_or_rate_entry(self):
        token = self.store.create()
        self.store.allow_post(token)
        self.store.delete(token)
        self.assertEqual({}, self.store._workspace_posts)
        self.assertFalse(hasattr(self.store, "_locks"))
        token = self.store.create()
        self.store.allow_post(token)
        self.store.sweep(datetime.now(timezone.utc) + timedelta(days=91))
        self.assertFalse(self.store.exists(token))
        self.assertEqual({}, self.store._workspace_posts)
        self.assertFalse(hasattr(self.store, "_locks"))

    def test_sweep_waits_for_in_progress_workspace_mutation(self):
        token = self.store.create()
        self.store.put_case(token, "one", normalised(payment()))
        entered, release, swept = threading.Event(), threading.Event(), threading.Event()

        def update(entry):
            entered.set()
            release.wait(2)

        writer = threading.Thread(target=lambda: self.store.update_case(token, "one", update))
        writer.start()
        self.assertTrue(entered.wait(1))

        def sweep():
            self.store.sweep(datetime.now(timezone.utc) + timedelta(days=91))
            swept.set()

        sweeper = threading.Thread(target=sweep)
        sweeper.start()
        try:
            self.assertFalse(swept.wait(0.1))
        finally:
            release.set()
            writer.join(2)
            sweeper.join(2)
        self.assertTrue(swept.is_set())


class HttpReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server("127.0.0.1", 0, "http://app.test", self.temp.name, max_workers=2)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, path, method="GET", fields=None, headers=None):
        supplied = {"Host": "app.test", **(headers or {})}
        body = None
        if method == "POST":
            supplied.setdefault("Origin", "http://app.test")
            supplied.setdefault("Content-Type", "application/x-www-form-urlencoded")
            body = urlencode(fields or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        connection.request(method, path, body, supplied)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    def workspace(self):
        response, _ = self.request("/start", "POST")
        self.assertEqual(303, response.status)
        return response.getheader("Location")

    def save(self, workspace, fields):
        response, body = self.request(workspace + "/cases", "POST", fields)
        self.assertEqual(303, response.status, body)
        return response.getheader("Location")

    def test_idle_partial_and_trickled_connections_do_not_use_worker_slots(self):
        connections = [socket.create_connection(("127.0.0.1", self.port), timeout=3) for _ in range(3)]
        try:
            connections[1].sendall(b"GET / HTTP/1.1\r\nHost: app.test\r\n")
            connections[2].sendall(b"POST /start HTTP/1.1\r\nHost: app.test\r\nContent-Length: 100\r\n\r\nx")
            response, data = self.request("/healthz")
            self.assertEqual((200, b"ok"), (response.status, data))
            started = time.monotonic()
            while time.monotonic() - started < INTAKE_TIMEOUT + 0.2:
                try:
                    connections[2].sendall(b"x")
                except OSError:
                    break
                time.sleep(0.1)
            time.sleep(0.2)
            for connection in connections:
                try:
                    self.assertEqual(b"", connection.recv(1))
                except ConnectionResetError:
                    pass
            self.assertEqual(200, self.request("/healthz")[0].status)
        finally:
            for connection in connections:
                connection.close()

    def test_handler_has_absolute_response_deadline(self):
        handler = object.__new__(Handler)
        timer = Mock()
        with patch("app.threading.Timer", return_value=timer) as factory, \
                patch("http.server.BaseHTTPRequestHandler.handle"):
            handler.handle()
        factory.assert_called_once_with(REQUEST_TIMEOUT, handler._expire_connection)
        timer.start.assert_called_once_with()
        timer.cancel.assert_called_once_with()
        self.assertTrue(timer.daemon)

    def test_full_cap_keeps_workspace_case_export_and_download_available(self):
        workspace = self.workspace()
        with patch("app.utc_today", return_value=TODAY):
            case = self.save(workspace, dict(BASE_FIELDS, delivered_date="2026-11-10", status=""))
            self.server.app.store.cap = 0
            for path in (workspace, case, case + "/message.txt", workspace + "/export"):
                with self.subTest(path=path):
                    self.assertEqual(200, self.request(path)[0].status)
            self.assertEqual(303, self.request(case + "/delete", "POST")[0].status)

    def test_workspace_loads_once_shows_next_deadline_and_never_scans_on_get(self):
        workspace = self.workspace()
        with patch("app.utc_today", return_value=TODAY):
            for _ in range(3):
                self.save(workspace, dict(BASE_FIELDS, delivered_date="2026-11-10"))
            store = self.server.app.store
            with patch.object(store, "read", wraps=store.read) as reader, \
                    patch.object(store, "_usage", side_effect=AssertionError("scan")), \
                    patch.object(store, "_write_locked", side_effect=AssertionError("write")):
                response, body = self.request(workspace)
                self.assertEqual(200, response.status)
                self.assertEqual(1, reader.call_count)
                self.assertIn(b"2026-12-10", body)
                self.assertIn(b"25 days remaining", body)

    def test_form_contact_flow_wait_issuer_confirm_download_and_export(self):
        workspace = self.workspace()
        fields = dict(BASE_FIELDS, problem_type="charged_twice",
                      charges="80, 2026-10-02, settled\n80, 2026-10-03, settled")
        with patch("app.utc_today", return_value=TODAY):
            case = self.save(workspace, fields)
            self.assertEqual(303, self.request(case + "/contact", "POST", contact("2026-11-11"))[0].status)
            response, body = self.request(case)
            self.assertEqual(200, response.status)
            self.assertIn(b"Wait until 2026-11-18", body)
        with patch("app.utc_today", return_value=date(2026, 11, 18)):
            response, body = self.request(case)
            self.assertEqual(200, response.status)
            self.assertIn(b"Talk to your card issuer", body)
            self.assertEqual(409, self.request(case + "/message.txt")[0].status)
            token, cid = workspace.split("/")[-1], case.split("/")[-1]
            row = self.server.app.store.read(token)["cases"][cid]
            raw = deepcopy(row["input"])
            raw["selfserve"]["contacts"] = row["contacts"]
            art = check(parse_json(json.dumps(raw)), date(2026, 11, 18))["artefact"]
            confirmation = {"packet_hash": art.packet_hash,
                            **{f"fact_{i}": "yes" for i in range(len(art.facts))}}
            self.assertEqual(303, self.request(case + "/confirm", "POST", confirmation)[0].status)
            response, text = self.request(case + "/message.txt")
            self.assertEqual(200, response.status)
            self.assertIn(b"SELF-REPORTED FACT SHEET", text)
            exported = json.loads(self.request(workspace + "/export")[1])
            self.assertEqual("2026-11-11", exported["cases"][cid]["contacts"][0]["date"])

    def test_proxy_host_alias_and_public_origin_validation(self):
        self.server.app.proxy_hosts.add("internal:8080")
        self.assertEqual(200, self.request("/", headers={"Host": "internal:8080"})[0].status)
        self.assertEqual(303, self.request("/start", "POST", headers={"Host": "internal:8080"})[0].status)
        self.assertEqual(403, self.request("/start", "POST", headers={"Host": "internal:8080", "Origin": "https://evil.test"})[0].status)
        self.assertEqual(400, self.request("/", headers={"Host": "wrong.test", "X-Forwarded-Host": "app.test"})[0].status)
        self.assertEqual(303, self.request("/start", "POST", headers={"Host": "rewritten:8080"})[0].status)

    def test_csp_and_copy_script_have_no_inline_execution(self):
        response, body = self.request("/")
        self.assertNotIn("unsafe-inline", response.getheader("Content-Security-Policy"))
        self.assertNotIn(b"onclick=", body)
        self.assertIn(b"/static/copy.js", body)
        response, script = self.request("/static/copy.js")
        self.assertEqual(200, response.status)
        self.assertIn(b"navigator.clipboard.writeText", script)
        workspace = self.workspace()
        with patch("app.utc_today", return_value=TODAY):
            case = self.save(workspace, dict(BASE_FIELDS, delivered_date="2026-11-10"))
            body = self.request(case)[1]
        self.assertIn(b"id='copy-message'", body)
        self.assertNotIn(b"onclick=", body)

    def test_exception_details_hidden_and_privacy_rejections_remain_clear(self):
        workspace = self.workspace()
        # bounded_call uses a spawned interpreter. Patching parse_form cannot
        # cross that process boundary, so the old test's claimed failure was
        # impossible. Inject at the actual parent-process compute boundary.
        with patch("app.bounded_call", side_effect=ValueError("private-secret-value")):
            response, body = self.request(workspace + "/cases", "POST", BASE_FIELDS)
        self.assertEqual(400, response.status)
        self.assertNotIn(b"private-secret-value", body)
        response, body = self.request(workspace + "/cases", "POST",
                                      dict(BASE_FIELDS, note="4111 1111 1111 1111"))
        self.assertEqual(400, response.status)
        self.assertIn(b"Card or bank account numbers are not accepted", body)
        self.assertNotIn(b"4111", body)

    def test_get_admission_is_bounded_but_health_stays_available(self):
        with patch.object(self.server.app.store, "allow_get", side_effect=RateLimitError()):
            self.assertEqual(429, self.request("/")[0].status)
            self.assertEqual(200, self.request("/healthz")[0].status)

    def test_unknown_workspace_post_cannot_create_rate_state(self):
        response, _ = self.request("/w/" + "a" * 32 + "/cases", "POST", BASE_FIELDS)
        self.assertEqual(404, response.status)
        self.assertEqual({}, self.server.app.store._workspace_posts)


if __name__ == "__main__":
    unittest.main()
