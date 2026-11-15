"""Review-driven compatibility fixes for the self-serve application."""
import hashlib
import json
import re
from copy import deepcopy
from datetime import date

from .artefacts import Artefact, packet_hash
from .models import Decision, Evidence, plain

_SETTING_FIELDS = {"wait_days", "grace_days", "amount_tolerance", "credit_wait_days"}
_PHONE = re.compile(r"(?:\+\d{7,15}\b|\b\d{3}[-. ]\d{3}[-. ]\d{4}\b|\(\d{3}\)\s*\d{3}[-. ]\d{4}\b)")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_DIGITS = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_ORDER_PHONE = re.compile(r"\d{3}[-. ]\d{3}[-. ]\d{4}")
# Marketplace order labels such as 123-4567890-1234567, optionally prefixed.
_ORDER_NUMBER = re.compile(r"(?:[A-Za-z]+-)?[0-9]{3}-[0-9]{7}-[0-9]{7}")


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


def guard(value, InputError):
    """Reject contact/payment data while permitting order labels in the label field only."""
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
            order_label = path in {("order", "order_id"), ("order_label",)}
            phone_label = order_label and (_ORDER_PHONE.fullmatch(current) or _ORDER_NUMBER.fullmatch(current))
            if _EMAIL.search(current) or (_PHONE.search(current) and not phone_label):
                raise InputError("Email addresses and phone numbers are not accepted.")
            if not phone_label and any(_luhn(match.group()) for match in _DIGITS.finditer(current)):
                raise InputError("Card or bank account numbers are not accepted.")


def _issuer_sheet(module, case, decision):
    facts = [e.to_dict() for e in decision.evidence]
    safe = {"order_placed", "order_cancelled", "shipped", "delivered", "credit_posted", "refund_promised"}
    timeline = [{"at": plain(e.at), "actor": e.actor, "kind": e.kind} for e in case.events if e.kind in safe]
    sample = bool(case._selfserve.get("sample"))
    entered_source = "Made-up sample data" if sample else "You entered"
    facts.extend([
        {"source": entered_source, "field": "order_reference", "value": case.order.order_id},
        {"source": entered_source, "field": "timeline", "value": timeline},
        {"source": entered_source, "field": "merchant_contact_log", "value": deepcopy(case._selfserve.get("contacts", []))},
    ])
    digest = packet_hash(facts)
    lines = [module.ISSUER_TITLE, "", "Facts:"]
    for fact in facts:
        value = module._display(fact["value"])
        lines.append(f"- {fact['field'].replace('_', ' ').capitalize()}: {value} [{fact['source'].lower()}]")
    lines.append("sha256: " + digest)
    text = "\n".join(lines) + "\n"
    lowered = text.lower()
    if "verified" in lowered or "guarantee" in lowered:
        raise module.InputError("Issuer fact sheets cannot contain claims of verification or guarantees.")
    return Artefact(text, digest, facts)


def install_selfserve_fixes(module):
    original_check = module.check
    module._guard = lambda value: guard(value, module.InputError)

    def fixed_check(case, today):
        result = original_check(case, today)
        decision = result["decision"]

        if case.order.status == "delivered" and case.order.delivered_at is None:
            decision = Decision(
                "HOLD", "HOLD_delivery_date", ["DELIVERED_DATE_MISSING"],
                decision.deviations, [], ["order.delivered_at"]
            )
            result.update(
                route="Missing info",
                decision=decision,
                deadlines=[],
                missing=["Delivered date"],
                artefact=Artefact("More information is needed before preparing a message:\n- Delivered date")
            )
            return result

        sample = bool(case._selfserve.get("sample"))
        evidence = []
        for item in decision.evidence:
            if item.field in _SETTING_FIELDS:
                source = "Tool setting"
            else:
                source = "Made-up sample data" if sample else "You entered"
            evidence.append(Evidence(source, item.field, item.value, item.checked_at))
        decision.evidence = evidence

        if decision.route == "C":
            result["artefact"] = _issuer_sheet(module, case, decision)
        elif sample:
            result["artefact"].text = result["artefact"].text.replace(
                "[you entered]", "[made-up sample data]"
            )
        return result

    module.check = fixed_check


def install_store_fixes(store_module):
    """Remove disclosure-producing tombstones and keep deletion outside write budgets."""
    Store = store_module.Store
    original_init = Store.__init__
    original_delete = Store.delete
    original_sweep = Store.sweep

    def clear_tombstones(instance):
        root = getattr(instance, "data_dir", None) or getattr(instance, "root", None)
        if root is None:
            return
        directory = __import__("pathlib").Path(root) / "tombstones"
        if directory.is_dir():
            for path in directory.iterdir():
                if path.is_file():
                    try:
                        path.unlink()
                    except OSError:
                        pass

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        clear_tombstones(self)

    def delete(self, token):
        result = original_delete(self, token)
        clear_tombstones(self)
        return result

    def sweep(self):
        result = original_sweep(self)
        clear_tombstones(self)
        return result

    Store.__init__ = init
    Store.delete = delete
    Store.sweep = sweep
