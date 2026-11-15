import html
import http.client
import json
import os
import tempfile
import threading
import unittest
from urllib.parse import urlencode

from app import NOTICE, make_server


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server("127.0.0.1", 0, "http://app.test", self.temp.name)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.app.stop.set()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        # A None value means that the header is intentionally absent. Passing
        # None to http.client is invalid and fails before a request reaches the
        # application, so remove such entries before constructing the request.
        supplied = {key: value for key, value in (headers or {}).items() if value is not None}
        h = {"Host": "app.test", **supplied}
        if method == "POST":
            h.setdefault("Origin", "http://app.test")
            h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        connection.request(method, path, body, h)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    def start(self):
        response, _ = self.request("POST", "/start", b"")
        return response.getheader("Location")

    def test_health_notice_token_and_delete(self):
        response, data = self.request("GET", "/healthz")
        self.assertEqual((200, b"ok"), (response.status, data))
        response, data = self.request("GET", "/")
        rendered = html.unescape(data.decode("utf-8"))
        self.assertIn(NOTICE, rendered)
        self.assertIn("manykiln.com/privacy", rendered)
        path = self.start()
        token = path.split("/")[-1]
        self.assertGreaterEqual(len(token), 32)
        for root, _, files in os.walk(self.temp.name):
            for name in files:
                with open(os.path.join(root, name), "rb") as stream:
                    self.assertNotIn(token.encode(), stream.read())
        response, _ = self.request("POST", path + "/delete", b"")
        self.assertEqual(303, response.status)
        self.assertEqual(404, self.request("GET", path)[0].status)

    def test_realistic_return_flow_export_and_security(self):
        workspace = self.start()
        fields = {
            "asked_item": "shoes",
            "asked_size": "10",
            "actual_size": "11",
            "merchant_name": "Shop",
            "order_label": "A-1",
            "item_title": "Shoes",
            "unit_price": "80",
            "quantity": "1",
            "ordered_quantity": "1",
            "total": "80",
            "currency": "USD",
            "order_date": "2026-10-20",
            "status": "delivered",
            "delivered_date": "2026-10-22",
            "return_days": "30",
            "problem_type": "wrong_item",
        }
        response, _ = self.request("POST", workspace + "/cases", urlencode(fields))
        self.assertEqual(303, response.status)
        case = response.getheader("Location")
        response, data = self.request("GET", case)
        self.assertIn(b"Return or cancel now", data)
        self.assertIn(b"size", data)
        self.assertIn(b"AI shopping agent", data)
        response, data = self.request("GET", workspace + "/export")
        self.assertTrue(json.loads(data)["cases"])
        self.assertEqual(400, self.request("GET", "/", headers={"Host": "wrong.test"})[0].status)
        self.assertEqual(403, self.request("POST", "/start", b"", {"Origin": "null"})[0].status)
        self.assertEqual(
            303,
            self.request(
                "POST",
                "/start",
                b"",
                {"Origin": None, "Referer": "http://app.test/"},
            )[0].status,
        )

    def test_body_cap(self):
        response, _ = self.request("POST", "/start", b"x" * (1024 * 1024 + 1))
        self.assertEqual(413, response.status)


if __name__ == "__main__":
    unittest.main()
