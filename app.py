"""Undocart self-serve application; Python standard library only."""
import html
import io
import json
import os
import secrets
import socket
import threading
import time
from copy import deepcopy
from datetime import date
from http.server import BaseHTTPRequestHandler
from urllib.parse import quote, urlparse

from app_review_patch import MADE_UP_NOTICE, relabel_tool_dates, sample_for_today, workspace_actions
from remedy.httpserver import LimitedServer, MAX_BODY, MAX_THREADS, REQUEST_TIMEOUT
from remedy.sample_data import RAWS
from remedy.selfserve import (InputError, check, normalised, parse_form, parse_json,
                              utc_today, validate_contact)
from remedy.selfserve_worker import (BusyError, ComputeTimeout, COMPUTE_TIMEOUT,
                                     bounded_call, evaluate_many, evaluate_raw, parse_fields)
from remedy.store import (CapacityError, EMPTY_WORKSPACE_TTL, MissingWorkspace, RateLimitError,
                          Store, WORKSPACE_TTL)

NOTICE = "Operated by AI agents. Early version: keep your own copy of anything important. Do not enter card or bank account numbers, bank statements, or other people's personal data. Nothing is charged."
STYLE = "body{max-width:900px;margin:auto;padding:1rem;font:16px/1.5 system-ui;background:#f6f3ea;color:#18221c}.notice,fieldset,pre{padding:1rem;margin:1rem 0;border:1px solid #ccc;border-radius:8px;background:white}.notice{background:#54221f;color:white}.notice a{color:white}.sample{background:#fff3b0;border:2px solid #a80;padding:1rem;margin:1rem 0;border-radius:8px}label{display:block;margin:.5rem 0}input,select,textarea{display:block;width:100%;padding:.5rem;box-sizing:border-box}input[type=checkbox]{width:auto}button,.button{display:inline-block;padding:.6rem 1rem;background:#176b52;color:white;border:0;border-radius:5px;text-decoration:none;margin:.2rem}pre{white-space:pre-wrap;overflow-wrap:anywhere}.danger{background:#922}.badge{font-weight:bold;font-size:1.2rem}form.inline{display:inline}"
COPY_SCRIPT = "document.addEventListener('click',async function(e){if(e.target.id==='copy-message'){try{await navigator.clipboard.writeText(document.getElementById('message').textContent);e.target.textContent='Copied';}catch(error){e.target.textContent='Select and copy the message below';}}});"

RETENTION_DAYS = WORKSPACE_TTL.days
EMPTY_RETENTION_DAYS = EMPTY_WORKSPACE_TTL.days
RETENTION_TEXT = (f"Saved cases are kept for {RETENTION_DAYS} days after your last visit; each visit restarts the count. "
                  f"A workspace with no cases is removed after {EMPTY_RETENTION_DAYS} day without a visit.")
GONE_MESSAGES = {
    "deleted": "This workspace was deleted using its own Delete button. Deleted data cannot be restored. If you exported it, your export file still holds the cases.",
    "expired": (f"This workspace was removed automatically: {RETENTION_DAYS} days passed without a visit "
                f"(or {EMPTY_RETENTION_DAYS} day, for a workspace with no cases). Removed data cannot be restored. "
                "If you exported it, your export file still holds the cases."),
}
# Typed-form facts that must never be invented. Each appears as
# [you entered] in the message the user sends.
REQUIRED_FORM_FIELDS = (("unit_price", "Unit price"), ("total", "Total"), ("order_date", "Order date"))
JSON_LIMIT_TEXT = "Pasted or uploaded JSON may hold about 8 KB of text in total, at most 500 characters per text value and 16 entries per list."
PRIVACY_BODY = (
    "<p>undocart asks for no account and no contact details. It stores only the order facts you type or paste, in a private workspace "
    "that is found by a random bookmark link; only a hash of that link is stored.</p>"
    "<p>Nothing is sent to the merchant, your card issuer or anyone else; you send messages yourself. No AI model or outside service is called.</p>"
    f"<p>{html.escape(RETENTION_TEXT)} You can export everything, or delete the workspace immediately, from any workspace page.</p>"
    "<p>Do not enter card or bank account numbers, bank statements, or other people's personal data.</p>"
    "<p>Full policy: <a href='https://manykiln.com/privacy'>https://manykiln.com/privacy</a></p>")


