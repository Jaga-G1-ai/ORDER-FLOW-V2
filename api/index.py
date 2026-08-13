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

import inspect
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


def _supported_kwargs(func, candidate_kwargs):
    """
    Filter a dict of candidate keyword arguments down to only the ones
    `func`'s signature actually declares (or pass everything through if the
    function accepts **kwargs). Keeps SDK-version drift (e.g. one release
    having `isIndex`, another not) from breaking the call outright.
    """
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return candidate_kwargs  # can't introspect (e.g. C extension) - pass as-is

    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return candidate_kwargs

    return {k: v for k, v in candidate_kwargs.items() if k in params}


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


def _mask(v):
    """Keep enough of a value to identify what it is, without leaking a full token."""
    if isinstance(v, str):
        return v if len(v) <= 8 else f"{v[:4]}...{v[-4:]} (len={len(v)})"
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    if isinstance(v, dict):
        return {k: _mask(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_mask(x) for x in v[:5]]
    return f"<{type(v).__name__}>"


def _dump_obj(obj):
    """Shallow, masked dump of an object's or dict's public attributes/keys."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return {k: _mask(v) for k, v in obj.items()}
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        try:
            return {k: _mask(v) for k, v in obj.to_dict().items()}
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        return {k: _mask(v) for k, v in vars(obj).items() if not k.startswith("_") and not callable(v)}
    return f"<{type(obj).__name__}, no __dict__>"


def _find_session_value(obj, names, seen=None):
    """Recursively search a dict/object tree for the first key matching any of `names`."""
    if seen is None:
        seen = set()
    if id(obj) in seen or obj is None:
        return None
    seen.add(id(obj))

    if isinstance(obj, dict):
        items = obj.items()
    elif hasattr(obj, "__dict__"):
        items = vars(obj).items()
    else:
        return None

    lowered_names = {n.lower().replace("_", "") for n in names}
    for k, v in items:
        if isinstance(k, str) and k.lower().replace("_", "") in lowered_names and v:
            return v
    for k, v in items:
        if isinstance(v, (dict,)) or (hasattr(v, "__dict__") and not callable(v)):
            found = _find_session_value(v, names, seen)
            if found:
                return found
    return None


def _extract_session(client, login_resp=None, validate_resp=None):
    """
    Newer neo_api_client releases don't need a consumer secret; the session
    details (session_token / sid / server_id) may show up either in the
    dict returned by totp_login()/totp_validate(), or as attributes on the
    client object (or its internal configuration object). Exact field/attr
    names have moved between SDK releases, so every known spot is searched.
    """
    sources = [validate_resp, login_resp, client, getattr(client, "configuration", None),
               getattr(client, "api_client", None)]
    targets = {
        "session_token": ["session_token", "sessionToken", "token"],
        "sid": ["sid", "SID"],
        "server_id": ["server_id", "serverId", "serverid"],
        "access_token": ["access_token", "accessToken", "bearer_token"],
    }
    session = {}
    for out_key, names in targets.items():
        for src in sources:
            val = _find_session_value(src, names)
            if val:
                session[out_key] = val
                break
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
        login_resp = client.totp_login(mobile_number=mobile_number, ucc=ucc, totp=totp)
        validate_resp = client.totp_validate(mpin=mpin)
    except Exception as exc:
        return 200, {"ok": False, "error": f"Login failed: {exc}"}

    session = _extract_session(client, login_resp=login_resp, validate_resp=validate_resp)
    if not session.get("session_token"):
        return 200, {
            "ok": False,
            "error": "Logged in but couldn't find session_token/sid/server_id anywhere in the "
                     "login response. See `debug` below for the actual shape returned by your "
                     "SDK version - share it and _extract_session()/_find_session_value() in "
                     "api/index.py can be pointed at the right field names.",
            "debug": {
                "login_resp": _dump_obj(login_resp),
                "validate_resp": _dump_obj(validate_resp),
                "client": _dump_obj(client),
                "client_configuration": _dump_obj(getattr(client, "configuration", None)),
            },
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
        # Different neo_api_client releases accept different keyword arguments
        # here (e.g. some don't have isIndex at all). Rather than guess and
        # break on every SDK bump, only pass the kwargs this installed
        # version's quotes() actually declares.
        candidate_kwargs = {
            "instrument_tokens": payload,
            "quote_type": "market_depth",
            "isIndex": False,
            "is_index": False,
            "session_token": session.get("session_token"),
            "sid": session.get("sid"),
            "server_id": session.get("server_id"),
        }
        response = client.quotes(**_supported_kwargs(client.quotes, candidate_kwargs))
    except Exception as exc:
        try:
            accepted = list(inspect.signature(client.quotes).parameters.keys())
        except Exception:
            accepted = None
        return 200, {
            "ok": False,
            "error": f"Quote fetch failed: {exc}",
            "debug": {"quotes_accepted_params": accepted},
        }

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
        _send_json(self, 200, {
            "ok": True,
            "message": "This is the API function, not the app. Open the site root ('/') for the "
                       "dashboard - it's served from public/index.html. POST here with "
                       "{action: 'login'|'quotes', ...} to use the API directly.",
        })

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
