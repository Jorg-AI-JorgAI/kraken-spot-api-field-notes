"""Minimal Kraken private REST call: signing, microsecond nonce, errors in
the body, one retry on an out-of-order nonce, and a short read cache.

Standard library only. Set KRAKEN_API_KEY and KRAKEN_PRIVATE_KEY in the
environment; never paste keys into code. The key needs Query Funds for the
Balance call used here, and must not have Withdraw Funds.
"""
import base64
import hashlib
import hmac
import os
import time
import urllib.parse
import urllib.request
import json

API = "https://api.kraken.com"
KEY = os.environ["KRAKEN_API_KEY"]
SECRET = os.environ["KRAKEN_PRIVATE_KEY"]

_cache: dict = {}           # method -> (expires_at, payload)
CACHE_TTL = {"Balance": 15, "TradeBalance": 15, "TradesHistory": 60}


def sign(path: str, data: dict, nonce: int) -> str:
    body = urllib.parse.urlencode({"nonce": str(nonce), **data})
    sha = hashlib.sha256((str(nonce) + body).encode()).digest()
    mac = hmac.new(base64.b64decode(SECRET), path.encode() + sha, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def private(method: str, data: dict | None = None, _retry: bool = True) -> dict:
    """POST /0/private/<method>. Kraken answers HTTP 200 even on failure,
    so the `error` array is the thing to read."""
    data = data or {}
    ttl = CACHE_TTL.get(method)
    now = time.time()
    if ttl and method in _cache and _cache[method][0] > now:
        return _cache[method][1]
    path = f"/0/private/{method}"
    nonce = time.time_ns() // 1000          # microseconds: parallel workers rarely collide
    body = urllib.parse.urlencode({"nonce": str(nonce), **data}).encode()
    req = urllib.request.Request(API + path, data=body, headers={
        "API-Key": KEY, "API-Sign": sign(path, data, nonce),
        "Content-Type": "application/x-www-form-urlencoded",
    })
    with urllib.request.urlopen(req, timeout=15) as r:   # always a timeout; a stalled call must not hang the caller
        payload = json.load(r)
    errors = payload.get("error") or []
    if errors:
        if any(e.startswith("EAPI:Invalid nonce") for e in errors) and _retry:
            return private(method, data, _retry=False)   # ordering, not auth: one retry with a fresh nonce
        if any(e in ("EAPI:Invalid key", "EAPI:Invalid signature", "EGeneral:Permission denied") for e in errors):
            raise PermissionError(f"the API key, not the request: {errors}")
        raise RuntimeError(f"Kraken {method}: {errors}")
    if ttl:
        _cache[method] = (now + ttl, payload["result"])
    return payload["result"]


def drop_cache() -> None:
    """Call after AddOrder or CancelOrder so the next balance read is real."""
    _cache.clear()


if __name__ == "__main__":
    print(private("Balance"))
