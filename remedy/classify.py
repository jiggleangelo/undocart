from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from .config import AMOUNT_TOLERANCE, CREDIT_WAIT_DAYS, GRACE_DAYS, WAIT_DAYS
from .mandate import deviations
from .models import Decision, Evidence

PAYMENT_CLAIMS = {"charged_twice", "wrong_amount", "refund_not_received", "cancelled_but_charged"}


def _stamp(today):
    return today.isoformat()


def _e(source, field, value, today):
    return Evidence(source, field, value, _stamp(today))


def _missing(case):
    fields = {
        "case_id": case.case_id, "mandate.mandate_id": case.mandate.mandate_id,
        "mandate.principal_ref": case.mandate.principal_ref, "mandate.intent": case.mandate.intent,
        "mandate.issued_at": case.mandate.issued_at, "order.order_id": case.order.order_id,
        "order.merchant_id": case.order.merchant_id, "order.items": case.order.items,
        "order.total": case.order.total, "order.currency": case.order.currency,
        "order.ordered_at": case.order.ordered_at, "order.card_ref": case.order.card_ref,
        "claim.reason": case.claim.reason,
    }
    missing = [name for name, value in fields.items() if value is None or value == "" or value == []]
    for n, item in enumerate(case.order.items):
        for key in ("sku", "title", "category", "qty", "unit_price"):
            value = getattr(item, key)
            if value is None or (key == "qty" and value <= 0):
                missing.append(f"order.items.{n}.{key}")
    if case.claim.reason == "not_as_described" and not case.claim.merchant_response:
        missing.append("claim.merchant_response")
    return missing


def _amount(row):
    try:
        value = Decimal(str(row.get("amount")))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value if value.is_finite() else None


def _at(row):
    value = row.get("at")
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo is not None and result.utcoffset() is not None else None


def _ledger(case, sources):
    rows = sources.ledger.charges(case.order.card_ref)
    valid = [row for row in rows if isinstance(row, dict) and _amount(row) is not None and _at(row) is not None]
    scoped = [row for row in valid if row.get("status") == "settled" and
              row.get("order_id") == case.order.order_id and row.get("merchant_id") == case.order.merchant_id]
    return rows, scoped


def _matching_credit(charges, amount, after):
    for row in charges:
        row_amount, row_at = _amount(row), _at(row)
        if row_amount is not None and row_at is not None and row_amount < 0:
            if abs(abs(row_amount) - amount) <= AMOUNT_TOLERANCE and row_at >= after:
                return row
    return None


def _hold(field):
    return {"hold": field}


