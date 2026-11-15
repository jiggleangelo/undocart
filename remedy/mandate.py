from .models import Case, Deviation


def deviations(case: Case) -> list[Deviation]:
    c, order = case.mandate.constraints, case.order
    out: list[Deviation] = []
    if c.max_total is not None and order.total is not None and order.total > c.max_total:
        out.append(Deviation("total", c.max_total, order.total))
    actual_qty = sum(i.qty or 0 for i in order.items)
    if c.quantity is not None and actual_qty != c.quantity:
        out.append(Deviation("quantity", c.quantity, actual_qty))
    for item in order.items:
        if c.max_unit_price is not None and item.unit_price is not None and item.unit_price > c.max_unit_price:
            out.append(Deviation(f"items.{item.sku}.unit_price", c.max_unit_price, item.unit_price))
        if c.allowed_categories and item.category not in c.allowed_categories:
            out.append(Deviation(f"items.{item.sku}.category", c.allowed_categories, item.category))
        for name, wanted in c.required_attributes.items():
            actual = item.attributes.get(name)
            if actual != wanted:
                out.append(Deviation(f"items.{item.sku}.attributes.{name}", wanted, actual))
    if c.deliver_by and order.promised_delivery and order.promised_delivery > c.deliver_by:
        out.append(Deviation("promised_delivery", c.deliver_by, order.promised_delivery))
    return out