def esc(value):
    return html.escape(str(value), quote=True)


def page(title, body):
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            f"<title>{esc(title)}</title><link rel='stylesheet' href='/static/style.css'>"
            "<script src='/static/copy.js' defer></script></head><body>"
            f"<div class='notice'>{esc(NOTICE)} <a href='https://manykiln.com/privacy'>Privacy</a> "
            "<a href='/privacy'>Privacy summary</a></div>"
            "<p>Rule-based tool, no AI model; not legal advice; nothing is sent.</p>"
            f"<main><h1>{esc(title)}</h1>{body}</main></body></html>")


def is_sample(entry):
    return bool((entry.get("input") or {}).get("selfserve", {}).get("sample"))


def sample_banner():
    return f"<p class='sample'><strong>{esc(MADE_UP_NOTICE)}</strong></p>"


def render_form(token, values=None, action=None):
    v = values or {}
    ss, order, mandate = v.get("selfserve", {}), v.get("order", {}), v.get("mandate", {})
    constraints = mandate.get("constraints", {})
    item = (order.get("items") or [{}])[0]
    attrs, wanted = item.get("attributes", {}), constraints.get("required_attributes", {})
    target = action or f"/w/{quote(token)}/cases"

    def inp(name, label, value=None, kind="text", required=False):
        value = "" if value is None else value
        flag = " required" if required else ""
        return f"<label>{esc(label)}<input type='{esc(kind)}' name='{esc(name)}' value='{esc(value)}'{flag}></label>"

    def select(name, label, choices, selected):
        options = "".join(f"<option value='{esc(key)}'{' selected' if key == selected else ''}>{esc(text)}</option>" for key, text in choices)
        return f"<label>{esc(label)}<select name='{esc(name)}'>{options}</select></label>"

    def section(title, fields):
        return f"<fieldset><legend>{esc(title)}</legend>{''.join(fields)}</fieldset>"

    cancelled = next((e.get("at", "")[:10] for e in v.get("events", []) if e.get("kind") == "order_cancelled" and e.get("at")), "")
    before = ss.get("cancellation_before_shipment")
    charges = "\n".join(f"{x.get('amount', '')}, {str(x.get('at', ''))[:10]}, {x.get('status', '')}" for x in ss.get("charges", []))
    parts = []
    if values is not None:
        parts.append("<p>This form edits the first item and the fields shown. Other items, attributes, categories, notes and events are kept. Use the full JSON editor below to edit those details.</p>")
    parts.append(f"<form method='post' action='{esc(target)}'>")
    parts.append(section("What you asked for", [
        inp("asked_item", "Item", mandate.get("intent")), inp("asked_size", "Size", wanted.get("size")),
        inp("asked_colour", "Colour", wanted.get("colour")), inp("max_unit_price", "Maximum unit price", constraints.get("max_unit_price")),
        inp("max_total", "Maximum total", constraints.get("max_total")), inp("quantity", "Quantity", constraints.get("quantity", 1), "number"),
        inp("deliver_by", "Deliver by", constraints.get("deliver_by"), "date")]))
    parts.append(section("What was ordered", [
        inp("merchant_name", "Merchant name", order.get("merchant_id")), inp("order_label", "Short order label", order.get("order_id")),
        inp("item_title", "Item title", item.get("title")), inp("actual_size", "Size", attrs.get("size")),
        inp("actual_colour", "Colour", attrs.get("colour")), inp("unit_price", "Unit price (required)", item.get("unit_price"), required=True),
        inp("ordered_quantity", "Quantity", item.get("qty", 1), "number"), inp("total", "Total (required)", order.get("total"), required=True),
        inp("currency", "Currency", order.get("currency") or "USD"),
        inp("order_date", "Order date (required)", str(order.get("ordered_at") or "")[:10], "date", required=True)]))
    parts.append(section("Delivery and cancellation", [
        select("status", "Status", [("", "Infer from dates"), ("placed", "Placed"), ("shipped", "Shipped"),
                                     ("in_transit", "In transit"), ("delivered", "Delivered"), ("cancelled", "Cancelled")], order.get("status", "")),
        inp("promised_delivery", "Promised delivery", order.get("promised_delivery"), "date"),
        inp("delivered_date", "Delivered date", str(order.get("delivered_at") or "")[:10], "date"),
        inp("cancellation_date", "Cancellation date", cancelled, "date"),
        select("cancellation_before_shipment", "Was cancellation before shipment?", [("", "Unknown"), ("yes", "Yes"), ("no", "No")],
               "yes" if before is True else "no" if before is False else "")]))
    parts.append(section("Merchant policy as you read it", [
        inp("return_days", "Return window in days (leave blank if unknown)", ss.get("return_days"), "number"),
        select("final_sale", "Final sale", [("no", "No"), ("yes", "Yes")], "yes" if ss.get("final_sale") else "no"),
        inp("cancel_until", "Cancellation allowed until", ss.get("cancel_until"), "date")]))
    reasons = ("wrong_item", "unwanted", "not_received", "charged_twice", "wrong_amount", "cancelled_but_charged", "refund_not_received", "not_as_described")
    parts.append(section("Problem", [select("problem_type", "Problem type", [(x, x.replace("_", " ")) for x in reasons], v.get("claim", {}).get("reason", "wrong_item"))]))
    parts.append(section("Charges you see", [f"<label>One per line: amount, YYYY-MM-DD, settled/pending. Example: 80.00, 2026-10-02, settled<textarea name='charges' rows='5'>{esc(charges)}</textarea></label>"]))
    parts.append(section("Refund promise", [inp("refund_date", "Promise date", ss.get("refund_date"), "date"), inp("refund_amount", "Amount", ss.get("refund_amount"))]))
    parts.append("<button>Save case</button></form>")
    json_value = json.dumps(v, indent=2, ensure_ascii=False) if values is not None else ""
    parts.append(f"<details><summary>Full JSON editor or upload</summary><p>Saving JSON replaces the case facts, including the contact log, with this document. {esc(JSON_LIMIT_TEXT)}</p><form method='post' action='{esc(target)}' enctype='multipart/form-data'>"
                 f"<label>Case JSON<textarea name='case_json' rows='24'>{esc(json_value)}</textarea></label>"
                 "<label>JSON file (takes precedence over the text)<input type='file' name='case_file' accept='application/json'></label>"
                 "<button>Save JSON case</button></form></details>")
    return "".join(parts)