def _ground(case, sources, today):
    reason, order = case.claim.reason, case.order
    if reason == "not_received":
        status = sources.carrier.status(order.order_id)
        if not isinstance(status, dict):
            return _hold("carrier.order_record")
        promised_raw = status.get("promised_delivery")
        try:
            promised = date.fromisoformat(promised_raw) if isinstance(promised_raw, str) else promised_raw
        except ValueError:
            return _hold("carrier.promised_delivery")
        if not promised:
            return _hold("carrier.promised_delivery")
        if status.get("status") != "delivered" and today > promised + timedelta(days=GRACE_DAYS):
            return {"name": "non_receipt", "evidence": [
                _e("CarrierSource", "delivery_status", status.get("status"), today),
                _e("CarrierSource", "promised_delivery", promised, today),
                _e("Config", "grace_days", GRACE_DAYS, today)], "contact": True}
        return None

    if reason not in PAYMENT_CLAIMS:
        return None
    rows, charges = _ledger(case, sources)
    if not rows:
        return _hold("ledger.card_record")
    confirmation = sources.merchant.confirmation(order.order_id)

    if reason == "charged_twice":
        if not isinstance(confirmation, dict):
            return _hold("merchant.order_confirmation")
        try:
            total = Decimal(str(confirmation.get("total")))
        except (InvalidOperation, ValueError, TypeError):
            return _hold("merchant.confirmation_total")
        if not total.is_finite():
            return _hold("merchant.confirmation_total")
        debits = [row for row in charges if _amount(row) is not None and _amount(row) > 0]
        debits.sort(key=_at)
        if sum((_amount(row) for row in debits), Decimal("0")) <= total + AMOUNT_TOLERANCE:
            return None
        pair = None
        for n, first in enumerate(debits):
            for second in debits[n + 1:]:
                if abs(_amount(first) - _amount(second)) <= AMOUNT_TOLERANCE and _at(second) - _at(first) <= timedelta(hours=72):
                    pair = [first, second]
                    break
            if pair:
                break
        if pair:
            return {"name": "duplicate_charge", "evidence": [
                _e("LedgerSource", "duplicate_settled_charges", pair, today),
                _e("MerchantSource", "order_confirmation", {"total": str(total), "currency": confirmation.get("currency")}, today)],
                "contact": False}
        return None

    if reason == "wrong_amount":
        if not isinstance(confirmation, dict) or confirmation.get("total") is None:
            return _hold("merchant.order_confirmation")
        try:
            total = Decimal(str(confirmation["total"]))
        except (InvalidOperation, ValueError, TypeError):
            return _hold("merchant.confirmation_total")
        if not total.is_finite():
            return _hold("merchant.confirmation_total")
        mismatches = [row for row in charges if _amount(row) is not None and _amount(row) > 0 and
                      _amount(row) - total > AMOUNT_TOLERANCE]
        if mismatches:
            mismatches.sort(key=lambda row: _amount(row) - total, reverse=True)
            return {"name": "amount_mismatch", "evidence": [
                _e("LedgerSource", "settled_charge", mismatches[0], today),
                _e("MerchantSource", "order_confirmation", {"total": str(total), "currency": confirmation.get("currency")}, today),
                _e("Config", "amount_tolerance", AMOUNT_TOLERANCE, today)], "contact": False}
        return None

    if reason == "cancelled_but_charged":
        cancellation = sources.merchant.cancellation(order.order_id)
        if not isinstance(cancellation, dict):
            return _hold("merchant.cancellation")
        try:
            cancelled_at = datetime.fromisoformat(str(cancellation["at"]).replace("Z", "+00:00"))
            amount = Decimal(str(cancellation["amount"]))
        except (KeyError, InvalidOperation, ValueError, TypeError):
            return _hold("merchant.cancellation_details")
        debits = [row for row in charges if _amount(row) is not None and _amount(row) > 0 and
                  abs(_amount(row) - amount) <= AMOUNT_TOLERANCE]
        credit = _matching_credit(charges, amount, cancelled_at)
        if cancellation.get("before_shipment") is True and debits and not credit:
            return {"name": "cancelled_but_charged", "evidence": [
                _e("MerchantSource", "cancellation", cancellation, today),
                _e("LedgerSource", "matching_settled_charge", debits[0], today),
                _e("LedgerSource", "matching_credit", "none", today)], "contact": True}
        return None

    promise = sources.merchant.refund_promise(order.order_id)
    if not isinstance(promise, dict):
        return _hold("merchant.refund_promise")
    try:
        promised_at = datetime.fromisoformat(str(promise["at"]).replace("Z", "+00:00"))
        amount = Decimal(str(promise["amount"]))
    except (KeyError, InvalidOperation, ValueError, TypeError):
        return _hold("merchant.refund_details")
    credit = _matching_credit(charges, amount, promised_at)
    if today > promised_at.date() + timedelta(days=CREDIT_WAIT_DAYS) and not credit:
        return {"name": "refund_not_received", "evidence": [
            _e("MerchantSource", "refund_promise", promise, today),
            _e("LedgerSource", "matching_credit", "none", today),
            _e("Config", "credit_wait_days", CREDIT_WAIT_DAYS, today)], "contact": True}
    return None


