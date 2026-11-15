# undocart

Prototype: Fix wrong AI-agent purchases: undocart

**Use it free, no account:** https://app-undocart.manykiln.com/  
**Try the demo (made-up data):** https://try-undocart.manykiln.com/  
**Product page:** https://undocart.manykiln.com/

> Built and maintained by AI agents at [Manykiln](https://manykiln.com), a studio whose products are written, tested and operated by AI agents. This is a working prototype, not a finished product. No payment is taken anywhere: any checkout, waitlist or payment code in this project runs against fakes only, and the hosted demo uses made-up data.

- Status: prototype; run the 48 automated tests yourself with `python3 -m unittest discover`
- Runs on the Python 3.12 standard library; run the usable app with `python3 app.py` (see below)
- Licence: MIT. Free to use, fork and improve.
- Issues and pull requests are not monitored yet.

---

# Remedy MVP

Remedy is a deterministic remedy engine. Its legacy demo uses fictional shopping cases and independent fake sources. Undocart is the separate self-serve app; it works only from facts the workspace owner enters.

## Run the demo

Python 3.12 standard library only; no installation or outbound network access is needed.

```sh
python3 demo.py
```

The legacy demo, batch interface and existing tests are unchanged. Run all tests, including the review regressions, with:

```sh
python3 -m unittest discover
```

## Batch mode

```sh
python3 -m remedy.batch input.jsonl output.csv --today 2026-10-24
```

Each JSONL line is an anonymised case. `--today` is mandatory for deterministic results.

## Usable app

Undocart accepts typed facts, or a JSON document in the existing case schema. The request limit is 1 MB, but pasted JSON is validated more tightly: about 8 KB of text in total, 500 characters per text value and 16 entries per list. Each case page shows:

- the route
- the deadlines
- how the order differs from what you asked for
- the missing facts
- a copy-ready message

Nothing is sent, verified or charged. No AI model or external service is called.

The home page has a 'Try with sample data' button. It opens a workspace of made-up orders. Their dates move with today's date, and every page that shows them says they are made-up. Every workspace page offers 'Export everything' and 'Delete this workspace now'. The first workspace screen holds the new-case form. The notice and a privacy link are on every page, and `/privacy` gives a short summary.

```sh
APP_HOST=127.0.0.1 \
PORT=8080 \
PUBLIC_BASE_URL=http://127.0.0.1:8080 \
DATA_DIR=./undocart-data \
APP_DATA_CAP_MB=200 \
python3 app.py
```

`DATA_DIR` must be persistent. Run exactly one application process per data directory.

### Storage layout

- Workspaces live at `DATA_DIR/ws/<token-sha256>/state.json`.
- Writes are atomic: a temporary file is written and fsynced, `os.replace` moves it into place, then the directory is fsynced.
- At startup, orphan temporary files left by a crash are removed.
- Delete and expiry also remove any temporary files.
- Older layouts are still read: `DATA_DIR/<digest>/workspace.json`, `DATA_DIR/<digest>/state.json` and `DATA_DIR/<digest>.json`. They move to the current layout on the next write.

### Retention

- A workspace with at least one case is kept for 90 days after its last visit or write. Each visit restarts the count.
- A workspace with no cases is removed after 1 day.
- The workspace page shows the date it is kept until, and the landing page states the policy.
- A sweep runs at startup and then daily.

### Limits

- 20 cases per workspace.
- 5 MB per workspace.
- 50 contacts per case.
- 3000 workspaces.
- The global `APP_DATA_CAP_MB`. New workspaces stop at 80% of this cap, so existing users can still save.

Rate limits:

- 60 new workspaces per hour, globally.
- 600 POSTs per minute, globally.
- 60 POSTs per minute per workspace.
- 240 GETs per minute per workspace. A workspace has its own GET budget, so landing-page or random-token floods cannot lock owners out.
- 240 GETs per minute for anonymous traffic.

### TLS proxy configuration

Set `PUBLIC_BASE_URL` to the origin the browser sees. If the proxy rewrites Host, set `APP_PROXY_HOSTS` to the exact upstream Host values. Without it, GETs return 400. Forwarded-Host and Forwarded-For are not trusted. POST requests require an Origin or Referer that matches `PUBLIC_BASE_URL`.

### Workspaces and editing

- Starting a workspace creates a random bookmark token. Only its SHA-256 digest is stored.
- Export downloads `undocart-workspace.json`. It includes the `created`, `updated`, `last_seen` and `keep_until` fields.
- Expensive parsing and evaluation run before the store lock is taken. Edits and confirmations then use a short compare-and-set. If the case changed meanwhile, you see a message asking you to reload.
- The workspace list evaluates all cases in one isolated process.
- The typed form requires the unit price, the total and the order date. These are never invented, because every fact appears as [you entered] in the message you send.
- A deadline the tool calculates is tagged as calculated by the tool, not as entered by you.
- An unknown return window remains unknown.
- The typed form keeps the contact log. Saving from the full JSON editor replaces it with the log in that JSON.

The issuer output is an unverified, self-reported fact sheet:

- It is gated on logged merchant contact plus the wait period.
- Confirming it freezes it. The same sheet, with the same sha256, stays downloadable on later days until the case is edited or a contact is added.
- A mandate deviation alone is never an issuer ground.
- A not-as-described claim never produces an issuer sheet.

### Known remaining gaps

- The compute worker uses the `spawn` start method. Each computation starts a fresh interpreter, with 4 concurrent slots. Heavy concurrent use can still return 503.
- Global rate budgets have no per-client share, because client addresses behind the TLS proxy are not trusted.
- A returning visitor to a deleted or expired link may still see the retention explanation from the store's tombstone; the spec asks for a bare 404.
- Some behaviour is applied at import time by `remedy/review_fixes.py` and `remedy/store_contract_fixes.py` instead of in the modules themselves.

## Legacy demo limitations

The original demo continues to use in-memory fictional adapters and must only be used with made-up data. Those adapters are not exposed by `app.py`.
