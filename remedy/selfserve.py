"""Deterministic adapters for facts entered by the workspace owner."""
import json
import re
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from .artefacts import Artefact, packet_hash
from .classify import PAYMENT_CLAIMS, classify
from .config import WAIT_DAYS
from .limits import validate_envelope
from .lint import DISCLOSURE, lint
from .models import Case, Decision, Evidence, money, plain
from .sources import Sources

_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE = re.compile(r"(?:\+\d{7,15}\b|\b\d{3}[-. ]\d{3}[-. ]\d{4}\b|\(\d{3}\)\s*\d{3}[-. ]\d{4}\b)")
_DIGITS = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_ORDER_NUMBER = re.compile(r"(?:[A-Za-z]+-)?[0-9]{3}-[0-9]{7}-[0-9]{7}")
MAX_CONTACTS = 50
ISSUER_TITLE = "SELF-REPORTED FACT SHEET - every fact below was entered by the cardholder and was not checked by this tool. The card issuer decides any reason code."


class InputError(ValueError):
    """Only deliberately public validation messages use this type."""


def utc_today():
    return datetime.now(timezone.utc).date()


def _luhn(value):
    digits = [int(x) for x in re.sub(r"\D", "", value)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, digit in enumerate(digits):
        if index % 2 == len(digits) % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _guard(value):
    pending = [((), value)]
    while pending:
        path, current = pending.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                pending.append(((), key))
                pending.append((path + (key,), child))
        elif isinstance(current, list):
            pending.extend((path + (index,), child) for index, child in enumerate(current))
        elif isinstance(current, str):
            if _EMAIL.search(current) or _PHONE.search(current):
                raise InputError("Email addresses and phone numbers are not accepted.")
            # Recognise the merchant's documented grouping only in the order
            # label field. Do not exempt arbitrary numbers or free text.
            order_number = path in {("order", "order_id"), ("order_label",)} and _ORDER_NUMBER.fullmatch(current)
            if not order_number and any(_luhn(match.group()) for match in _DIGITS.finditer(current)):
                raise InputError("Card or bank account numbers are not accepted.")


def validate_contact(row):
    validate_envelope(row)
    _guard(row)
    if not isinstance(row, dict) or set(row) != {"date", "channel", "response"}:
        raise InputError("Each contact needs a date, channel and response.")
    try:
        day = date.fromisoformat(row["date"])
    except (ValueError, TypeError):
        raise InputError("Use a valid contact date in YYYY-MM-DD format.") from None
    if not isinstance(row["channel"], str) or not re.fullmatch(r"[A-Za-z][A-Za-z -]{0,29}", row["channel"]):
        raise InputError("Channel must be a short word.")
    if row["response"] not in {"none", "refused", "partial", "resolved"}:
        raise InputError("Choose a valid merchant response.")
    return {"date": day.isoformat(), "channel": row["channel"], "response": row["response"]}


def _case_from_raw(raw):
    if not isinstance(raw, dict):
        raise InputError("The JSON case must be an object.")
    ss = raw.get("selfserve") or {}
    if not isinstance(ss, dict):
        raise InputError("The selfserve facts must be an object.")
    contacts = ss.get("contacts") or []
    if not isinstance(contacts, list) or len(contacts) > MAX_CONTACTS:
        raise InputError("A case can hold at most 50 contact entries.")
    envelope = deepcopy(raw)
    envelope["selfserve"] = {k: deepcopy(v) for k, v in ss.items() if k != "contacts"}
    validate_envelope(envelope)
    _guard(envelope)
    contacts = [validate_contact(row) for row in contacts]
    case = Case.from_dict(envelope, pii_guard=_guard)
    data = deepcopy(envelope["selfserve"])
    data["contacts"] = contacts
    days = data.get("return_days")
    if days is not None and (isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 3650):
        raise InputError("Return window must be between 0 and 3650 days.")
    if not isinstance(data.get("final_sale", False), bool):
        raise InputError("Final sale must be yes or no.")
    for key in ("cancel_until", "refund_date"):
        if data.get(key):
            try:
                data[key] = date.fromisoformat(data[key]).isoformat()
            except (ValueError, TypeError):
                raise InputError("Policy and refund dates must use YYYY-MM-DD format.") from None
    if data.get("refund_amount") is not None:
        data["refund_amount"] = plain(money(data["refund_amount"]))
    if data.get("cancellation_before_shipment") not in (None, True, False):
        raise InputError("State whether cancellation was before shipment.")
    charges = data.get("charges") or []
    if not isinstance(charges, list):
        raise InputError("Charges must be a list.")
    for index, row in enumerate(charges, 1):
        if not isinstance(row, dict) or row.get("status") not in {"settled", "pending"}:
            raise InputError(f"Charge {index}: status must be settled or pending.")
        try:
            amount = money(row.get("amount"))
            if amount is None:
                raise ValueError()
        except (ValueError, ArithmeticError):
            raise InputError(f"Charge {index}: enter a valid decimal amount.") from None
        try:
            at = datetime.fromisoformat(str(row.get("at", "")).replace("Z", "+00:00"))
            if at.tzinfo is None or at.utcoffset() is None:
                raise ValueError()
        except ValueError:
            raise InputError(f"Charge {index}: use an ISO timestamp with a timezone, such as 2026-10-02T12:00:00+00:00.") from None
        row.update(amount=plain(amount), at=at.isoformat(), order_id=case.order.order_id,
                   merchant_id=case.order.merchant_id)
        row.setdefault("currency", case.order.currency)
    data["charges"] = charges
    case._selfserve = data
    return case


def _value(fields, name, default=""):
    value = fields.get(name, default)
    if isinstance(value, list):
        value = value[-1] if value else default
    return str(value).strip()


def _iso_datetime(day):
    return datetime.combine(date.fromisoformat(day), time(12), timezone.utc).isoformat()


def form_to_dict(fields):
    today = utc_today().isoformat()
    asked = _value(fields, "asked_item") or _value(fields, "item") or "item"
    required, actual = {}, {}
    for key, aliases in (("size", ("asked_size", "actual_size")), ("colour", ("asked_colour", "actual_colour"))):
        wanted, ordered = _value(fields, aliases[0]), _value(fields, aliases[1])
        if key == "colour":
            wanted = wanted or _value(fields, "asked_color")
            ordered = ordered or _value(fields, "actual_color")
        if key == "size":
            ordered = ordered or _value(fields, "ordered_size")
        if wanted:
            required[key] = wanted
        if ordered:
            actual[key] = ordered
    quantity = int(_value(fields, "quantity", "1") or "1")
    ordered_quantity = int(_value(fields, "ordered_quantity", str(quantity)) or str(quantity))
    unit = _value(fields, "unit_price", "1.00") or "1.00"
    total = _value(fields, "total") or str(Decimal(unit) * ordered_quantity)
    order_date = _value(fields, "order_date") or today
    delivered = _value(fields, "delivered_date")
    cancelled = _value(fields, "cancellation_date")
    status = _value(fields, "status").lower().replace(" ", "_").replace("-", "_")
    status = {"intransit": "in_transit", "canceled": "cancelled"}.get(status, status)
    if not status:
        status = "cancelled" if cancelled else "delivered" if delivered else "placed"
    if status not in {"placed", "shipped", "in_transit", "delivered", "cancelled"}:
        raise InputError("Choose a valid order status.")
    if delivered and status in {"placed", "shipped", "in_transit"}:
        status = "delivered"
    merchant = _value(fields, "merchant_name") or "Merchant"
    label = _value(fields, "order_label") or "order"
    currency = (_value(fields, "currency") or "USD").upper()
    charges = []
    for line_number, line in enumerate(_value(fields, "charges").splitlines(), 1):
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3:
            raise InputError(f"Charge line {line_number}: use amount, YYYY-MM-DD, settled or pending.")
        amount, at, charge_status = parts
        try:
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", at):
                raise ValueError()
            timestamp = _iso_datetime(at)
        except ValueError:
            raise InputError(f"Charge line {line_number}: date must use YYYY-MM-DD, for example 2026-10-02.") from None
        try:
            if money(amount) is None:
                raise ValueError()
        except (ValueError, ArithmeticError):
            raise InputError(f"Charge line {line_number}: enter a valid decimal amount.") from None
        if charge_status.lower() not in {"settled", "pending"}:
            raise InputError(f"Charge line {line_number}: status must be settled or pending.")
        charges.append({"amount": amount, "currency": currency, "status": charge_status.lower(),
                        "at": timestamp, "order_id": label, "merchant_id": merchant})
    events = [{"at": _iso_datetime(order_date), "actor": "agent", "kind": "order_placed"}]
    if cancelled:
        events.append({"at": _iso_datetime(cancelled), "actor": "merchant", "kind": "order_cancelled"})
    before, days = _value(fields, "cancellation_before_shipment"), _value(fields, "return_days")
    return {
        "case_id": _value(fields, "case_id") or "case",
        "mandate": {"mandate_id": "self-serve", "principal_ref": "workspace-owner", "intent": asked,
                    "issued_at": _iso_datetime(order_date), "constraints": {
                        "max_unit_price": _value(fields, "max_unit_price") or None,
                        "max_total": _value(fields, "max_total") or None,
                        "quantity": quantity, "allowed_categories": [], "required_attributes": required,
                        "deliver_by": _value(fields, "deliver_by") or None}},
        "order": {"order_id": label, "merchant_id": merchant,
                  "items": [{"sku": "item", "title": _value(fields, "item_title") or _value(fields, "ordered_item") or asked,
                             "category": "item", "qty": ordered_quantity, "unit_price": unit, "attributes": actual}],
                  "total": total, "currency": currency, "ordered_at": _iso_datetime(order_date),
                  "card_ref": "case-scoped-charges", "status": status,
                  "promised_delivery": _value(fields, "promised_delivery") or None,
                  "delivered_at": _iso_datetime(delivered) if delivered else None},
        "events": events,
        "claim": {"reason": _value(fields, "problem_type") or "unwanted", "merchant_response": None,
                  "note": _value(fields, "note")},
        "selfserve": {"return_days": int(days) if days else None,
                      "final_sale": _value(fields, "final_sale").lower() in {"yes", "true", "on", "1"},
                      "cancel_until": _value(fields, "cancel_until") or None, "charges": charges,
                      "cancellation_before_shipment": True if before == "yes" else False if before == "no" else None,
                      "refund_date": _value(fields, "refund_date") or None,
                      "refund_amount": _value(fields, "refund_amount") or None}}


def _merge_form(original, updated):
    """Update only fields represented by the form; preserve imported detail."""
    result = deepcopy(original)
    mandate = result.setdefault("mandate", {})
    mandate["intent"] = updated["mandate"]["intent"]
    constraints = mandate.setdefault("constraints", {})
    for key in ("max_unit_price", "max_total", "quantity", "deliver_by"):
        constraints[key] = updated["mandate"]["constraints"][key]
    wanted = constraints.setdefault("required_attributes", {})
    for key in ("size", "colour"):
        wanted.pop(key, None)
        if key in updated["mandate"]["constraints"]["required_attributes"]:
            wanted[key] = updated["mandate"]["constraints"]["required_attributes"][key]
    order = result.setdefault("order", {})
    for key in ("order_id", "merchant_id", "total", "currency", "status", "promised_delivery"):
        order[key] = updated["order"][key]
    for key in ("ordered_at", "delivered_at"):
        old, new = order.get(key), updated["order"][key]
        if str(old or "")[:10] != str(new or "")[:10]:
            order[key] = new
    if not order.get("items"):
        order["items"] = deepcopy(updated["order"]["items"])
    else:
        first, changed = order["items"][0], updated["order"]["items"][0]
        for key in ("title", "qty", "unit_price"):
            first[key] = changed[key]
        attrs = first.setdefault("attributes", {})
        for key in ("size", "colour"):
            attrs.pop(key, None)
            if key in changed["attributes"]:
                attrs[key] = changed["attributes"][key]
    events = result.setdefault("events", [])
    for kind in ("order_placed", "order_cancelled"):
        previous = next((event for event in events if event.get("kind") == kind), None)
        replacement = next((event for event in updated["events"] if event["kind"] == kind), None)
        if replacement is None:
            events[:] = [event for event in events if event.get("kind") != kind]
        elif previous is None:
            events.append(deepcopy(replacement))
        elif str(previous.get("at") or "")[:10] != replacement["at"][:10]:
            previous["at"] = replacement["at"]
    result.setdefault("claim", {})["reason"] = updated["claim"]["reason"]
    ss = result.setdefault("selfserve", {})
    for key, value in updated["selfserve"].items():
        if key == "charges":
            old = ss.get("charges") or []
            # Keep exact timestamps and extra charge fields when the displayed
            # amount/date/status lines did not change.
            signature = lambda rows: [(str(r.get("amount", "")), str(r.get("at", ""))[:10], r.get("status")) for r in rows]
            if signature(old) == signature(value):
                continue
        ss[key] = deepcopy(value)
    return result


def parse_form(fields, original=None):
    validate_envelope(fields)
    _guard(fields)
    raw = form_to_dict(fields)
    if original is not None:
        raw = _merge_form(original, raw)
    return _case_from_raw(raw)


def parse_json(text):
    if len(text.encode("utf-8")) > 1024 * 1024:
        raise InputError("Requests must be 1 MB or less.")
    case = _case_from_raw(json.loads(text))
    case.order.card_ref = "case-scoped-charges"
    case.mandate.principal_ref = case.mandate.principal_ref or "workspace-owner"
    case.mandate.mandate_id = case.mandate.mandate_id or "self-serve"
    case.mandate.intent = case.mandate.intent or "shopping request"
    for key, default in (("return_days", None), ("final_sale", False), ("cancel_until", None),
                         ("charges", []), ("refund_date", None), ("refund_amount", None)):
        case._selfserve.setdefault(key, default)
    if not case._selfserve["contacts"] and case.claim.merchant_contacted_at:
        case._selfserve["contacts"] = [validate_contact({
            "date": case.claim.merchant_contacted_at.date().isoformat(), "channel": "other",
            "response": case.claim.merchant_response or "none"})]
    return case


def normalised(case):
    raw = case.to_dict()
    raw["selfserve"] = deepcopy(case._selfserve)
    return raw


class _Policy:
    def __init__(self, case):
        self.case = case

    def policy(self, merchant_id):
        data = self.case._selfserve
        days = data.get("return_days")
        return {"return_days": days if days is not None else -1,
                "non_returnable": [item.category for item in self.case.order.items] if data.get("final_sale") else [],
                "cancel_supported": bool(data.get("cancel_until")),
                "clause": f"{days}-day returns" if days is not None else "return window unknown"}


class _Carrier:
    def __init__(self, case):
        self.case = case

    def status(self, order_id):
        return {"status": self.case.order.status, "promised_delivery": plain(self.case.order.promised_delivery)}


class _Ledger:
    def __init__(self, case):
        self.case = case

    def charges(self, card_ref):
        return deepcopy(self.case._selfserve.get("charges") or [])


class _Merchant:
    def __init__(self, case):
        self.case = case

    def confirmation(self, order_id):
        order = self.case.order
        return {"total": plain(order.total), "currency": order.currency, "status": order.status,
                "ordered_at": plain(order.ordered_at), "promised_delivery": plain(order.promised_delivery),
                "delivered_at": plain(order.delivered_at), "cancel_deadline": self.case._selfserve.get("cancel_until")}

    def cancellation(self, order_id):
        event = next((e for e in self.case.events if e.kind == "order_cancelled" and e.at), None)
        before = self.case._selfserve.get("cancellation_before_shipment")
        if not event or before is None:
            return None
        return {"before_shipment": before, "amount": plain(self.case.order.total), "at": plain(event.at)}

    def refund_promise(self, order_id):
        data = self.case._selfserve
        if not data.get("refund_date") or not data.get("refund_amount"):
            return None
        return {"at": _iso_datetime(data["refund_date"]), "amount": data["refund_amount"]}

    def contact(self, order_id):
        contacts = self.case._selfserve.get("contacts") or []
        if not contacts:
            return None
        _, row = max(enumerate(contacts), key=lambda pair: (pair[1]["date"], pair[0]))
        return {"actor": "merchant", "contacted_at": _iso_datetime(row["date"]),
                "channel": row["channel"], "response": row["response"]}


class UserReportedSources(Sources):
    def __init__(self, case):
        super().__init__(_Policy(case), _Carrier(case), _Ledger(case), _Merchant(case), {})

    def permits(self, case):
        return True


def _missing_labels(items):
    labels = {"claim.merchant_response": "What the merchant said about the description problem",
              "carrier.promised_delivery": "Promised delivery date",
              "merchant.refund_promise": "Refund promise date and amount",
              "merchant.cancellation": "Cancellation date and whether it was before shipment",
              "ledger.card_record": "At least one charge line",
              "policy.return_days": "Return window in days — check the merchant's policy before assuming the return deadline has passed"}
    return [labels.get(item, item.replace(".", " › ")) for item in items]


def _gate_issuer(case, decision, sources, today):
    if decision.route != "C":
        return
    contact = sources.merchant.contact(case.order.order_id)
    eligible = date.fromisoformat(contact["contacted_at"][:10]) + timedelta(days=WAIT_DAYS) if contact else None
    if contact and not any(e.field == "merchant_contact" for e in decision.evidence):
        decision.evidence.append(Evidence("You entered", "merchant_contact", contact, today.isoformat()))
    if contact and contact["response"] == "resolved":
        decision.route, decision.sub = "B", "B_goodwill"
        decision.reasons = ["MERCHANT_REPORTED_RESOLVED"]
    elif eligible is None or today < eligible:
        decision.route, decision.sub = "B", "B_merchant_first"
        decision.reasons = ["MERCHANT_CONTACT_REQUIRED"]
        decision.c_eligible_after = eligible.isoformat() if eligible else None
        decision.evidence.append(Evidence("Config", "wait_days", WAIT_DAYS, today.isoformat()))


def difference_label(case, field):
    labels = {"total": "Order total", "quantity": "Total quantity", "promised_delivery": "Promised delivery date"}
    if field in labels:
        return labels[field]
    for item in case.order.items:
        prefix = f"items.{item.sku}."
        if field.startswith(prefix):
            detail = field[len(prefix):].removeprefix("attributes.").replace("_", " ")
            title = item.title if item.title and item.title != "item" else "Ordered item"
            return f"{title}: {detail}"
    return field.replace("_", " ").replace(".", " — ")


def _display(value):
    value = plain(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return "not entered" if value is None else str(value)


def _day(value):
    """Return the YYYY-MM-DD form of an entered date or timestamp, if any."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    return text[:10] if re.match(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text) else None


def _entered_values(case, decision):
    """Values derived from the user's own entries that a message may print.

    Stored timestamps such as 2026-10-01T12:00:00+00:00 do not expose the day
    number to the unchanged lint tokenizer, so the date-only forms printed in
    messages are supplied explicitly. Nothing here is invented: every value is
    an entered fact, a date part of one, or a difference computed from them.
    """
    values = [json.dumps(plain(case._selfserve), sort_keys=True)]
    days = [_day(case.order.ordered_at), _day(case.order.delivered_at), _day(case.order.promised_delivery)]
    days.extend(_day(event.at) for event in case.events)
    days.extend(_day(row.get("at")) for row in case._selfserve.get("charges") or [])
    days.extend(_day(row.get("date")) for row in case._selfserve.get("contacts") or [])
    days.append(decision.c_eligible_after)
    values.extend(day for day in days if day)
    for deviation in decision.deviations:
        values.extend((_display(deviation.expected), _display(deviation.actual)))
    return tuple(values)


def _merchant_message(case, decision):
    order = case.order
    greeting = f"Hello {order.merchant_id}," if order.merchant_id and order.merchant_id != "Merchant" else "Hello,"
    lines = [greeting, "", DISCLOSURE]
    if order.order_id and order.order_id != "order":
        lines.append(f"Order reference: {order.order_id} [you entered]")
    if order.ordered_at:
        lines.append(f"Order date: {order.ordered_at.date().isoformat()} [you entered]")
    for item in order.items:
        if item.title and item.title != "item":
            lines.append(f"Item: {item.title}; quantity: {item.qty} [you entered]")
    issues = {
        "wrong_item": "The item ordered differs from what I asked my shopping agent to buy.",
        "unwanted": "I would like to return or cancel this purchase.",
        "not_received": "I have not received this order.",
        "charged_twice": "I believe I was charged twice for this order. Please review the charges below.",
        "wrong_amount": "The amount charged appears different from the order total. Please review the amounts below.",
        "cancelled_but_charged": "I cancelled this order, but I still see a charge and need help resolving it.",
        "refund_not_received": "I have not received the refund I was expecting.",
        "not_as_described": "I believe the item differs from its description and would like help resolving this."
    }
    lines.extend(["", issues.get(case.claim.reason, "Please help me resolve this order.") + " [you entered]"])
    if order.total is not None:
        lines.append(f"Order total: {order.total} {order.currency} [you entered]")
    if case.claim.reason == "not_received" and order.promised_delivery:
        lines.append(f"Promised delivery date: {order.promised_delivery.isoformat()} [you entered]")
    for row in case._selfserve.get("charges", []):
        lines.append(f"Charge: {row['amount']} {row.get('currency') or order.currency}, {row['at'][:10]}, {row['status']} [you entered]")
    if case._selfserve.get("refund_date"):
        lines.append(f"Refund promise: {case._selfserve['refund_date']}; amount: {_display(case._selfserve.get('refund_amount'))} [you entered]")
    for deviation in decision.deviations:
        lines.append(f"{difference_label(case, deviation.field)}: requested {_display(deviation.expected)}; ordered {_display(deviation.actual)} [you entered]")
    if decision.sub == "A_return":
        deadline = next(e.value for e in decision.evidence if e.field == "return_deadline")
        lines.extend([f"My understanding of the return deadline is {deadline}. [you entered]",
                      "Please send return instructions and confirm how the refund to my original payment method will be processed."])
    elif decision.sub == "A_cancel":
        deadline = next(e.value for e in decision.evidence if e.field == "cancel_deadline")
        lines.extend([f"My understanding of the cancellation deadline is {deadline}. [you entered]",
                      "Please cancel this order and confirm any refund to my original payment method."])
    elif case.claim.reason == "not_received":
        lines.append("Please check the delivery and let me know whether you can arrange delivery, a replacement, or a refund.")
    elif case.claim.reason in PAYMENT_CLAIMS:
        lines.append("Please review the payment records, explain any difference, and arrange any correction or refund that is due.")
    else:
        lines.append("Could you help with a return, exchange, or cancellation? Please let me know the available options and any conditions.")
    if decision.sub == "B_merchant_first" and decision.c_eligible_after:
        lines.append(f"I would be grateful for a reply by {decision.c_eligible_after}.")
    lines.extend(["", "Thank you for your help."])
    return "\n".join(lines) + "\n"


def _issuer_sheet(case, decision):
    facts = [e.to_dict() for e in decision.evidence]
    safe_kinds = {"order_placed", "order_cancelled", "shipped", "delivered", "credit_posted", "refund_promised"}
    timeline = [{"at": plain(e.at), "actor": e.actor, "kind": e.kind} for e in case.events if e.kind in safe_kinds]
    facts.extend([
        {"source": "You entered", "field": "order_reference", "value": case.order.order_id},
        {"source": "You entered", "field": "timeline", "value": timeline},
        {"source": "You entered", "field": "merchant_contact_log", "value": deepcopy(case._selfserve.get("contacts", []))},
    ])
    digest = packet_hash(facts)
    lines = [ISSUER_TITLE, "", "Facts:"]
    for fact in facts:
        lines.append(f"- {fact['field'].replace('_', ' ').capitalize()}: {_display(fact['value'])} [you entered]")
    lines.append("sha256: " + digest)
    text = "\n".join(lines) + "\n"
    if "verified" in text.lower() or "guarantee" in text.lower():
        raise InputError("Issuer fact sheets cannot contain claims of verification or guarantees.")
    lint(text, "C", case, decision.evidence, (digest, json.dumps(plain(facts), sort_keys=True)))
    return Artefact(text, digest, facts)


def check(case, today):
    sources = UserReportedSources(case)
    decision = classify(case, sources, today)
    unknown_window = (case._selfserve.get("return_days") is None and not case._selfserve.get("final_sale")
                      and case.claim.reason not in PAYMENT_CLAIMS
                      and (case.order.status == "delivered" or case.order.delivered_at is not None)
                      and decision.sub != "A_cancel")
    if unknown_window:
        missing = list(dict.fromkeys(decision.missing + ["policy.return_days"]))
        decision = Decision("HOLD", "HOLD_policy", ["RETURN_WINDOW_UNKNOWN"], decision.deviations, [], missing)
    _gate_issuer(case, decision, sources, today)
    if case.claim.reason == "not_as_described" and decision.route == "C":
        raise ValueError("Description claims cannot produce issuer sheets")
    decision.evidence = [Evidence("You entered", e.field, e.value, e.checked_at) for e in decision.evidence]
    decision.reasons = [reason.replace("INDEPENDENT_SOURCE_MISSING", "ENTERED_FACT_MISSING")
                        .replace("NO_VERIFIED_DISPUTE_GROUND", "NO_SUPPORTED_PAYMENT_OR_DELIVERY_ISSUE")
                        .replace("VERIFIED_", "ENTERED_") for reason in decision.reasons]
    labels = {"A": "Return or cancel now", "C": "Talk to your card issuer", "HOLD": "Missing info"}
    route = labels.get(decision.route, "Ask the merchant (goodwill)")
    if decision.sub == "B_merchant_first":
        route = "Wait until " + decision.c_eligible_after if decision.c_eligible_after and today.isoformat() < decision.c_eligible_after else "Ask the merchant"
    deadlines = []
    for evidence in decision.evidence:
        if evidence.field in {"return_deadline", "cancel_deadline"}:
            day = date.fromisoformat(str(plain(evidence.value))[:10])
            deadlines.append({"label": "Return deadline" if evidence.field == "return_deadline" else "Cancellation deadline",
                              "date": day.isoformat(), "days_remaining": (day - today).days})
    if decision.c_eligible_after:
        day = date.fromisoformat(decision.c_eligible_after)
        deadlines.append({"label": "Review after merchant contact (tool wait period)", "date": day.isoformat(),
                          "days_remaining": (day - today).days})
    missing = _missing_labels(decision.missing)
    if decision.route == "C":
        artefact = _issuer_sheet(case, decision)
    elif decision.route == "HOLD":
        artefact = Artefact("More information is needed before preparing a message:\n" + "\n".join("- " + item for item in missing))
    else:
        text = _merchant_message(case, decision)
        lint(text, decision.route, case, decision.evidence, _entered_values(case, decision))
        artefact = Artefact(text)
    differences = [{"field": difference_label(case, d.field), "expected": plain(d.expected), "actual": plain(d.actual)}
                   for d in decision.deviations]
    return {"route": route, "decision": decision, "deadlines": deadlines, "deviations": differences,
            "missing": missing, "artefact": artefact}
