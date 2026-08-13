"""
api/index.py -> single Vercel Python entrypoint (POST /api/index)

Vercel's Python runtime only auto-detects entrypoint files named app.py,
index.py, server.py, main.py, wsgi.py, or asgi.py (at the project root or
inside src/, app/, api/) and loads a top-level `handler` (BaseHTTPRequestHandler
subclass) / `app` (ASGI/WSGI) variable from it. Arbitrary filenames like
login.py or quotes.py are NOT auto-wired as separate functions, so both
routes live here in one file and are dispatched by an "action" field in the
JSON body instead of by URL path.

Frontend calls:
    POST /api/index  {"action": "login",  ...}
    POST /api/index  {"action": "quotes", ...}
"""

import json
import os
from http.server import BaseHTTPRequestHandler

from neo_api_client import NeoAPI


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _cors(handler):
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")


def _send_json(handler, code, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    _cors(handler)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_body(handler):
    length = int(handler.headers.get("Content-Length", 0))
    raw = handler.rfile.read(length).decode("utf-8") if length else "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _to_num(v):
    try:
        if isinstance(v, str):
            v = v.replace(",", "")
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _extract_totals(quote):
    """
    Pull total bid qty / total ask qty / ltp out of one instrument's quote
    payload. Kotak's exact field names have varied across SDK versions, so
    several known shapes are checked defensively:
      A) ready-made totals: tbq/tsq, totalBuyQuantity/totalSellQuantity, ...
      B) a depth object with buy[]/sell[] level arrays (sum the levels)
      C) flattened bp1..bp5/bq1..bq5/sp1..sp5/sq1..sq5 fields
    """
    total_bid, total_ask, ltp = None, None, None

    for k in ("tbq", "totalBuyQuantity", "total_buy_quantity", "buy_quantity", "totBuyQty", "total_buy"):
        if k in quote:
            total_bid = _to_num(quote[k])
            break
    for k in ("tsq", "totalSellQuantity", "total_sell_quantity", "sell_quantity", "totSellQty", "total_sell"):
        if k in quote:
            total_ask = _to_num(quote[k])
            break

    depth = quote.get("depth") or quote.get("marketDepth") or quote.get("market_depth")
    if isinstance(depth, dict):
        buy_levels = depth.get("buy") or depth.get("Buy") or []
        sell_levels = depth.get("sell") or depth.get("Sell") or []
        if total_bid is None and buy_levels:
            total_bid = sum(_to_num(l.get("quantity", l.get("qty", 0))) for l in buy_levels)
        if total_ask is None and sell_levels:
            total_ask = sum(_to_num(l.get("quantity", l.get("qty", 0))) for l in sell_levels)

    if total_bid is None:
        bq_keys = [f"bq{i}" for i in range(1, 6)]
        if any(k in quote for k in bq_keys):
            total_bid = sum(_to_num(quote.get(k, 0)) for k in bq_keys)
    if total_ask is None:
        sq_keys = [f"sq{i}" for i in range(1, 6)]
        if any(k in quote for k in sq_keys):
            total_ask = sum(_to_num(quote.get(k, 0)) for k in sq_keys)

    for k in ("ltp", "last_price", "lastTradedPrice", "lp"):
        if k in quote:
            ltp = _to_num(quote[k])
            break

    return total_bid, total_ask, ltp


def _extract_session(client):
    """
    Newer neo_api_client releases don't need a consumer secret; after
    totp_login()+totp_validate() succeed, the session details (session_token /
    sid / server_id) live as attributes on the client (or its internal
    configuration object). Exact attribute names have moved between SDK
    releases, so a few known spots are probed defensively.
    """
    candidates = [client, getattr(client, "configuration", None), getattr(client, "api_client", None)]
    keys = {
        "session_token": ["session_token", "sessionToken"],
        "sid": ["sid", "SID"],
        "server_id": ["server_id", "serverId"],
        "access_token": ["access_token", "accessToken", "bearer_token"],
    }
    session = {}
    for obj in candidates:
        if obj is None:
            continue
        for out_key, attr_names in keys.items():
            if out_key in session:
                continue
            for attr in attr_names:
                if hasattr(obj, attr):
                    val = getattr(obj, attr)
                    if val:
                        session[out_key] = val
    return session


# ---------------------------------------------------------------------------
# action handlers
# ---------------------------------------------------------------------------
def _handle_login(form):
    consumer_key = str(form.get("consumer_key") or os.environ.get("KOTAK_CONSUMER_KEY", "")).strip()
    mobile_number = str(form.get("mobile_number", "")).strip()
    ucc = str(form.get("ucc", "")).strip()
    totp = str(form.get("totp", "")).strip()
    mpin = str(form.get("mpin", "")).strip()

    if not all([consumer_key, mobile_number, ucc, totp, mpin]):
        return 400, {"ok": False, "error": "consumer_key, mobile_number, ucc, totp and mpin are all required"}

    try:
        # Current neo_api_client only needs consumer_key - no consumer_secret.
        client = NeoAPI(
            consumer_key=consumer_key,
            environment=os.environ.get("KOTAK_ENVIRONMENT", "prod"),
            access_token=None,
            neo_fin_key=None,
        )
        client.totp_login(mobile_number=mobile_number, ucc=ucc, totp=totp)
        client.totp_validate(mpin=mpin)
    except Exception as exc:
        return 200, {"ok": False, "error": f"Login failed: {exc}"}

    session = _extract_session(client)
    if not session.get("session_token"):
        return 200, {
            "ok": False,
            "error": "Logged in but couldn't read session_token/sid/server_id off the "
                     "client object. Check the SDK version's attribute names and update "
                     "_extract_session() in api/index.py.",
        }

    return 200, {"ok": True, "consumer_key": consumer_key, "session": session}


def _handle_quotes(form):
    consumer_key = str(form.get("consumer_key") or os.environ.get("KOTAK_CONSUMER_KEY", "")).strip()
    session = form.get("session") or {}
    instruments = form.get("instruments") or []

    if not consumer_key or not session.get("session_token") or not instruments:
        return 400, {"ok": False, "error": "consumer_key, session (from the login action) and instruments[] are required"}

    payload = [
        {"instrument_token": str(i["token"]), "exchange_segment": i.get("exchange_segment", "nse_cm")}
        for i in instruments
    ]

    try:
        client = NeoAPI(
            consumer_key=consumer_key,
            environment=os.environ.get("KOTAK_ENVIRONMENT", "prod"),
            access_token=None,
            neo_fin_key=None,
        )
        response = client.quotes(
            instrument_tokens=payload,
            quote_type="market_depth",
            isIndex=False,
            session_token=session.get("session_token"),
            sid=session.get("sid"),
            server_id=session.get("server_id"),
        )
    except Exception as exc:
        return 200, {"ok": False, "error": f"Quote fetch failed: {exc}"}

    rows_raw = response.get("message", response) if isinstance(response, dict) else response
    if not isinstance(rows_raw, list):
        rows_raw = [rows_raw]

    by_token = {}
    for q in rows_raw:
        tok = str(q.get("instrument_token") or q.get("tk") or q.get("token") or "")
        by_token[tok] = q

    cards = []
    for inst in instruments:
        q = by_token.get(str(inst["token"]), {})
        total_bid, total_ask, ltp = _extract_totals(q)
        cards.append({
            "token": inst["token"],
            "symbol": inst.get("symbol", inst["token"]),
            "ltp": ltp,
            "total_bid_qty": total_bid,
            "total_ask_qty": total_ask,
        })

    return 200, {"ok": True, "cards": cards}


# ---------------------------------------------------------------------------
# Vercel entrypoint
# ---------------------------------------------------------------------------
class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        _cors(self)
        self.end_headers()

    def do_GET(self):
        _send_json(self, 200, {"ok": True, "message": "POST {action: 'login'|'quotes', ...} to this endpoint."})

    def do_POST(self):
        form = _read_json_body(self)
        action = str(form.get("action", "")).strip()

        if action == "login":
            code, payload = _handle_login(form)
        elif action == "quotes":
            code, payload = _handle_quotes(form)
        else:
            code, payload = 400, {"ok": False, "error": "unknown or missing 'action' (expected 'login' or 'quotes')"}

        _send_json(self, code, payload)

    def log_message(self, format, *args):
        pass
