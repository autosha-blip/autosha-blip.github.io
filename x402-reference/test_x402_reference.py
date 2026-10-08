#!/usr/bin/env python3
"""Self-test for x402_reference. Run: python3 test_x402_reference.py

Proves the paywall FAILS CLOSED: every bad or missing payment is Denied, and a
fully valid one passes. No network, no dependencies.
"""
import base64
import json
import time

from x402_reference import (Denied, Price, server_challenge, verify_payment)

PRICE = Price(pay_to="0xdD28076BdC986999f0240c03367Aaf0dB1728896", amount_base_units=50000)
RES = "http://13.62.217.51:8080/api/risk-brief"

def hdr(payload: dict) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()

def good_payload(**over):
    now = int(time.time())
    auth = {"from": "0x1111111111111111111111111111111111111111",
            "to": PRICE.pay_to, "value": "50000",
            "validAfter": str(now - 10), "validBefore": str(now + 300),
            "nonce": over.pop("nonce", "0xabc123")}
    auth.update(over.pop("auth", {}))
    p = {"x402Version": 2, "scheme": "exact", "network": "eip155:8453",
         "payload": {"signature": "0xdead", "authorization": auth}}
    p.update(over)
    return p

def expect_denied(name, header, used=None):
    try:
        verify_payment(header, PRICE, RES, used_nonces=used)
    except Denied as e:
        print(f"  DENY  ok  {name}  ({e})")
        return True
    print(f"  FAIL!! {name} was ACCEPTED")
    return False

def main():
    ok = True
    used = set()

    print("challenge shape:")
    c = server_challenge(RES, PRICE, "demo")
    a = c["accepts"][0]
    need = {"scheme","network","asset","currency","payTo","recipient",
            "maxAmountRequired","amount","maxTimeoutSeconds","resource",
            "description","mimeType","extra"}
    missing = need - set(a)
    print("  accepts keys complete:", not missing, "" if not missing else missing)
    ok &= not missing

    print("fail-closed gates:")
    ok &= expect_denied("no header", None)
    ok &= expect_denied("not base64", "!!!!")
    ok &= expect_denied("empty obj", hdr({}))
    ok &= expect_denied("wrong scheme", hdr(good_payload(scheme="nope")))
    ok &= expect_denied("wrong network", hdr(good_payload(network="eip155:1")))
    ok &= expect_denied("recipient mismatch", hdr(good_payload(auth={"to": "0x2222222222222222222222222222222222222222"})))
    ok &= expect_denied("underpaid", hdr(good_payload(auth={"value": "49999"})))
    ok &= expect_denied("not yet valid", hdr(good_payload(auth={"validAfter": str(int(time.time())+1000)})))
    ok &= expect_denied("expired", hdr(good_payload(auth={"validBefore": "1"})))
    ok &= expect_denied("no signature", hdr({"x402Version":2,"scheme":"exact","network":"eip155:8453","payload":{"authorization":good_payload()["payload"]["authorization"]}}))
    ok &= expect_denied("missing value field", hdr(good_payload(auth={"value": None})))
    ok &= expect_denied("bad int value", hdr(good_payload(auth={"value": "abc"})))

    print("replay:")
    h = hdr(good_payload(nonce="0xreplay"))
    try:
        verify_payment(h, PRICE, RES, used_nonces=used)
        print("  ACCEPT ok  first use")
    except Denied as e:
        print("  FAIL!! first use denied:", e); ok = False
    ok &= expect_denied("nonce reuse", h, used=used)

    # replay check must use the same set
    try:
        verify_payment(h, PRICE, RES, used_nonces=used)
        print("  FAIL!! replay accepted across calls"); ok = False
    except Denied:
        print("  DENY  ok  replay across calls")

    print("happy path:")
    try:
        f = verify_payment(hdr(good_payload(nonce="0xgood")), PRICE, RES, used_nonces=set())
        print("  ACCEPT ok  value=%d to=%s" % (f.value, f.to)); ok &= (f.value == 50000)
    except Denied as e:
        print("  FAIL!! valid payment denied:", e); ok = False

    print("\nRESULT:", "ALL GOOD — paywall fails closed" if ok else "TESTS FAILED")
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
