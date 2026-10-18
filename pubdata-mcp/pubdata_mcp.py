#!/usr/bin/env python3
"""
pubdata-mcp — a Model Context Protocol (MCP) server that exposes the
public-data API at https://13-62-217-51.sslip.io as tools any MCP-capable
agent (Claude Desktop, Cursor, and compatible clients) can call directly.

Why this exists
---------------
An HTTP API is a website: someone has to find it, read the docs, and write
a client. An MCP server is a *socket*: the tools appear inside the agent's
own runtime with no integration work. That is a distribution channel, not
another endpoint.

Design notes
------------
- Stdio transport, JSON-RPC 2.0, MCP protocol revision 2024-11-05.
- Zero third-party dependencies (stdlib only). Runs on Python 3.9+.
- Free tools call the public endpoints.
- Paid tools (x402) are declared with their price so the agent knows they
  exist; calling one returns the 402 challenge untouched. Payment is made
  by the MCP client's own wallet layer, never by this server.
- SPEND CAP: this server will not hand out pay-and-retry instructions once
  the caller's authorised spend would exceed a local cap (default 0.10 USDC,
  override with PUBDATA_MCP_MAX_SPEND_USDC). Worst-case spend is therefore
  bounded even if the driving agent loops. State lives in
  ~/.pubdata-mcp-spend.json; the cap is a guard-rail, not a wallet.
"""

import json
import sys
import urllib.parse
import urllib.request

BASE = "https://13-62-217-51.sslip.io"
PROTOCOL_VERSION = "2024-11-05"

import os
import time

SPEND_STATE = os.path.join(os.path.expanduser("~"), ".pubdata-mcp-spend.json")


def _spend_path():
    """Resolve the spend file at CALL time so embedders/tests can override it."""
    return os.environ.get("PUBDATA_MCP_SPEND_FILE") or SPEND_STATE
DEFAULT_CAP_USDC = 0.10

# name -> path, used only to phrase the 402 message
ENDPOINTS = {
    "base_fee": "/api/base-fee", "earthquakes": "/api/earthquakes", "iss": "/api/iss",
    "weather": "/api/weather", "postcard": "/api/postcard", "risk_brief": "/api/risk-brief",
}


def _cap():
    try:
        return float(os.environ.get("PUBDATA_MCP_MAX_SPEND_USDC", DEFAULT_CAP_USDC))
    except ValueError:
        return DEFAULT_CAP_USDC


def _load_spend():
    try:
        with open(_spend_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return float(d.get("authorised_usdc", 0.0)), list(d.get("log", []))
    except (OSError, ValueError, TypeError):
        return 0.0, []


def _save_spend(total, log):
    try:
        with open(_spend_path(), "w", encoding="utf-8") as fh:
            json.dump({"authorised_usdc": round(total, 8), "log": log[-50:]}, fh)
    except OSError:
        pass


def _parse_price(body):
    """Best-effort price (USDC) from a 402 challenge body. None if unknown."""
    try:
        d = json.loads(body)
    except ValueError:
        return None
    for key in ("price_usdc", "price"):
        if isinstance(d.get(key), (int, float)):
            return float(d[key])
    for acc in (d.get("accepts") or []):
        for key in ("maxAmountRequired", "amount", "price"):
            v = acc.get(key)
            if isinstance(v, str) and v.isdigit():
                return int(v) / 1e6      # atomic USDC units
            if isinstance(v, (int, float)):
                return float(v)
    return None


def _authorise(price):
    """Return (allowed, reason). Refuse if unknown price or over cap."""
    cap = _cap()
    spent, log = _load_spend()
    if price is None:
        return False, ("unknown price in challenge; refusing to authorise payment "
                       "(fail closed). lifetime cap=%.4f USDC" % cap)
    if price <= 0:
        return False, "non-positive price in challenge; refusing (fail closed)."
    if spent + price > cap + 1e-9:
        return False, ("lifetime spend cap reached: %.4f + %.4f > %.4f USDC. Raise "
                       "PUBDATA_MCP_MAX_SPEND_USDC if this is intended." % (spent, price, cap))
    spent += price
    log.append({"ts": time.time(), "usdc": round(price, 8)})
    _save_spend(spent, log)
    return True, "authorised %.4f USDC (total %.4f / cap %.4f)" % (price, spent, cap)

TOOLS = [
    {
        "name": "base_fee",
        "description": "Current Base L2 gas fee estimate (gwei), for costing x402 payments before making them.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "annotations": {"price": "free"},
    },
    {
        "name": "earthquakes",
        "description": "Recent earthquakes worldwide (USGS), magnitude-ordered, with place, time and coordinates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {
                    "type": "string",
                    "enum": ["hour", "day", "week"],
                    "description": "Time window to summarise (default: day).",
                },
                "min_magnitude": {"type": "number", "description": "Drop quakes below this magnitude."},
            },
            "required": [],
        },
        "annotations": {"price": "free"},
    },
    {
        "name": "iss",
        "description": "Current position and altitude of the International Space Station.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "annotations": {"price": "free"},
    },
    {
        "name": "weather",
        "description": "Live weather for a coordinate: temperature, wind, and a condition word.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "-90..90"},
                "lon": {"type": "number", "description": "-180..180"},
            },
            "required": ["lat", "lon"],
        },
        "annotations": {"price": "free"},
    },
    {
        "name": "postcard",
        "description": "A few lines of live verse composed from the weather, the nearest recent earthquake and the ISS position at a coordinate. Human-legible; nice for demos.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
            },
            "required": ["lat", "lon"],
        },
        "annotations": {"price": "free"},
    },
    {
        "name": "spend_status",
        "description": "Show this MCP server's local x402 spend cap: cap, amount authorised so far, and remaining. Use it to decide before calling a paid tool.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "annotations": {"price": "free"},
    },
    {
        "name": "risk_brief",
        "description": (
            "PAID (x402, USDC on Base): a single fused brief for a coordinate — "
            "weather, nearest seismic activity and an exposure summary. "
            "Calling it returns the HTTP 402 challenge; pay with your x402 client "
            "and retry. This MCP server never touches your wallet."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
            },
            "required": ["lat", "lon"],
        },
        "annotations": {"price": "x402 (USDC on Base)", "endpoint": "/api/risk-brief"},
    },
]