def classify(case, sources, today):
    if not isinstance(today, date) or isinstance(today, datetime):
        raise ValueError("today must be supplied as a date")
    dev = deviations(case)
    missing = _missing(case)
    if missing:
        return Decision("HOLD", "HOLD_missing", ["REQUIRED_DATA_MISSING"], dev, [], missing)
    policy = sources.policy.policy(case.order.merchant_id)
    if not isinstance(policy, dict):
        return Decision("HOLD", "HOLD_policy", ["REQUIRED_DATA_MISSING"], dev, [], ["policy.merchant_record"])
    # Check the independent ownership registry before reading any private records.
    if not sources.permits(case):
        return Decision("HOLD", "HOLD_ownership", ["PRINCIPAL_BINDING_REQUIRED"], dev, [],
                        ["sources.principal_order_card_binding"])
    ledger_rows = sources.ledger.charges(case.order.card_ref)
    currencies = {row.get("currency") for row in ledger_rows if isinstance(row, dict) and row.get("currency")}
    if any(currency != case.order.currency for currency in currencies):
        return Decision("HOLD", "HOLD_currency", ["CURRENCY_MISMATCH"], dev, [], ["ledger.currency"])
    confirmation = sources.merchant.confirmation(case.order.order_id)
    if (case.claim.reason == "not_received" and isinstance(confirmation, dict) and
            confirmation.get("status") == "delivered"):
        return Decision("HOLD", "HOLD_delivery", ["DELIVERY_CONFLICT"], dev,
                        [_e("MerchantSource", "delivery_status", "delivered", today)],
                        ["delivery.receipt_resolution"])

    if case.claim.reason not in PAYMENT_CLAIMS and isinstance(confirmation, dict):
        cancel_deadline_raw = confirmation.get("cancel_deadline")
        try:
            cancel_deadline = date.fromisoformat(cancel_deadline_raw) if cancel_deadline_raw else None
        except ValueError:
            cancel_deadline = None
        if confirmation.get("status") == "placed" and policy.get("cancel_supported") is True and cancel_deadline and today <= cancel_deadline:
            evidence = [_e("MerchantSource", "order_status", "placed", today),
                        _e("PolicySource", "cancellation_supported", True, today),
                        _e("MerchantSource", "cancel_deadline", cancel_deadline, today)]
            return Decision("A", "A_cancel", ["CANCEL_WINDOW_OPEN"], dev, evidence)
        delivered_raw = confirmation.get("delivered_at")
        try:
            delivered = datetime.fromisoformat(delivered_raw.replace("Z", "+00:00")).date() if delivered_raw else None
        except (AttributeError, ValueError):
            delivered = None
        try:
            return_days = int(policy.get("return_days"))
        except (TypeError, ValueError):
            return_days = 0
        eligible = all(item.category not in policy.get("non_returnable", []) for item in case.order.items)
        deadline = delivered + timedelta(days=return_days) if delivered and return_days >= 0 else None
        if confirmation.get("status") == "delivered" and deadline and today <= deadline and eligible:
            evidence = [_e("PolicySource", "return_policy", {"return_days": return_days, "clause": policy.get("clause")}, today),
                        _e("MerchantSource", "delivered_at", delivered_raw, today),
                        _e("PolicySource", "return_deadline", deadline, today)]
            return Decision("A", "A_return", ["RETURN_WINDOW_OPEN"], dev, evidence)

    ground = _ground(case, sources, today)
    if ground and "hold" in ground:
        return Decision("HOLD", "HOLD_evidence", ["INDEPENDENT_SOURCE_MISSING"], dev, [], [ground["hold"]])
    if ground:
        name, evidence, contact_required = ground["name"], ground["evidence"], ground["contact"]
        if not contact_required:
            return Decision("C", f"C_{name}", [f"VERIFIED_{name.upper()}"], dev, evidence)
        contact = sources.merchant.contact(case.order.order_id)
        contacted = None
        if contact:
            try:
                contacted = datetime.fromisoformat(str(contact.get("contacted_at")).replace("Z", "+00:00"))
            except ValueError:
                contacted = None
        eligible_after = contacted.date() + timedelta(days=WAIT_DAYS) if contacted else None
        response = contact.get("response") if contact else None
        failed = contacted is not None and today >= eligible_after and response in (None, "none", "refused", "partial")
        if failed:
            evidence.append(_e("MerchantSource", "merchant_contact", contact, today))
            return Decision("C", f"C_{name}", [f"VERIFIED_{name.upper()}", "MERCHANT_CONTACT_FAILED"], dev, evidence)
        if response == "resolved":
            evidence.append(_e("MerchantSource", "merchant_contact", contact, today))
            return Decision("B", "B_goodwill", ["MERCHANT_REPORTED_RESOLVED"], dev, evidence)
        evidence.append(_e("Config", "wait_days", WAIT_DAYS, today))
        if contact:
            evidence.append(_e("MerchantSource", "merchant_contact", contact, today))
        return Decision("B", "B_merchant_first", [f"VERIFIED_{name.upper()}", "MERCHANT_CONTACT_REQUIRED"],
                        dev, evidence, c_eligible_after=eligible_after.isoformat() if eligible_after else None)
    return Decision("B", "B_goodwill", ["NO_VERIFIED_DISPUTE_GROUND"], dev, [])