def _require_typed_facts(fields):
    missing = [label for name, label in REQUIRED_FORM_FIELDS if not str(fields.get(name, "") or "").strip()]
    if missing:
        raise InputError("Please fill in: " + ", ".join(missing) + ". undocart never fills in order facts for you, "
                         "because every fact appears as [you entered] in the message you send.")


def _json_mode(fields):
    return "case_json" in fields or "case_file" in fields


def _prepare_case(fields, today, original=None):
    """Parse through the app-visible adapters, validate output, and return storage JSON."""
    if _json_mode(fields):
        text = fields.get("case_json") or ""
        if not text.strip():
            raise InputError("Paste case JSON or choose a JSON file before saving the JSON case.")
        case = parse_json(text)
    else:
        _require_typed_facts(fields)
        case = parse_form(fields, original)
    check(case, today)
    return normalised(case)


def _prepare_many(raws, today):
    """Prepare several made-up sample cases in one isolated computation."""
    return [_prepare_case({"case_json": json.dumps(raw)}, today) for raw in raws]


def _snapshot(entry):
    return json.dumps([entry.get("input"), entry.get("contacts", [])], sort_keys=True, default=str)


def _require_unchanged(entry, snapshot):
    if _snapshot(entry) != snapshot:
        raise InputError("This case changed while your request was being processed. Reload the page and try again.")


