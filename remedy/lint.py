import json
import re
import unicodedata

from .models import plain

BANNED = ("fraud", "stolen", "unauthori", "never authorized", "did not order",
          "i did not make", "identity theft")
DISCLOSURE = "This order was placed by an AI shopping agent acting for the customer."
NUMBER = re.compile(r"(?<![A-Za-z0-9_])-?\d+(?:\.\d+)?(?![A-Za-z0-9_])")
CODE = re.compile(r"(?:\b\d{2}\.\d\b|\b4\d{3}\b).{0,20}\bcode\b|\bcode\b.{0,20}(?:\b\d{2}\.\d\b|\b4\d{3}\b)", re.I)


def _canonical(value):
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _allowed_blob(case, evidence, extra):
    structured = {
        "case_id": case.case_id,
        "mandate": {
            "mandate_id": case.mandate.mandate_id,
            "constraints": plain(case.mandate.constraints.__dict__),
            "issued_at": plain(case.mandate.issued_at),
        },
        "order": {
            "order_id": case.order.order_id,
            "merchant_id": case.order.merchant_id,
            "total": plain(case.order.total),
            "currency": case.order.currency,
            "ordered_at": plain(case.order.ordered_at),
            "promised_delivery": plain(case.order.promised_delivery),
            "delivered_at": plain(case.order.delivered_at),
            "items": [{"sku": item.sku, "title": item.title, "category": item.category, "qty": item.qty,
                       "unit_price": plain(item.unit_price), "attributes": plain(item.attributes)}
                      for item in case.order.items],
        },
        "events": [{"at": plain(event.at), "actor": event.actor, "kind": event.kind}
                   for event in case.events],
        "evidence": [e.to_dict() for e in evidence],
        "extra": list(extra),
    }
    return json.dumps(plain(structured), sort_keys=True)


def lint(text, route, case, evidence, extra=()):
    canonical = _canonical(text)
    for phrase in BANNED:
        if _canonical(phrase) in canonical:
            raise ValueError("banned phrase")
    if CODE.search(unicodedata.normalize("NFKC", text)):
        raise ValueError("reason-code pattern")
    if route in ("A", "B") and DISCLOSURE not in text:
        raise ValueError("agent disclosure missing")
    allowed = set(NUMBER.findall(_allowed_blob(case, evidence, extra)))
    orphan = [value for value in NUMBER.findall(text) if value not in allowed]
    if orphan:
        raise ValueError("orphan number")
    return text
