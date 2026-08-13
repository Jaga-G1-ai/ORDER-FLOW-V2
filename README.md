# Order Book Watch — Kotak Neo, on Vercel

A live total-bid-qty / total-ask-qty dashboard for Kotak Neo instruments,
built to deploy as a static frontend + Python serverless functions on
**Vercel** (rather than a long-running server like Render).

## Why the architecture is different from a normal server app
Vercel doesn't run a persistent process, so there's no background thread to
poll quotes continuously the way a Flask/Render app could. Instead:

- **`api/index.py`** is a *single* Python entrypoint handling two actions,
  dispatched by an `"action"` field in the POST body (`"login"` or
  `"quotes"`) rather than by URL path. This is required by Vercel's current
  Python runtime, which only auto-wires files named `app.py`, `index.py`,
  `server.py`, `main.py`, `wsgi.py`, or `asgi.py` as entrypoints — arbitrary
  filenames like `login.py`/`quotes.py` are not picked up as separate
  functions, which is why an earlier version of this repo failed to deploy.
  - `action: "login"` — logs in once (TOTP + MPIN — current `neo_api_client`
    builds don't need a consumer secret) and returns the `session_token` /
    `sid` / `server_id` Kotak issues.
  - `action: "quotes"` — stateless: takes that session info straight from
    the browser on every call and passes it into `client.quotes(...)`, which
    Kotak Neo supports without repeating the full login. The browser itself
    drives the polling loop (every 5s) by calling this endpoint repeatedly.
- **The frontend lives in `public/index.html`, not the project root.**
  Once Vercel detects a Python entrypoint, it *routes every request to it*
  — including `/` — so a root-level `index.html` would never be reached; it'd
  always lose to the Python catch-all. Files under `public/**` are the one
  exception Vercel serves directly, bypassing the function, which is why the
  frontend has to live there to actually show up instead of the function's
  JSON response.
- **History** is stored in the browser's `localStorage`, not on the server
  (Vercel functions have no persistent disk), so "later viewing" survives
  page reloads and browser restarts on that machine. Export to CSV any time.
- The session token lives only in `sessionStorage` (cleared when the tab
  closes) — it's never written to Vercel or to disk anywhere.

## Repo layout
```
├── public/
│   └── index.html       # static frontend (login form + live dashboard + history)
├── api/
│   └── index.py          # single Vercel entrypoint: POST /api/index {action, ...}
├── pyproject.toml
├── vercel.json
└── .env.example
```

## 1. Push to GitHub
```bash
git init
git add .
git commit -m "Kotak Neo order book dashboard"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

## 2. Deploy on Vercel
1. Go to vercel.com → **Add New → Project** → import the GitHub repo.
2. Framework preset: **Other** (no build step needed).
3. (Optional) add an environment variable `KOTAK_CONSUMER_KEY` under
   Project Settings → Environment Variables if you want the login form's
   consumer-key field to default to your key. Everything else (mobile, UCC,
   TOTP, MPIN) is entered in the browser per session, never stored server-side.
4. Deploy. Vercel auto-detects `api/index.py` as the Python entrypoint and
   serves everything under `public/` (i.e. `public/index.html`) directly as
   static content.

## 3. Use it
1. Open the deployed URL → sign in with Consumer Key, mobile, UCC, TOTP, MPIN.
2. Upload your `FNO_TOKEN.xlsx` (columns `SYMBOL`, `TOKEN`) — parsed entirely
   in the browser (via SheetJS), nothing is uploaded anywhere.
3. Cards update every 5 seconds showing **Total Bid Qty** / **Total Ask Qty** /
   LTP per symbol.
4. Scroll to **Stored history** to browse or export what's been captured.

## Notes / things to double check
- **Exchange segment**: tokens are added with `nse_cm` by default (matches
  the cash-market tokens in `FNO_TOKEN.xlsx`). Change per-instrument via the
  manual-add row if you're tracking F&O contract tokens instead (`nse_fo`).
- **Field names**: `api/index.py`'s `_extract_totals()` / `_extract_session()`
  defensively check several known Kotak response shapes, since exact field
  names have shifted across SDK releases. If your account/SDK returns
  something different, the `login` action will tell you plainly if it
  couldn't find `session_token` on the client object — check those two
  functions and add the attribute name there.
- **Vercel function timeout**: TOTP login + a quotes call should complete
  well under the 15s limit set in `vercel.json`; raise it if you're on a
  Pro plan and see timeouts with very large instrument lists.
- This is a single-user, browser-side-session tool by design (no server
  database, no multi-user accounts). If you need multiple people using it
  independently, that already works as-is — each browser tab holds its own
  session and history.
