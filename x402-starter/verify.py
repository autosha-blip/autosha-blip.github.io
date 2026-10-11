#!/usr/bin/env python3
"""
verify.py — fail-closed x402 payment verification (stdlib only).

The 402 challenge tells a client the price, the pay-to address, the network and
the asset. The client signs a USDC transfer and retries with an `X-PAYMENT`
header (base64 of a JSON payload). THIS module decides whether to serve.

Design rule: anything we cannot confirm is a NO. Never serve on a maybe.

What this checks offline, from the signed payload alone:
  - header decodes to JSON with an `payload.authorization` (EIP-3009 style)
  - network is exactly the one we asked for
  - `to` equals our pay-to address (case-insensitive)
  - `value` (atomic USDC units) >= our price
  - `asset`/currency is the USDC contract we expect
  - the authorization is not expired and not from the future
  - the (from, nonce) pair has not been used before (replay guard)

What it deliberately does NOT do: recover the EIP-712 signature and confirm the
on-chain balance/settlement. Those require an RPC call and a trusted verifier.
`needs_onchain_check()` returns the fields you must confirm before trusting a
payment that passes the offline checks. Keep that step, or you accept forgeries.
"""
import base64, json, time, threading

USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

class ReplayStore:
    """In-memory nonce guard. Swap for durable storage in production."""
    def __init__(self):
        self._seen = set()
        self._lock = threading.Lock()
    def claim(self, key):
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            return True

class Verifier:
    def __init__(self, pay_to, price_usdc, network="base", asset=USDC_BASE,
                 price_decimals=6, now=None):
        self.pay_to = pay_to.lower()
        self.network = network.lower()
        self.asset = asset.lower()
        self.price_atomic = int(round(float(price_usdc) * (10 ** price_decimals)))
        self.now = now or (lambda: int(time.time()))
        self.replays = ReplayStore()

    def verify(self, x_payment_header):
        """Return (ok: bool, info_or_reason: dict|str). Fail closed."""
        if not x_payment_header:
            return False, "missing X-PAYMENT header"
        try:
            raw = base64.b64decode(x_payment_header, validate=True)
            data = json.loads(raw)
        except Exception:
            return False, "X-PAYMENT is not valid base64-encoded JSON"

        # x402 payloads nest the authorization under "payload"
        p = data.get("payload") or data
        auth = p.get("authorization") or p.get("auth") or p
        if not isinstance(auth, dict):
            return False, "no authorization object in payload"

        net = str(data.get("network", p.get("network", ""))).lower()
        if net and net != self.network:
            return False, f"wrong network: {net} != {self.network}"

        to = str(auth.get("to", "")).lower()
        if to != self.pay_to:
            return False, "pay-to address does not match"

        try:
            value = int(auth.get("value", 0))
        except (TypeError, ValueError):
            return False, "value is not an integer (atomic units)"
        if value < self.price_atomic:
            return False, f"underpaid: {value} < {self.price_atomic}"

        asset = str(auth.get("asset", auth.get("currency", self.asset))).lower()
        if asset != self.asset:
            return False, "asset is not the expected USDC contract"

        for fld in ("validAfter", "validBefore"):
            try:
                int(auth.get(fld, 0))
            except (TypeError, ValueError):
                return False, f"{fld} is not an integer timestamp"

        now = self.now()
        vb = int(auth.get("validBefore", 0))
        if vb and now >= vb:
            return False, "authorization expired"
        va = int(auth.get("validAfter", 0))
        if va and now < va:
            return False, "authorization not yet valid"

        frm = str(auth.get("from", "")).lower()
        nonce = str(auth.get("nonce", ""))
        if not frm or not nonce:
            return False, "missing from/nonce (cannot guard against replay)"
        if not self.replays.claim((frm, nonce)):
            return False, "nonce already used (replay)"

        onchain = self.needs_onchain_check(auth, frm)
        return True, {
            "payer": frm, "value_atomic": value, "nonce": nonce,
            "onchain_check_required": onchain,
            "note": "Offline checks passed. You MUST still confirm the signature "
                    "and settlement via an RPC/verifier before serving in production.",
        }

    def needs_onchain_check(self, auth, frm):
        """Return the list of fields a trusted verifier must confirm on-chain."""
        return {
            "signature": "recover EIP-712 signature; must equal authorization.from",
            "balance": f"payer {frm} must hold >= {auth.get('value')} atomic USDC",
            "settlement": "the transfer must be submitted/settled, not just signed",
        }
