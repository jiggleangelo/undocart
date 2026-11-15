import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .limits import (MAX_ATTRIBUTES, MAX_CATEGORIES, MAX_EVENTS, MAX_ITEMS,
                     limited, validate_envelope)

_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE = re.compile(r"(?:\+\d{7,15}\b|\b\d{3}[-. ]\d{3}[-. ]\d{4}\b|\(\d{3}\)\s*\d{3}[-. ]\d{4}\b)")
_PAN = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_ALLOWED_REASONS = {"unwanted", "wrong_item", "not_as_described", "not_received", "charged_twice", "wrong_amount", "cancelled_but_charged", "refund_not_received"}
_ALLOWED_RESPONSES = {None, "none", "refused", "partial", "resolved"}
_ALLOWED_EVENT_ACTORS = {"agent", "merchant", "carrier", "ledger", "system"}
_ALLOWED_EVENT_KINDS = {"order_placed", "order_cancelled", "shipped", "delivered", "merchant_contact", "merchant_response", "refund_promised", "credit_posted"}


def _mapping(value, name):
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _list(value, name):
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return value


def _string(value, name, required=False, maximum=500):
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{name} must be a string")
    if len(value) > maximum:
        raise ValueError(f"{name} is too long")
    return value


def _integer(value, name, positive=False):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if abs(value) > 1000000 or (positive and value <= 0):
        raise ValueError(f"{name} is out of range")
    return value


def dt(value, name="datetime"):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO-8601 datetime") from exc
    else:
        raise ValueError(f"{name} must be an ISO-8601 datetime")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return result


def day(value, name="date"):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        raise ValueError(f"{name} must be a date")
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 date") from exc


def money(value, name="amount"):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{name} must be a decimal amount")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal amount") from exc
    if (not result.is_finite() or len(result.as_tuple().digits) > 28 or
            abs(result.as_tuple().exponent) > 12):
        raise ValueError(f"{name} is out of range")
    return result


