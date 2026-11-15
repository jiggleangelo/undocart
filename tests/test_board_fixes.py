"""One or more regression tests per board / reviewer fix."""
import http.client
import json
import re
import tempfile
import threading
import unittest
from copy import deepcopy
from datetime import date, timedelta
from unittest.mock import patch
from urllib.parse import urlencode

import app as app_module
from app import make_server
from app_review_patch import MADE_UP_NOTICE, relabel_tool_dates, sample_for_today
from remedy.review_fixes import guard
from remedy.sample_data import RAWS
from remedy.selfserve import InputError

TODAY = date(2026, 11, 15)
BASE_FIELDS = {
    "asked_item": "Shoes", "merchant_name": "Shop", "order_label": "Order-A",
    "item_title": "Shoes", "unit_price": "80", "total": "80", "currency": "USD",
    "quantity": "1", "ordered_quantity": "1", "order_date": "2026-10-01",
    "return_days": "30", "problem_type": "wrong_item", "delivered_date": "2026-11-10",
}


class UnitTests(unittest.TestCase):
    def test_sample_dates_move_with_today(self):
        today = date(2027, 3, 1)
        value = sample_for_today(RAWS[0], today)
        self.assertTrue(value["selfserve"]["sample"])
        self.assertTrue(value["order"]["delivered_at"].startswith((today - timedelta(days=4)).isoformat()))

    def test_tool_deadline_is_not_tagged_as_entered(self):
        text = "My understanding of the return deadline is 2026-11-20. [you entered]\nOrder total: 80 [you entered]"
        out = relabel_tool_dates(text)
        self.assertNotIn("deadline is 2026-11-20. [you entered]", out)
        self.assertIn("calculated by this tool", out)
        self.assertIn("Order total: 80 [you entered]", out)

    def test_marketplace_order_number_is_allowed_in_label_only(self):
        guard({"order": {"order_id": "123-4567890-1234567"}}, InputError)
        guard({"order_label": "123-4567890-1234569"}, InputError)
        with self.assertRaises(InputError):
            guard({"claim": {"note": "a@b.example"}}, InputError)


class HttpTests(unittest.TestCase):
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
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    def workspace(self):
        response, _ = self.request("/start", "POST")
        return response.getheader("Location")

    def save(self, workspace, fields):
        response, body = self.request(workspace + "/cases", "POST", fields)
        self.assertEqual(303, response.status, body)
        return response.getheader("Location")

    def test_home_has_sample_button_notice_and_privacy(self):
        body = self.request("/")[1]
        self.assertIn(b"Try with sample data", body)
        self.assertIn(b"manykiln.com/privacy", body)
        response, privacy = self.request("/privacy")
        self.assertEqual(200, response.status)
        self.assertIn(b"manykiln.com/privacy", privacy)

    def test_sample_workspace_is_labelled_made_up_and_has_core_actions(self):
        response, _ = self.request("/sample", "POST")
        self.assertEqual(303, response.status)
        workspace = response.getheader("Location")
        body = self.request(workspace)[1].decode()
        self.assertIn("Made-up sample data", body)
        self.assertIn("made-up sample", body)
        self.assertIn("Export everything", body)
        self.assertIn("Delete this workspace now", body)
        self.assertIn("name='unit_price'", body)  # core job on the first screen
        self.assertIn("manykiln.com/privacy", body)
        link = re.search(r"href='(/w/[^']+/c/sample-1)'", body).group(1)
        case = self.request(link)[1].decode()
        self.assertIn("made-up sample data", case)
        self.assertNotIn("[you entered]", case)
        self.assertIn("Export everything", case)
        self.assertIn("Delete this workspace now", case)

    def test_every_workspace_page_has_export_and_delete(self):
        workspace = self.workspace()
        with patch("app.utc_today", return_value=TODAY):
            case = self.save(workspace, BASE_FIELDS)
            for path in (workspace, workspace + "/new", case, case + "/edit"):
                body = self.request(path)[1].decode()
                self.assertIn("Export everything", body, path)
                self.assertIn("Delete this workspace now", body, path)
                self.assertIn("manykiln.com/privacy", body, path)

    def test_workspace_list_uses_one_computation(self):
        workspace = self.workspace()
        with patch("app.utc_today", return_value=TODAY):
            for _ in range(3):
                self.save(workspace, BASE_FIELDS)
            real = app_module.bounded_call
            calls = []

            def counting(*args, **kwargs):
                calls.append(1)
                return real(*args, **kwargs)

            with patch("app.bounded_call", side_effect=counting):
                response, _ = self.request(workspace)
        self.assertEqual(200, response.status)
        self.assertEqual(1, len(calls))

    def test_empty_json_submission_gets_json_message(self):
        workspace = self.workspace()
        response, body = self.request(workspace + "/cases", "POST", {"case_json": ""})
        self.assertEqual(400, response.status)
        self.assertIn(b"Paste case JSON", body)
        self.assertNotIn(b"Please fill in", body)

    def test_json_editor_contact_edits_are_kept(self):
        workspace = self.workspace()
        with patch("app.utc_today", return_value=TODAY):
            case = self.save(workspace, BASE_FIELDS)
            self.assertEqual(303, self.request(case + "/contact", "POST",
                                               {"date": "2026-11-01", "channel": "chat", "response": "none"})[0].status)
            token, cid = workspace.split("/")[-1], case.split("/")[-1]
            row = self.server.app.store.read(token)["cases"][cid]
            raw = deepcopy(row["input"])
            raw["selfserve"]["contacts"] = []
            response, body = self.request(case + "/edit", "POST", {"case_json": json.dumps(raw)})
            self.assertEqual(303, response.status, body)
        exported = json.loads(self.request(workspace + "/export")[1])
        self.assertEqual([], exported["cases"][cid]["contacts"])

    def test_json_help_text_matches_real_limits(self):
        workspace = self.workspace()
        body = self.request(workspace)[1]
        self.assertNotIn(b"request limit 1 MB", body)
        self.assertIn(b"8 KB", body)


if __name__ == "__main__":
    unittest.main()
