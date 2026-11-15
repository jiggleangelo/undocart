"""Optional real-browser regression; enable with REMEDY_BROWSER_TESTS=1.

Requires Playwright and its Chromium/Firefox browsers in the test environment.
The application itself remains standard-library-only.
"""
import os
import threading
import unittest

from demo import make_server


@unittest.skipUnless(os.environ.get("REMEDY_BROWSER_TESTS") == "1", "real-browser suite is opt-in")
class BrowserFlowTests(unittest.TestCase):
    def test_real_browser_intake_confirm_and_reject(self):
        from playwright.sync_api import sync_playwright

        server = make_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with sync_playwright() as playwright:
                for browser_type in (playwright.chromium, playwright.firefox):
                    with self.subTest(browser=browser_type.name):
                        browser = browser_type.launch()
                        try:
                            page = browser.new_page()
                            posts = []
                            page.on("request", lambda request: posts.append(request) if request.method == "POST" else None)
                            page.goto(origin + "/new")
                            page.get_by_role("button", name="Classify", exact=True).click()
                            page.wait_for_url("**/case/custom-*")
                            self.assertIn("Route C", page.locator("body").inner_text())
                            for checkbox in page.locator("input[type=checkbox]").all():
                                checkbox.check()
                            page.get_by_role("button", name="Confirm and send (simulated)", exact=True).click()
                            page.get_by_text("Simulated receipt:", exact=False).wait_for()
                            self.assertIn("FAKE-", page.locator("body").inner_text())
                            page.goto(origin + "/case/sample-6")
                            page.get_by_role("button", name="Reject", exact=True).click()
                            page.get_by_text("The frozen result was rejected.", exact=False).wait_for()
                            self.assertEqual(3, len(posts))
                            for request in posts:
                                self.assertEqual(origin, request.all_headers().get("origin"))
                        finally:
                            browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
