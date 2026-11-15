from copy import deepcopy
from datetime import date
from decimal import Decimal

from .models import Case
from .sources import FakeCarrierSource, FakeLedgerSource, FakeMerchantSource, FakePolicySource, Sources

DEMO_TODAY = date(2026, 10, 24)
STORIES = {
    "sample-1": "Agent bought size 11 instead of 10; delivered four days ago.",
    "sample-2": "Agent ordered three lamps instead of one; not shipped.",
    "sample-3": "Wrong-colour headphones arrived outside the return window.",
    "sample-4": "An unwanted final-sale item.",
    "sample-5": "Package is late and the merchant has not been contacted.",
    "sample-6": "Late package; merchant contacted nine days ago without response.",
    "sample-7": "The ledger independently shows two identical charges.",
    "sample-8": "Consumer says charged twice, but the ledger shows one charge.",
}
EXPECTED = ["A_return", "A_cancel", "B_goodwill", "B_goodwill", "B_merchant_first",
            "C_non_receipt", "C_duplicate_charge", "B_goodwill"]


def _raw(n, reason="unwanted", status="delivered", delivered="2026-10-20T12:00:00+00:00",
         category="shoes", qty=1, wanted_qty=1, attributes=None, wanted=None, total="80.00", contacted=None):
    attributes = attributes or {"size": "10", "color": "black"}
    wanted = wanted or {"size": "10"}
    return {
        "case_id": f"sample-{n}",
        "mandate": {"mandate_id": f"m_{n}", "principal_ref": f"u_demo_{n}",
            "intent": "made-up shopping request", "issued_at": "2026-10-01T09:00:00+00:00",
            "constraints": {"max_unit_price": "120.00", "max_total": "150.00", "quantity": wanted_qty,
                "allowed_categories": [category], "required_attributes": wanted, "deliver_by": "2026-10-15"}},
        "order": {"order_id": f"order_{n}", "merchant_id": "merchant_demo",
            "items": [{"sku": f"sku_{n}", "title": f"Demo item {n}", "category": category,
                "qty": qty, "unit_price": total, "attributes": attributes}],
            "total": str(Decimal(total) * qty), "currency": "USD", "ordered_at": "2026-10-02T10:00:00+00:00",
            "card_ref": f"card_tok_{n}", "status": status, "promised_delivery": "2026-10-12",
            "delivered_at": delivered},
        "events": [{"at": "2026-10-02T10:00:00+00:00", "actor": "agent", "kind": "order_placed",
                    "detail": "Made-up order created"}],
        "claim": {"reason": reason, "merchant_contacted_at": contacted,
                  "merchant_response": "none" if contacted else None, "note": "Made-up sample note"}
    }


RAWS = [
    _raw(1, "wrong_item", attributes={"size": "11"}),
    _raw(2, "wrong_item", status="placed", delivered=None, category="lamps", qty=3, wanted_qty=1, total="20.00"),
    _raw(3, "wrong_item", delivered="2026-09-09T12:00:00+00:00", category="headphones",
         attributes={"color": "red"}, wanted={"color": "blue"}),
    _raw(4, "unwanted", category="final-sale"),
    _raw(5, "not_received", status="shipped", delivered=None),
    _raw(6, "not_received", status="shipped", delivered=None, contacted="2026-10-15T09:00:00+00:00"),
    _raw(7, "charged_twice", total="50.00"),
    _raw(8, "charged_twice", total="60.00"),
]
SAMPLE_CASES = {raw["case_id"]: Case.from_dict(raw) for raw in RAWS}

POLICIES = {"merchant_demo": {"return_days": 30, "non_returnable": ["final-sale"],
                                "cancel_supported": True, "clause": "30-day returns"}}
CARRIER = {
    "order_5": {"status": "in_transit", "promised_delivery": "2026-10-12", "checked_at": "2026-10-24"},
    "order_6": {"status": "in_transit", "promised_delivery": "2026-10-12", "checked_at": "2026-10-24"},
}
LEDGER = {
    "card_tok_7": [
        {"amount": "50.00", "currency": "USD", "merchant_id": "merchant_demo", "order_id": "order_7", "status": "settled", "at": "2026-10-02T10:02:00+00:00"},
        {"amount": "50.00", "currency": "USD", "merchant_id": "merchant_demo", "order_id": "order_7", "status": "settled", "at": "2026-10-02T10:03:00+00:00"}],
    "card_tok_8": [{"amount": "60.00", "currency": "USD", "merchant_id": "merchant_demo", "order_id": "order_8", "status": "settled", "at": "2026-10-02T10:02:00+00:00"}],
}
CONFIRMATIONS = {}
for n, raw in enumerate(RAWS, 1):
    order = raw["order"]
    CONFIRMATIONS[f"order_{n}"] = {
        "total": order["total"], "currency": order["currency"], "status": order["status"],
        "ordered_at": order["ordered_at"], "promised_delivery": order["promised_delivery"],
        "delivered_at": order["delivered_at"],
    }
CONFIRMATIONS["order_2"]["cancel_deadline"] = "2026-10-25"
CANCELLATIONS = {
    "fixture_cancelled_order": {"before_shipment": True, "amount": "80.00", "at": "2026-10-03T10:00:00+00:00"}
}
REFUNDS = {
    "fixture_refund_order": {"amount": "80.00", "at": "2026-10-03T10:00:00+00:00"}
}
CONTACTS = {
    "order_6": {"actor": "merchant", "contacted_at": "2026-10-15T09:00:00+00:00", "response": "none"}
}
# These bindings belong to the independent fake fixture, not claimant input.
BINDINGS = {f"order_{n}": (f"u_demo_{n}", f"card_tok_{n}", "merchant_demo") for n in range(1, 9)}


def make_sources():
    return Sources(FakePolicySource(deepcopy(POLICIES)), FakeCarrierSource(deepcopy(CARRIER)),
                   FakeLedgerSource(deepcopy(LEDGER)),
                   FakeMerchantSource(deepcopy(CONFIRMATIONS), deepcopy(CANCELLATIONS),
                                      deepcopy(REFUNDS), deepcopy(CONTACTS)), deepcopy(BINDINGS))
