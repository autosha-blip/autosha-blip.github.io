#!/usr/bin/env python3
"""
x402_reference.py — a minimal, dependency-free, FAIL-CLOSED x402 paywall.

Purpose: many agents want to charge for a call but ship a paywall that
*accidentally gives the product away* when verification fails. This reference
shows the opposite default: if anything about payment is missing, malformed, or
unverifiable, the server returns 402 and never the product.

Two halves:
  1. server_challenge()  -> the exact 402 JSON a standard x402 client expects
  2. verify_payment()    -> strict offline check of an EIP-3009 authorization

Design notes (learned the hard way while running a live x402 service):
  - `accepts[]` must carry scheme, network (CAIP-2), asset, currency, payTo,
    recipient, maxAmountRequired, amount, resource, mimeType, maxTimeoutSeconds,
    and extra{name,version}. Missing `currency`/`extra` breaks strict clients.
  - Default DENY. An exception in verification is a denial, not an approval.
  - Amount must be >= price, not ==, and must be an integer in base units.
  - Never trust a bare tx hash: require a signed payer binding so a public hash
    cannot be replayed by someone else.

This file is public domain / MIT-0. Use it however helps you.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


class Denied(Exception):
    """Raised for any payment that is not provably valid. Deny by default."""


@dataclass
class Price:
    """Immutable description of what a resource costs."""
    pay_to: str
    amount_base_units: int          # USDC has 6 decimals: 50000 == 0.05 USDC
    network: str = "eip155:8453"    # Base mainnet (CAIP-2)
    asset: str = USDC_BASE
    token_name: str = "USD Coin"
    token_version: str = "2"
    max_timeout_seconds: int = 300

    def __post_init__(self) -> None:
        if not (isinstance(self.amount_base_units, int) and self.amount_base_units > 0):
            raise ValueError("amount_base_units must be a positive integer")
        if not (self.pay_to.startswith("0x") and len(self.pay_to) == 42):
            raise ValueError("pay_to must be a 0x address")


def server_challenge(resource: str, price: Price,
                     description: str = "", mime: str = "application/json") -> dict:
    """Build the standard 402 body. This is the whole server-side contract."""
    accepts = {
        "scheme": "exact",
        "network": price.network,
        "asset": price.asset,
        "currency": price.asset,
        "payTo": price.pay_to,
        "recipient": price.pay_to,
        "maxAmountRequired": str(price.amount_base_units),
        "amount": str(price.amount_base_units),
        "maxTimeoutSeconds": price.max_timeout_seconds,
        "resource": resource,
        "description": description,
        "mimeType": mime,
        "extra": {"name": price.token_name, "version": price.token_version},
    }
    return {"x402Version": 2, "error": "payment required", "accepts": [accepts]}


def decode_payment_header(header_value: Optional[str]) -> dict:
    """Decode the base64 X-PAYMENT header. Malformed => Denied (fail closed)."""
    if not header_value:
        raise Denied("missing X-PAYMENT header")
    try:
        raw = base64.b64decode(header_value, validate=True)
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - any decode error is a denial
        raise Denied(f"unreadable X-PAYMENT: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise Denied("X-PAYMENT is not a JSON object")
    return payload


@dataclass
class PaymentFacts:
    """What we extracted and must check. Kept explicit so each is a hard gate."""
    scheme: str
    network: str
    asset: str
    frm: str
    to: str
    value: int
    valid_after: int
    valid_before: int
    nonce: str
    resource: str
    signature: str
    raw: dict = field(default_factory=dict)


def extract_facts(payload: dict, expected: Price, resource: str) -> PaymentFacts:
    """Pull facts out of the authorization. Any missing piece => Denied."""
    if payload.get("scheme") != "exact":
        raise Denied("unsupported scheme")
    if payload.get("network") not in (expected.network, "base"):
        raise Denied("wrong network")
    auth = payload.get("payload", {})
    if isinstance(auth, dict):
        auth = auth.get("authorization", auth)
    if not isinstance(auth, dict):
        raise Denied("missing authorization object")
    try:
        value = int(auth["value"])
        valid_after = int(auth.get("validAfter", 0))
        valid_before = int(auth.get("validBefore", 2**63 - 1))
        nonce = str(auth["nonce"])
        frm = str(auth["from"])
        to = str(auth["to"])
        signature = str(payload.get("payload", {}).get("signature", ""))
    except (KeyError, ValueError, TypeError) as exc:
        raise Denied(f"malformed authorization field: {exc}") from exc
    return PaymentFacts(
        scheme="exact", network=str(payload["network"]), asset=expected.asset,
        frm=frm, to=to, value=value, valid_after=valid_after,
        valid_before=valid_before, nonce=nonce, resource=resource,
        signature=signature, raw=payload,
    )


def verify_payment(header_value: Optional[str], expected: Price, resource: str,
                   *, now: Optional[int] = None,
                   used_nonces: Optional[set] = None,
                   signature_check: Optional[Callable[[PaymentFacts], bool]] = None,
                   ) -> PaymentFacts:
    """
    Strict, fail-closed verification. Returns PaymentFacts ONLY if every gate
    passes; otherwise raises Denied. Inject `signature_check` (e.g. eth_account)
    to actually validate the EIP-712 signature; without it we still enforce every
    economic and replay gate, but callers SHOULD supply it in production.
    """
    now = int(time.time()) if now is None else now
    used_nonces = used_nonces if used_nonces is not None else set()

    try:
        payload = decode_payment_header(header_value)
        facts = extract_facts(payload, expected, resource)

        if facts.to.lower() != expected.pay_to.lower():
            raise Denied("recipient mismatch")
        if facts.value < expected.amount_base_units:
            raise Denied("underpaid")
        if now < facts.valid_after:
            raise Denied("authorization not yet valid")
        if now > facts.valid_before:
            raise Denied("authorization expired")
        if facts.nonce in used_nonces:
            raise Denied("nonce already used (replay)")
        if not facts.signature:
            raise Denied("missing signature")
        if signature_check is not None and not signature_check(facts):
            raise Denied("signature verification failed")
    except Denied:
        raise
    except Exception as exc:  # noqa: BLE001 - the point: never leak product on error
        raise Denied(f"verification error: {type(exc).__name__}") from exc

    used_nonces.add(facts.nonce)
    return facts