def _origin(value):
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("invalid origin")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("invalid origin")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


class App:
    def __init__(self, data_dir, public_base_url):
        self.base = public_base_url.rstrip("/")
        self.origin = _origin(self.base)
        self.parsed = urlparse(self.base)
        self.proxy_hosts = {value.strip().lower() for value in os.environ.get("APP_PROXY_HOSTS", "").split(",") if value.strip()}
        self.store = Store(data_dir, os.environ.get("APP_DATA_CAP_MB", "200"))
        self.store.sweep()
        self.stop = threading.Event()
        self.sweeper = threading.Thread(target=self._sweep, daemon=True)
        self.sweeper.start()

    def _sweep(self):
        while not self.stop.wait(86400):
            self.store.sweep()


def _token_of(path):
    parts = [x for x in path.split("/") if x]
    return parts[1] if len(parts) > 1 and parts[0] == "w" else None


class Handler(BaseHTTPRequestHandler):
    server_version = "Undocart"
    timeout = REQUEST_TIMEOUT

    def setup(self):
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT)
        self.rfile.close()
        self.rfile = io.BytesIO(self.server.take_input(self.connection))

    def _expire_connection(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def handle(self):
        self.compute_deadline = time.monotonic() + REQUEST_TIMEOUT - 0.5
        timer = threading.Timer(REQUEST_TIMEOUT, self._expire_connection)
        timer.daemon = True
        timer.start()
        try:
            super().handle()
        finally:
            timer.cancel()

    def _compute(self, function, *args):
        remaining = self.compute_deadline - time.monotonic()
        return bounded_call(function, *args, timeout=min(COMPUTE_TIMEOUT, remaining))

    def log_message(self, *args):
        pass

    @property
    def app(self):
        return self.server.app

    def _headers(self, status, ctype="text/html; charset=utf-8", length=0, location=None, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        if location:
            self.send_header("Location", location)
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def send(self, status, data, ctype="text/html; charset=utf-8", filename=None):
        raw = data.encode() if isinstance(data, str) else data
        self._headers(status, ctype, len(raw), filename=filename)
        self.wfile.write(raw)

    def redirect(self, path):
        self._headers(303, location=path)

    def error_page(self, status, title, message):
        self.send(status, page(title, "<p>" + esc(message) + "</p>"))

    def _not_found(self):
        """404 page; explains a removed workspace from its tombstone."""
        message = "Workspace or page not found."
        try:
            token = _token_of(urlparse(self.path).path)
            if token and not self.app.store.exists(token):
                reason = self.app.store.gone(token)
                if reason in GONE_MESSAGES:
                    message = GONE_MESSAGES[reason]
        except Exception:
            pass
        self.error_page(404, "Not found", message)

    def _host_ok(self):
        values = self.headers.get_all("Host", [])
        if len(values) != 1:
            return False
        host = values[0]
        if not host or any(c.isspace() or c in "/@,?#\\" for c in host):
            return False
        try:
            parsed = urlparse("http://" + host)
            parsed.port
            if not parsed.hostname:
                return False
        except ValueError:
            return False
        return (host.lower() == self.app.parsed.netloc.lower() or host.lower() in self.app.proxy_hosts or
                (self.command == "POST" and self._origin_ok()))

    def _origin_ok(self):
        origins = self.headers.get_all("Origin", [])
        try:
            if origins:
                return len(origins) == 1 and _origin(origins[0]) == self.app.origin
            refs = self.headers.get_all("Referer", [])
            if len(refs) != 1:
                return False
            parsed = urlparse(refs[0])
            return _origin(f"{parsed.scheme}://{parsed.netloc}") == self.app.origin
        except ValueError:
            return False

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0:
            raise InputError("A valid Content-Length is required.")
        if length > MAX_BODY:
            raise OverflowError()
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise InputError("Incomplete request.")
        return raw

    def _fields(self, raw):
        return self._compute(parse_fields, self.headers.get("Content-Type", ""), raw)

    def _load_case(self, state, cid, today):
        entry = state["cases"][cid]
        raw = deepcopy(entry["input"])
        raw.setdefault("selfserve", {})["contacts"] = deepcopy(entry.get("contacts", []))
        case, result = self._compute(evaluate_raw, raw, today)
        # A deadline the tool computed is not a fact the user entered.
        result["artefact"].text = relabel_tool_dates(result["artefact"].text)
        return entry, case, result

    def _issuer_sheet(self, state, cid, entry, result, today):
        """Return (artefact, confirmed_on or None, needs_confirmation).

        Confirmation freezes the sheet the user confirmed. A later day still
        serves that same sheet, with the same sha256, while the inputs are
        unchanged. Any edit or new contact clears the confirmation.
        """
        art = result["artefact"]
        if result["decision"].route != "C":
            return art, None, False
        confirmed = entry.get("confirmed_hash")
        if not confirmed:
            return art, None, True
        if confirmed == art.packet_hash:
            return art, entry.get("confirmed_on"), False
        try:
            day = date.fromisoformat(str(entry.get("confirmed_on") or ""))
        except ValueError:
            return art, None, True
        if day > today:
            return art, None, True
        frozen = self._load_case(state, cid, day)[2]
        if frozen["decision"].route == "C" and frozen["artefact"].packet_hash == confirmed:
            return frozen["artefact"], day.isoformat(), False
        return art, None, True

    def _dispatch(self, method):
        try:
            if not self._host_ok():
                return self.error_page(400, "Bad request", "Invalid Host header.")
            path = urlparse(self.path).path
            if method == "GET" and path == "/healthz":
                return self.send(200, "ok", "text/plain; charset=utf-8")
            if method == "GET":
                if path.startswith("/static/"):
                    return self._get(path)
                self.app.store.allow_get(_token_of(path))
                return self._get(path)
            if not self._origin_ok():
                return self.error_page(403, "Forbidden", "Cross-origin request rejected.")
            return self._post(path)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return
        except (BusyError, ComputeTimeout):
            self.error_page(503, "Please try again", "The processing limit was reached. Your saved case has not been changed.")
        except OverflowError:
            self.error_page(413, "Too large", "Requests must be 1 MB or less.")
        except (MissingWorkspace, KeyError):
            self._not_found()
        except RateLimitError:
            self.error_page(429, "Too many requests", "Please try again later.")
        except CapacityError:
            self.error_page(507, "Storage unavailable", "No new data can be saved. You can still view, export or delete saved data.")
        except InputError as exc:
            self.error_page(400, "Check your input", str(exc))
        except (ValueError, TypeError, AttributeError, RecursionError, ArithmeticError):
            self.error_page(400, "Check your input", "The submitted data is invalid. Check dates, amounts and required fields.")
        except Exception:
            self.error_page(500, "Unable to complete request", "Please try again. Keep your own copy of important information.")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _get(self, path):
        if path == "/static/style.css":
            return self.send(200, STYLE, "text/css; charset=utf-8")
        if path == "/static/copy.js":
            return self.send(200, COPY_SCRIPT, "application/javascript; charset=utf-8")
        if path == "/privacy":
            return self.send(200, page("Privacy summary", PRIVACY_BODY))
        if path == "/":
            return self.send(200, page("undocart", "<p>Find the deadline and least-confrontational next step when an AI shopping order goes wrong.</p>"
                                       f"<p>{esc(RETENTION_TEXT)} Export a copy if you may be away longer.</p>"
                                       "<form method='post' action='/start'><button>Start a workspace</button></form>"
                                       "<form method='post' action='/sample'><button>Try with sample data</button></form>"
                                       "<p>The sample workspace holds made-up orders, clearly labelled as made-up. You can export or delete it at any time.</p>"))
        parts = [x for x in path.split("/") if x]
        if len(parts) < 2 or parts[0] != "w":
            raise KeyError(path)
        token = parts[1]
        state = self.app.store.read(token)
        base = "/w/" + quote(token, safe="")
        actions = workspace_actions(token)
        today = utc_today()
        if len(parts) == 2:
            cids = list(state["cases"])
            raws = []
            for cid in cids:
                entry = state["cases"][cid]
                raw = deepcopy(entry["input"])
                raw.setdefault("selfserve", {})["contacts"] = deepcopy(entry.get("contacts", []))
                raws.append(raw)
            # One isolated computation for the whole list, not one per case.
            results = self._compute(evaluate_many, raws, today) if raws else []
            rows, any_sample = [], False
            for cid, result in zip(cids, results):
                link = base + "/c/" + quote(cid, safe="")
                entry = state["cases"][cid]
                order = entry["input"].get("order", {})
                title = order.get("order_id") or cid
                label = ""
                if is_sample(entry):
                    any_sample = True
                    label = " (made-up sample)"
                future = [row for row in result["deadlines"] if row.get("days_remaining", -1) >= 0]
                deadline = min(future, key=lambda row: (row["date"], row["label"])) if future else None
                suffix = (f" — next deadline {esc(deadline['date'])} ({deadline['days_remaining']} days remaining)"
                          if deadline else " — no calculated deadline")
                rows.append(f"<li><a href='{link}'>{esc(title)}</a>{esc(label)}{suffix}</li>")
            keep = str(state.get("keep_until", ""))[:10]
            if state["cases"]:
                retention = (f"<p>Kept for {RETENTION_DAYS} days after your last visit: at least until {esc(keep)}. "
                             "Each visit restarts the count. Export a copy if you may be away longer.</p>")
            else:
                retention = (f"<p>This workspace has no cases yet. An empty workspace is removed after {EMPTY_RETENTION_DAYS} day "
                             f"without a visit. Once you save a case, it is kept for {RETENTION_DAYS} days after your last visit.</p>")
            body = (actions + (sample_banner() if any_sample else "") +
                    "<p><strong>Bookmark this link: it is the only key and cannot be recovered.</strong></p>" + retention +
                    f"<h2>Your cases</h2><ul>{''.join(rows)}</ul>"
                    "<h2>Start a case</h2><p>Tell us what happened with an order; you get a deadline, a route and a message to send yourself.</p>"
                    + render_form(token))
            return self.send(200, page("Your private workspace", body))
        if len(parts) == 3 and parts[2] == "new":
            return self.send(200, page("New case", actions + render_form(token)))
        if len(parts) == 3 and parts[2] == "export":
            return self.send(200, json.dumps(state, indent=2, sort_keys=True), "application/json; charset=utf-8", "undocart-workspace.json")
        if len(parts) not in {4, 5} or parts[2] != "c":
            raise KeyError(path)
        cid = parts[3]
        entry = state["cases"][cid]
        url = base + "/c/" + quote(cid, safe="")
        if len(parts) == 5 and parts[4] == "edit":
            value = deepcopy(entry["input"])
            value.setdefault("selfserve", {})["contacts"] = deepcopy(entry.get("contacts", []))
            return self.send(200, page("Edit case", actions + (sample_banner() if is_sample(entry) else "") + render_form(token, value, url + "/edit")))
        entry, case, result = self._load_case(state, cid, today)
        art, confirmed_on, needs_confirmation = self._issuer_sheet(state, cid, entry, result, today)
        if len(parts) == 5:
            if parts[4] != "message.txt":
                raise KeyError(path)
            if needs_confirmation:
                return self.error_page(409, "Confirmation required", "Confirm every fact before downloading this issuer fact sheet.")
            return self.send(200, art.text, "text/plain; charset=utf-8", "undocart-message.txt")
        sample = is_sample(entry)
        tag = "made-up sample data" if sample else "you entered"
        deadlines = "".join(f"<li>{esc(x['label'])}: {esc(x['date'])} ({x['days_remaining']} days remaining)</li>" for x in result["deadlines"]) or "<li>No calculated deadline</li>"
        differences = "".join(f"<li>{esc(x['field'])}: asked for {esc(x['expected'])}; ordered {esc(x['actual'])} [{tag}]</li>" for x in result["deviations"]) or "<li>No difference found from the entered mandate.</li>"
        missing = "".join(f"<li>{esc(x)}</li>" for x in result["missing"]) or "<li>None</li>"
        confirmation = ""
        if needs_confirmation:
            boxes = "".join(f"<label><input type='checkbox' name='fact_{i}' value='yes'>Confirm {esc(f['field'])}: {esc(json.dumps(f['value'], sort_keys=True))}</label>" for i, f in enumerate(art.facts or []))
            confirmation = f"<form method='post' action='{url}/confirm'>{boxes}<input type='hidden' name='packet_hash' value='{esc(art.packet_hash)}'><button>Confirm fact sheet</button></form>"
        issuer_notice = ""
        if result["decision"].route == "C":
            issuer_notice = "<p>This is an unverified, self-reported fact sheet.</p>"
            if confirmed_on:
                issuer_notice += f"<p>Showing the fact sheet you confirmed on {esc(confirmed_on)}.</p>"
        body = (actions + (sample_banner() if sample else "") +
                f"<p class='badge'>{esc(result['route'])}</p><h2>Why</h2><ul>"
                + "".join(f"<li>{esc(x.replace('_', ' ').capitalize())}</li>" for x in result["decision"].reasons)
                + f"</ul><h2>Deadlines</h2><ul>{deadlines}</ul><h2>How the order differs</h2><ul>{differences}</ul>"
                f"<h2>Missing facts</h2><ul>{missing}</ul><h2>Message to send yourself</h2>{issuer_notice}<pre id='message'>{esc(art.text)}</pre>"
                f"<button type='button' id='copy-message'>Copy</button><a class='button' href='{url}/message.txt'>Download .txt</a>{confirmation}"
                f"<p>Send the message yourself first; undocart sends nothing.</p><form method='post' action='{url}/contact'>"
                "<label>Contact date<input name='date' type='date' required></label><label>Channel<input name='channel' placeholder='channel word' required></label>"
                "<label>Response<select name='response'><option>none</option><option>refused</option><option>partial</option><option>resolved</option></select></label>"
                f"<button>Add merchant contact</button></form><a href='{url}/edit'>Edit</a><form method='post' action='{url}/delete'><button class='danger'>Delete case</button></form>")
        return self.send(200, page("Case " + cid, body))

    def _post(self, path):
        parts = [x for x in path.split("/") if x]
        token = _token_of(path)
        self.app.store.allow_post(token)
        raw = self._body()
        fields = self._fields(raw) if raw else {}
        if fields.get("case_file"):
            fields["case_json"] = fields["case_file"]
        if path in {"/start", "/sample"}:
            samples = []
            if path == "/sample":
                today = utc_today()
                shifted = [sample_for_today(source, today) for source in RAWS]
                samples = self._compute(_prepare_many, shifted, today)
            token = self.app.store.create()
            for value in samples:
                self.app.store.put_case(token, value["case_id"], value)
            return self.redirect("/w/" + quote(token, safe=""))
        if not token:
            raise MissingWorkspace()
        base = "/w/" + quote(token, safe="")
        if len(parts) == 3 and parts[2] == "delete":
            self.app.store.delete(token)
            return self.redirect("/")
        create = len(parts) == 3 and parts[2] == "cases"
        action = parts[4] if len(parts) == 5 and parts[2] == "c" else None
        if create:
            value = self._compute(_prepare_case, fields, utc_today())
            cid = "case-" + secrets.token_urlsafe(9)
            value["case_id"] = cid
            self.app.store.put_case(token, cid, value)
            return self.redirect(base + "/c/" + quote(cid, safe=""))
        if action:
            cid = parts[3]
            url = base + "/c/" + quote(cid, safe="")
            if action == "edit":
                # Compute outside the store lock, then compare-and-set.
                state = self.app.store.read(token)
                entry = state["cases"][cid]
                snapshot = _snapshot(entry)
                original = deepcopy(entry["input"])
                original.setdefault("selfserve", {})["contacts"] = deepcopy(entry.get("contacts", []))
                json_mode = _json_mode(fields)
                value = self._compute(_prepare_case, fields, utc_today(), original)
                value["case_id"] = cid

                def edit(current):
                    _require_unchanged(current, snapshot)
                    updated = deepcopy(value)
                    # The typed form has no contact fields, so it keeps the log.
                    # The JSON editor shows the log, so its edits are honoured.
                    if json_mode:
                        contacts = deepcopy(value.get("selfserve", {}).get("contacts", []))
                    else:
                        contacts = deepcopy(current.get("contacts", []))
                    updated.setdefault("selfserve", {})["contacts"] = contacts
                    current["input"] = updated
                    current["contacts"] = deepcopy(contacts)
                    current["confirmed_hash"] = None
                    current["confirmed_on"] = None
                self.app.store.update_case(token, cid, edit)
                return self.redirect(url)
            if action == "delete":
                self.app.store.delete_case(token, cid)
                return self.redirect(base)
            if action == "contact":
                row = self._compute(validate_contact, {key: fields.get(key, "") for key in ("date", "channel", "response")})
                if row["date"] > utc_today().isoformat():
                    raise InputError("Contact dates cannot be in the future.")
                self.app.store.add_contact(token, cid, row)
                return self.redirect(url)
            if action == "confirm":
                today = utc_today()
                state = self.app.store.read(token)
                entry = state["cases"][cid]
                snapshot = _snapshot(entry)
                result = self._load_case(state, cid, today)[2]
                art = result["artefact"]
                if result["decision"].route != "C" or fields.get("packet_hash") != art.packet_hash or any(fields.get(f"fact_{i}") != "yes" for i in range(len(art.facts or []))):
                    raise InputError("Every fact and the matching hash must be confirmed.")

                def confirm(current):
                    _require_unchanged(current, snapshot)
                    current["confirmed_hash"] = art.packet_hash
                    current["confirmed_on"] = today.isoformat()
                self.app.store.update_case(token, cid, confirm)
                return self.redirect(url)
        raise KeyError(path)


def make_server(host=None, port=None, public_base_url=None, data_dir=None, max_workers=MAX_THREADS):
    host = host or os.environ.get("APP_HOST", "127.0.0.1")
    port = int(port if port is not None else os.environ.get("PORT", "8080"))
    public_base_url = public_base_url or os.environ.get("PUBLIC_BASE_URL", f"http://{host}:{port}")
    data_dir = data_dir or os.environ.get("DATA_DIR")
    if not data_dir:
        raise RuntimeError("DATA_DIR is required")
    app = App(data_dir, public_base_url)
    try:
        server = LimitedServer((host, port), Handler, max_workers=max_workers)
    except Exception:
        app.stop.set()
        raise
    server.app = app
    return server


def main():
    server = make_server()
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
