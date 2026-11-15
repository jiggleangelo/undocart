import hashlib
import json
from dataclasses import dataclass
from string import Template

from .limits import MAX_ARTEFACT_BYTES
from .lint import DISCLOSURE, lint
from .models import plain

A_RETURN = Template("""$disclosure
Order: $order_id
Items: $items
Merchant policy: $clause [PolicySource].
Verified delivery: $delivered [MerchantSource].
Deadline: $deadline [PolicySource].
Requested remedy: accept the return and refund the original payment method.
""")
A_CANCEL = Template("""$disclosure
Order: $order_id
Items: $items
Verified order status: placed [MerchantSource].
Merchant policy: cancellation is supported [PolicySource].
Cancellation deadline: $deadline [MerchantSource].
Requested remedy: cancel the order and refund the original payment method.
""")
B = Template("""$disclosure
Order: $order_id
$issue
Mandate comparison: $deviations
Please review this case and, if possible, offer a merchant resolution. This is a request, not a claim of entitlement.
""")
C = Template("""DISPUTE FACT PACKET — case $case_id — sha256 $packet_hash
Prepared by software for the cardholder to review. The card issuer selects any reason code.
Facts:
$facts
Timeline: $timeline
Merchant contact: $contact
Not included: reason code, characterisation of intent, consumer free-text note, or event free text.
""")


@dataclass
class Artefact:
    text: str
    packet_hash: str | None = None
    facts: list[dict] | None = None

    def __post_init__(self):
        if len(self.text.encode("utf-8")) > MAX_ARTEFACT_BYTES:
            raise ValueError("artefact is too large")
        if len(json.dumps(plain(self.facts or [])).encode("utf-8")) > MAX_ARTEFACT_BYTES:
            raise ValueError("packet is too large")

    def to_dict(self):
        return {"text": self.text, "packet_hash": self.packet_hash, "facts": plain(self.facts or [])}


def packet_hash(facts):
    raw = json.dumps(plain(facts), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def verify_packet(facts, supplied_hash):
    return bool(supplied_hash) and packet_hash(facts) == supplied_hash


def _items(case):
    return "; ".join(f"{item.qty} × {item.title} ({item.sku}) at {item.unit_price} {case.order.currency}" for item in case.order.items)


def _deviations(decision):
    if not decision.deviations:
        return "No mandate deviation was identified."
    return "; ".join(f"{item.field}: expected {plain(item.expected)}, actual {plain(item.actual)}" for item in decision.deviations)


def _display(value):
    if isinstance(value, (dict, list)):
        return json.dumps(plain(value), sort_keys=True, ensure_ascii=False, separators=(", ", ": "))
    return str(plain(value))


def _facts(decision):
    return [e.to_dict() for e in decision.evidence]


def _fact_text(facts):
    labels = {
        "duplicate_settled_charges": "Two settled charges mapped to the same order",
        "order_confirmation": "Merchant order confirmation",
        "settled_charge": "Settled charge",
        "matching_settled_charge": "Matching settled charge",
        "delivery_status": "Carrier delivery status",
        "promised_delivery": "Carrier promised-delivery date",
        "cancellation": "Merchant cancellation record",
        "refund_promise": "Merchant refund promise",
        "matching_credit": "Matching ledger credit",
        "merchant_contact": "Merchant contact record",
    }
    return "\n".join(f"- {labels.get(f['field'], f['field'].replace('_', ' ').title())}: {_display(f['value'])} [{f['source']}]" for f in facts)


def generate(case, decision):
    if decision.route == "HOLD":
        return Artefact("No artefact was generated. Missing: " + ", ".join(decision.missing))
    facts = _facts(decision)
    digest = packet_hash(facts)
    if decision.sub == "A_return":
        policy, delivered, deadline = decision.evidence[:3]
        clause = policy.value.get("clause") or f"{policy.value.get('return_days')}-day returns"
        text = A_RETURN.substitute(disclosure=DISCLOSURE, order_id=case.order.order_id, items=_items(case),
                                   clause=clause, delivered=delivered.value, deadline=deadline.value)
    elif decision.sub == "A_cancel":
        deadline = next(e.value for e in decision.evidence if e.field == "cancel_deadline")
        text = A_CANCEL.substitute(disclosure=DISCLOSURE, order_id=case.order.order_id,
                                   items=_items(case), deadline=deadline)
    elif decision.route == "B":
        issue = "No independently verified payment or delivery issue was found."
        if decision.sub == "B_merchant_first":
            verified = next((e for e in decision.evidence if e.source != "Config"), decision.evidence[0])
            wait = next((e.value for e in decision.evidence if e.field == "wait_days"), None)
            eligibility = decision.c_eligible_after or "after independently recorded merchant contact"
            issue = (f"Verified issue: {verified.source} reports {verified.field} as {_display(verified.value)}. "
                     f"Please contact the merchant and allow {wait} days; further review is eligible on {eligibility}.")
        text = B.substitute(disclosure=DISCLOSURE, order_id=case.order.order_id,
                            issue=issue, deviations=_deviations(decision))
    else:
        safe_events = [event for event in case.events if event.kind in {"order_placed", "order_cancelled", "shipped", "delivered", "credit_posted"}]
        timeline = "; ".join(f"{event.at.isoformat()} {event.actor} {event.kind}" for event in safe_events) or "none"
        contact_evidence = next((e for e in decision.evidence if e.field == "merchant_contact"), None)
        contact = _display(contact_evidence.value) + " [MerchantSource]" if contact_evidence else "none"
        text = C.substitute(case_id=case.case_id, packet_hash=digest, facts=_fact_text(facts),
                            timeline=timeline, contact=contact)
    artefact = Artefact(text, digest, facts)
    lint(text, decision.route, case, decision.evidence, (digest,))
    return artefact
