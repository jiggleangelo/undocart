import html
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import date

from remedy.review_fixes import guard
from remedy.sample_data import RAWS
from remedy.selfserve import InputError, check, parse_form, parse_json
from remedy.selfserve_worker import _context


class ReviewFixTests(unittest.TestCase):
    def test_delivered_without_date_is_missing(self):
        raw = deepcopy(RAWS[0])
        raw["order"]["status"] = "delivered"
        raw["order"]["delivered_at"] = None
        raw["selfserve"] = {"return_days": 30, "final_sale": False, "charges": []}
        result = check(parse_json(json.dumps(raw)), date(2026, 10, 24))
        self.assertEqual("Missing info", result["route"])
        self.assertIn("Delivered date", result["missing"])

    def test_phone_shaped_order_label_is_allowed(self):
        fields = {
            "order_label": "123-456-7890", "unit_price": "10", "total": "10",
            "order_date": "2026-10-20", "quantity": "1", "ordered_quantity": "1"
        }
        case = parse_form(fields)
        self.assertEqual("123-456-7890", case.order.order_id)

    def test_phone_in_free_text_is_rejected(self):
        with self.assertRaises(InputError):
            guard({"claim": {"note": "Call 123-456-7890"}}, InputError)

    def test_sample_attribution_is_not_you_entered(self):
        raw = deepcopy(RAWS[6])
        raw["selfserve"] = {"return_days": 30, "final_sale": False, "charges": [], "sample": True}
        case = parse_json(json.dumps(raw))
        result = check(case, date(2026, 10, 24))
        self.assertNotIn("[you entered]", result["artefact"].text.lower())

    def test_worker_uses_spawn(self):
        self.assertEqual("spawn", _context.get_start_method())


if __name__ == "__main__":
    unittest.main()