def _get(path, params=None, timeout=20):
    url = BASE + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    req = urllib.request.Request(url, headers={"User-Agent": "pubdata-mcp/1.3.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def call_tool(name, args):
    """Return (text, is_error)."""
    args = args or {}

    if name == "spend_status":
        spent, log = _load_spend()
        return (json.dumps({"cap_usdc": _cap(), "authorised_usdc": round(spent, 8),
                            "remaining_usdc": round(_cap() - spent, 8),
                            "recent": log[-5:]}), False)
    if name == "base_fee":
        code, body = _get("/api/base-fee")
    elif name == "earthquakes":
        code, body = _get(
            "/api/earthquakes",
            {"window": args.get("window"), "min_magnitude": args.get("min_magnitude")},
        )
    elif name == "iss":
        code, body = _get("/api/iss")
    elif name == "weather":
        code, body = _get("/api/weather", {"lat": args.get("lat"), "lon": args.get("lon")})
    elif name == "postcard":
        code, body = _get("/api/postcard", {"lat": args.get("lat"), "lon": args.get("lon")})
    elif name == "risk_brief":
        code, body = _get("/api/risk-brief", {"lat": args.get("lat"), "lon": args.get("lon")})
    else:
        return ("Unknown tool: %s" % name, True)

    if code == 402:
        # EVERY paid tool goes through the same fail-closed gate. Keyed on the
        # challenge's own price, never on the tool name: if a paid tool is ever
        # added (or an endpoint starts charging), it cannot bypass the cap by
        # being a name this branch did not anticipate.
        price = _parse_price(body)
        allowed, why = _authorise(price)
        if not allowed:
            # Withhold EVERYTHING a wallet-enabled client could pay with:
            # no pay_to, no amount, no how-to-pay, not even the price.
            return ("PAYMENT BLOCKED BY LIFETIME SPEND CAP. This server's lifetime "
                    "authorised spend has reached its cap. The payment challenge is "
                    "deliberately withheld and nothing was paid. See the free "
                    "spend_status tool, or raise PUBDATA_MCP_MAX_SPEND_USDC.", False)
        return ("PAYMENT REQUIRED (402). Within lifetime cap: %s. Pay with your x402 client against "
                "%s%s and retry.\n\n%s" % (why, BASE, ENDPOINTS.get(name, ""), body), False)
    if code >= 400:
        return ("Upstream returned HTTP %d: %s" % (code, body[:500]), True)
    return (body, False)


def _reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def handle(msg):
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        return _reply(
            msg_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "pubdata-mcp", "version": "1.3.0"},
            },
        )
    if method == "notifications/initialized":
        return  # notification, no reply
    if method == "tools/list":
        return _reply(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        text, is_err = call_tool(params.get("name"), params.get("arguments"))
        return _reply(
            msg_id,
            {"content": [{"type": "text", "text": text}], "isError": is_err},
        )
    if method == "ping":
        return _reply(msg_id, {})
    if msg_id is not None:
        return _reply(msg_id, error={"code": -32601, "message": "Method not found: %s" % method})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            handle(msg)
        except Exception as exc:  # never die on one bad call
            if msg.get("id") is not None:
                _reply(msg["id"], error={"code": -32603, "message": str(exc)})


if __name__ == "__main__":
    main()
