import unittest
from copy import deepcopy

from remedy.classify import classify
from remedy.models import Case
from remedy.sample_data import DEMO_TODAY, EXPECTED, RAWS, SAMPLE_CASES, make_sources


class ClassifyTests(unittest.TestCase):
    def test_samples(self):
        sources = make_sources()
        self.assertEqual(EXPECTED, [classify(case, sources, DEMO_TODAY).sub for case in SAMPLE_CASES.values()])

    def test_deviation_and_unverified_claim_never_create_c(self):
        for key in ("sample-1", "sample-8"):
            self.assertNotEqual("C", classify(SAMPLE_CASES[key], make_sources(), DEMO_TODAY).route)

    def test_contact_precondition(self):
        self.assertEqual("B_merchant_first", classify(SAMPLE_CASES["sample-5"], make_sources(), DEMO_TODAY).sub)
        self.assertEqual("C_non_receipt", classify(SAMPLE_CASES["sample-6"], make_sources(), DEMO_TODAY).sub)

    def test_missing_and_not_as_described_hold(self):
        raw = deepcopy(RAWS[0]); raw["order"]["currency"] = None
        self.assertEqual("HOLD", classify(Case.from_dict(raw), make_sources(), DEMO_TODAY).route)
        raw = deepcopy(RAWS[0]); raw["claim"]["reason"] = "not_as_described"
        self.assertEqual("HOLD", classify(Case.from_dict(raw), make_sources(), DEMO_TODAY).route)

    def test_unknown_merchant_policy_holds(self):
        raw = deepcopy(RAWS[0])
        raw["order"]["merchant_id"] = "merchant_unknown"
        decision = classify(Case.from_dict(raw), make_sources(), DEMO_TODAY)
        self.assertEqual(("HOLD", "HOLD_policy"), (decision.route, decision.sub))
        self.assertIn("policy.merchant_record", decision.missing)

    def test_undercharge_and_split_charges_are_not_amount_mismatches(self):
        raw = deepcopy(RAWS[7])
        raw["claim"]["reason"] = "wrong_amount"
        raw["order"]["total"] = "100.00"
        case = Case.from_dict(raw)
        sources = make_sources()
        sources.merchant.confirmations[case.order.order_id]["total"] = "100.00"
        sources.ledger.data[case.order.card_ref] = [
            {"amount": "50.00", "currency": "USD", "merchant_id": case.order.merchant_id,
             "order_id": case.order.order_id, "status": "settled", "at": "2026-10-02T10:02:00+00:00"},
            {"amount": "50.00", "currency": "USD", "merchant_id": case.order.merchant_id,
             "order_id": case.order.order_id, "status": "settled", "at": "2026-10-02T10:03:00+00:00"},
        ]
        self.assertEqual("B_goodwill", classify(case, sources, DEMO_TODAY).sub)
        sources.ledger.data[case.order.card_ref] = [sources.ledger.data[case.order.card_ref][0]]
        self.assertEqual("B_goodwill", classify(case, sources, DEMO_TODAY).sub)

    def test_equal_installments_are_not_duplicate_charges(self):
        raw = deepcopy(RAWS[7])
        raw["claim"]["reason"] = "charged_twice"
        raw["order"]["total"] = "100.00"
        case = Case.from_dict(raw)
        sources = make_sources()
        sources.merchant.confirmations[case.order.order_id]["total"] = "100.00"
        sources.ledger.data[case.order.card_ref] = [
            {"amount": "50.00", "currency": "USD", "merchant_id": case.order.merchant_id,
             "order_id": case.order.order_id, "status": "settled", "at": "2026-10-02T10:02:00+00:00"},
            {"amount": "50.00", "currency": "USD", "merchant_id": case.order.merchant_id,
             "order_id": case.order.order_id, "status": "settled", "at": "2026-10-02T10:03:00+00:00"},
        ]
        self.assertEqual("B_goodwill", classify(case, sources, DEMO_TODAY).sub)

    def test_deterministic(self):
        case = SAMPLE_CASES["sample-7"]
        self.assertEqual(classify(case, make_sources(), DEMO_TODAY).to_dict(),
                         classify(case, make_sources(), DEMO_TODAY).to_dict())


if __name__ == "__main__": unittest.main()