def plain(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def _reject_pii(value):
    if isinstance(value, Mapping):
        for key, child in value.items():
            _reject_pii(key)
            _reject_pii(child)
    elif isinstance(value, list):
        for child in value:
            _reject_pii(child)
    elif isinstance(value, str):
        if _EMAIL.search(value) or _PHONE.search(value) or _PAN.search(value):
            raise ValueError("personal or payment-card data is not accepted")


@dataclass
class Constraints:
    max_unit_price: Decimal | None = None
    max_total: Decimal | None = None
    quantity: int | None = None
    allowed_categories: list[str] | None = None
    required_attributes: dict[str, str] = field(default_factory=dict)
    deliver_by: date | None = None


@dataclass
class Mandate:
    mandate_id: str | None = None
    principal_ref: str | None = None
    intent: str | None = None
    constraints: Constraints = field(default_factory=Constraints)
    issued_at: datetime | None = None


@dataclass
class Item:
    sku: str | None = None
    title: str | None = None
    category: str | None = None
    qty: int | None = None
    unit_price: Decimal | None = None
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class Order:
    order_id: str | None = None
    merchant_id: str | None = None
    items: list[Item] = field(default_factory=list)
    total: Decimal | None = None
    currency: str | None = None
    ordered_at: datetime | None = None
    card_ref: str | None = None
    status: str | None = None
    promised_delivery: date | None = None
    delivered_at: datetime | None = None


@dataclass
class Event:
    at: datetime | None = None
    actor: str | None = None
    kind: str | None = None
    detail: str | None = None


@dataclass
class Claim:
    reason: str | None = None
    merchant_contacted_at: datetime | None = None
    merchant_response: str | None = None
    note: str = ""
    contact_token: str | None = None


@dataclass
class Case:
    case_id: str | None
    mandate: Mandate
    order: Order
    events: list[Event]
    claim: Claim

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, pii_guard=None):
        # A caller may supply its own complete privacy policy without changing
        # module globals. The demo and batch retain their original default.
        raw = _mapping(raw, "case")
        validate_envelope(raw)
        (_reject_pii if pii_guard is None else pii_guard)(raw)
        m = _mapping(raw.get("mandate") or {}, "mandate")
        c = _mapping(m.get("constraints") or {}, "mandate.constraints")
        allowed = c.get("allowed_categories")
        if allowed is not None:
            allowed = [_string(v, "allowed category", True, 100) for v in
                       limited(_list(allowed, "allowed_categories"), MAX_CATEGORIES, "allowed_categories")]
        attrs = limited(_mapping(c.get("required_attributes") or {}, "required_attributes"),
                        MAX_ATTRIBUTES, "required_attributes")
        attrs = {_string(k, "attribute name", True, 100): _string(v, "attribute value", True, 200)
                 for k, v in attrs.items()}
        mandate = Mandate(
            _string(m.get("mandate_id"), "mandate_id", maximum=100),
            _string(m.get("principal_ref"), "principal_ref", maximum=100),
            _string(m.get("intent"), "intent", maximum=500),
            Constraints(money(c.get("max_unit_price"), "max_unit_price"), money(c.get("max_total"), "max_total"),
                        _integer(c.get("quantity"), "quantity", True), allowed, attrs,
                        day(c.get("deliver_by"), "deliver_by")),
            dt(m.get("issued_at"), "issued_at"),
        )
        o = _mapping(raw.get("order") or {}, "order")
        items = []
        for n, item_raw in enumerate(limited(_list(o.get("items", []), "order.items"), MAX_ITEMS, "order.items")):
            item_raw = _mapping(item_raw, f"order.items.{n}")
            item_attrs = limited(_mapping(item_raw.get("attributes") or {}, f"order.items.{n}.attributes"),
                                 MAX_ATTRIBUTES, "item attributes")
            items.append(Item(
                _string(item_raw.get("sku"), "sku", maximum=100),
                _string(item_raw.get("title"), "title", maximum=200),
                _string(item_raw.get("category"), "category", maximum=100),
                _integer(item_raw.get("qty"), "qty", True),
                money(item_raw.get("unit_price"), "unit_price"),
                {_string(k, "attribute name", True, 100): _string(v, "attribute value", True, 200)
                 for k, v in item_attrs.items()},
            ))
        order = Order(
            _string(o.get("order_id"), "order_id", maximum=100),
            _string(o.get("merchant_id"), "merchant_id", maximum=100),
            items, money(o.get("total"), "total"),
            _string(o.get("currency"), "currency", maximum=3),
            dt(o.get("ordered_at"), "ordered_at"),
            _string(o.get("card_ref"), "card_ref", maximum=100),
            _string(o.get("status"), "status", maximum=50),
            day(o.get("promised_delivery"), "promised_delivery"),
            dt(o.get("delivered_at"), "delivered_at"),
        )
        if order.total is not None and order.total <= 0:
            raise ValueError("total must be positive")
        events = []
        for n, event_raw in enumerate(limited(_list(raw.get("events", []), "events"), MAX_EVENTS, "events")):
            event_raw = _mapping(event_raw, f"events.{n}")
            actor = _string(event_raw.get("actor"), "event actor", True, 30)
            kind = _string(event_raw.get("kind"), "event kind", True, 50)
            if actor not in _ALLOWED_EVENT_ACTORS or kind not in _ALLOWED_EVENT_KINDS:
                raise ValueError("event actor or kind is not allowed")
            events.append(Event(dt(event_raw.get("at"), "event.at"), actor, kind,
                                _string(event_raw.get("detail"), "event detail", maximum=300)))
        q = _mapping(raw.get("claim") or {}, "claim")
        reason = _string(q.get("reason"), "claim reason", maximum=50)
        response = q.get("merchant_response")
        if reason is not None and reason not in _ALLOWED_REASONS:
            raise ValueError("claim reason is not supported")
        if response not in _ALLOWED_RESPONSES:
            raise ValueError("merchant response is not supported")
        note = _string(q.get("note") or "", "claim note", maximum=500)
        claim = Claim(reason, dt(q.get("merchant_contacted_at"), "merchant_contacted_at"), response,
                      note, _string(q.get("contact_token"), "contact_token", maximum=100))
        return cls(_string(raw.get("case_id"), "case_id", maximum=100), mandate, order, events, claim)

    def to_dict(self):
        return plain(asdict(self))


@dataclass(frozen=True)
class Deviation:
    field: str
    expected: Any
    actual: Any

    def to_dict(self):
        return plain(asdict(self))


@dataclass(frozen=True)
class Evidence:
    source: str
    field: str
    value: Any
    checked_at: str

    def to_dict(self):
        return plain(asdict(self))


@dataclass
class Decision:
    route: str
    sub: str
    reasons: list[str]
    deviations: list[Deviation]
    evidence: list[Evidence]
    missing: list[str] = field(default_factory=list)
    c_eligible_after: str | None = None

    def to_dict(self):
        return plain(asdict(self))
