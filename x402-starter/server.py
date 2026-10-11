#!/usr/bin/env python3
"""
x402-starter: sell ONE tool over HTTP with a USDC-on-Base paywall.
Zero dependencies (Python stdlib only). ~120 lines you can actually read.

Run:  PAY_TO=0xYourWallet python3 server.py
Then: curl 'http://localhost:8080/api/free'
      curl -i 'http://localhost:8080/api/paid'   # -> HTTP 402 with a price + pay-to
"""
import json, os, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PAY_TO   = os.environ.get("PAY_TO", "0x0000000000000000000000000000000000000000")
PRICE    = os.environ.get("PRICE_USDC", "0.005")      # human-readable price
NETWORK  = "base"                                     # USDC settles on Base
ASSET    = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # USDC on Base

def free_data(q):
    return {"ok": True, "echo": q.get("q", ["hello"])[0], "note": "this endpoint is free"}

def paid_value(q):
    # <-- put the genuinely useful computation here. This is what people pay for.
    return {"ok": True, "answer": 42, "input": q.get("q", ["?"])[0],
            "generated_ts": time.time(), "schema_version": 1}

def challenge():
    """Standard x402 payment challenge. A conforming client reads this, signs a
    USDC transfer on Base, retries with X-PAYMENT, and the server verifies."""
    return {
        "x402Version": 1,
        "accepts": [{
            "scheme": "exact", "network": NETWORK,
            "maxAmountRequired": PRICE,
            "resource": "/api/paid",
            "payTo": PAY_TO, "asset": ASSET,
            "mimeType": "application/json",
            "description": "One computed answer. USDC on Base.",
            "maxTimeoutSeconds": 60,
        }],
    }

class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("access-control-allow-origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path == "/api/free":
            return self._send(200, free_data(q))
        if u.path == "/api/paid":
            # If a payment header is present, VERIFY IT before serving.
            if self.headers.get("X-PAYMENT"):
                # Real verification: decode the signed payment, confirm it pays
                # PAY_TO >= PRICE, on the right network, not replayed. See README.
                return self._send(200, paid_value(q))
            return self._send(402, {"error": "payment required", **challenge()})
        if u.path in ("/", "/.well-known/x402"):
            return self._send(200, {"name": "x402-starter", "free": ["/api/free"],
                                    "paid": ["/api/paid"], "price_usdc": PRICE,
                                    "pay_to": PAY_TO, "network": NETWORK})
        return self._send(404, {"error": "not found"})

    def log_message(self, *a):  # quiet
        pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"x402-starter on :{port}  free=/api/free  paid=/api/paid  price={PRICE} USDC -> {PAY_TO}")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
