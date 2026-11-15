"""Regression tests for the third outside review; one or more per fix."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import urlencode

import app as app_module
from app import make_server
from remedy.selfserve import check, parse_json
from remedy.store import (CREATE_HEADROOM, CapacityError, MAX_CASES, MAX_WORKSPACE_BYTES,
                          RateLimitError, Store)

TODAY = date(2026, 11, 15)
BASE_FIELDS = {
    "asked_item": "Shoes", "merchant_name": "Shop", "order_label": "Order-A",
    "item_title": "Shoes", "unit_price": "80", "total": "80", "currency": "USD",
    "quantity": "1", "ordered_quantity": "1", "order_date": "2026-10-01",
    "return_days": "30", "problem_type": "wrong_item",
}
CONTACT = {"date": "2026-10-20", "channel": "chat", "response": "none"}


def now():
    return datetime.now(timezone.utc)


class StoreRound3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_cases_are_kept_ninety_days_and_expiry_leaves_tombstone(self):
        token = self.store.create()
        self.store.put_case(token, "one", {"case_id": "one"})
        self.assertEqual(0, self.store.sweep(now() + timedelta(days=31)))
        self.assertEqual(0, self.store.sweep(now() + timedelta(days=89)))
        self.assertTrue(self.store.exists(token))
        self.assertEqual(1, self.store.sweep(now() + timedelta(days=91)))
        self.assertFalse(self.store.exists(token))
        self.assertEqual("expired", self.store.gone(token))

    def test_empty_workspaces_expire_after_one_day_only(self):
        empty = self.store.create()
        full = self.store.create()
        self.store.put_case(full, "one", {"case_id": "one"})
        self.store.sweep(now() + timedelta(days=2))
        self.assertFalse(self.store.exists(empty))
        self.assertTrue(self.store.exists(full))

    def test_creation_and_global_post_budgets(self):
        with patch("remedy.store.CREATE_LIMIT", 3):
            for _ in range(3):
                self.store.create()
            with self.assertRaises(RateLimitError):
                self.store.create()
        with patch("remedy.store.GLOBAL_POST_LIMIT", 2):
            self.store.allow_post(None)
            self.store.allow_post(None)
            with self.assertRaises(RateLimitError):
                self.store.allow_post(None)

    def test_creation_stops_before_cap_so_existing_users_can_save(self):
        tokens = [self.store.create() for _ in range(10)]
        for token in tokens:
            self.store.put_case(token, "one", {"case_id": "one", "padding": "x" * 2000})
        self.store.cap = int(self.store._bytes / CREATE_HEADROOM) - 1
        with self.assertRaises(CapacityError):
            self.store.create()
        self.store.add_contact(tokens[0], "one", CONTACT)
        self.assertEqual(1, len(self.store.read(tokens[0])["cases"]["one"]["contacts"]))

    def test_anonymous_get_flood_does_not_block_owner(self):
        token = self.store.create()
        with patch("remedy.store.GET_LIMIT", 2):
            self.store.allow_get(None)
            self.store.allow_get("unknown-token")
            with self.assertRaises(RateLimitError):
                self.store.allow_get(None)
            for _ in range(5):
                self.store.allow_get(token)

    def test_spec_limits_layout_timestamps_and_delete_tombstone(self):
        self.assertEqual(20, MAX_CASES)
        self.assertEqual(5 * 1024 * 1024, MAX_WORKSPACE_BYTES)
        token = self.store.create()
        digest = self.store.digest(token)
        self.assertTrue((self.store.ws / digest / "state.json").is_file())
        state = self.store.read(token)
        for key in ("created", "updated", "last_seen", "keep_until"):
            self.assertIn(key, state)
        for n in range(MAX_CASES):
            self.store.put_case(token, f"c{n}", {"case_id": f"c{n}"})
        with self.assertRaises(CapacityError):
            self.store.put_case(token, "extra", {"case_id": "extra"})
        self.store.delete(token)
        self.assertEqual("deleted", self.store.gone(token))
        self.assertIsNone(self.store.gone("never-existed"))
        with open(self.store.tombstones / digest, "rb") as stream:
            self.assertNotIn(token.encode(), stream.read())

    def test_legacy_layout_is_read_and_migrated(self):
        token = "t" * 32
        digest = Store.digest(token)
        legacy = os.path.join(self.temp.name, digest)
        os.mkdir(legacy)
        with open(os.path.join(legacy, "workspace.json"), "w") as stream:
            json.dump({"cases": {}}, stream)
        store = Store(self.temp.name)
        self.assertTrue(store.exists(token))
        store.put_case(token, "one", {"case_id": "one"})
        self.assertTrue((store.ws / digest / "state.json").is_file())
        self.assertFalse(os.path.exists(legacy))
        self.assertEqual(store._usage(), store._bytes)

    def test_orphan_temp_files_are_removed_on_delete_and_restart(self):
        token = self.store.create()
        directory = self.store.ws / self.store.digest(token)
        (directory / ".workspace-deadbeef.tmp").write_bytes(b'{"cases":{"secret":1}}')
        self.store.delete(token)
        self.assertFalse(directory.exists())
        token = self.store.create()
        orphan = self.store.ws / self.store.digest(token) / ".workspace-cafebabe.tmp"
        orphan.write_bytes(b'{"cases":{}}')
        restarted = Store(self.temp.name)
        self.assertFalse(orphan.exists())
        self.assertEqual(restarted._usage(), restarted._bytes)

    def test_directory_is_fsynced_after_replace(self):
        with patch("remedy.store._fsync_dir") as fsync:
            token = self.store.create()
        synced = [call.args[0] for call in fsync.call_args_list]
        self.assertIn(self.store.ws / self.store.digest(token), synced)


class HttpRound3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server("127.0.0.1", 0, "http://app.test", self.temp.name, max_workers=2)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.app.stop.set()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, path, method="GET", fields=None):
        headers = {"Host": "app.test"}
        body = None
        if method == "POST":
            headers["Origin"] = "http://app.test"
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body = urlencode(fields or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=6)
        connection.request(method, path, body, headers)
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

    def test_workspace_page_discloses_retention_and_expired_link_explains(self):
        workspace = self.workspace()
        body = self.request(workspace)[1]
        self.assertIn(b"90 days", body)
        self.assertIn(b"1 day", body)
        with patch("app.utc_today", return_value=TODAY):
            self.save(workspace, dict(BASE_FIELDS, delivered_date="2026-11-10"))
            body = self.request(workspace)[1]
        self.assertIn(b"at least until", body)
        self.server.app.store.sweep(now() + timedelta(days=91))
        response, body = self.request(workspace)
        self.assertEqual(404, response.status)
        self.assertIn(b"removed automatically", body)

    def test_blank_price_total_or_date_is_rejected_not_invented(self):
        workspace = self.workspace()
        fields = dict(BASE_FIELDS)
        for name in ("unit_price", "total", "order_date"):
            fields.pop(name)
        response, body = self.request(workspace + "/cases", "POST", fields)
        self.assertEqual(400, response.status)
        self.assertIn(b"Please fill in", body)
        exported = json.loads(self.request(workspace + "/export")[1])
        self.assertEqual({}, exported["cases"])

    def test_edit_compute_runs_without_holding_store_lock(self):
        workspace = self.workspace()
        with patch("app.utc_today", return_value=TODAY):
            case = self.save(workspace, dict(BASE_FIELDS, delivered_date="2026-11-10"))
        lock = self.server.app.store._lock
        real = app_module.bounded_call
        seen = []

        def probe(*args, **kwargs):
            result = []

            def attempt():
                acquired = lock.acquire(timeout=1)
                if acquired:
                    lock.release()
                result.append(acquired)
            other = threading.Thread(target=attempt)
            other.start()
            other.join(2)
            seen.append(bool(result and result[0]))
            return real(*args, **kwargs)

        with patch("app.utc_today", return_value=TODAY), patch("app.bounded_call", side_effect=probe):
            response, body = self.request(case + "/edit", "POST", dict(BASE_FIELDS, delivered_date="2026-11-10", merchant_name="New"))
        self.assertEqual(303, response.status, body)
        self.assertTrue(seen)
        self.assertTrue(all(seen))

    def test_confirmed_issuer_sheet_stays_downloadable_next_day(self):
        workspace = self.workspace()
        fields = dict(BASE_FIELDS, problem_type="charged_twice",
                      charges="80, 2026-10-02, settled\n80, 2026-10-03, settled")
        with patch("app.utc_today", return_value=TODAY):
            case = self.save(workspace, fields)
            self.assertEqual(303, self.request(case + "/contact", "POST", dict(CONTACT, date="2026-11-11"))[0].status)
        day = date(2026, 11, 18)
        token, cid = workspace.split("/")[-1], case.split("/")[-1]
        row = self.server.app.store.read(token)["cases"][cid]
        raw = deepcopy(row["input"])
        raw["selfserve"]["contacts"] = row["contacts"]
        art = check(parse_json(json.dumps(raw)), day)["artefact"]
        confirmation = {"packet_hash": art.packet_hash, **{f"fact_{i}": "yes" for i in range(len(art.facts))}}
        with patch("app.utc_today", return_value=day):
            self.assertEqual(303, self.request(case + "/confirm", "POST", confirmation)[0].status)
        with patch("app.utc_today", return_value=day + timedelta(days=1)):
            response, text = self.request(case + "/message.txt")
        self.assertEqual(200, response.status)
        self.assertIn(b"SELF-REPORTED FACT SHEET", text)
        self.assertEqual(day.isoformat(), self.server.app.store.read(token)["cases"][cid]["confirmed_on"])


if __name__ == "__main__":
    unittest.main()
